"""Offline issue projection provenance checks. See ../AGENTS.md."""
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_cloud_state import StateUnavailable
from monitor_findings import dispatch_findings, encode_findings
from quant_platform_kit.strategy_lifecycle.watch.strategy_watch import build_strategy_monitoring_finding


class FindingsTests(unittest.TestCase):
    def test_roundtrip_uses_same_issue_only_finding_without_comments(self):
        finding = build_strategy_monitoring_finding(domain='crypto', profile='synthetic', severity='warning',
                                                     metrics={'overall_score': 55}, signals=[], source='quant-monitor')
        raw = encode_findings([finding])
        with patch('quant_platform_kit.strategy_lifecycle.watch.runner.dispatch_strategy_watch_findings',
                   return_value={'issues': [], 'errors': 0}) as dispatch:
            dispatch_findings(raw)
        restored = dispatch.call_args.args[0][0]
        self.assertEqual(restored.to_dict(), finding.to_dict())
        self.assertEqual(dispatch.call_args.kwargs, {'dry_run': True, 'comment_existing': False})

    def test_unregistered_repository_and_stale_projection_never_dispatch(self):
        finding = build_strategy_monitoring_finding(domain='crypto', profile='synthetic', severity='warning',
                                                     metrics={'overall_score': 55}, signals=[], source='quant-monitor')
        for kind in ('repo', 'stale'):
            value = json.loads(encode_findings([finding]))
            if kind == 'repo':
                value['findings'][0]['snapshot']['repo'] = 'other/unsafe'
            else:
                value['as_of'] = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
            with patch('quant_platform_kit.strategy_lifecycle.watch.runner.dispatch_strategy_watch_findings') as dispatch, \
                    self.assertRaises(StateUnavailable):
                dispatch_findings(json.dumps(value).encode(), dry_run=False)
            dispatch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
