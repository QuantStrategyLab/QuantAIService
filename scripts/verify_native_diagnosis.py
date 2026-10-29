#!/usr/bin/env python3
"""Manual synthetic acceptance for the existing caller; see ../AGENTS.md."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

import diagnose_monitor_oidc as caller
from health_cycle import DOMAINS


def verify(root, *, env, opener=None, now=None):
    if (env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_RUN_ATTEMPT') != '1'
            or env.get('AI_SERVICE_DIAGNOSIS_READY') != 'true'
            or not caller.identity_available(env, include_healthy=True)):
        raise caller.Rejected('invalid_native_verification_identity')
    # A fresh fixture cannot load, erase or reset the production daily ledger.
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    health = root / 'data/health'
    health.mkdir(parents=True)
    now = now or datetime.now(timezone.utc)
    (health / 'cycle_synthetic.json').write_text(json.dumps({
        'domains': list(DOMAINS), 'as_of': now.isoformat(), 'data_errors': []}))
    original_prompt = caller._operational_diagnosis_prompt

    def synthetic_prompt(errors, **kwargs):
        return ('SYNTHETIC CONNECTION TEST ONLY. This classification is a test fixture, '
                'not a production observation. Do not investigate accounts or perform actions.\n'
                + original_prompt(errors, **kwargs))

    # Only the input is synthetic; OIDC, submission, polling and result binding
    # use the unmodified production caller. No cloud-state client is constructed.
    with patch.object(caller, '_operational_diagnosis_prompt', synthetic_prompt):
        result = caller.diagnose(root, root / 'state', include_healthy=True,
                                 env=env, opener=opener, now=now)
    if result.get('status') != 'succeeded' or result.get('protocol') != 'native_tasks_v1':
        raise caller.Rejected('native_verification_incomplete')
    return {**result, 'synthetic_only': True, 'business_access': False,
            'production_ledger_access': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = verify(args.workspace, env=os.environ)
        code = 0
    except Exception as exc:
        result = {'status': 'failed', 'reason': ('http_status_' + str(exc.code)) if isinstance(exc, HTTPError) else type(exc).__name__,
                  'synthetic_only': True, 'business_access': False,
                  'production_ledger_access': False}
        code = 3
    args.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    caller.save(args.workspace / 'receipt.json', result)
    print(json.dumps(result, sort_keys=True))
    return code


if __name__ == '__main__':
    raise SystemExit(main())
