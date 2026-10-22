"""Synthetic App keys and transport only. No GitHub or worker E2E claims."""
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from ai_service.auth import Principal
from ai_service.github_apps import AppRejected, GitHubApps, GitHubComments, validate_apps
from ai_service.runtime import Conflict, Policy, TaskRuntime
from ai_service.server import Application, build_application, validate_configuration
from ai_service.transport import HttpReply

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def config(owner='owner'):
    return {'enabled': True, 'owner': owner, 'resource': 'https://service.example/mcp',
            'issuers': {}, 'state_path': 'unused.sqlite',
            'projects': {owner + '/repo': {'providers': {'agent': 'dot'}, 'models': {'dot': ['model']}}},
            'providers': {'dot': {'type': 'dot_events'}},
            'grants': [{'identity': 'dot-worker', 'issuer': 'https://issuer.example', 'subject': 'synthetic',
                        'project': owner + '/repo', 'provider': 'dot', 'scopes': ['tasks:work']}],
            'github_apps': {'dot-worker': {'provider': 'dot', 'app_id': 101, 'installation_id': 201,
                                           'private_key_ref': 'synthetic:dot'}}}


class FakeGitHub:
    def __init__(self, owner='owner', app_id=101, installation_id=201):
        self.owner, self.app_id, self.installation_id = owner, app_id, installation_id
        self.calls, self.comments = [], []
        self.behavior = 'success'
        self.token_permissions = {'issues': 'write', 'metadata': 'read'}
        self.issue_is_pr = False
        self.pages = {}
        self.app_owner = owner
        self.expiry = '2099-01-01T00:00:00Z'

    def comment(self, body, number=7):
        return {'id': 500 + len(self.comments), 'body': body,
                'issue_url': f'https://api.github.com/repos/{self.owner}/repo/issues/{number}',
                'performed_via_github_app': {'id': self.app_id},
                'user': {'type': 'Bot', 'login': 'synthetic-app[bot]'}}

    def request(self, url, *, method='GET', headers=None, body=b'', **kwargs):
        self.calls.append((url, method, json.loads(body) if body else None))
        self.assert_fixed(url)
        if url.endswith('/app'):
            claims = jwt.decode(headers['Authorization'][7:], KEY.public_key(), algorithms=['RS256'])
            assert claims['iss'] == str(self.app_id) and claims['exp'] - claims['iat'] <= 600
            data = {'id': self.app_id, 'owner': {'login': self.app_owner}, 'slug': 'synthetic-app'}
        elif url.endswith('/app/installations/' + str(self.installation_id)):
            data = {'id': self.installation_id, 'app_id': self.app_id, 'account': {'login': self.owner},
                    'permissions': {'issues': 'write'}, 'suspended_at': None}
        elif url.endswith('/access_tokens'):
            assert method == 'POST' and json.loads(body) == {'repositories': ['repo'], 'permissions': {'issues': 'write'}}
            data = {'token': 'synthetic-installation-token', 'expires_at': self.expiry,
                    'permissions': self.token_permissions, 'repositories': [{'full_name': self.owner + '/repo'}]}
        elif '/comments?' in url:
            page = int(url.split('page=')[-1])
            data = self.pages.get(page, self.comments if page == 1 else [])
        elif url.endswith('/comments'):
            content = json.loads(body)['body']
            if self.behavior in {'success', 'lost', 'malformed'}:
                self.comments.append(self.comment(content))
            if self.behavior in {'lost', 'timeout'}:
                raise TimeoutError('private transport detail')
            if self.behavior == 'reject':
                return HttpReply(403, b'private raw rejection')
            if self.behavior == 'server-error':
                return HttpReply(502, b'private upstream body')
            if self.behavior == 'malformed':
                return HttpReply(201, b'not json')
            return HttpReply(201, json.dumps(self.comments[-1]).encode())
        else:
            data = {'number': 7, 'repository_url': f'https://api.github.com/repos/{self.owner}/repo'}
            if self.issue_is_pr:
                data['pull_request'] = {}
        return HttpReply(200, json.dumps(data).encode())

    @staticmethod
    def assert_fixed(url):
        assert url.startswith('https://api.github.com/')

    @property
    def posts(self):
        return sum(method == 'POST' and url.endswith('/comments') for url, method, _ in self.calls)


class Callback:
    mode = 'agent'
    callback_driven = True

    def submit(self, request, **kwargs):
        assert 'issue' not in request
        return {'native': 'synthetic'}


class AppRoutesTests(unittest.TestCase):
    def test_four_synthetic_identity_routes_sign_and_scope_installation_tokens(self):
        for owner in ('QuantStrategyLab', 'Pigbibi'):
            for provider in ('dot', 'grok'):
                with self.subTest(owner=owner, provider=provider):
                    c = config(owner)
                    identity = provider + '-worker'
                    c['providers'] = {provider: {'type': 'dot_events' if provider == 'dot' else 'grok_webhook'}}
                    c['grants'][0].update(identity=identity, provider=provider)
                    route = {'provider': provider, 'app_id': 101 if provider == 'dot' else 102,
                             'installation_id': 201 if provider == 'dot' else 202, 'private_key_ref': 'synthetic:' + provider}
                    c['github_apps'] = {identity: route}
                    http = FakeGitHub(owner, route['app_id'], route['installation_id'])
                    loader = []
                    apps = GitHubApps(c, transport=http, key_loader=lambda ref: loader.append(ref) or KEY)
                    chosen = apps.select(owner + '/repo', identity, provider)
                    token, _ = apps.credentials(owner + '/repo', identity, provider, chosen)
                    self.assertEqual(token, 'synthetic-installation-token')
                    self.assertEqual(loader, ['synthetic:' + provider])

    def test_static_missing_conflicting_and_cross_owner_fail_before_key_or_network(self):
        for mutate in (lambda c: c.pop('owner'), lambda c: c.update(github_apps={}),
                       lambda c: c['github_apps']['dot-worker'].pop('installation_id'),
                       lambda c: c['github_apps']['dot-worker'].update(app_id=True),
                       lambda c: c['github_apps']['dot-worker'].update(private_key_ref='/unapproved/path'),
                       lambda c: c['github_apps']['dot-worker'].update(provider='other'),
                       lambda c: c['github_apps'].update(unknown=c['github_apps']['dot-worker']),
                       lambda c: c['grants'].append({**c['grants'][0], 'identity': 'unmapped'})):
            c = config()
            mutate(c)
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validate_configuration(c)
        http = FakeGitHub()
        loader = []
        apps = GitHubApps(config(), key_loader=lambda ref: loader.append(ref) or KEY, transport=http)
        for project, worker, provider in [('other/repo', 'dot-worker', 'dot'), ('owner/no', 'dot-worker', 'dot'),
                                           ('owner/repo', 'other', 'dot'), ('owner/repo', 'dot-worker', 'grok')]:
            with self.assertRaises(AppRejected):
                apps.select(project, worker, provider)
        self.assertFalse(http.calls or loader)

    def test_conflicting_installation_and_private_key_are_rejected(self):
        for field in ('app_id', 'installation_id', 'private_key_ref'):
            c = config()
            c['grants'].append({**c['grants'][0], 'identity': 'second'})
            c['github_apps']['second'] = {'provider': 'dot', 'app_id': 102,
                'installation_id': 202, 'private_key_ref': 'synthetic:second'}
            c['github_apps']['second'][field] = c['github_apps']['dot-worker'][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_apps(c)

    def test_approved_loader_is_required_no_file_environment_or_network_fallback(self):
        http = FakeGitHub()
        apps = GitHubApps(config(), transport=http)
        route = apps.select('owner/repo', 'dot-worker', 'dot')
        with self.assertRaises(RuntimeError):
            apps.credentials('owner/repo', 'dot-worker', 'dot', route)
        self.assertFalse(http.calls)

    def test_app_owner_and_token_permission_mismatch_rejected(self):
        for field, value in [('app_owner', 'other'), ('token_permissions', {'issues': 'write', 'contents': 'write'}),
                              ('token_permissions', {'issues': 'read'}), ('expiry', '2000-01-01T00:00:00Z')]:
            http = FakeGitHub()
            setattr(http, field, value)
            apps = GitHubApps(config(), key_loader=lambda ref: KEY, transport=http)
            with self.subTest(field=field), self.assertRaises(AppRejected):
                apps.credentials('owner/repo', 'dot-worker', 'dot', apps.select('owner/repo', 'dot-worker', 'dot'))
            self.assertEqual(http.posts, 0)

    def test_cache_is_worker_bound_and_expires_early(self):
        c = config()
        c['grants'].append({**c['grants'][0], 'identity': 'second'})
        c['github_apps']['second'] = c['github_apps']['dot-worker'].copy()
        http = FakeGitHub()
        apps = GitHubApps(c, key_loader=lambda ref: KEY, transport=http)
        for worker in ('dot-worker', 'dot-worker', 'second'):
            apps.credentials('owner/repo', worker, 'dot', apps.select('owner/repo', worker, 'dot'))
        self.assertEqual(sum(url.endswith('/access_tokens') for url, _, _ in http.calls), 2)
        expired = datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp()
        with patch('ai_service.github_apps.time.time', return_value=expired), patch('ai_service.github_apps.jwt.encode', return_value='synthetic'):
            http.request = lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError('private'))
            with self.assertRaises(RuntimeError) as error:
                apps.credentials('owner/repo', 'dot-worker', 'dot', apps.select('owner/repo', 'dot-worker', 'dot'))
        self.assertNotIn('private', str(error.exception))


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = config()
        self.c['state_path'] = str(Path(self.tmp.name) / 'tasks.sqlite')
        self.http = FakeGitHub()
        self.apps = GitHubApps(self.c, key_loader=lambda ref: KEY, transport=self.http)
        self.policy = {'owner/repo': Policy({'agent': 'dot'}, {'dot': {'model'}})}
        self.runtime = TaskRuntime(Path(self.c['state_path']), {'dot': Callback()}, self.policy)
        self.comments = GitHubComments(self.runtime, self.apps)
        self.request = {'mode': 'agent', 'objective': 'private objective', 'model': 'model',
                        'materials': ['private material'], 'output_schema': {'type': 'object'},
                        'issue': {'repository': 'owner/repo', 'number': 7}}

    def task(self):
        task = self.runtime.submit('owner/repo', 'submission', self.request)
        self.runtime.tick()
        return task['id']

    def claim(self, task_id):
        return self.runtime.claim('owner/repo', task_id, 'dot', 'dot-worker', 'attempt')

    def report(self, task_id, revision=1, output=None):
        return self.runtime.report('owner/repo', task_id, 'dot', 'dot-worker', 'attempt', revision, 'failed', output)

    def test_explicit_binding_is_immutable_and_rejects_cross_repo_null_and_pr(self):
        task_id = self.task()
        for issue in (None, {'repository': 'other/repo', 'number': 7}, {'repository': 'owner/repo', 'number': True},
                      {'repository': 'owner/repo', 'number': 0}, {'repository': 'owner/repo', 'number': 7, 'url': 'https://evil.example'}):
            with self.subTest(issue=issue), self.assertRaises(ValueError):
                self.runtime.submit('owner/repo', 'new', {**self.request, 'issue': issue})
        with self.assertRaises(Conflict):
            self.runtime.submit('owner/repo', 'submission', {**self.request, 'issue': {'repository': 'owner/repo', 'number': 8}})
        self.http.issue_is_pr = True
        self.claim(task_id)
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'rejected')
        self.assertEqual(self.http.posts, 0)

    def test_no_app_or_issue_keeps_existing_behavior(self):
        self.runtime.github_comments = None
        with self.assertRaises(PermissionError):
            self.runtime.submit('owner/repo', 'bind', self.request)
        request = {k: v for k, v in self.request.items() if k != 'issue'}
        task = self.runtime.submit('owner/repo', 'plain', request)
        self.runtime.tick()
        self.claim(task['id'])
        self.report(task['id'])
        self.assertFalse(self.http.calls)

    def test_claim_report_retries_and_same_failure_are_deduplicated_across_restart(self):
        task_id = self.task()
        self.claim(task_id)
        self.claim(task_id)
        for _ in range(3):
            self.report(task_id)
            self.comments.flush()
        restarted = TaskRuntime(Path(self.c['state_path']), {'dot': Callback()}, self.policy)
        comments = GitHubComments(restarted, self.apps)
        restarted.report('owner/repo', task_id, 'dot', 'dot-worker', 'attempt', 1, 'failed')
        comments.flush()
        self.assertEqual(self.http.posts, 2)
        self.assertEqual([r['state'] for r in comments.status(task_id)], ['sent', 'sent'])
        self.assertNotIn('private', json.dumps(self.http.comments))
        self.assertNotIn('synthetic-installation-token', Path(self.c['state_path']).read_bytes().decode(errors='ignore'))
        with self.assertRaises(Conflict):
            self.report(task_id, output={'different': True})

    def test_unknown_lost_reply_is_read_back_with_no_repost(self):
        task_id = self.task()
        self.claim(task_id)
        self.http.behavior = 'lost'
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'unknown')
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'sent')
        self.assertEqual(self.http.posts, 1)

    def test_timeout_never_reposts_even_when_readback_finds_nothing(self):
        self.http.behavior = 'timeout'
        task_id = self.task()
        self.claim(task_id)
        for _ in range(3):
            self.comments.flush()
        self.assertEqual(self.http.posts, 1)
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'unknown')

    def test_server_error_never_reposts(self):
        self.http.behavior = 'server-error'
        task_id = self.task()
        self.claim(task_id)
        for _ in range(3):
            self.comments.flush()
        self.assertEqual(self.http.posts, 1)
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'unknown')

    def test_malformed_acceptance_can_be_read_back(self):
        self.http.behavior = 'malformed'
        task_id = self.task()
        self.claim(task_id)
        self.comments.flush()
        self.comments.flush()
        self.assertEqual(self.http.posts, 1)
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'sent')

    def test_crashed_send_recovers_unknown_and_ignores_spoofed_marker(self):
        task_id = self.task()
        self.claim(task_id)
        with self.runtime.connection() as db:
            row = db.execute('SELECT * FROM github_comments').fetchone()
            db.execute("UPDATE github_comments SET state='sending'")
        spoof = self.http.comment(row['body'])
        spoof['performed_via_github_app']['id'] = 999
        self.http.comments.append(spoof)
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'unknown')
        self.assertEqual(self.http.posts, 0)
        self.http.pages = {1: [spoof] * 100, 2: [self.http.comment(row['body'])]}
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'sent')
        self.assertEqual(self.http.posts, 0)

    def test_rejected_comment_does_not_change_failed_task_or_repeat_failure(self):
        task_id = self.task()
        self.claim(task_id)
        self.report(task_id)
        self.http.behavior = 'reject'
        for _ in range(3):
            self.comments.flush()
            result = self.report(task_id)
            self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.http.posts, 2)
        self.assertEqual([r['state'] for r in self.comments.status(task_id)], ['rejected', 'rejected'])
        self.assertNotIn('private', json.dumps(self.comments.status(task_id)))

    def test_transient_read_preflight_failure_can_recover_before_any_post(self):
        task_id = self.task()
        self.claim(task_id)
        original = self.http.request
        for code in (503, 429, 408):
            def unavailable(url, **kwargs):
                if '/issues/' in url and not url.endswith('/comments') and '/comments?' not in url:
                    return HttpReply(code, b'private transient response')
                return original(url, **kwargs)
            with self.subTest(code=code), patch.object(self.http, 'request', side_effect=unavailable):
                self.comments.flush()
                self.assertEqual(self.comments.status(task_id)[0]['state'], 'pending')
                self.assertEqual(self.http.posts, 0)
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'sent')
        self.assertEqual(self.http.posts, 1)

    def test_preflight_unavailable_retries_without_task_reexecution(self):
        task_id = self.task()
        self.claim(task_id)
        self.apps.key_loader = None
        self.comments.flush()
        self.assertEqual(self.comments.status(task_id)[0]['state'], 'pending')
        self.assertEqual(self.http.posts, 0)
        self.apps.key_loader = lambda ref: KEY
        self.comments.flush()
        self.assertEqual(self.http.posts, 1)

    def test_atomic_enqueue_rollback_and_revision_attempt_provider_boundaries(self):
        task_id = self.task()
        with patch.object(self.comments, 'enqueue', side_effect=RuntimeError('simulated disk failure')):
            with self.assertRaises(RuntimeError):
                self.claim(task_id)
        self.assertIsNone(self.runtime.get('owner/repo', task_id)['attempt'])
        self.claim(task_id)
        with self.assertRaises(Conflict):
            self.runtime.claim('owner/repo', task_id, 'dot', 'dot-worker', 'different')
        with self.assertRaises(PermissionError):
            self.runtime.claim('owner/repo', task_id, 'other', 'dot-worker', 'attempt')
        self.runtime.message('owner/repo', task_id, 'message', {'text': 'private update'})
        with self.assertRaises(Conflict):
            self.report(task_id)
        self.assertEqual(len(self.comments.status(task_id)), 1)
        with patch.object(self.comments, 'enqueue', side_effect=RuntimeError('simulated disk failure')):
            with self.assertRaises(RuntimeError):
                self.report(task_id, revision=2)
        self.assertEqual(self.runtime.get('owner/repo', task_id)['status'], 'running')
        self.report(task_id, revision=2)
        self.comments.flush()
        self.assertEqual(self.http.posts, 2)
        self.assertNotEqual(*[r['delivery_key'] for r in self.comments.status(task_id)])

    def test_concurrent_senders_and_duplicate_claims_create_one_comment(self):
        task_id = self.task()
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.claim(task_id), range(4)))
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda _: self.comments.flush(), range(4)))
        self.assertEqual(self.http.posts, 1)

    def test_mcp_inbound_authority_unchanged_and_comment_status_is_separate(self):
        self.c['grants'][0]['scopes'] = ['tasks:work', 'tasks:read']
        app = build_application(self.c, github_key_loader=lambda ref: KEY, github_transport=self.http)
        app.runtime.providers['dot'] = Callback()
        caller = Principal('caller', 'owner/repo', frozenset({'tasks:submit'}))
        _, task = app.dispatch('POST', '/v1/tasks', {'Idempotency-Key': 'via-api'}, self.request, principal=caller)
        app.runtime.tick()
        worker = Principal('dot-worker', 'owner/repo', frozenset({'tasks:work'}), 'dot')
        args = {'name': 'claim_task', 'arguments': {'task_id': task['id'], 'attempt_id': 'attempt'}}
        with self.assertRaises(PermissionError):
            app.call_tool(caller, args)
        result = app.call_tool(worker, args)['structuredContent']
        self.assertEqual(result['status'], 'running')
        self.assertEqual(result['github_comments'][0]['state'], 'pending')
        self.assertEqual(self.http.posts, 0)
        with self.assertRaises(Exception):
            app.authenticate({'Authorization': 'Bearer synthetic-installation-token'})
        self.assertIsInstance(app, Application)

    def test_check_configuration_never_consumes_app_credentials(self):
        with patch('ai_service.github_apps.PublicHTTPS.request', side_effect=AssertionError('network')):
            validate_configuration(self.c)
        self.assertFalse(self.http.calls)


if __name__ == '__main__':
    unittest.main()
