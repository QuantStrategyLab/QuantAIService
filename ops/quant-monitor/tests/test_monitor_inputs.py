"""Offline input scope and refresh checks. No real data or external calls."""
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor_cloud_state as state
import monitor_inputs as inputs

NOW = datetime(2026, 10, 27, tzinfo=timezone.utc)


def fixture(root):
    manifest = {'schema_version': state.SCHEMA, 'source_revision': 'a' * 40,
                'qpk_revision': 'b' * 40, 'captured_at': NOW.isoformat()}
    (root / 'manifest.json').write_text(json.dumps(manifest))
    for name, body in {
        'monitor/data/alert-state/health_cycle.json': {'original': 'unknown'},
        'monitor/data/lifecycle-store/live_runs/crypto/record.json': {'original': 'live'},
        'monitor/data/lifecycle-store/monitor/crypto/record.json': {'original': 'monitor'},
        'monitor/data/lifecycle-artifacts/status.json': {'original': 'source-status'},
        'projects/CryptoLivePoolPipelines/data/output/matrix.csv': 'a,b\n1,2\n',
    }.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body if isinstance(body, str) else json.dumps(body))


class InputBoundaryTests(unittest.TestCase):
    def test_cloud_stage_log_contains_only_fixed_stage_and_duration(self):
        output = io.StringIO()
        with patch.dict(os.environ, {'MONITOR_INPUT_REFRESH_IN_CYCLE': 'true'}), \
             patch('sys.stdout', output):
            inputs._stage('archive_reads', 'finished', 1.23456)
        self.assertEqual(json.loads(output.getvalue()), {'component': 'monitor_input_refresh',
                         'stage': 'archive_reads', 'event': 'finished', 'elapsed_seconds': 1.235})

    def test_roundtrip_preserves_live_and_snapshot_data_but_not_core_ledger(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root, target = Path(directory), Path(other)
            fixture(root)
            raw = inputs.input_archive(root)
            state.restore(raw, target)
            self.assertFalse((target / 'monitor/data/alert-state').exists())
            self.assertFalse((target / 'monitor/data/lifecycle-store/monitor').exists())
            for path in root.rglob('*'):
                if path.is_file() and inputs.input_member(path.relative_to(root).as_posix()):
                    self.assertEqual(path.read_bytes(), (target / path.relative_to(root)).read_bytes())

    def test_refresh_cannot_overwrite_alert_or_computed_monitor_history(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
            root, source = Path(directory), Path(other)
            fixture(root)
            fixture(source)
            ledger = root / 'monitor/data/alert-state/health_cycle.json'
            original = ledger.read_bytes()
            inputs.apply_inputs(root, inputs.input_archive(source), now=NOW)
            self.assertEqual(ledger.read_bytes(), original)
            self.assertTrue((root / 'monitor/data/lifecycle-store/monitor/crypto/record.json').exists())

    def test_stale_or_overscoped_inputs_reject_before_mutation(self):
        for bad in ('stale', 'ledger'):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as other:
                root, source = Path(directory), Path(other)
                fixture(root)
                fixture(source)
                raw = inputs.input_archive(source) if bad == 'stale' else state.pack(source)
                before = (root / 'monitor/data/alert-state/health_cycle.json').read_bytes()
                with self.assertRaises(state.StateUnavailable):
                    inputs.apply_inputs(root, raw, now=NOW + timedelta(hours=3) if bad == 'stale' else NOW)
                self.assertEqual((root / 'monitor/data/alert-state/health_cycle.json').read_bytes(), before)

    def test_refresh_regenerates_flattened_aliases_and_retains_versions_and_live(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            versions = root / 'monitor/data/lifecycle-artifacts/versions/crypto/1/history.json'
            versions.parent.mkdir(parents=True)
            versions.write_text('{"history":true}')
            current = root / 'monitor/data/lifecycle-artifacts/current/crypto'
            current.mkdir(parents=True)
            (current / 'old.json').write_text('{}')
            def refresh(_):
                self.assertFalse(current.exists())
                self.assertFalse((root / 'projects/CryptoLivePoolPipelines/data/output').exists())
                self.assertTrue(versions.exists())
                self.assertTrue((root / 'monitor/data/lifecycle-store/live_runs/crypto/record.json').exists())
            with patch('sync_lifecycle_artifacts.main', side_effect=refresh), \
                    patch('sync_binance_live_runs.run_from_environment', return_value={'status': 'disabled'}):
                inputs.refresh_inputs(root)

    def test_adapter_uncertain_commit_is_not_released_or_replayed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture(root)
            store = Mock()
            store.claim.return_value = 7
            store.read.return_value = (inputs.input_archive(root), 3)
            store.commit.side_effect = state.StateUnavailable('write_unknown')
            with patch.dict(os.environ, {'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1'}), \
                    patch.object(inputs, 'refresh_inputs'), self.assertRaises(state.StateUnavailable):
                inputs.run_adapter(store)
            store.release.assert_not_called()

    def test_read_failure_releases_known_claim_without_writing_empty_inputs(self):
        store = Mock()
        store.claim.return_value = 7
        store.read.side_effect = state.StateUnavailable('read_failed')
        with patch.dict(os.environ, {'GITHUB_RUN_ID': '1', 'GITHUB_RUN_ATTEMPT': '1'}), \
                self.assertRaises(state.StateUnavailable):
            inputs.run_adapter(store)
        store.release.assert_called_once_with(claim_generation=7)
        store.commit.assert_not_called()

    def test_cloud_run_uses_real_execution_and_labels_refreshed_input(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as restored:
            root = Path(directory)
            fixture(root)
            store = Mock()
            store.claim.return_value = 7
            store.read.return_value = (inputs.input_archive(root), 3)
            with patch.object(inputs, 'refresh_inputs'):
                result = inputs.run_adapter(store, execution_id='synthetic-execution', source_kind='cloud_run')
            self.assertEqual(result['status'], 'refreshed')
            self.assertEqual(store.claim.call_args.args[0]['execution_id'], 'synthetic-execution')
            manifest = state.restore(store.commit.call_args.args[0], Path(restored))
            self.assertEqual(manifest['source_kind'], 'cloud_run')
            self.assertEqual(store.commit.call_args.kwargs, {'state_generation': 3, 'claim_generation': 7})

    def test_cloud_run_rejects_missing_identity_before_claim(self):
        store = Mock()
        with self.assertRaises(ValueError):
            inputs.run_adapter(store, execution_id='', source_kind='cloud_run')
        store.claim.assert_not_called()

    def test_app_token_requests_only_the_existing_reader_scope(self):
        response = Mock()
        response.status = 201
        response.read.return_value = json.dumps({'token': 'synthetic-token', 'permissions': {'actions': 'read'},
                                                  'repositories': [{'full_name': 'QuantStrategyLab/' + name}
                                                                   for name in inputs._READER_REPOSITORIES]}).encode()
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('jwt.encode', return_value='synthetic-jwt') as sign:
            token = inputs.mint_reader_token('12', '34', 'synthetic-key', opener=opener, now=1000)
        request = opener.open.call_args.args[0]
        self.assertEqual(token, 'synthetic-token')
        self.assertEqual(json.loads(request.data), {'repositories': list(inputs._READER_REPOSITORIES),
                                                   'permissions': {'actions': 'read'}})
        self.assertEqual(request.full_url, 'https://api.github.com/app/installations/34/access_tokens')
        self.assertEqual(sign.call_args.kwargs['algorithm'], 'RS256')

    def test_reader_installation_matches_the_original_app_and_organization(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b'{"id":34,"app_id":12,"account":{"login":"QuantStrategyLab"}}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('jwt.encode', return_value='synthetic-jwt'):
            self.assertEqual(inputs.reader_installation_id('12', 'synthetic-key', opener=opener, now=1000), '34')
            with self.assertRaises(state.StateUnavailable):
                inputs.reader_installation_id('13', 'synthetic-key', opener=opener, now=1000)

    def test_app_token_errors_are_fixed_and_do_not_expose_response(self):
        opener = Mock()
        opener.open.side_effect = RuntimeError('synthetic-sensitive-response')
        with patch('jwt.encode', return_value='synthetic-jwt'), self.assertRaises(state.StateUnavailable) as result:
            inputs.mint_reader_token('12', '34', 'synthetic-key', opener=opener, now=1000)
        self.assertEqual(str(result.exception), 'monitor_input_reader_unavailable')

    def test_app_token_without_full_reader_scope_is_rejected(self):
        response = Mock()
        response.status = 201
        response.read.return_value = b'{"token":"synthetic","permissions":{"actions":"read"},"repositories":[]}'
        opener = Mock()
        opener.open.return_value.__enter__ = Mock(return_value=response)
        opener.open.return_value.__exit__ = Mock(return_value=False)
        with patch('jwt.encode', return_value='synthetic-jwt'), self.assertRaises(state.StateUnavailable):
            inputs.mint_reader_token('12', '34', 'synthetic-key', opener=opener, now=1000)


if __name__ == '__main__':
    unittest.main()


def test_archive_reader_paginates_reads_exact_generation_and_is_idempotent(tmp_path):
    from test_binance_report_input import report
    raw = json.dumps(report()).encode()
    blob = Mock(name='blob')
    blob.name = 'reports/one.json'
    blob.generation = 19
    remote = Mock()
    remote.download_as_bytes.return_value = raw
    bucket = Mock()
    bucket.blob.return_value = remote
    page1 = Mock()
    page1.pages = iter([[]])
    page1.next_page_token = 'page2'
    page2 = Mock()
    page2.pages = iter([[blob]])
    page2.next_page_token = None
    client = Mock()
    client.bucket.return_value = bucket
    client.list_blobs.side_effect = [page1, page2]
    args = dict(client=client, uri='gs://safe-bucket/reports', stream_id='synthetic', account_scope_sha256='a' * 64)
    assert inputs.refresh_binance_report_archive(tmp_path, **args) == 1
    remote.download_as_bytes.assert_called_once_with(if_generation_match=19, timeout=60, retry=None)
    bucket.blob.assert_called_once_with(blob.name, generation=19)
    page3 = Mock()
    page3.pages = iter([[blob]])
    page3.next_page_token = None
    client.list_blobs.side_effect = [page3]
    assert inputs.refresh_binance_report_archive(tmp_path, **args) == 0


def test_archive_corrupt_existing_snapshot_never_becomes_empty(tmp_path):
    from sync_binance_live_runs import BinanceLiveRunsError
    path = tmp_path / 'monitor/data/lifecycle-store/live_runs/crypto/crypto_live_pool_rotation/streams/synthetic/bad.json'
    path.parent.mkdir(parents=True)
    path.write_text('{')
    with unittest.TestCase().assertRaisesRegex(BinanceLiveRunsError, 'existing_record_read_failed'):
        inputs.refresh_binance_report_archive(tmp_path, client=Mock(), uri='gs://safe-bucket/reports',
                                             stream_id='synthetic', account_scope_sha256='a' * 64)


def test_archive_failure_is_local_to_crypto_and_other_inputs_continue(tmp_path):
    from google.cloud import storage
    from sync_binance_live_runs import BinanceLiveRunsError
    status = tmp_path / 'monitor/data/lifecycle-artifacts/status.json'
    status.parent.mkdir(parents=True)
    status.write_text(json.dumps({'schema_version': 'quant_monitor_lifecycle_artifact_status.v1',
                                  'domains': {'us_equity': {'status': 'ready'}, 'crypto': {'status': 'ready'}}}))
    env = {'BINANCE_REPORT_ARCHIVE_URI': 'gs://safe-bucket/reports',
           'BINANCE_REPORT_ARCHIVE_STREAM_ID': 'synthetic',
           'BINANCE_REPORT_ARCHIVE_ACCOUNT_SCOPE_SHA256': 'a' * 64}
    with patch.dict(os.environ, env, clear=True), patch('sync_lifecycle_artifacts.main'), \
         patch('sync_binance_live_runs.run_from_environment'), patch.object(storage, 'Client'), \
         patch.object(inputs, 'refresh_binance_report_archive', side_effect=BinanceLiveRunsError('archive_read_failed')):
        inputs.refresh_inputs(tmp_path)
    result = json.loads(status.read_text())
    assert result['domains']['us_equity']['status'] == 'ready'
    assert result['domains']['crypto']['status'] == 'ready'
    source = json.loads((status.parent / 'binance-report-source.json').read_text())
    assert source['status'] == 'error'
    assert source['reason'] == 'archive_read_failed'


def test_archive_verification_mode_does_not_access_input_store_or_write(tmp_path, capsys):
    from google.cloud import storage
    env = {'VERIFY_BINANCE_REPORT_ARCHIVE_ONLY': 'true', 'BINANCE_REPORT_ARCHIVE_URI': 'gs://safe/reports',
           'BINANCE_REPORT_ARCHIVE_STREAM_ID': 'synthetic',
           'BINANCE_REPORT_ARCHIVE_ACCOUNT_SCOPE_SHA256': 'a' * 64}
    with patch.dict(os.environ, env, clear=True), patch.object(storage, 'Client'), \
         patch.object(inputs, 'run_adapter', side_effect=AssertionError('must not access input store')), \
         patch.object(inputs, 'refresh_binance_report_archive', return_value=2) as read:
        assert inputs.main() == 0
    assert read.call_args.kwargs['write'] is False
    assert json.loads(capsys.readouterr().out) == {'status': 'verified', 'records': 2}


def test_stream_key_alias_cannot_overwrite_existing_record(tmp_path):
    from test_binance_report_input import report
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    from sync_binance_live_runs import BinanceLiveRunsError
    from binance_report_input import report_to_live_record

    root = tmp_path / 'monitor/data/lifecycle-store'
    store = PerformanceStore(local_root=root)
    previous = report_to_live_record(report(), stream_id='a-b', account_scope_sha256='a' * 64,
                                     source_sha256='b' * 64)
    previous['lifecycle_stream_id'] = 'a:b'
    store.save_live_run_record('crypto_live_pool_rotation', 'crypto', previous, stream_id='a:b')
    path = next(root.rglob('*.json'))
    before = path.read_bytes()
    with unittest.TestCase().assertRaisesRegex(BinanceLiveRunsError, 'archive_binding_invalid'):
        inputs.refresh_binance_report_archive(tmp_path, client=Mock(), uri='gs://safe/reports',
                                             stream_id='a:b', account_scope_sha256='a' * 64)
    client = Mock()
    page = Mock()
    page.pages = iter([[]])
    page.next_page_token = None
    client.list_blobs.return_value = page
    with unittest.TestCase().assertRaisesRegex(BinanceLiveRunsError, 'existing_record_invalid'):
        inputs.refresh_binance_report_archive(tmp_path, client=client, uri='gs://safe/reports',
                                             stream_id='a-b', account_scope_sha256='a' * 64)
    assert path.read_bytes() == before


def test_archive_readonly_error_exposes_only_safe_cause_metadata(capsys):
    from google.api_core.exceptions import Forbidden
    from google.cloud import storage
    from sync_binance_live_runs import BinanceLiveRunsError

    try:
        try:
            raise Forbidden('private-uri private-principal financial-body')
        except Forbidden as cause:
            raise BinanceLiveRunsError('archive_list_failed') from cause
    except BinanceLiveRunsError as error:
        failure = error
    with patch.dict(os.environ, {'VERIFY_BINANCE_REPORT_ARCHIVE_ONLY': 'true'}, clear=True), \
         patch.object(storage, 'Client'), \
         patch.object(inputs, 'refresh_binance_report_archive', side_effect=failure):
        assert inputs.main() == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {'status': 'unavailable', 'reason': 'archive_list_failed',
                                  'error_type': 'Forbidden', 'http_status': 403}
    assert 'private' not in output


def test_archive_unknown_cause_is_not_exposed():
    from sync_binance_live_runs import BinanceLiveRunsError

    class PrivateSecret(Exception):
        code = '403 private'
    try:
        try:
            raise PrivateSecret('secret')
        except PrivateSecret as cause:
            raise BinanceLiveRunsError('archive_list_failed') from cause
    except BinanceLiveRunsError as error:
        assert inputs._archive_error_details(error) == {'error_type': 'Other'}
