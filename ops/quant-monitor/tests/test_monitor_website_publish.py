"""Synthetic website sync transport checks; no external calls."""
import base64
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_cloud_state import StateUnavailable
from monitor_website_publish import NoRedirect, publish_snapshot, publish_saved_snapshot, read_sync_token

VERSION = 'projects/123456/secrets/synthetic-sync/versions/1'


class WebsitePublishTests(unittest.TestCase):
    def test_adc_reads_only_the_exact_version_without_redirecting(self):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code, response.content = 200, b'synthetic response'
        response.json.return_value = {'name': VERSION, 'payload': {'data': base64.b64encode(b'synthetic').decode()}}
        self.assertEqual(read_sync_token(VERSION, session=session), 'synthetic')
        session.get.assert_called_once_with('https://secretmanager.googleapis.com/v1/' + VERSION + ':access',
                                            timeout=20, allow_redirects=False)

    def test_unbound_secret_or_unconfirmed_access_never_supplies_a_token(self):
        for version in ('', VERSION.replace('/1', '/latest'), 'https://other.invalid', VERSION + '?query=1'):
            with self.subTest(version=version):
                session = MagicMock()
                with self.assertRaises(StateUnavailable):
                    read_sync_token(version, session=session)
                session.get.assert_not_called()
        for status, name, encoded in ((403, VERSION, 'c3ludGhldGlj'),
                                      (200, VERSION + '2', 'c3ludGhldGlj'), (200, VERSION, '%%%')):
            with self.subTest(status=status, name=name, encoded=encoded):
                session = MagicMock()
                response = session.get.return_value.__enter__.return_value
                response.status_code, response.content = status, b'synthetic response'
                response.json.return_value = {'name': name, 'payload': {'data': encoded}}
                with self.assertRaises(StateUnavailable):
                    read_sync_token(VERSION, session=session)

    def test_saved_receipt_is_checkpointed_before_credentials_or_http(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
            snapshot.parent.mkdir(parents=True)
            raw = b'{"schema_version":"strategy_health_dashboard.v1","generated_at":"original"}'
            snapshot.write_bytes(raw)
            receipt = snapshot.parent / 'website-publication.json'
            events = []
            def checkpoint():
                events.append('checkpoint')
                self.assertEqual(json.loads(receipt.read_text())['status'], 'unconfirmed')
            def read(*args, **kwargs):
                self.assertEqual(events, ['checkpoint'])
                events.append('credential')
                return 'synthetic-token'
            def send(*args, **kwargs):
                self.assertEqual(events, ['checkpoint', 'credential'])
                events.append('http')
            with patch('monitor_website_publish.read_sync_token', side_effect=read), \
                    patch('monitor_website_publish.publish_snapshot', side_effect=send):
                self.assertEqual(publish_saved_snapshot(root, checkpoint=checkpoint, url='synthetic',
                                                        secret_version=VERSION), 'accepted')
            record = json.loads(receipt.read_text())
            self.assertEqual(record['snapshot_sha256'], hashlib.sha256(raw).hexdigest())
            self.assertNotIn('synthetic-token', receipt.read_text())
            self.assertEqual(snapshot.read_bytes(), raw)

    def test_uncertain_checkpoint_never_reads_credentials_or_sends(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('monitor_website_publish.read_sync_token') as read, \
                patch('monitor_website_publish.publish_snapshot') as send:
            with self.assertRaises(StateUnavailable):
                publish_saved_snapshot(Path(directory), url='synthetic', secret_version=VERSION,
                                       checkpoint=Mock(side_effect=StateUnavailable('unknown write')))
            read.assert_not_called()
            send.assert_not_called()

    def test_http_failure_remains_unconfirmed_and_is_not_retried(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('monitor_website_publish.read_sync_token', return_value='synthetic-token'), \
                patch('monitor_website_publish.publish_snapshot', side_effect=TimeoutError('private detail')) as send:
            root = Path(directory)
            self.assertEqual(publish_saved_snapshot(root, checkpoint=Mock(), url='synthetic',
                                                    secret_version=VERSION), 'unconfirmed')
            send.assert_called_once()
            receipt = (root / 'monitor/data/health/website-publication.json').read_text()
            self.assertNotIn('private detail', receipt)
            self.assertNotIn('synthetic-token', receipt)
            self.assertIsNone(json.loads(receipt)['snapshot_sha256'])

    def test_fixed_failure_reasons_never_include_private_detail_and_send_at_most_once(self):
        from urllib.error import HTTPError
        url = 'https://synthetic.invalid/api/internal/sync-strategy-health'
        cases = [
            ('http_rejected', HTTPError(url, 403, 'SENTINEL_PRIVATE', {}, None), None),
            ('transport_unknown', TimeoutError('SENTINEL_PRIVATE'), None),
            ('response_invalid', None, b'SENTINEL_PRIVATE'),
            ('ack_mismatch', None, b'{"ok":false,"message":"SENTINEL_PRIVATE"}'),
            ('accepted', None, b'{"ok":true}'),
        ]
        for reason, error, body in cases:
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
                snapshot.parent.mkdir(parents=True)
                snapshot.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
                opener = MagicMock()
                if error:
                    opener.open.side_effect = error
                else:
                    response = opener.open.return_value.__enter__.return_value
                    response.status = 200
                    response.read.return_value = body
                with patch('monitor_website_publish.read_sync_token', return_value='SENTINEL_TOKEN'):
                    status = publish_saved_snapshot(root, checkpoint=Mock(), url=url, secret_version=VERSION, opener=opener)
                receipt = (snapshot.parent / 'website-publication.json').read_text()
                self.assertEqual(json.loads(receipt)['reason'], reason)
                self.assertEqual(json.loads(receipt)['http_status'], {'http_rejected': 403, 'transport_unknown': None}.get(reason, 200))
                self.assertEqual(status, 'accepted' if reason == 'accepted' else 'unconfirmed')
                self.assertNotIn('SENTINEL', receipt)
                opener.open.assert_called_once()

    def test_http_status_is_bounded_and_never_contains_private_details(self):
        from urllib.error import HTTPError
        url = 'https://synthetic.invalid/api/internal/sync-strategy-health'
        cases = [(HTTPError(url, code, 'SENTINEL', {}, None), expected)
                 for code, expected in ((401, 401), (403, 403), (404, 404), (500, 500), (599, 599),
                                        (99, None), (600, None), (True, None))]
        cases.append((TimeoutError('SENTINEL'), None))
        for error, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
                snapshot.parent.mkdir(parents=True)
                snapshot.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
                opener = Mock()
                opener.open.side_effect = error
                with patch('monitor_website_publish.read_sync_token', return_value='SENTINEL_TOKEN'):
                    publish_saved_snapshot(root, checkpoint=Mock(), url=url, secret_version=VERSION, opener=opener)
                raw = (snapshot.parent / 'website-publication.json').read_text()
                self.assertEqual(json.loads(raw)['http_status'], expected)
                self.assertNotIn('SENTINEL', raw)
                opener.open.assert_called_once()

    def test_response_read_failure_preserves_observed_status_without_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
            opener = MagicMock()
            response = opener.open.return_value.__enter__.return_value
            response.status = 200
            response.read.side_effect = TimeoutError('SENTINEL')
            with patch('monitor_website_publish.read_sync_token', return_value='SENTINEL_TOKEN'):
                status = publish_saved_snapshot(root, checkpoint=Mock(),
                                                url='https://synthetic.invalid/api/internal/sync-strategy-health',
                                                secret_version=VERSION, opener=opener)
            raw = (snapshot.parent / 'website-publication.json').read_text()
            self.assertEqual((status, json.loads(raw)['reason'], json.loads(raw)['http_status']),
                             ('unconfirmed', 'transport_unknown', 200))
            self.assertNotIn('SENTINEL', raw)
            opener.open.assert_called_once()

    def test_rejected_response_evidence_is_bounded_and_redacted(self):
        import io
        from urllib.error import HTTPError
        url = 'https://synthetic.invalid/api/internal/sync-strategy-health'
        cases = [
            ('text/html', b'<h1>Error code: 1020</h1>SENTINEL_PRIVATE', 'cf_1020_marker', 'complete'),
            ('application/json', b'{"error":"strategy health sync token is invalid","detail":"SENTINEL_PRIVATE"}', 'token_invalid', 'complete'),
            ('application/json', b'{"error":"SENTINEL_PRIVATE"}', 'other', 'complete'),
            ('text/html', b'SENTINEL_PRIVATE' * 400, 'unknown', 'truncated'),
        ]
        for content_type, body, marker, read_status in cases:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
                snapshot.parent.mkdir(parents=True)
                snapshot.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
                headers = {'Content-Type': content_type, 'cf-mitigated': 'challenge',
                           'cf-ray': 'SENTINEL_RAY', 'Server': 'SENTINEL_SERVER'}
                stream = io.BytesIO(body)
                opener = Mock()
                opener.open.side_effect = HTTPError(url, 403, 'SENTINEL_ERROR', headers, stream)
                with patch('monitor_website_publish.read_sync_token', return_value='SENTINEL_TOKEN'):
                    status = publish_saved_snapshot(root, checkpoint=Mock(), url=url, secret_version=VERSION, opener=opener)
                raw = (snapshot.parent / 'website-publication.json').read_text()
                receipt = json.loads(raw)
                self.assertEqual((status, receipt['http_status']), ('unconfirmed', 403))
                self.assertEqual(receipt['response_evidence'], {'content_type': 'json' if content_type == 'application/json' else 'html',
                    'challenge': 'challenge', 'cf_ray': 'present', 'body_marker': marker, 'body_read': read_status})
                self.assertNotIn('SENTINEL', raw)
                opener.open.assert_called_once()

    def test_unknown_response_or_failed_body_read_preserves_unknown(self):
        from urllib.error import HTTPError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
            snapshot.parent.mkdir(parents=True)
            snapshot.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
            stream = Mock()
            stream.read.side_effect = TimeoutError('SENTINEL_PRIVATE')
            opener = Mock()
            opener.open.side_effect = HTTPError('https://synthetic.invalid', 403, 'secret', {}, stream)
            with patch('monitor_website_publish.read_sync_token', return_value='SENTINEL_TOKEN'):
                publish_saved_snapshot(root, checkpoint=Mock(),
                    url='https://synthetic.invalid/api/internal/sync-strategy-health', secret_version=VERSION, opener=opener)
            raw = (snapshot.parent / 'website-publication.json').read_text()
            self.assertEqual(json.loads(raw)['response_evidence']['body_marker'], 'unknown')
            self.assertEqual(json.loads(raw)['response_evidence']['body_read'], 'unknown')
            self.assertNotIn('SENTINEL', raw)
            opener.open.assert_called_once()

    def test_pre_send_failures_do_not_send(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            opener = Mock()
            with patch('monitor_website_publish.read_sync_token', side_effect=RuntimeError('SENTINEL')):
                status = publish_saved_snapshot(root, checkpoint=Mock(), url='bad', secret_version=VERSION, opener=opener)
            receipt = json.loads((root / 'monitor/data/health/website-publication.json').read_text())
            self.assertEqual((status, receipt['reason'], receipt['http_status']), ('unconfirmed', 'credential_unavailable', None))
            opener.open.assert_not_called()

    def test_original_snapshot_and_dedicated_token_are_sent_once_in_memory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            raw = json.dumps({'schema_version': 'strategy_health_dashboard.v1', 'generated_at': 'original'}).encode()
            path.write_bytes(raw)
            opener = MagicMock()
            response = opener.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"ok":true}'
            publish_snapshot(path, url='https://synthetic.invalid/api/internal/sync-strategy-health',
                             token='synthetic', opener=opener)
            opener.open.assert_called_once()
            request = opener.open.call_args.args[0]
            self.assertEqual(request.data, raw)
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic')
            self.assertIsNone(NoRedirect().redirect_request(None, None, None, None, None, None))

    def test_bad_target_and_unknown_response_cannot_be_claimed_as_published(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'snapshot.json'
            path.write_text('{"schema_version":"strategy_health_dashboard.v1"}')
            opener = Mock()
            with self.assertRaises(StateUnavailable):
                publish_snapshot(path, url='https://synthetic.invalid/other', token='synthetic', opener=opener)
            opener.open.assert_not_called()
            opener.open.side_effect = TimeoutError()
            with self.assertRaisesRegex(StateUnavailable, 'transport_unknown'):
                publish_snapshot(path, url='https://synthetic.invalid/api/internal/sync-strategy-health',
                                 token='synthetic', opener=opener)
            opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
