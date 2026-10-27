"""One bounded cloud monitor cycle. See ../AGENTS.md and ../README.md.

Cloud Run native infrastructure monitoring is retained. This job owns the
provider-independent strategy/data checks, not broker access or fixed egress.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from monitor_cloud_state import CloudState, PROJECTS, pack, restore


def run_once(store, evaluate, *, mode='preview', execution_id='', refresh=None, publish=None):
    if mode not in {'preview', 'run'}:
        raise ValueError('monitor_mode_invalid')
    if mode == 'run' and not execution_id:
        raise ValueError('monitor_execution_identity_required')
    claim_generation = None
    if mode == 'run':
        # Claim before reading state: a delayed second invocation must not run
        # effects against the snapshot read before the previous cycle committed.
        claim_generation = store.claim({'schema_version': 'quant.monitor-claim.v1',
                                        'execution_id': execution_id,
                                        'started_at': datetime.now(timezone.utc).isoformat()})
        if claim_generation is None:
            return {'status': 'blocked', 'reason': 'previous_cycle_active_or_uncertain', 'mode': mode}
    raw, generation = store.read('current.tar.gz')
    with tempfile.TemporaryDirectory(prefix='qsl-health-') as directory:
        root = Path(directory)
        manifest = restore(raw, root)
        # Preview has no claim, refresh, publication, notification, Issue or AI call.
        if mode == 'run' and refresh is not None:
            refresh(root)
        result = evaluate(root, effects_enabled=mode == 'run')
        if mode == 'run':
            if publish is not None:
                publish(root)
            store.commit(pack(root), state_generation=generation, claim_generation=claim_generation)
        return {'status': 'evaluated', 'mode': mode, 'healthy': result['healthy'],
                'snapshot_count': result['snapshot_count'], 'data_status': result['data_status'],
                'source_matches_runtime': manifest['source_revision'] == os.environ.get('MONITOR_SOURCE_REVISION'),
                'qpk_matches_runtime': manifest['qpk_revision'] == os.environ.get('MONITOR_QPK_REVISION')}


def evaluate_workspace(root: Path, *, effects_enabled: bool):
    import health_cycle

    # An archive contains data only. Strategy code is copied from the immutable
    # image, not downloaded from its data archive or pulled from main each run.
    code = Path(os.environ['MONITOR_PROJECT_CODE_ROOT'])
    for project in PROJECTS:
        target = root / 'projects' / project
        shutil.copytree(code / project, target, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns('.git', 'data', '__pycache__'))
    values = {'QUANT_MONITOR_ROOT': str(root / 'monitor'), 'PROJECTS_ROOT': str(root / 'projects'),
              'QUANT_PROJECTS_ROOT': str(root / 'projects'),
              'LIFECYCLE_LOCAL_ROOT': str(root / 'monitor/data/lifecycle-store'),
              'LIFECYCLE_PERFORMANCE_BUCKET': ''}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        # Child libraries may print domain details. Only the safe projection
        # below reaches Cloud Logging; the full cycle remains protected data.
        result = {}
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            health_cycle.main(effects_enabled=effects_enabled, emit_summary=False, on_summary=result.update)
        status = result.get('snapshot_data_status')
        return {'healthy': result.get('ok') is True,
                'snapshot_count': int(result['snapshot_count']),
                'data_status': status if status in {'ready', 'partial', 'unavailable'} else 'unavailable'}
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # Persist only data and state, never image code or runtime credentials.
        for project in PROJECTS:
            target = root / 'projects' / project
            for child in target.iterdir():
                if child.name == 'data':
                    continue
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()


def main():
    mode = os.environ.get('MONITOR_MODE', 'preview')
    # Production adoption is deliberately separate from image preparation.
    if mode != 'preview':
        raise ValueError('monitor_production_adoption_not_configured')
    from google.cloud import storage

    try:
        store = CloudState(storage.Client(), os.environ['MONITOR_STATE_GCS_URI'])
        result = run_once(store, evaluate_workspace, mode=mode)
        print(json.dumps({'component': 'strategy_health', 'severity': 'INFO' if result['healthy'] else 'WARNING', **result}))
        return 0
    except Exception:
        print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR',
                          'status': 'unavailable', 'reason': 'monitor_preview_failed', 'mode': mode}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
