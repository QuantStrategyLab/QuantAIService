"""Synthetic dashboard contracts, HTTP sampling and access boundaries."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.auth import Principal
from ai_service.dashboard import Dashboard, MAX_SAMPLES
from ai_service.server import validate_configuration, build_application
from quant_ai_service.server import validate_configuration as validate_quant
from test_configuration_check import configuration
import test_task_server


class DashboardTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'quota.json'
        self.now = 1000
        self.dashboard = Dashboard(self.path, clock=lambda: self.now, monotonic=lambda: self.now)
        self.observation = {'remaining': 18.4, 'limit': 60, 'unit': 'usd',
                            'window_end': 2000, 'observed_at': 990}
        self.snapshot = {'version': 1, 'projects': {'owner/repo': {'grok': self.observation},
                                                 'other/repo': {'grok': {**self.observation, 'limit': 999}}}}

    def write(self):
        self.path.write_text(json.dumps(self.snapshot))
        self.path.chmod(0o600)

    def quota(self):
        return self.dashboard.quotas('owner/repo', {'grok'})['providers']['grok']

    def test_exact_nearest_rank_p95_over_raw_samples_and_expiry(self):
        self.assertIsNone(self.dashboard.health('owner/repo')['p95_ms'])
        self.assertEqual(self.dashboard.health('owner/repo')['status'], 'unknown')
        for value in range(1, 101):
            self.dashboard.record('owner/repo', value, 500 if value == 100 else 200)
        self.dashboard.record('other/repo', 100000, 200)
        result = self.dashboard.health('owner/repo')
        self.assertEqual((result['p95_ms'], result['total'], result['errors']), (95, 100, 1))
        self.assertEqual(result['status'], 'degraded')
        self.now += 300
        self.assertIsNone(self.dashboard.health('owner/repo')['p95_ms'])
        self.assertEqual(self.dashboard.health('owner/repo')['total'], 0)

    def test_single_zero_sample_and_bounded_retention(self):
        self.dashboard.record('owner/repo', 0, 200)
        self.assertEqual(self.dashboard.health('owner/repo')['p95_ms'], 0)
        for value in range(MAX_SAMPLES + 1):
            self.dashboard.record('owner/repo', value, 200)
        self.assertEqual(self.dashboard.health('owner/repo')['total'], MAX_SAMPLES)
        self.dashboard.record('owner/repo', float('nan'), 200)
        self.assertEqual(self.dashboard.health('owner/repo')['total'], MAX_SAMPLES)

    def test_real_quota_values_projection_and_no_cross_project_leak(self):
        self.write()
        self.assertEqual(self.quota(), {'status': 'available', **self.observation})
        self.assertEqual(self.dashboard.quotas('owner/repo', {'grok'})['status'], 'available')
        result = self.dashboard.quotas('other/repo', {'grok', 'dot'})
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['providers']['dot']['status'], 'unavailable')
        self.assertEqual(self.dashboard.quotas('owner/repo', set())['providers'], {})

    def test_unknown_limit_or_reset_is_never_inferred(self):
        for key in ('limit', 'window_end', 'remaining', 'observed_at', 'unit'):
            with self.subTest(key=key):
                original = self.snapshot['projects']['owner/repo']['grok']
                self.snapshot['projects']['owner/repo']['grok'] = {k: v for k, v in original.items() if k != key}
                self.write()
                self.assertEqual(self.quota()['status'], 'unavailable')
                self.assertIsNone(self.quota()['limit'])
                self.assertIsNone(self.quota()['window_end'])
                self.snapshot['projects']['owner/repo']['grok'] = original

    def test_invalid_numbers_units_and_expired_observations_fail_closed(self):
        for change in ({'limit': -1}, {'limit': True}, {'remaining': 61}, {'limit': float('inf')},
                       {'remaining': float('nan')}, {'window_end': None}, {'unit': 'invented'},
                       {'observed_at': 1001}, {'window_end': 990}, {'unit': 'percent'},
                       {'observed_at': 700}, {'window_end': 1000}):
            with self.subTest(change=change):
                self.snapshot['projects']['owner/repo']['grok'] = {**self.observation, **change}
                self.write()
                result = self.quota()
                expected = 'stale' if change in ({'observed_at': 700}, {'window_end': 1000}) else 'unavailable'
                self.assertEqual(result['status'], expected)
                self.assertIsNone(result['remaining'])
                self.assertIsNone(result['window_end'])

    def test_absent_unreadable_malformed_or_insecure_snapshot(self):
        self.assertEqual(Dashboard().quotas('owner/repo', {'grok'})['providers']['grok']['status'], 'not_configured')
        self.assertEqual(self.quota()['status'], 'unavailable')
        self.write()
        self.path.chmod(0o644)
        self.assertEqual(self.quota()['status'], 'unavailable')
        self.path.chmod(0o600)
        for content in ('not json', '[]', '{"version": 2}', '{"version": 1, "projects": []}'):
            self.path.write_text(content)
            self.assertEqual(self.quota()['status'], 'unavailable')
        link = self.path.with_name('link')
        link.symlink_to(self.path)
        self.dashboard.quota_path = link
        self.assertEqual(self.quota()['status'], 'unavailable')
        self.dashboard.quota_path = self.path
        with patch('ai_service.dashboard.os.open', side_effect=PermissionError):
            self.assertEqual(self.quota()['status'], 'unavailable')

    def test_startup_validates_path_without_reading_it_or_credentials(self):
        config = configuration(self.path.with_name('state.sqlite'))
        config['owner'] = 'owner'
        config['dashboard_quota_path'] = str(self.path)
        config['grants'] = [{'issuer': 'https://issuer.example', 'subject': 'dashboard',
                             'identity': 'dashboard', 'project': 'owner/project', 'scopes': ['dashboard:read']}]
        validate_quant(config)
        with patch.object(Dashboard, 'quotas', side_effect=AssertionError('snapshot read during startup')):
            validate_configuration(config)
            app = build_application(config)
        self.assertEqual(app.dashboard.quota_path, self.path)
        for path in ('relative.json', '', 123):
            changed = copy.deepcopy(config)
            changed['dashboard_quota_path'] = path
            with self.subTest(path=path), self.assertRaises(ValueError):
                validate_configuration(changed)


class DashboardHTTPTests(unittest.TestCase):
    # Reuse the offline HTTP parser fixture without inheriting its test methods.
    request_digest = test_task_server.TaskServerTests.request_digest
    lookup_http = test_task_server.TaskServerTests.lookup_http

    def setUp(self):
        test_task_server.TaskServerTests.setUp(self)
        self.app.auth.principals = {**self.app.auth.principals,
            'dashboard': Principal('dashboard', 'owner/repo', frozenset({'dashboard:read'})),
            'worker-dashboard': Principal('worker', 'owner/repo', frozenset({'dashboard:read'}), 'grok')}
        self.now = 1000
        self.app.dashboard = Dashboard(clock=lambda: self.now, monotonic=lambda: self.now)

    def test_auth_scope_worker_and_project_boundaries(self):
        for path in ('/v2/ai/health', '/v2/ai/quota'):
            for identity, expected in [('not-admitted', 401), ('caller', 403), ('bot', 403),
                                       ('worker-dashboard', 403), ('dashboard', 200)]:
                with self.subTest(path=path, identity=identity):
                    self.assertEqual(self.lookup_http(identity=identity, target=path)[0], expected)
            self.assertEqual(self.lookup_http(identity='dashboard', target=path+'?project=other/repo')[0], 403)
            self.assertEqual(self.lookup_http(identity='dashboard', target=path+'?project=&project=owner/repo')[0], 400)
            self.assertEqual(self.lookup_http(identity='dashboard', target=path, body=b'{}')[0], 400)
            self.assertEqual(self.lookup_http(identity='dashboard', target=path, body=b'null')[0], 400)
            self.assertEqual(self.app.dispatch('POST', path, {'Authorization': 'dashboard'})[0], 405)
        for path in ('/v1/ai/health', '/v1/ai/quota'):
            self.assertEqual(self.lookup_http(identity='dashboard', target=path)[0], 404)

    def test_http_task_requests_feed_health_without_polling_or_project_leaks(self):
        # The task lookup returns 404, but its processing time is still a sample.
        self.assertEqual(self.lookup_http()[0], 404)
        self.assertEqual(self.lookup_http(identity='other')[0], 404)
        self.assertEqual(self.lookup_http(identity='not-admitted')[0], 401)
        self.lookup_http(target='/v1/tasks/lookup?project=other/repo')
        status, health = self.lookup_http(identity='dashboard', target='/v2/ai/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['total'], 1)
        self.assertIsInstance(health['p95_ms'], float)
        self.assertGreaterEqual(health['p95_ms'], 0)
        self.assertEqual(self.lookup_http(identity='dashboard', target='/v2/ai/health')[1]['total'], 1)
        self.assertEqual(self.app.dashboard.health('other/repo')['total'], 1)
        self.assertEqual(self.lookup_http(identity='dashboard', target='/v2/ai/quota')[1]['providers']['grok']['limit'], None)

    def test_http_returns_explicit_quota_fields_and_aggregate_p95(self):
        snapshot = self.runtime.path.with_name('quota.json')
        snapshot.write_text(json.dumps({'version': 1, 'projects': {'owner/repo': {'grok': {
            'remaining': 18.4, 'limit': 60, 'unit': 'usd', 'observed_at': 990, 'window_end': 2000,
            'private_extra': 'must-not-be-returned'}}}}))
        snapshot.chmod(0o600)
        self.app.dashboard.quota_path = snapshot
        before = self.runtime.path.read_bytes()
        status, result = self.lookup_http(identity='dashboard', target='/v2/ai/quota')
        self.assertEqual(status, 200)
        row = result['providers']['grok']
        self.assertEqual((row['remaining'], row['limit'], row['window_end']), (18.4, 60, 2000))
        self.assertNotIn('private_extra', row)
        with patch.object(self.runtime, 'lookup', side_effect=RuntimeError('synthetic failure')), \
                patch('ai_service.server.time.monotonic', side_effect=[1, 1.095]):
            self.assertEqual(self.lookup_http()[0], 500)
        status, health = self.lookup_http(identity='dashboard', target='/v2/ai/health')
        self.assertEqual(status, 200)
        self.assertAlmostEqual(health['p95_ms'], 95)
        self.assertEqual((health['status'], health['errors'], health['total']), ('degraded', 1, 1))
        self.assertEqual(self.runtime.path.read_bytes(), before)
        self.assertTrue(all(provider.calls == 0 for provider in self.runtime.providers.values()))

    def test_revoked_project_fails_before_observation_read(self):
        del self.runtime.policies['owner/repo']
        with patch.object(self.app.dashboard, 'quotas', side_effect=AssertionError('observation read')):
            self.assertEqual(self.lookup_http(identity='dashboard', target='/v2/ai/quota')[0], 403)


if __name__ == '__main__':
    unittest.main()
