"""Synthetic cloud migration checks. No cloud, model, broker or notification calls."""
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloud_run_health_job as job
import monitor_cloud_state as state
import export_local_monitor_state as local_adapter
import health_cycle

MANIFEST = {'schema_version': state.SCHEMA, 'source_revision': 'a' * 40, 'qpk_revision': 'b' * 40}
LEDGER = {'fingerprint': 'old', 'deliveries': {'synthetic-event': {'status': 'unknown'}},
          'operational_diagnosis_attempts': ['previous-attempt']}


def workspace(root):
    (root / 'manifest.json').write_text(json.dumps(MANIFEST))
    path = root / 'monitor/data/alert-state/health_cycle.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(LEDGER))
    return state.pack(root)


class MemoryStore:
    def __init__(self, raw):
        self.raw, self.generation, self.owner = raw, 1, None
        self.calls, self.lock = [], threading.Lock()

    def read(self, name):
        self.calls.append('read')
        return self.raw, self.generation

    def claim(self, record):
        with self.lock:
            self.calls.append('claim')
            if self.owner is not None:
                return None
            self.owner = record['execution_id']
            return 5

    def commit(self, raw, *, state_generation, claim_generation):
        self.calls.append('commit')
        if (state_generation, claim_generation) != (self.generation, 5):
            raise state.StateUnavailable('generation_conflict')
        self.raw, self.owner = raw, None
        self.generation += 1

    def checkpoint(self, raw, *, state_generation, claim_generation):
        self.calls.append('checkpoint')
        if (state_generation, claim_generation) != (self.generation, 5) or self.owner is None:
            raise state.StateUnavailable('generation_conflict')
        self.raw = raw
        self.generation += 1
        return self.generation

    def release(self, *, claim_generation):
        if claim_generation != 5:
            raise state.StateUnavailable('claim_conflict')
        self.calls.append('release')
        self.owner = None


class CloudJobTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MemoryStore(workspace(Path(self.directory.name)))

    def evaluate(self, root, *, effects_enabled):
        ledger = root / 'monitor/data/alert-state/health_cycle.json'
        self.assertEqual(json.loads(ledger.read_text()), LEDGER)
        if effects_enabled:
            ledger.write_text(json.dumps({**LEDGER, 'new_event': 'sent'}))
        return {'healthy': False, 'snapshot_count': 0, 'data_status': 'unavailable'}

    def test_preview_is_read_only_even_with_all_callbacks_present(self):
        refresh, publish, website_publish = Mock(), Mock(), Mock()
        before = self.store.raw
        def evaluate(root, *, effects_enabled):
            self.assertFalse(effects_enabled)
            return self.evaluate(root, effects_enabled=effects_enabled)
        result = job.run_once(self.store, evaluate, refresh=refresh, publish=publish,
                              website_publish=website_publish)
        self.assertEqual(self.store.calls, ['read'])
        self.assertEqual(self.store.raw, before)
        refresh.assert_not_called()
        publish.assert_not_called()
        website_publish.assert_not_called()
        self.assertEqual((result['healthy'], result['data_status']), (False, 'unavailable'))
        self.assertNotIn('deliveries', json.dumps(result))

    def test_commit_preserves_legacy_unknown_and_diagnosis_state(self):
        job.run_once(self.store, self.evaluate, mode='run', execution_id='synthetic')
        self.assertEqual(self.store.calls, ['claim', 'read', 'commit'])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state.restore(self.store.raw, root)
            ledger = json.loads((root / 'monitor/data/alert-state/health_cycle.json').read_text())
        for key in LEDGER:
            self.assertEqual(ledger[key], LEDGER[key])
        self.assertEqual(ledger['new_event'], 'sent')

    def test_uncertain_effect_keeps_claim_and_blocks_replay(self):
        calls = []
        def uncertain(*args, **kwargs):
            calls.append('effect')
            raise TimeoutError('synthetic outcome unknown')
        with self.assertRaises(TimeoutError):
            job.run_once(self.store, uncertain, mode='run', execution_id='first')
        result = job.run_once(self.store, uncertain, mode='run', execution_id='second')
        self.assertEqual(calls, ['effect'])
        self.assertEqual(result['status'], 'blocked')

    def test_computed_results_are_saved_before_derived_publication(self):
        def publish(root, **kwargs):
            self.assertEqual(self.store.calls, ['claim', 'read', 'checkpoint'])
            with tempfile.TemporaryDirectory() as directory:
                restored = Path(directory)
                state.restore(self.store.raw, restored)
                ledger = json.loads((restored / 'monitor/data/alert-state/health_cycle.json').read_text())
                self.assertEqual(ledger['new_event'], 'sent')
        job.run_once(self.store, self.evaluate, mode='run', execution_id='first', publish=publish)
        self.assertEqual(self.store.calls[-1], 'commit')
        self.assertIsNone(self.store.owner)

    def test_failed_website_does_not_lose_results_or_block_next_cycle(self):
        from monitor_website_publish import publish_saved_snapshot

        with patch('monitor_website_publish.read_sync_token', side_effect=RuntimeError('synthetic denied')) as read:
            def website(root, *, checkpoint):
                return publish_saved_snapshot(root, checkpoint=checkpoint, url='synthetic', secret_version='synthetic')
            result = job.run_once(self.store, self.evaluate, mode='run', execution_id='first', website_publish=website)
            self.assertEqual(result['website_publication'], 'unconfirmed')
            self.assertEqual(result['website_publication_reason'], 'credential_unavailable')
            self.assertIsNone(result['website_publication_http_status'])
            self.assertIsNone(self.store.owner)
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                state.restore(self.store.raw, root)
                receipt = json.loads((root / 'monitor/data/health/website-publication.json').read_text())
                self.assertEqual(receipt['status'], 'unconfirmed')
                ledger = json.loads((root / 'monitor/data/alert-state/health_cycle.json').read_text())
                self.assertEqual(ledger['deliveries'], LEDGER['deliveries'])
                self.assertEqual(ledger['new_event'], 'sent')
            evaluate = Mock(return_value={'healthy': True, 'snapshot_count': 1, 'data_status': 'ready'})
            again = job.run_once(self.store, evaluate, mode='run', execution_id='second', website_publish=website)
            self.assertEqual(again['status'], 'evaluated')
            self.assertIsNone(self.store.owner)
            self.assertEqual(read.call_count, 2)

    def test_untrusted_website_reason_cannot_leak_or_fail_cycle(self):
        for reason in (['SENTINEL_PRIVATE'], 'SENTINEL_PRIVATE'):
            with self.subTest(reason=reason):
                store = MemoryStore(self.store.raw)
                def website(root, *, checkpoint):
                    receipt = root / 'monitor/data/health/website-publication.json'
                    receipt.parent.mkdir(parents=True, exist_ok=True)
                    receipt.write_text(json.dumps({'reason': reason, 'http_status': 'SENTINEL_PRIVATE'}))
                    return 'unconfirmed'
                result = job.run_once(store, self.evaluate, mode='run', execution_id='first', website_publish=website)
                self.assertEqual(result['website_publication_reason'], 'unknown')
                self.assertIsNone(result['website_publication_http_status'])
                self.assertNotIn('SENTINEL_PRIVATE', json.dumps(result))

    def test_uncertain_computation_save_stops_before_any_publication(self):
        publish, website = Mock(), Mock()
        self.store.checkpoint = Mock(side_effect=state.StateUnavailable('unknown_write'))
        with self.assertRaises(state.StateUnavailable):
            job.run_once(self.store, self.evaluate, mode='run', execution_id='first',
                         publish=publish, website_publish=website)
        publish.assert_not_called()
        website.assert_not_called()
        self.assertIsNotNone(self.store.owner)

    def test_definite_input_failure_before_effects_releases_only_its_claim(self):
        before = self.store.raw
        evaluate = Mock()
        with self.assertRaises(state.StateUnavailable):
            job.run_once(self.store, evaluate, mode='run', execution_id='first',
                         refresh=Mock(side_effect=state.StateUnavailable('stale_input')))
        evaluate.assert_not_called()
        self.assertIsNone(self.store.owner)
        self.assertEqual(self.store.raw, before)
        self.assertEqual(self.store.calls, ['claim', 'read', 'release'])

    def test_concurrent_cycles_cannot_read_old_state_before_claim(self):
        entered, release = threading.Event(), threading.Event()
        def evaluate(root, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return self.evaluate(root, **kwargs)
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(job.run_once, self.store, evaluate, mode='run', execution_id='first')
            self.assertTrue(entered.wait(5))
            second = job.run_once(self.store, evaluate, mode='run', execution_id='second')
            release.set()
            first.result(5)
        self.assertEqual(second['status'], 'blocked')
        self.assertEqual(self.store.calls.count('read'), 1)

    def test_production_cli_requires_notification_binding_before_any_claim(self):
        storage = types.ModuleType('google.cloud.storage')
        storage.Client = Mock()
        google = types.ModuleType('google')
        cloud = types.ModuleType('google.cloud')
        google.cloud, cloud.storage = cloud, storage
        with patch.dict(os.environ, {'MONITOR_MODE': 'run', 'MONITOR_STATE_GCS_URI': 'gs://synthetic/state'}, clear=True), \
                patch.dict(sys.modules, {'google': google, 'google.cloud': cloud, 'google.cloud.storage': storage}), \
                patch.object(job, 'CloudState') as store:
            self.assertEqual(job.main(), 1)
            store.return_value.claim.assert_not_called()

    def test_missing_website_binding_does_not_block_cli_and_is_reported_as_warning(self):
        storage = types.ModuleType('google.cloud.storage')
        storage.Client = Mock()
        google = types.ModuleType('google')
        cloud = types.ModuleType('google.cloud')
        google.cloud, cloud.storage = cloud, storage
        result = {'status': 'evaluated', 'healthy': True, 'website_publication': 'unconfirmed'}
        output = io.StringIO()
        with patch.dict(os.environ, {'MONITOR_MODE': 'run', 'MONITOR_STATE_GCS_URI': 'gs://synthetic/state',
                                     'TELEGRAM_TOKEN': 'synthetic', 'GLOBAL_TELEGRAM_CHAT_ID': 'synthetic'}, clear=True), \
                patch.dict(sys.modules, {'google': google, 'google.cloud': cloud, 'google.cloud.storage': storage}), \
                patch.object(job, 'run_once', return_value=result) as run, \
                patch('sys.stdout', output):
            self.assertEqual(job.main(), 0)
            run.assert_called_once()
            self.assertTrue(callable(run.call_args.kwargs['website_publish']))
        self.assertEqual(json.loads(output.getvalue())['severity'], 'WARNING')

    def test_checkpoint_failure_stops_before_telegram_and_retains_claim(self):
        send = Mock()
        self.store.checkpoint = Mock(side_effect=state.StateUnavailable('unknown_write'))
        def evaluate(root, **kwargs):
            health_cycle._write_alert_state(root / 'monitor', LEDGER)
            send()
        with self.assertRaises(state.StateUnavailable):
            job.run_once(self.store, evaluate, mode='run', execution_id='first')
        send.assert_not_called()
        self.assertIsNotNone(self.store.owner)
        self.assertEqual(job.run_once(self.store, evaluate, mode='run', execution_id='second')['status'], 'blocked')

    def test_intermediate_checkpoint_keeps_image_code_out_of_state(self):
        def save(raw, **kwargs):
            self.store.raw = raw
            self.store.generation += 1
            return self.store.generation
        self.store.checkpoint = Mock(side_effect=save)
        def evaluate(root, **kwargs):
            code = root / 'projects/CryptoStrategies/script.py'
            code.parent.mkdir(parents=True)
            code.write_text('image code')
            health_cycle._write_alert_state(root / 'monitor', LEDGER)
            code.unlink()
            return {'healthy': False, 'snapshot_count': 0, 'data_status': 'unavailable'}
        job.run_once(self.store, evaluate, mode='run', execution_id='first')
        self.store.checkpoint.assert_called_once()
        self.assertNotIn(b'image code', self.store.raw)


class PortableStateTests(unittest.TestCase):
    def archive(self, entries):
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode='w:gz') as archive:
            for name, data, kind in entries:
                entry = tarfile.TarInfo(name)
                entry.type = kind
                entry.size = len(data)
                entry.linkname = '/outside' if kind != tarfile.REGTYPE else ''
                archive.addfile(entry, io.BytesIO(data))
        return raw.getvalue()

    def test_invalid_members_reject_before_any_file_is_written(self):
        cases = [('../outside.json', tarfile.REGTYPE), ('/outside.json', tarfile.REGTYPE),
                 ('monitor/data/health/credentials.json', tarfile.REGTYPE),
                 ('monitor/data/health/result.json', tarfile.SYMTYPE),
                 ('monitor/data/health/result.json', tarfile.LNKTYPE),
                 ('projects/CryptoStrategies/scripts/run.py', tarfile.REGTYPE)]
        for name, kind in cases:
            with self.subTest(name=name, kind=kind), tempfile.TemporaryDirectory() as directory:
                raw = self.archive([('manifest.json', json.dumps(MANIFEST).encode(), tarfile.REGTYPE),
                                    (name, b'{}', kind)])
                root = Path(directory)
                with self.assertRaises(state.StateUnavailable):
                    state.restore(raw, root)
                self.assertEqual(list(root.iterdir()), [])

    def test_duplicate_members_and_expansion_limit_are_rejected(self):
        raw = self.archive([('manifest.json', json.dumps(MANIFEST).encode(), tarfile.REGTYPE)] * 2)
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(state.StateUnavailable):
            state.restore(raw, Path(directory))
        raw = self.archive([('manifest.json', json.dumps(MANIFEST).encode(), tarfile.REGTYPE)])
        with tempfile.TemporaryDirectory() as directory, patch.object(state, 'MAX_EXPANDED', 2):
            with self.assertRaises(state.StateUnavailable):
                state.restore(raw, Path(directory))

    def test_local_lock_is_not_exported_but_delivery_ledger_is(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace(root)
            (root / 'monitor/data/alert-state/health_cycle.json.lock').touch()
            raw = state.pack(root)
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            self.assertIn('monitor/data/alert-state/health_cycle.json', archive.getnames())
            self.assertFalse(any(name.endswith('.lock') for name in archive.getnames()))


class HealthPreviewTests(unittest.TestCase):
    def test_real_core_preview_never_calls_effects_or_clears_prior_delivery(self):
        for errors in ([], [{'domain': 'crypto', 'code': 'monitor_data_unavailable', 'error_type': 'RuntimeError'}]):
            with self.subTest(errors=errors), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                workspace(root)
                ledger = root / 'monitor/data/alert-state/health_cycle.json'
                before = ledger.read_bytes()
                modules = {}
                for name, symbol in (
                    ('drift_detector', 'run_drift_detection'), ('performance_monitor', 'run_monitor'),
                    ('health_dashboard', 'build_dashboard'), ('watch.runner', 'dispatch_strategy_watch_findings'),
                ):
                    full = 'quant_platform_kit.strategy_lifecycle.' + name
                    module = types.ModuleType(full)
                    setattr(module, symbol, Mock())
                    modules[full] = module
                def dashboard(*, output_dir, **kwargs):
                    path = Path(output_dir)
                    path.mkdir(parents=True, exist_ok=True)
                    (path / 'strategy_health_dashboard.json').write_text('{"strategies":[]}')
                modules['quant_platform_kit.strategy_lifecycle.health_dashboard'].build_dashboard = dashboard
                snapshot = types.ModuleType('build_dashboard_snapshot')
                snapshot.build_payload = Mock(return_value={'data_status': 'ready', 'errors': [], 'strategies': []})
                modules[snapshot.__name__] = snapshot
                with (
                    patch.dict(os.environ, {'QUANT_MONITOR_ROOT': str(root / 'monitor'),
                                            'TELEGRAM_TOKEN': 'synthetic', 'GLOBAL_TELEGRAM_CHAT_ID': 'synthetic'}, clear=True),
                    patch.dict(sys.modules, modules),
                    patch.object(health_cycle, '_load_expected_coverage', return_value=({}, {}, None, {}, set())),
                    patch.object(health_cycle, '_refresh_and_collect_drift', return_value=({}, {}, errors)),
                    patch.object(health_cycle, '_build_monitoring_findings', return_value=[{'synthetic': True}]),
                    patch.object(health_cycle, 'deliver_telegram_targets') as send,
                    patch.object(health_cycle, '_run_operational_diagnosis') as diagnose,
                    patch.object(health_cycle, '_clear_alert') as clear,
                    patch('builtins.print') as output,
                    patch('socket.create_connection', side_effect=AssertionError('network forbidden')),
                ):
                    self.assertEqual(health_cycle.main(effects_enabled=False, emit_summary=False), 2 if errors else 0)
                for callback in (send, diagnose, clear, output,
                                 modules['quant_platform_kit.strategy_lifecycle.watch.runner'].dispatch_strategy_watch_findings):
                    callback.assert_not_called()
                self.assertEqual(ledger.read_bytes(), before)


class LocalAdapterTests(unittest.TestCase):
    def bindings(self, root):
        monitor = root / 'monitor'
        projects = {name: root / 'projects' / name / 'data' for name in state.DATA_PROJECTS}
        for path in [monitor / 'data', *projects.values()]:
            path.mkdir(parents=True)
        return monitor, projects

    def test_source_adapter_preserves_observation_times_and_unknown_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor, projects = self.bindings(root)
            ledger = monitor / 'data/alert-state/health_cycle.json'
            ledger.parent.mkdir()
            ledger.write_text(json.dumps({**LEDGER, 'as_of': '2026-01-01T00:00:00Z'}))
            raw, count = local_adapter.capture(monitor, projects, source_revision='a' * 40, qpk_revision='b' * 40)
            self.assertGreater(count, 0)
            target = root / 'restored'
            target.mkdir()
            manifest = state.restore(raw, target)
            self.assertEqual(manifest['source_kind'], 'self_hosted')
            self.assertEqual((target / 'monitor/data/alert-state/health_cycle.json').read_bytes(), ledger.read_bytes())

    def test_source_adapter_rejects_a_link_outside_configured_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor, projects = self.bindings(root)
            outside = root / 'outside.json'
            outside.write_text('{"synthetic":"must-not-be-read"}')
            health = monitor / 'data/health'
            health.mkdir()
            (health / 'result.json').symlink_to(outside)
            with self.assertRaisesRegex(state.StateUnavailable, 'outside_data'):
                local_adapter.capture(monitor, projects, source_revision='a' * 40, qpk_revision='b' * 40)

    def test_external_actions_checkpoint_is_preserved_without_its_local_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            monitor, projects = self.bindings(root)
            actions = root / 'diagnosis-actions'
            actions.mkdir()
            record = {'job_id': None, 'request_sha256': 'c' * 64, 'service_sha256': 'd' * 64,
                      'observed_at': '2026-10-26T00:00:00Z', 'deadline': 1792973460}
            (actions / '2026-10-26.json').write_text(json.dumps(record))
            (actions / 'lock').touch()
            raw, _ = local_adapter.capture(monitor, projects, source_revision='a' * 40,
                                            qpk_revision='b' * 40, diagnosis_actions=actions)
            target = root / 'restored'
            target.mkdir()
            state.restore(raw, target)
            self.assertEqual(json.loads((target / 'monitor/data/diagnosis-actions/2026-10-26.json').read_text()), record)
            self.assertFalse((target / 'monitor/data/diagnosis-actions/lock').exists())


class CloudStateTests(unittest.TestCase):
    def setUp(self):
        self.active, self.current = Mock(), Mock()
        self.active.generation = 7
        self.current.generation, self.current.size = 3, 2
        self.current.download_as_bytes.return_value = b'{}'
        bucket = Mock()
        bucket.blob.side_effect = lambda name: self.active if name.endswith('/active.json') else self.current
        client = Mock()
        client.bucket.return_value = bucket
        self.store = state.CloudState(client, 'gs://synthetic-monitor/state')
        self.precondition = type('PreconditionFailed', (Exception,), {})
        exception_module = types.ModuleType('google.api_core.exceptions')
        exception_module.PreconditionFailed = self.precondition
        modules = {'google': types.ModuleType('google'), 'google.api_core': types.ModuleType('google.api_core'),
                   exception_module.__name__: exception_module}
        self.modules = patch.dict(sys.modules, modules)
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def test_reads_pin_generation_and_disable_implicit_retries(self):
        self.assertEqual(self.store.read('current.tar.gz'), (b'{}', 3))
        self.current.reload.assert_called_once_with(timeout=20, retry=None)
        self.current.download_as_bytes.assert_called_once_with(if_generation_match=3, timeout=60, retry=None)

    def test_only_create_zero_can_claim_and_existing_claim_is_not_overwritten(self):
        self.assertEqual(self.store.claim({'synthetic': True}), 7)
        self.assertEqual(self.active.upload_from_string.call_args.kwargs['if_generation_match'], 0)
        self.assertIsNone(self.active.upload_from_string.call_args.kwargs['retry'])
        self.active.upload_from_string.side_effect = self.precondition()
        self.assertIsNone(self.store.claim({'synthetic': True}))

    def test_commit_uses_both_exact_generations_and_preserves_uncertain_claim(self):
        self.store.commit(b'synthetic', state_generation=3, claim_generation=7)
        self.assertEqual(self.current.upload_from_string.call_args.kwargs['if_generation_match'], 3)
        self.active.delete.assert_called_once_with(if_generation_match=7, timeout=20, retry=None)
        self.active.delete.reset_mock()
        self.current.upload_from_string.side_effect = TimeoutError('must-not-leak-private-details')
        with self.assertRaisesRegex(state.StateUnavailable, '^checkpoint_outcome_unknown$'):
            self.store.commit(b'synthetic', state_generation=3, claim_generation=7)
        self.active.delete.assert_not_called()

    def test_intermediate_checkpoint_keeps_the_same_claim_until_explicit_release(self):
        self.current.generation = 4
        generation = self.store.checkpoint(b'intent', state_generation=3, claim_generation=7)
        self.assertEqual(generation, 4)
        self.active.reload.assert_called_once_with(if_generation_match=7, timeout=20, retry=None)
        self.active.delete.assert_not_called()
        self.current.generation = 5
        generation = self.store.checkpoint(b'accepted', state_generation=generation, claim_generation=7)
        self.assertEqual(generation, 5)
        self.assertEqual(self.current.upload_from_string.call_args.kwargs['if_generation_match'], 4)
        self.active.delete.assert_not_called()
        self.store.release(claim_generation=7)
        self.active.delete.assert_called_once_with(if_generation_match=7, timeout=20, retry=None)

    def test_unreadable_state_is_never_converted_to_an_empty_success(self):
        self.current.reload.side_effect = OSError('private')
        with self.assertRaisesRegex(state.StateUnavailable, '^state_read_failed$'):
            self.store.read('current.tar.gz')
        self.current.download_as_bytes.assert_not_called()


if __name__ == '__main__':
    unittest.main()
