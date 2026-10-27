import base64
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'sync_sentinel_secret', Path(__file__).resolve().parents[1]
    / 'ops/quant-monitor/scripts/sync_sentinel_secret.py')
maintenance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(maintenance)
TOKEN = 'synthetic-only-not-a-credential'
BOT_HASH = hashlib.sha256(b'12345').hexdigest()
TARGETS = ['projects/example-' + suffix + '/secrets/sentinel-token' for suffix in 'abcd']


class Requests:
    def __init__(self, *, bad_permission=None, write_failure=None, wrong_version=False, bot_id=12345):
        self.calls = []
        self.bad_permission = bad_permission
        self.write_failure = write_failure
        self.wrong_version = wrong_version
        self.bot_id = bot_id
        self.writes = 0

    def __call__(self, method, url, body):
        self.calls.append((method, url, body))
        if url.startswith('https://api.telegram.org/'):
            return {'ok': True, 'result': {'id': self.bot_id, 'is_bot': True}}
        if url.endswith(':testIamPermissions'):
            if self.bad_permission is not None and TARGETS[self.bad_permission] in url:
                return {'permissions': []}
            return {'permissions': ['secretmanager.versions.add']}
        self.writes += 1
        if self.write_failure == self.writes:
            raise OSError('sensitive upstream context must never escape')
        if self.wrong_version:
            return {'name': TARGETS[-1] + '/versions/1'}
        return {'name': url.removeprefix('https://secretmanager.googleapis.com/v1/').removesuffix(':addVersion') + '/versions/1'}


def run(requests, *, mode='inspect', targets=None, token=TOKEN, bot_hash=BOT_HASH):
    return maintenance.maintain(mode=mode, token=token, expected_bot_hash=bot_hash,
                                targets=TARGETS if targets is None else targets, request=requests)


class SentinelMaintenanceTests(unittest.TestCase):
    def test_inspect_verifies_all_permissions_without_versions(self):
        requests = Requests()
        result = run(requests)
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['versions_added'], 0)
        self.assertTrue(result['bot_identity_verified'])
        self.assertEqual(requests.writes, 0)
        self.assertEqual(len(requests.calls), 5)

    def test_sync_preflights_every_target_before_first_version(self):
        requests = Requests()
        result = run(requests, mode='sync')
        self.assertEqual(result['versions_added'], 4)
        self.assertTrue(all(url.endswith(':testIamPermissions') for _, url, _ in requests.calls[1:5]))
        self.assertTrue(all(url.endswith(':addVersion') for _, url, _ in requests.calls[5:]))
        for _, _, body in requests.calls[5:]:
            self.assertEqual(base64.b64decode(body['payload']['data']), TOKEN.encode())

    def test_bot_mismatch_prevents_all_cloud_requests(self):
        requests = Requests(bot_id=99999)
        with self.assertRaises(maintenance.Failure) as raised:
            run(requests, mode='sync')
        self.assertEqual(raised.exception.code, 'bot_identity_mismatch')
        self.assertEqual(len(requests.calls), 1)

    def test_invalid_configuration_has_no_network(self):
        for options in [{'token': ''}, {'bot_hash': ''}, {'targets': TARGETS[:3]},
                        {'targets': [*TARGETS[:3], TARGETS[0]]},
                        {'targets': [*TARGETS[:3], 'projects/example-z/secrets/name?leak=true']}]:
            with self.subTest(options=options):
                requests = Requests()
                with self.assertRaises(maintenance.Failure):
                    run(requests, **options)
                self.assertEqual(requests.calls, [])

    def test_permission_failure_on_last_target_prevents_all_writes(self):
        requests = Requests(bad_permission=3)
        with self.assertRaises(maintenance.Failure) as raised:
            run(requests, mode='sync')
        self.assertEqual(raised.exception.code, 'target_permission_missing')
        self.assertEqual(requests.writes, 0)

    def test_unknown_second_write_stops_without_retry_or_later_targets(self):
        requests = Requests(write_failure=2)
        with self.assertRaises(maintenance.Failure) as raised:
            run(requests, mode='sync')
        self.assertEqual(raised.exception.code, 'write_outcome_unknown')
        self.assertEqual(raised.exception.versions_added, 1)
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(requests.writes, 2)
        self.assertNotIn('sensitive', str(raised.exception))

    def test_wrong_version_response_stops_as_unknown(self):
        requests = Requests(wrong_version=True)
        with self.assertRaises(maintenance.Failure) as raised:
            run(requests, mode='sync')
        self.assertTrue(raised.exception.outcome_unknown)
        self.assertEqual(requests.writes, 1)

    def test_malformed_permission_result_does_not_authorize_sync(self):
        requests = Requests()

        def malformed(method, url, body):
            if url.endswith(':testIamPermissions'):
                return {'permissions': 'secretmanager.versions.add'}
            return requests(method, url, body)

        with self.assertRaises(maintenance.Failure) as raised:
            run(malformed, mode='sync')
        self.assertEqual(raised.exception.code, 'permission_check_failed')
        self.assertEqual(requests.writes, 0)

    def test_cli_rejects_missing_token_before_loading_adc(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt = Path(directory) / 'receipt.json'
            env = {'QUANT_SENTINEL_SECRET_TARGETS_JSON': json.dumps(TARGETS),
                   'QUANT_SENTINEL_EXPECTED_BOT_ID_SHA256': BOT_HASH}
            with mock.patch.dict('os.environ', env, clear=True), mock.patch.dict(
                    'sys.modules', {'google.auth': None}), mock.patch(
                    'sys.argv', ['sync', '--receipt', str(receipt)]), mock.patch('sys.stdout', io.StringIO()):
                self.assertEqual(maintenance.main(), 1)
            self.assertEqual(json.loads(receipt.read_text())['reason'], 'invalid_configuration')

    def test_google_authorization_never_goes_to_telegram(self):
        class Credentials:
            def before_request(self, _transport, _method, _url, headers):
                headers['authorization'] = 'synthetic-google-bearer'

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                return io.BytesIO(b'{}')

        opener = Opener()
        request = maintenance.make_request(Credentials(), object(), opener=opener)
        request('POST', 'https://api.telegram.org/bot' + TOKEN + '/getMe', {})
        request('POST', 'https://secretmanager.googleapis.com/v1/' + TARGETS[0] + ':testIamPermissions', {})
        self.assertIsNone(opener.requests[0].get_header('Authorization'))
        self.assertEqual(opener.requests[1].get_header('Authorization'), 'synthetic-google-bearer')

    def test_redirects_are_not_followed(self):
        self.assertIsNone(maintenance.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://elsewhere.invalid'))

    def test_inspect_receipt_does_not_contain_credentials_or_targets(self):
        result = json.dumps(run(Requests()))
        self.assertNotIn(TOKEN, result)
        self.assertNotIn(TARGETS[0], result)
        self.assertNotIn(BOT_HASH, result)


if __name__ == '__main__':
    unittest.main()
