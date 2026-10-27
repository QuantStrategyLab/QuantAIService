"""Bounded issue-only projection adapter. See ../AGENTS.md."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from monitor_cloud_state import StateUnavailable

SCHEMA = 'quant.monitor-strategy-findings.v1'


def encode_findings(findings):
    raw = json.dumps({'schema_version': SCHEMA, 'as_of': datetime.now(timezone.utc).isoformat(),
                      'findings': [finding.to_dict() for finding in findings]}).encode()
    if len(raw) > 2 * 1024 * 1024:
        raise StateUnavailable('monitor_findings_too_large')
    return raw


def dispatch_findings(raw, *, dry_run=True, now=None):
    from quant_platform_kit.strategy_lifecycle.watch.runner import dispatch_strategy_watch_findings
    from quant_platform_kit.strategy_lifecycle.watch.strategy_watch import (
        STRATEGY_WATCH_REGISTRY, StrategyWatchFinding, StrategyWatchSnapshot,
    )

    if len(raw) > 2 * 1024 * 1024:
        raise StateUnavailable('monitor_findings_too_large')
    value = json.loads(raw)
    if set(value) != {'schema_version', 'as_of', 'findings'} or value['schema_version'] != SCHEMA:
        raise StateUnavailable('monitor_findings_invalid')
    at = datetime.fromisoformat(value['as_of'].replace('Z', '+00:00'))
    if at.tzinfo is None or not -timedelta(minutes=5) <= (now or datetime.now(timezone.utc)) - at <= timedelta(hours=2):
        raise StateUnavailable('monitor_findings_stale')
    if not isinstance(value['findings'], list) or len(value['findings']) > 1000:
        raise StateUnavailable('monitor_findings_invalid')
    findings = []
    for record in value['findings']:
        if set(record) != {'schema_version', 'snapshot', 'severity', 'signals', 'finding_type', 'registry_context'}:
            raise StateUnavailable('monitor_finding_invalid')
        snapshot = StrategyWatchSnapshot.from_dict(record['snapshot'])
        # The existing monitoring builder omits snapshot.domain. Its resolved
        # repository is the original dispatch identity; preserve it unchanged.
        if snapshot.repo not in {item.repository for item in STRATEGY_WATCH_REGISTRY}:
            raise StateUnavailable('monitor_finding_repository_invalid')
        findings.append(StrategyWatchFinding(snapshot=snapshot, severity=record['severity'], signals=record['signals'],
                                             finding_type=record['finding_type'], registry_context=record['registry_context']))
    result = dispatch_strategy_watch_findings(findings, dry_run=dry_run, comment_existing=False)
    if result.get('errors'):
        raise StateUnavailable('monitor_issue_record_failed')
    return {'status': 'evaluated', 'finding_count': len(findings), 'dry_run': dry_run}


def main():
    import os
    from google.cloud import storage
    from monitor_cloud_state import CloudState

    try:
        raw, _ = CloudState(storage.Client(), os.environ['MONITOR_CORE_GCS_URI'],
                            state_name='strategy-findings.json').read()
        print(json.dumps(dispatch_findings(raw, dry_run=os.environ.get('ISSUE_DELIVERY_ENABLED') != 'true')))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'reason': 'monitor_issue_adapter_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
