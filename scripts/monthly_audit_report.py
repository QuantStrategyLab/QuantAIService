import argparse
import calendar
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess


TARGETS = (
    ('QuantStrategyLab/HkEquitySnapshotPipelines', 'HK Snapshot Monthly Audit', 'monthly_snapshot_audit.yml'),
    ('QuantStrategyLab/UsEquitySnapshotPipelines', 'Monthly Snapshot AI Review', 'monthly_review.yml'),
)


def validate_month(month):
    if not re.fullmatch(r'[0-9]{4}-(0[1-9]|1[0-2])', month) or month.startswith('0000-'):
        raise ValueError('month must be a valid YYYY-MM')
    return month


def github_api(method, endpoint, payload=None, paginate=False):
    command = ['gh', 'api', '--method', method, endpoint]
    if paginate:
        command.extend(['--paginate', '--slurp'])
    if payload is not None:
        command.extend(['--input', '-'])
    result = subprocess.run(command, input=json.dumps(payload) if payload is not None else None,
                            text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError(f'GitHub API failed: {method} {endpoint} (exit {result.returncode})')
    data = json.loads(result.stdout)
    return [item for page in data for item in page] if paginate else data


def evidence_snapshot(text, limit):
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    excerpt = text[:limit]
    fence = '`' * max(3, max((len(match) + 1 for match in re.findall(r'`+', excerpt)), default=3))
    suffix = '\n[Excerpt truncated; SHA256 covers the complete text.]' if len(text) > limit else ''
    return f'SHA256: `{digest}`\n\n{fence}text\n{excerpt}{suffix}\n{fence}'


def collect_report(month, api=github_api):
    validate_month(month)
    observed_at = datetime.now(timezone.utc).isoformat()
    year, month_number = map(int, month.split('-'))
    last_day = calendar.monthrange(year, month_number)[1]
    sections = [
        f'# Monthly Audit Review: {month}',
        f'Observed at: `{observed_at}`. Mode: **report-only / quality unverified**.',
        '本报告只汇总两个源仓库已有的 issue、评论与 run，不调用旧 AI reviewer，不修复、合并、部署或交易。'
        '报告生成成功不等于月审质量通过。缺失项仍由源 issue 跟踪；源正文可能在月内或月后更新，'
        '下面的 SHA256 仅固定本次读取内容，不证明历史时点一致。',
    ]
    gaps = []
    for repository, prefix, workflow in TARGETS:
        sections.append(f'## {repository}')
        sections.append(f'Workflow: https://github.com/{repository}/blob/main/.github/workflows/{workflow}')
        try:
            issues = api('GET', f'repos/{repository}/issues?state=all&labels=monthly-review&per_page=100', paginate=True)
            matches = [issue for issue in issues if 'pull_request' not in issue and issue['title'] == f'{prefix}: {month}']
            runs = api('GET', f'repos/{repository}/actions/workflows/{workflow}/runs?branch=main&per_page=1'
                       f'&created={month}-01..{month}-{last_day:02d}')['workflow_runs']
            if runs:
                run = runs[0]
                sections.append(f'本期最近 run（不证明与 issue 正文一一绑定）: {run["html_url"]}; '
                                f'`{run["status"]}/{run["conclusion"]}`; SHA `{run["head_sha"]}`.')
                if run['conclusion'] != 'success':
                    gaps.append(f'{repository}: 最近 run 未成功')
            else:
                sections.append('**证据不足：没有本期 main 分支 workflow run。**')
                gaps.append(f'{repository}: 缺本期 run')
            if len(matches) != 1:
                sections.append(f'**证据不足：预期一个本期源 issue，实际 {len(matches)} 个。**')
                sections.extend(issue['html_url'] for issue in matches)
                gaps.append(f'{repository}: 本期源 issue 缺失或不唯一')
                continue
            issue = matches[0]
            sections.append(f'Source issue: {issue["html_url"]}; state `{issue["state"]}`; updated `{issue["updated_at"]}`.')
            sections.append('### Source report snapshot\n\n' + evidence_snapshot(issue.get('body') or '', 12000))
            comments = api('GET', f'repos/{repository}/issues/{issue["number"]}/comments?per_page=100', paginate=True)
            if not comments:
                sections.append('**证据不足：源 issue 没有评论，不能据此确认独立审阅结论或修复验收。**')
                gaps.append(f'{repository}: 缺审阅/修复验收评论')
            for comment in comments[-2:]:
                sections.append(f'### Recent source comment: {comment["html_url"]}\n\n'
                                + evidence_snapshot(comment.get('body') or '', 4000))
            sections.append(f'源评论总数：{len(comments)}；最多摘录最近两条。评论存在也不代表质量通过。')
        except RuntimeError as error:
            sections.append(f'**证据不足：源 GitHub API 读取失败。** {error}')
            gaps.append(f'{repository}: GitHub API 读取失败')
    sections.append('## Conclusion\n\n**质量未验收。** 本汇总不自动关闭源 issue，也不把源 workflow success 解释为质量通过。')
    sections.extend(f'- 证据不足：{gap}' for gap in gaps)
    if not gaps:
        sections.append('源 issue 与成功 run 均可读取；内容与修复是否合格仍需审阅。')
    return {'month': month, 'observed_at': observed_at, 'targets': [target[0] for target in TARGETS],
            'title': f'Monthly Audit Review: {month}', 'body': '\n\n'.join(sections), 'evidence_gaps': gaps,
            'quality_accepted': False}


def publish_report(report, repository, api=github_api):
    if repository != 'QuantStrategyLab/QuantAIService':
        raise ValueError('Publishing is restricted to QuantStrategyLab/QuantAIService')
    issues = api('GET', f'repos/{repository}/issues?state=all&per_page=100', paginate=True)
    matches = [issue for issue in issues if 'pull_request' not in issue and issue['title'] == report['title']]
    if matches:
        return {'action': 'reused_without_changes', 'issues': [issue['html_url'] for issue in matches]}
    issue = api('POST', f'repos/{repository}/issues',
                {'title': report['title'], 'body': report['body'], 'labels': ['monthly-review']})
    return {'action': 'created', 'issues': [issue['html_url']]}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Collect source-owned monthly evidence; dry-run by default.')
    parser.add_argument('--month', default='')
    parser.add_argument('--publish', action='store_true')
    arguments = parser.parse_args(argv)
    month = arguments.month or datetime.now(timezone.utc).strftime('%Y-%m')
    report = collect_report(month)
    report['publication'] = publish_report(report, os.environ.get('GITHUB_REPOSITORY', '')) if arguments.publish else {'action': 'dry_run'}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        Path(summary_path).write_text(report['body'] + '\n\nPublication: ' + json.dumps(report['publication']), encoding='utf-8')


if __name__ == '__main__':
    main()
