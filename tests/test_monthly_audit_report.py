import contextlib
import io
import json
import unittest
from unittest.mock import Mock, patch

from scripts import monthly_audit_report as monthly


def fake_api(method, endpoint, payload=None, paginate=False):
    if '/actions/workflows/' in endpoint:
        return {'workflow_runs': [{'html_url': 'https://github.com/example/actions/runs/1',
                                  'status': 'completed', 'conclusion': 'success', 'head_sha': 'a' * 40}]}
    if '/comments?' in endpoint:
        return []
    prefix = monthly.TARGETS[0][1] if 'HkEquity' in endpoint else monthly.TARGETS[1][1]
    return [{'title': prefix + ': 2026-11', 'html_url': 'https://github.com/example/issues/1',
             'body': 'warning: missing profile\n```\nnot executable', 'number': 1,
             'state': 'open', 'updated_at': '2026-11-02T00:00:00Z'}]


class MonthlyAuditReportTests(unittest.TestCase):
    def test_month_validation_rejects_invalid_dates_and_api_query_injection(self):
        for month in ('2026-00', '2026-13', '0000-01', '2026-1', '2026-11&branch=other'):
            with self.subTest(month=month), self.assertRaises(ValueError):
                monthly.collect_report(month, Mock())

    def test_successful_runs_never_accept_quality_and_empty_comments_are_gaps(self):
        api = Mock(side_effect=fake_api)
        report = monthly.collect_report('2026-11', api)
        self.assertFalse(report['quality_accepted'])
        self.assertEqual(len(report['evidence_gaps']), 2)
        self.assertIn('SHA256:', report['body'])
        self.assertIn('````text', report['body'])
        self.assertTrue(all(call.args[0] == 'GET' for call in api.call_args_list))
        self.assertTrue(all(call.kwargs.get('paginate') for call in api.call_args_list if '/issues?' in call.args[1]))

    def test_missing_issue_run_and_source_api_failure_are_explicit(self):
        def missing(method, endpoint, **kwargs):
            return {'workflow_runs': []} if '/actions/' in endpoint else []
        report = monthly.collect_report('2026-11', missing)
        self.assertEqual(len(report['evidence_gaps']), 4)
        report = monthly.collect_report('2026-11', Mock(side_effect=RuntimeError('unavailable')))
        self.assertEqual(len(report['evidence_gaps']), 2)
        self.assertIn('GitHub API 读取失败', report['body'])

    def test_closed_issue_is_reused_without_edit_comment_or_reopen(self):
        api = Mock(return_value=[{'title': 'Monthly Audit Review: 2026-11', 'state': 'closed',
                                  'html_url': 'https://github.com/example/issues/2'}])
        result = monthly.publish_report({'title': 'Monthly Audit Review: 2026-11'}, 'QuantStrategyLab/QuantAIService', api)
        self.assertEqual(result['action'], 'reused_without_changes')
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'GET')

    def test_publish_creates_only_central_issue_and_rejects_other_repositories(self):
        api = Mock(side_effect=[[], {'html_url': 'https://github.com/example/issues/3'}])
        report = {'title': 'Monthly Audit Review: 2026-11', 'body': 'quality unverified'}
        result = monthly.publish_report(report, 'QuantStrategyLab/QuantAIService', api)
        self.assertEqual(result['action'], 'created')
        self.assertEqual(api.call_args.args[0], 'POST')
        with self.assertRaises(ValueError):
            monthly.publish_report(report, 'other/repo', api)
        self.assertEqual(api.call_count, 2)

    def test_default_cli_is_dry_run_and_never_calls_publisher(self):
        with patch.object(monthly, 'collect_report', return_value={'body': 'test'}) as collect, \
                patch.object(monthly, 'publish_report') as publish, \
                patch.dict(monthly.os.environ, {'GITHUB_STEP_SUMMARY': ''}), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            monthly.main(['--month', '2026-11'])
        collect.assert_called_once_with('2026-11')
        publish.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['publication']['action'], 'dry_run')

    def test_api_paginates_without_exposing_subprocess_stderr(self):
        with patch.object(monthly.subprocess, 'run', return_value=Mock(returncode=0, stdout='[[1], [2]]')) as run:
            self.assertEqual(monthly.github_api('GET', 'repos/example/issues', paginate=True), [1, 2])
        self.assertIn('--paginate', run.call_args.args[0])
        with patch.object(monthly.subprocess, 'run', return_value=Mock(returncode=1, stderr='secret')):
            with self.assertRaisesRegex(RuntimeError, r'\(exit 1\)') as error:
                monthly.github_api('GET', 'repos/example/issues')
        self.assertNotIn('secret', str(error.exception))


if __name__ == '__main__':
    unittest.main()
