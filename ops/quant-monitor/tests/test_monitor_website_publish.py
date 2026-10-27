"""Synthetic website sync transport checks; no external calls."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_cloud_state import StateUnavailable
from monitor_website_publish import NoRedirect, publish_snapshot


class WebsitePublishTests(unittest.TestCase):
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
            with self.assertRaisesRegex(StateUnavailable, 'unconfirmed'):
                publish_snapshot(path, url='https://synthetic.invalid/api/internal/sync-strategy-health',
                                 token='synthetic', opener=opener)
            opener.open.assert_called_once()


if __name__ == '__main__':
    unittest.main()
