"""Offline input scope and refresh checks. No real data or external calls."""
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
import sync_lifecycle_artifacts as sync

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
            with patch.object(sync, 'main', side_effect=refresh), \
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


if __name__ == '__main__':
    unittest.main()
