"""Synthetic real QPK return conversion; no provider access or writes."""
import json
import io
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor_inputs


def report(day):
    start = datetime(2026, 10, day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    return {'schema_version': 'runtime_report.v1', 'platform': 'binance',
            'strategy_profile': 'crypto_live_pool_rotation', 'strategy_domain': 'crypto',
            'dry_run': False, 'status': 'ok', 'started_at': end.isoformat(),
            'external_cash_flow_interval': {'account_scope_sha256': 'a' * 64,
                'start_at': start.isoformat(), 'end_at': end.isoformat(),
                'end_equity_usdt': str(123456 + day), 'net_external_cash_flow': '0',
                'currency': 'USDT', 'valuation_basis': 'checkpoint_quantities_sampled_prices'}}


class ArchiveSegmentTest(unittest.TestCase):
    def test_actual_archive_path_uses_qpk_without_writes_or_amounts(self):
        monitor_inputs.archive_return_segment([])  # preload QPK before patching modules
        class Blob:
            def __init__(self, index):
                self.name, self.generation = f'protected/{index}.json', index
            def download_as_bytes(self, **_kwargs):
                return json.dumps(report(self.generation)).encode()
        class Iterator:
            pages = iter([[Blob(i) for i in range(1, 5)]])
            next_page_token = None
        class Client:
            def bucket(self, _name): return self
            def list_blobs(self, *_args, **_kwargs): return Iterator()
            def blob(self, _name, generation): return Blob(generation)
        store = types.ModuleType('quant_platform_kit.strategy_lifecycle.performance_store')
        store.PerformanceStore = lambda **_kwargs: self.fail('write store used')
        found = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {store.__name__: store}):
            count = monitor_inputs.refresh_binance_report_archive(
                Path(directory), client=Client(), uri='gs://synthetic/protected', stream_id='sample',
                account_scope_sha256='a' * 64, write=False,
                inspect=lambda records: found.append(monitor_inputs.archive_return_segment(records)))
            self.assertEqual(list(Path(directory).iterdir()), [])
        self.assertEqual(count, 4)
        result = found[0]
        self.assertEqual(result['archive_projected_records'], 4)
        self.assertEqual((result['normalized_interval_count'], result['segment_return_count']), (4, 3))
        self.assertEqual((result['segment_start_date'], result['segment_end_date']), ('2026-10-02', '2026-10-05'))
        self.assertEqual(result['monitor_window_status'], 'unknown')
        self.assertIsNone(result['qualified_return_count'])
        self.assertIsNone(result['excluded_count'])
        self.assertNotIn('12345', json.dumps(result))
        self.assertNotIn('a' * 64, json.dumps(result))

    def test_no_interval_does_not_invent_window_or_returns(self):
        result = monitor_inputs.archive_return_segment([])
        self.assertEqual(result['segment_status'], 'interval_coverage_unavailable')
        self.assertIsNone(result['qualified_return_count'])
        self.assertIsNone(result['excluded_count'])

    def test_qpk_reports_missing_interval_without_qualifying_window(self):
        from binance_report_input import report_to_live_record
        records = [report_to_live_record(report(day), stream_id='sample',
                   account_scope_sha256='a' * 64, source_sha256='b' * 64) for day in (1, 2, 4, 5)]
        result = monitor_inputs.archive_return_segment(records)
        self.assertEqual(result['segment_status'], 'truncated_segment')
        self.assertEqual(result['reasons'], ['missing_interval'])
        self.assertEqual(result['segment_return_count'], 1)
        self.assertIsNone(result['qualified_return_count'])
        self.assertIsNone(result['excluded_count'])

    def test_failure_is_fixed_output_and_does_not_expose_exception(self):
        with patch.object(monitor_inputs, '_archive_return_segment', side_effect=ValueError('secret-value')):
            result = monitor_inputs.archive_return_segment([{}])
        self.assertEqual(result['segment_status'], 'unavailable')
        self.assertNotIn('secret-value', json.dumps(result))

    def test_main_archive_only_reports_summary_without_input_state(self):
        monitor_inputs.archive_return_segment([])
        google = types.ModuleType('google')
        cloud = types.ModuleType('google.cloud')
        storage = types.ModuleType('google.cloud.storage')
        google.cloud, cloud.storage, storage.Client = cloud, storage, lambda: object()
        def refresh(*_args, **kwargs):
            self.assertFalse(kwargs['write'])
            kwargs['inspect']([])
            return 0
        output = io.StringIO()
        modules = {'google': google, 'google.cloud': cloud, 'google.cloud.storage': storage}
        with (patch.dict(sys.modules, modules),
              patch.dict(os.environ, {'VERIFY_BINANCE_REPORT_ARCHIVE_ONLY': 'true'}, clear=True),
              patch.object(monitor_inputs, 'refresh_binance_report_archive', side_effect=refresh),
              patch.object(monitor_inputs, 'run_adapter', side_effect=AssertionError('input access')),
              patch('sys.stdout', output)):
            self.assertEqual(monitor_inputs.main(), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(result['records'], 0)
        self.assertEqual(result['archive_return_segment']['segment_status'], 'interval_coverage_unavailable')

    def test_write_mode_cannot_use_inspection_callback(self):
        store = types.ModuleType('quant_platform_kit.strategy_lifecycle.performance_store')
        store.PerformanceStore = lambda **_kwargs: self.fail('write store used')
        class Client:
            def bucket(self, _name): return self
            def list_blobs(self, *_args, **_kwargs):
                return types.SimpleNamespace(pages=iter([[]]), next_page_token=None)
        with tempfile.TemporaryDirectory() as directory, patch.dict(sys.modules, {store.__name__: store}):
            with self.assertRaisesRegex(ValueError, 'inspection_requires_read_only'):
                monitor_inputs.refresh_binance_report_archive(
                    Path(directory), client=Client(), uri='gs://synthetic/protected', stream_id='sample',
                    account_scope_sha256='a' * 64, write=True, inspect=lambda _records: None)


if __name__ == '__main__':
    unittest.main()
