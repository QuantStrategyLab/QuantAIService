import hashlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from ai_service.runtime import TaskRuntime, Conflict, NotFound, Policy, ProviderReply


class FakeProvider:
    mode = 'agent'

    def __init__(self):
        self.calls = 0
        self.fail_submit = False
        self.reply = ProviderReply('running')

    def submit(self, request, *, task_id, owner):
        self.calls += 1
        if self.fail_submit:
            raise TimeoutError('untrusted secret provider body')
        return {'session_id': 'session-1', 'run_id': 'run-1'}

    def poll(self, handle):
        return self.reply

    def cancel(self, handle):
        return None


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'tasks.sqlite'
        self.provider = FakeProvider()
        self.policy = Policy(providers={'agent': 'test'}, models={'test': {'test-model'}})
        self.runtime = TaskRuntime(self.path, {'test': self.provider}, {'owner/repo': self.policy})
        self.request = {'mode': 'agent', 'objective': 'Summarize supplied evidence',
                        'model': 'test-model', 'materials': [],
                        'output_schema': {'type': 'object', 'required': ['summary'],
                                          'properties': {'summary': {'type': 'string'}},
                                          'additionalProperties': False}}

    def tearDown(self):
        self.tmp.cleanup()

    def request_digest(self, request=None):
        return hashlib.sha256(json.dumps(self.request if request is None else request,
            sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode()).hexdigest()

    def test_lookup_returns_original_id_with_only_sql_reads(self):
        self.request['materials'] = [{'text': '合成证据'}]
        task = self.runtime.submit('owner/repo', 'lost-response', self.request)
        before = self.path.read_bytes()
        original_connection = self.runtime.connection
        actions = []

        @contextmanager
        def read_only(**kwargs):
            with original_connection(**kwargs) as db:
                def authorize(action, *_):
                    actions.append(action)
                    return sqlite3.SQLITE_OK if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ} else sqlite3.SQLITE_DENY
                db.set_authorizer(authorize)
                yield db

        with patch.object(self.runtime, 'connection', read_only), \
                patch.object(self.runtime, 'submit', side_effect=AssertionError('creation forbidden')), \
                patch.object(self.runtime, 'tick', side_effect=AssertionError('execution forbidden')):
            for _ in range(2):
                self.assertEqual(self.runtime.lookup('owner/repo', 'lost-response', self.request_digest()), task)
        self.assertTrue(actions)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.provider.calls, 0)

    def test_lookup_missing_project_key_and_digest_do_not_reveal_id(self):
        self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.policies['other/repo'] = self.policy
        for owner, key, digest in [('other/repo', 'same', self.request_digest()),
                ('owner/repo', 'missing', self.request_digest()),
                ('owner/repo', "same' OR 1=1 --", self.request_digest()),
                ('owner/repo', 'same', '0' * 64)]:
            with self.subTest(owner=owner, key=key), self.assertRaises(NotFound):
                self.runtime.lookup(owner, key, digest)
        self.assertEqual(self.provider.calls, 0)

    def test_lookup_before_post_commit_is_unknown_then_finds_the_same_task(self):
        ready, release = threading.Event(), threading.Event()
        original_connection = self.runtime.connection
        writer = []

        @contextmanager
        def delay_commit(**kwargs):
            with original_connection(**kwargs) as db:
                yield db
                if threading.get_ident() in writer:
                    ready.set()
                    if not release.wait(5):
                        raise AssertionError('synthetic writer release timed out')

        def post():
            writer.append(threading.get_ident())
            return self.runtime.submit('owner/repo', 'in-flight', self.request)

        with patch.object(self.runtime, 'connection', delay_commit), ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(post)
            try:
                self.assertTrue(ready.wait(5))
                with self.assertRaises(NotFound):
                    self.runtime.lookup('owner/repo', 'in-flight', self.request_digest())
            finally:
                release.set()
            task = pending.result(timeout=5)
        self.assertEqual(self.runtime.lookup('owner/repo', 'in-flight', self.request_digest())['id'], task['id'])
        with self.runtime.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tasks').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0], 1)
        self.assertEqual(self.provider.calls, 0)

    def test_lookup_does_not_resolve_ambiguous_legacy_rows(self):
        self.runtime.submit('owner/repo', 'same', self.request)
        # An isolated malformed legacy DB, never a schema migration or shared fixture.
        with self.runtime.connection() as db:
            db.execute('CREATE TABLE legacy_tasks AS SELECT * FROM tasks')
            db.execute('DROP TABLE tasks')
            db.execute('ALTER TABLE legacy_tasks RENAME TO tasks')
            db.execute("INSERT INTO tasks SELECT 'duplicate', owner, idem, digest, provider, request, status, handle, output, artifacts, error, created, updated, deadline, cancel_sent, worker, attempt, revision, report_digest FROM tasks")
        before = self.path.read_bytes()
        with self.assertRaises(NotFound):
            self.runtime.lookup('owner/repo', 'same', self.request_digest())
        self.assertEqual(self.path.read_bytes(), before)

    def test_lookup_request_or_digest_drift_and_revocation_stay_unknown(self):
        task = self.runtime.submit('owner/repo', 'same', self.request)
        for body, digest in [(json.dumps({**self.request, 'objective': 'drift'}), self.request_digest()),
                             ('not-json', self.request_digest()), (json.dumps(self.request), '0' * 64)]:
            with self.subTest(body=body):
                with self.runtime.connection() as db:
                    db.execute('UPDATE tasks SET request=?, digest=? WHERE id=?', (body, digest, task['id']))
                with self.assertRaises(NotFound):
                    self.runtime.lookup('owner/repo', 'same', self.request_digest())
        del self.runtime.policies['owner/repo']
        with self.assertRaises(PermissionError):
            self.runtime.lookup('owner/repo', 'same', self.request_digest())
        self.assertEqual(self.provider.calls, 0)

    def test_lookup_repeated_key_conflict_preserves_original_binding(self):
        task = self.runtime.submit('owner/repo', 'same', self.request)
        changed = {**self.request, 'objective': 'different'}
        with self.assertRaises(Conflict):
            self.runtime.submit('owner/repo', 'same', changed)
        before = self.path.read_bytes()
        with self.assertRaises(NotFound):
            self.runtime.lookup('owner/repo', 'same', self.request_digest(changed))
        self.assertEqual(self.runtime.lookup('owner/repo', 'same', self.request_digest())['id'], task['id'])
        self.assertEqual(self.path.read_bytes(), before)

    def test_lookup_validates_frozen_key_and_full_digest_before_sql(self):
        cases = [(None, 'a' * 64), ('', 'a' * 64), ('x' * 201, 'a' * 64),
                 ('bad\r\nkey', 'a' * 64), ('key', None), ('key', 'a' * 63),
                 ('key', 'A' * 64), ('key', 'g' * 64)]
        with patch.object(self.runtime, 'connection', side_effect=AssertionError('no SQL for invalid identity')):
            for key, digest in cases:
                with self.subTest(key=key, digest=digest), self.assertRaises(ValueError):
                    self.runtime.lookup('owner/repo', key, digest)

    def test_lookup_missing_database_is_not_recreated(self):
        self.runtime.submit('owner/repo', 'same', self.request)
        self.path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            self.runtime.lookup('owner/repo', 'same', self.request_digest())
        self.assertFalse(self.path.exists())
        self.assertEqual(self.provider.calls, 0)

    def test_concurrent_idempotency_submits_once(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda _: self.runtime.submit('owner/repo', 'same', self.request), range(4)))
        self.assertEqual(len({r['id'] for r in records}), 1)
        self.runtime.tick()
        self.assertEqual(self.provider.calls, 1)
        self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.tick()
        self.assertEqual(self.provider.calls, 1)

    def test_same_key_different_request_conflicts(self):
        self.runtime.submit('owner/repo', 'same', self.request)
        with self.assertRaises(Conflict):
            self.runtime.submit('owner/repo', 'same', {**self.request, 'objective': 'Different'})

    def test_owner_cannot_read_or_cancel_another_project(self):
        record = self.runtime.submit('owner/repo', 'same', self.request)
        for operation in (self.runtime.get, self.runtime.cancel):
            with self.assertRaises(NotFound):
                operation('other/repo', record['id'])

    def test_unknown_acceptance_is_never_retried(self):
        self.provider.fail_submit = True
        record = self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.tick()
        for _ in range(3):
            self.runtime.tick()
        result = self.runtime.get('owner/repo', record['id'])
        self.assertEqual(result['status'], 'outcome_unknown')
        self.assertEqual(self.provider.calls, 1)
        self.assertNotIn('secret', str(result))

    def test_restart_resumes_handle_without_resubmission(self):
        record = self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.tick()
        restarted = TaskRuntime(self.path, {'test': self.provider}, {'owner/repo': self.policy})
        self.provider.reply = ProviderReply('completed', output={'summary': 'ok'})
        restarted.tick()
        result = restarted.get('owner/repo', record['id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['output'], {'summary': 'ok'})
        self.assertEqual(self.provider.calls, 1)

    def test_crash_during_submission_is_unknown_not_new_job(self):
        record = self.runtime.submit('owner/repo', 'same', self.request)
        with self.runtime.connection() as db:
            db.execute("UPDATE tasks SET status='submitting' WHERE id=?", (record['id'],))
        restarted = TaskRuntime(self.path, {'test': self.provider}, {'owner/repo': self.policy})
        restarted.tick()
        self.assertEqual(restarted.get('owner/repo', record['id'])['status'], 'outcome_unknown')
        self.assertEqual(self.provider.calls, 0)

    def test_invalid_completed_output_fails_business_schema(self):
        record = self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.tick()
        self.provider.reply = ProviderReply('completed', output={'summary': 12})
        self.runtime.tick()
        self.assertEqual(self.runtime.get('owner/repo', record['id'])['status'], 'invalid_output')

    def test_cancel_needs_provider_confirmation(self):
        record = self.runtime.submit('owner/repo', 'same', self.request)
        self.runtime.tick()
        self.assertEqual(self.runtime.cancel('owner/repo', record['id'])['status'], 'cancel_requested')
        self.runtime.tick()
        self.assertEqual(self.runtime.get('owner/repo', record['id'])['status'], 'cancel_requested')
        self.provider.reply = ProviderReply('cancelled')
        self.runtime.tick()
        self.assertEqual(self.runtime.get('owner/repo', record['id'])['status'], 'cancelled')

    def test_client_cannot_choose_provider_credentials_or_permissions(self):
        for key in ('provider', 'api_key', 'tools', 'repo', 'session_id'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.runtime.submit('owner/repo', key, {**self.request, key: 'untrusted'})
        self.assertEqual(self.provider.calls, 0)

    def test_disabled_mode_and_unknown_model_rejected_before_queue(self):
        for request in ({**self.request, 'mode': 'api'}, {**self.request, 'model': 'other'}):
            with self.assertRaises(PermissionError):
                self.runtime.submit('owner/repo', 'same', request)

    def test_quota_counts_unknown_and_cancel_requested(self):
        self.runtime.policies['owner/repo'] = Policy(providers={'agent': 'test'}, models={'test': {'test-model'}}, max_active=1)
        self.provider.fail_submit = True
        self.runtime.submit('owner/repo', 'first', self.request)
        self.runtime.tick()
        with self.assertRaises(Conflict):
            self.runtime.submit('owner/repo', 'second', self.request)

    def test_new_business_goal_needs_no_service_change(self):
        for i, objective in enumerate(('Explain supplied screenshot', 'Analyze supplied research', 'Rewrite supplied report')):
            self.runtime.submit('owner/repo', str(i), {**self.request, 'objective': objective})
        self.runtime.tick()
        self.assertEqual(self.provider.calls, 3)


class CallbackProvider(FakeProvider):
    callback_driven = True

    def submit(self, request, *, task_id, owner):
        self.calls += 1
        return {'task_id': task_id}


class AgentTaskTests(unittest.TestCase):
    def setUp(self):
        RuntimeTests.setUp(self)
        self.provider = CallbackProvider()
        self.runtime.providers['test'] = self.provider

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_one_attempt_can_claim_even_for_same_bot(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        self.runtime.tick()
        self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt-1')
        with self.assertRaises(Conflict):
            self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt-2')
        self.assertEqual(self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt-1')['attempt_id'], 'attempt-1')

    def test_wrong_worker_cannot_report(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        self.runtime.tick()
        self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
        with self.assertRaises(PermissionError):
            self.runtime.report('owner/repo', task['id'], 'test', 'other', 'attempt', 1, 'completed', {'summary': 'ok'})

    def test_unknown_notification_can_be_resolved_by_authenticated_claim(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        with self.runtime.connection() as db:
            db.execute("UPDATE tasks SET status='outcome_unknown' WHERE id=?", (task['id'],))
        self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
        result = self.runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt', 1, 'completed', {'summary': 'ok'})
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.provider.calls, 0)

    def test_new_material_invalidates_old_completion(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        self.runtime.tick()
        self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
        self.runtime.message('owner/repo', task['id'], 'update-1', {'text': 'Use corrected evidence'})
        with self.assertRaises(Conflict):
            self.runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt', 1, 'completed', {'summary': 'stale'})
        result = self.runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt', 2, 'completed', {'summary': 'current'})
        self.assertEqual(result['output'], {'summary': 'current'})

    def test_messages_and_reports_are_idempotent(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        self.runtime.tick()
        self.runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
        for _ in range(2):
            self.runtime.message('owner/repo', task['id'], 'update-1', {'text': 'Corrected evidence'})
        self.assertEqual(self.runtime.get('owner/repo', task['id'])['revision'], 2)
        for _ in range(2):
            self.runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt', 2, 'completed', {'summary': 'ok'})
        with self.assertRaises(Conflict):
            self.runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt', 2, 'completed', {'summary': 'changed'})

    def test_reviewer_provider_cannot_claim_author_task(self):
        task = self.runtime.submit('owner/repo', 'key', self.request)
        self.runtime.tick()
        with self.assertRaises(PermissionError):
            self.runtime.claim('owner/repo', task['id'], 'reviewer-provider', 'reviewer', 'attempt')


class AgentDeadlineReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.case_number = 0

    def claimed_task(self, state='running'):
        self.case_number += 1
        provider = CallbackProvider()
        policy = Policy(providers={'agent': 'test'}, models={'test': {'test-model'}})
        runtime = TaskRuntime(Path(self.tmp.name) / f'{self.case_number}.sqlite',
                              {'test': provider}, {'owner/repo': policy})
        request = {'mode': 'agent', 'objective': 'Synthetic deadline regression',
                   'model': 'test-model', 'timeout_seconds': 60,
                   'output_schema': {'type': 'object', 'required': ['summary'],
                                     'properties': {'summary': {'type': 'string'}},
                                     'additionalProperties': False}}
        with patch('ai_service.runtime.time.time', return_value=1000.0):
            task = runtime.submit('owner/repo', 'key', request)
            runtime.tick()
            runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
            if state == 'cancel_requested':
                runtime.cancel('owner/repo', task['id'])
        return runtime, task

    def test_first_completed_report_has_exclusive_deadline(self):
        for state in ('running', 'cancel_requested'):
            for offset in (-0.001, 0.0, 0.001):
                with self.subTest(state=state, offset=offset):
                    runtime, task = self.claimed_task(state)
                    before = runtime.get('owner/repo', task['id'])
                    with patch('ai_service.runtime.time.time', return_value=task['deadline'] + offset):
                        if offset < 0:
                            result = runtime.report('owner/repo', task['id'], 'test', 'bot',
                                                    'attempt', 1, 'completed', {'summary': 'ok'})
                            self.assertEqual(result['status'], 'completed')
                            self.assertEqual(result['updated'], task['deadline'] + offset)
                        else:
                            with self.assertRaisesRegex(Conflict, 'deadline'):
                                runtime.report('owner/repo', task['id'], 'test', 'bot',
                                               'attempt', 1, 'completed', {'summary': 'ok'})
                            self.assertEqual(runtime.get('owner/repo', task['id']), before)

    def test_timeout_cancel_cannot_be_overwritten_by_late_completion(self):
        runtime, task = self.claimed_task()
        with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 0.464):
            runtime.tick()
        before = runtime.get('owner/repo', task['id'])
        self.assertEqual(before['status'], 'cancel_requested')
        with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 10.695):
            with self.assertRaisesRegex(Conflict, 'deadline'):
                runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                               1, 'completed', {'summary': 'ok'})
        self.assertEqual(runtime.get('owner/repo', task['id']), before)

    def test_valid_completion_replay_keeps_original_terminal_after_deadline(self):
        runtime, task = self.claimed_task()
        with patch('ai_service.runtime.time.time', return_value=task['deadline'] - 0.001):
            first = runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                   1, 'completed', {'summary': 'ok'})
        with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 10):
            self.assertEqual(runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                            1, 'completed', {'summary': 'ok'}), first)
            with self.assertRaisesRegex(Conflict, 'different final report'):
                runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                               1, 'completed', {'summary': 'changed'})
            for worker, attempt, revision, error in [('other', 'attempt', 1, PermissionError),
                                                     ('bot', 'other', 1, PermissionError),
                                                     ('bot', 'attempt', 2, Conflict)]:
                with self.subTest(worker=worker, attempt=attempt, revision=revision):
                    with self.assertRaises(error):
                        runtime.report('owner/repo', task['id'], 'test', worker, attempt,
                                       revision, 'completed', {'summary': 'ok'})
        self.assertEqual(runtime.get('owner/repo', task['id']), first)

    def test_late_cancelled_and_failed_confirmation_and_replay_are_preserved(self):
        for state in ('running', 'cancel_requested'):
            for status in ('cancelled', 'failed'):
                with self.subTest(state=state, status=status):
                    runtime, task = self.claimed_task(state)
                    with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 10):
                        first = runtime.report('owner/repo', task['id'], 'test', 'bot',
                                               'attempt', 1, status)
                    self.assertEqual(first['status'], status)
                    with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 20):
                        self.assertEqual(runtime.report('owner/repo', task['id'], 'test', 'bot',
                                                        'attempt', 1, status), first)
                        with self.assertRaisesRegex(Conflict, 'different final report'):
                            runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                           1, 'completed', {'summary': 'ok'})

    def test_wrong_attempt_or_revision_cannot_bypass_first_report_checks(self):
        for state in ('running', 'cancel_requested'):
            for offset in (-1, 1):
                for worker, attempt, revision, error in [('other', 'attempt', 1, PermissionError),
                                                         ('bot', 'other', 1, PermissionError),
                                                         ('bot', 'attempt', 2, Conflict)]:
                    with self.subTest(state=state, offset=offset, worker=worker,
                                      attempt=attempt, revision=revision):
                        runtime, task = self.claimed_task(state)
                        before = runtime.get('owner/repo', task['id'])
                        with patch('ai_service.runtime.time.time', return_value=task['deadline'] + offset):
                            with self.assertRaises(error):
                                runtime.report('owner/repo', task['id'], 'test', worker, attempt,
                                               revision, 'completed', {'summary': 'ok'})
                        self.assertEqual(runtime.get('owner/repo', task['id']), before)

    def test_completed_report_uses_one_acceptance_timestamp(self):
        runtime, task = self.claimed_task()
        accepted = task['deadline'] - 0.001
        with patch('ai_service.runtime.time.time', side_effect=[accepted, task['deadline'] + 1]) as clock:
            result = runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                    1, 'completed', {'summary': 'ok'})
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(result['updated'], accepted)
        self.assertEqual(runtime.events('owner/repo', task['id'])[-1]['created'], accepted)

    def test_late_first_report_does_not_bypass_provider_or_project_binding(self):
        for state in ('running', 'cancel_requested'):
            for owner, provider, error in [('owner/repo', 'other-provider', PermissionError),
                                           ('other/repo', 'test', NotFound)]:
                with self.subTest(state=state, owner=owner, provider=provider):
                    runtime, task = self.claimed_task(state)
                    runtime.policies['other/repo'] = runtime.policies['owner/repo']
                    before = runtime.get('owner/repo', task['id'])
                    with patch('ai_service.runtime.time.time', return_value=task['deadline'] + 1):
                        with self.assertRaises(error):
                            runtime.report(owner, task['id'], provider, 'bot', 'attempt',
                                           1, 'completed', {'summary': 'ok'})
                    self.assertEqual(runtime.get('owner/repo', task['id']), before)

    def test_invalid_output_remains_invalid_before_deadline(self):
        runtime, task = self.claimed_task()
        with patch('ai_service.runtime.time.time', return_value=task['deadline'] - 1):
            result = runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                    1, 'completed', {'summary': 12})
        self.assertEqual(result['status'], 'invalid_output')
        self.assertIsNone(result['output'])


class RecordingAPIProvider(CallbackProvider):
    mode = 'api'
    callback_driven = False

    def __init__(self):
        super().__init__()
        self.polled = []

    def poll(self, handle):
        self.polled.append(handle['task_id'])
        return ProviderReply('completed', output={'summary': 'ok'})


class SchemaReferenceTests(unittest.TestCase):
    def setUp(self):
        RuntimeTests.setUp(self)

    def tearDown(self):
        RuntimeTests.tearDown(self)

    def _runtime(self, name, *, native):
        provider = CallbackProvider() if native else RecordingAPIProvider()
        mode = 'agent' if native else 'api'
        policy = Policy({mode: 'test'}, {'test': {'test-model'}})
        runtime = TaskRuntime(self.path.with_name(name + '.sqlite'), {'test': provider},
                              {'owner/repo': policy})
        return runtime, provider, {**self.request, 'mode': mode}

    def _unresolved_schemas(self):
        return ({'$ref': '#/$defs/missing'}, {'$ref': '#missing-anchor'},
                {'$dynamicRef': '#missing-anchor'})

    def test_native_unresolved_reference_is_terminal_and_report_retry_is_idempotent(self):
        for i, schema in enumerate(self._unresolved_schemas()):
            with self.subTest(schema=schema):
                runtime, provider, request = self._runtime(f'native-{i}', native=True)
                task = runtime.submit('owner/repo', 'bad', {**request, 'output_schema': schema})
                runtime.tick()
                runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
                before = len(runtime.events('owner/repo', task['id']))
                result = runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                        1, 'completed', {'summary': 'ok'})
                self.assertEqual(result['status'], 'invalid_output')
                self.assertEqual(result['error'], 'output_schema_mismatch')
                self.assertIsNone(result['output'])
                self.assertEqual(len(runtime.events('owner/repo', task['id'])), before + 1)
                self.assertEqual(runtime.report('owner/repo', task['id'], 'test', 'bot',
                                                'attempt', 1, 'completed', {'summary': 'ok'}), result)
                self.assertEqual(len(runtime.events('owner/repo', task['id'])), before + 1)
                with self.assertRaises(Conflict):
                    runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                   1, 'completed', {'summary': 'changed'})
                runtime.tick()
                self.assertEqual(provider.calls, 1)
                self.assertEqual(runtime.get('owner/repo', task['id']), result)

    def test_api_unresolved_reference_does_not_block_later_valid_task(self):
        for i, schema in enumerate(self._unresolved_schemas()):
            with self.subTest(schema=schema):
                runtime, provider, request = self._runtime(f'api-{i}', native=False)
                with patch('ai_service.runtime.time.time', return_value=100.0):
                    bad = runtime.submit('owner/repo', 'bad', {**request, 'output_schema': schema})
                with patch('ai_service.runtime.time.time', return_value=101.0):
                    good = runtime.submit('owner/repo', 'good', request)
                with patch('ai_service.runtime.time.time', return_value=102.0):
                    runtime.tick()
                    runtime.tick()
                    bad_result = runtime.get('owner/repo', bad['id'])
                    self.assertEqual(bad_result['status'], 'invalid_output')
                    self.assertEqual(bad_result['error'], 'output_schema_mismatch')
                    self.assertIsNone(bad_result['output'])
                    self.assertEqual(runtime.get('owner/repo', good['id'])['status'], 'completed')
                    self.assertEqual(provider.polled, [bad['id'], good['id']])
                    events = runtime.events('owner/repo', bad['id'])
                    runtime.tick()
                    self.assertEqual(runtime.get('owner/repo', bad['id']), bad_result)
                    self.assertEqual(runtime.events('owner/repo', bad['id']), events)
                    self.assertEqual(provider.polled, [bad['id'], good['id']])
                    self.assertEqual(provider.calls, 2)

    def test_valid_local_references_still_complete_on_both_paths(self):
        for native in (True, False):
            for i, (keyword, reference) in enumerate((('$ref', '#/$defs/result'),
                                                     ('$ref', '#result'),
                                                     ('$dynamicRef', '#dynamic-result'))):
                with self.subTest(native=native, reference=reference):
                    runtime, _, request = self._runtime(f'valid-{native}-{i}', native=native)
                    schema = {'$defs': {'result': {**self.request['output_schema'],
                                                  '$anchor': 'result',
                                                  '$dynamicAnchor': 'dynamic-result'}},
                              keyword: reference}
                    task = runtime.submit('owner/repo', 'valid', {**request, 'output_schema': schema})
                    runtime.tick()
                    if native:
                        runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
                        runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                       1, 'completed', {'summary': 'ok'})
                    else:
                        runtime.tick()
                    result = runtime.get('owner/repo', task['id'])
                    self.assertEqual(result['status'], 'completed')
                    self.assertEqual(result['output'], {'summary': 'ok'})

    def test_unrelated_validation_errors_are_not_swallowed(self):
        for native in (True, False):
            with self.subTest(native=native):
                runtime, _, request = self._runtime(f'fault-{native}', native=native)
                task = runtime.submit('owner/repo', 'valid', request)
                runtime.tick()
                if native:
                    runtime.claim('owner/repo', task['id'], 'test', 'bot', 'attempt')
                before = runtime.get('owner/repo', task['id'])
                events = runtime.events('owner/repo', task['id'])
                with patch('ai_service.runtime.Draft202012Validator.validate',
                           side_effect=RuntimeError('synthetic validator fault')):
                    with self.assertRaisesRegex(RuntimeError, 'synthetic validator fault'):
                        if native:
                            runtime.report('owner/repo', task['id'], 'test', 'bot', 'attempt',
                                           1, 'completed', {'summary': 'ok'})
                        else:
                            runtime.tick()
                self.assertEqual(runtime.get('owner/repo', task['id']), before)
                self.assertEqual(runtime.events('owner/repo', task['id']), events)

    def test_external_references_remain_rejected_before_submission(self):
        for keyword in ('$ref', '$dynamicRef'):
            with self.subTest(keyword=keyword):
                with self.assertRaisesRegex(ValueError, 'external schema references are forbidden'):
                    self.runtime.submit('owner/repo', keyword,
                                        {**self.request, 'output_schema':
                                         {keyword: 'https://example.invalid/private-schema'}})
        self.assertEqual(self.provider.calls, 0)


class TaskProfileTests(unittest.TestCase):
    def test_project_configured_profile_selects_independent_worker_assignment(self):
        with tempfile.TemporaryDirectory() as temp:
            author, reviewer = CallbackProvider(), CallbackProvider()
            policy = Policy({'agent': 'author', 'agent:second-opinion': 'reviewer'},
                            {'author': {'configured'}, 'reviewer': {'configured'}})
            runtime = TaskRuntime(Path(temp) / 'state.sqlite', {'author': author, 'reviewer': reviewer},
                                  {'owner/repo': policy})
            request = {'mode': 'agent', 'model': 'configured', 'objective': 'Review supplied material',
                       'profile': 'second-opinion', 'output_schema': {'type': 'object'}}
            task = runtime.submit('owner/repo', 'one', request)
            runtime.tick()
            self.assertEqual(task['provider'], 'reviewer')
            self.assertEqual(author.calls, 0)
            self.assertEqual(reviewer.calls, 1)
            with self.assertRaises(PermissionError):
                runtime.claim('owner/repo', task['id'], 'author', 'author-identity', 'one')
            with self.assertRaises(PermissionError):
                runtime.submit('owner/repo', 'two', {**request, 'profile': 'not-configured'})

if __name__ == '__main__':
    unittest.main()
