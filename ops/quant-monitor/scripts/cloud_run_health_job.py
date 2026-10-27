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

from monitor_cloud_state import CloudState, PROJECTS, StateUnavailable, pack, restore


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
        def checkpoint():
            nonlocal generation
            generation = store.checkpoint(pack(root, image_code_present=True), state_generation=generation,
                                          claim_generation=claim_generation)
        import health_cycle
        with health_cycle.cloud_alert_checkpoint(checkpoint if mode == 'run' else None):
            result = evaluate(root, effects_enabled=mode == 'run')
        if mode == 'run':
            if publish is not None:
                publish(root, claim_generation=claim_generation)
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
            def record_findings(findings, **_kwargs):
                from monitor_findings import encode_findings
                path = root / 'monitor/data/strategy-reviews/cloud-findings.json'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(encode_findings(findings))
                return {'issues': [], 'errors': 0}
            health_cycle.main(effects_enabled=effects_enabled, emit_summary=False, on_summary=result.update,
                              findings_dispatch=record_findings, diagnosis_enabled=False)
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


def publish_projections(client, uri, root, *, claim_generation):
    from google.api_core.exceptions import NotFound
    from monitor_diagnosis_state import observation_from_workspace

    payloads = {'diagnosis-observation.json': observation_from_workspace(root / 'monitor'),
                'strategy-findings.json': (root / 'monitor/data/strategy-reviews/cloud-findings.json').read_bytes()}
    for name, raw in payloads.items():
        state = CloudState(client, uri, state_name=name)
        blob = state.bucket.blob(state.prefix + '/' + name)
        try:
            blob.reload(timeout=20, retry=None)
            generation = int(blob.generation)
        except NotFound:
            generation = 0
        # The common core claim owns both bounded projections.
        blob.metadata = {'monitor-producer': 'cloud_run'}
        state.bucket.blob(state.prefix + '/active.json').reload(
            if_generation_match=claim_generation, timeout=20, retry=None)
        blob.upload_from_string(raw, content_type='application/json', if_generation_match=generation,
                                timeout=30, retry=None)


def main():
    mode = os.environ.get('MONITOR_MODE', 'preview')
    from google.cloud import storage

    try:
        client = storage.Client()
        uri = os.environ['MONITOR_STATE_GCS_URI']
        store = CloudState(client, uri)
        def refresh(root):
            from monitor_inputs import apply_inputs
            raw, _ = CloudState(client, os.environ['MONITOR_INPUT_GCS_URI']).read()
            apply_inputs(root, raw)
        def evaluate(root, *, effects_enabled):
            if not effects_enabled:
                refresh(root)
            return evaluate_workspace(root, effects_enabled=effects_enabled)
        if mode == 'run' and (not os.environ.get('TELEGRAM_TOKEN') or not os.environ.get('GLOBAL_TELEGRAM_CHAT_ID')):
            raise StateUnavailable('monitor_notification_binding_missing')
        result = run_once(store, evaluate, mode=mode, execution_id=os.environ.get('CLOUD_RUN_EXECUTION', ''),
                          refresh=refresh, publish=lambda root, **kwargs: publish_projections(client, uri, root, **kwargs))
        if result['status'] == 'blocked':
            print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR', **result}))
            return 1
        print(json.dumps({'component': 'strategy_health', 'severity': 'INFO' if result['healthy'] else 'WARNING', **result}))
        return 0
    except Exception:
        print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR',
                          'status': 'unavailable', 'reason': 'monitor_cycle_failed', 'mode': mode}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
