"""Offline checks for exact-key watchdog monitoring."""
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cloud_run_health_job as job


class HeartbeatTests(unittest.TestCase):
    now = datetime(2026, 11, 2, 22, 0, tzinfo=timezone.utc)

    def environment(self):
        return patch.dict(os.environ, {'PAPER_WATCHDOG_BUCKET': 'synthetic-bucket',
                                      'PAPER_WATCHDOG_ACTIVATED_AT': (self.now-timedelta(hours=1)).isoformat()})

    def test_disabled_does_not_read(self):
        with patch.dict(os.environ, {'PAPER_WATCHDOG_BUCKET': ''}):
            client = Mock()
            self.assertEqual(job.paper_watchdog_errors(client=client, now=self.now), [])
            client.assert_not_called()

    def test_fresh_reads_only_heartbeat_prefix(self):
        with self.environment():
            client = Mock()
            blob = client.bucket.return_value.blob.return_value
            blob.size, blob.generation = 80, 7
            blob.download_as_bytes.return_value = json.dumps({'at': self.now.isoformat()}).encode()
            self.assertEqual(job.paper_watchdog_errors(client=client, now=self.now), [])
            client.bucket.return_value.blob.assert_called_once_with('alerts/watch-heartbeat/20261102T2200Z.json')
            blob.download_as_bytes.assert_called_once_with(if_generation_match=7, timeout=5, retry=None)

    def test_stale_reads_four_keys_then_alerts(self):
        with self.environment():
            client = Mock()
            blob = client.bucket.return_value.blob.return_value
            blob.size, blob.generation = 80, 7
            blob.download_as_bytes.return_value = json.dumps({'at': (self.now-timedelta(hours=2)).isoformat()}).encode()
            self.assertEqual(job.paper_watchdog_errors(client=client, now=self.now), ['paper_watchdog_heartbeat_missing'])
            self.assertEqual(client.bucket.return_value.blob.call_count, 4)

    def test_permission_failure_is_sanitized(self):
        with self.environment():
            client = Mock()
            client.bucket.side_effect = RuntimeError('private-token-account')
            result = job.paper_watchdog_errors(client=client, now=self.now)
            self.assertEqual(result, ['paper_watchdog_heartbeat_unavailable'])

    def test_grace_period_does_not_read(self):
        with self.environment(), patch.dict(os.environ, {'PAPER_WATCHDOG_ACTIVATED_AT': self.now.isoformat()}):
            client = Mock()
            self.assertEqual(job.paper_watchdog_errors(client=client, now=self.now), [])
            client.bucket.assert_not_called()
