import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import publish_saved_monitor_observation as publisher
from test_monitor_diagnosis_state import cycle

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)


def observation(at):
    return json.dumps({'schema_version': 'quant.monitor-diagnosis-observation.v1',
                       'status': 'available', 'cycle': cycle(as_of=at.isoformat())}).encode()


class ObservationPublisherTests(unittest.TestCase):
    def test_cloud_handoff_and_newer_cycle_cannot_be_overwritten(self):
        current, candidate = observation(NOW), observation(NOW - timedelta(minutes=30))
        self.assertEqual(publisher.plan_publication(candidate, current, 'cloud_run'), 'producer_changed')
        self.assertEqual(publisher.plan_publication(candidate, current, publisher.PRODUCER), 'newer_observation_exists')
        self.assertEqual(publisher.plan_publication(current, current, publisher.PRODUCER), 'unchanged')

    def test_saved_cycle_is_published_without_running_the_monitor_or_changing_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data/health/cycle_20261027T070000Z.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(cycle(private_balance=123456)))
            client = Mock()
            client.read.return_value = (observation(NOW - timedelta(minutes=30)),
                                        {'generation': '7', 'custom_fields': {'monitor-producer': publisher.PRODUCER}})
            client.write.return_value = 8
            result = publisher.publish(root, client, now=NOW)
            self.assertEqual((result['status'], result['observed_at']), ('published', NOW.isoformat()))
            body, expected_generation = client.write.call_args.args
            self.assertNotIn(b'123456', body)
            self.assertEqual(expected_generation, 7)

    def test_wrapper_keeps_original_exit_and_does_not_rerun_failed_health(self):
        wrapper = Path(__file__).resolve().parents[1] / 'scripts/run_health_with_projection.sh'
        for code in (0, 1, 2):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                entry = root / 'health.sh'
                entry.write_text(f'echo health >> "{root / "calls"}"\nexit {code}\n')
                python = root / 'python3'
                python.write_text(f'#!/bin/bash\necho projection >> "{root / "calls"}"\n')
                python.chmod(0o755)
                result = subprocess.run(['bash', str(wrapper), str(entry), str(root / 'config.json')],
                                        env={'PATH': str(root) + ':' + os.environ['PATH']}, capture_output=True)
                self.assertEqual(result.returncode, code)
                calls = (root / 'calls').read_text().splitlines()
                self.assertEqual(calls, ['health', 'projection'] if code in (0, 2) else ['health'])


if __name__ == '__main__':
    unittest.main()
