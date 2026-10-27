"""Minimum diagnostic projection and verbatim cross-day checkpoint fixtures."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import monitor_diagnosis_state as api
from monitor_cloud_state import StateUnavailable

NOW = datetime(2026, 10, 27, 7, 0, tzinfo=timezone.utc)
DOMAINS = ['cn_equity', 'hk_equity', 'us_equity', 'crypto']
ERROR = {'domain': 'crypto', 'code': 'monitor_data_unavailable', 'error_type': 'RuntimeError'}


def cycle(**kwargs):
    return {'domains': DOMAINS, 'as_of': NOW.isoformat(), 'data_errors': [ERROR], **kwargs}


class DiagnosisProjectionTests(unittest.TestCase):
    def test_projection_has_only_original_time_domains_and_fixed_error_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'data/health/cycle_20261027T070000Z.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(cycle(private_amount=123456, account='must-not-leak',
                                            data_errors=[{**ERROR, 'private_uri': 'must-not-leak'}])))
            raw = api.observation_from_workspace(root, now=NOW)
        self.assertNotIn(b'123456', raw)
        self.assertNotIn(b'must-not-leak', raw)
        self.assertEqual(api.validate_observation(raw, now=NOW), cycle())

    def test_unavailable_stale_partial_domains_and_bad_error_are_never_healthy(self):
        bad = [
            {'schema_version': api.OBSERVATION_SCHEMA, 'status': 'unavailable', 'cycle': None},
            {'schema_version': api.OBSERVATION_SCHEMA, 'status': 'stale', 'cycle': cycle()},
            {'schema_version': api.OBSERVATION_SCHEMA, 'status': 'available', 'cycle': cycle(domains=['crypto'])},
            {'schema_version': api.OBSERVATION_SCHEMA, 'status': 'available', 'cycle': cycle(data_errors=[{'bad': 'secret'}])},
            {'schema_version': api.OBSERVATION_SCHEMA, 'status': 'available',
             'cycle': cycle(as_of=(NOW - timedelta(hours=3)).isoformat())},
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(StateUnavailable):
                api.validate_observation(json.dumps(value).encode(), now=NOW)

    def test_invalid_latest_cycle_does_not_fall_back_to_an_older_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data/health').mkdir(parents=True)
            (root / 'data/health/cycle_1.json').write_text(json.dumps(cycle(data_errors=[])))
            (root / 'data/health/cycle_2.json').write_text('{}')
            result = json.loads(api.observation_from_workspace(root, now=NOW))
        self.assertEqual((result['status'], result['cycle']), ('unavailable', None))


class DiagnosisCheckpointTests(unittest.TestCase):
    def test_records_are_preserved_byte_for_byte_including_null_and_cross_day_pending(self):
        raw = b'{ "job_id":null,"request_sha256":"synthetic","unknown_field":42 }\n'
        accepted = b'{"job_id":"synthetic-accepted","route":{"model":"original"},"deadline":12,"receipt":null}'
        with tempfile.TemporaryDirectory() as directory:
            source, target = Path(directory) / 'source', Path(directory) / 'restored'
            source.mkdir()
            (source / '2026-10-26.json').write_bytes(raw)
            (source / '2026-10-27.json').write_bytes(accepted)
            (source / 'lock').touch()
            encoded = api.encode_checkpoints(source)
            api.restore_checkpoints(encoded, target)
            self.assertEqual((target / '2026-10-26.json').read_bytes(), raw)
            self.assertEqual((target / '2026-10-27.json').read_bytes(), accepted)
            self.assertFalse((target / 'lock').exists())

    def test_missing_source_is_not_initialized_as_an_empty_day_book(self):
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(StateUnavailable):
            api.encode_checkpoints(Path(directory) / 'absent')

    def test_invalid_member_rejects_before_creating_a_partial_directory(self):
        raw = json.dumps({'schema_version': api.CHECKPOINT_SCHEMA,
                          'files': {'../../outside.json': 'e30='}}).encode()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'target'
            with self.assertRaises(StateUnavailable):
                api.restore_checkpoints(raw, target)
            self.assertFalse(target.exists())

    def test_diagnosis_store_uses_no_full_archive_and_keeps_separate_claim_namespace(self):
        client, bucket, blob = Mock(), Mock(), Mock()
        client.bucket.return_value = bucket
        bucket.blob.return_value = blob
        blob.size, blob.generation = 2, 4
        blob.download_as_bytes.return_value = b'{}'
        store = api.DiagnosisState(client, checkpoint_uri='gs://synthetic/diagnosis-actions/checkpoints.json',
                                   observation_uri='gs://synthetic/core/diagnosis-observation.json')
        store.read()
        bucket.blob.assert_called_once_with('diagnosis-actions/checkpoints.json')
        self.assertNotIn('current.tar.gz', str(client.mock_calls))


if __name__ == '__main__':
    unittest.main()
