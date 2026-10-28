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


def run_once(store, evaluate, *, mode='preview', execution_id='', refresh=None, publish=None,
             website_publish=None):
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
    effects_started = False
    try:
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
            effects_started = mode == 'run'
            with health_cycle.cloud_alert_checkpoint(checkpoint if mode == 'run' else None):
                result = evaluate(root, effects_enabled=mode == 'run')
            website_status = 'not_requested'
            if mode == 'run':
                if publish is not None or website_publish is not None:
                    # Derived publication cannot precede durable computation and
                    # notification state. Retain ownership through publication.
                    checkpoint()
                if publish is not None:
                    publish(root, claim_generation=claim_generation)
                if website_publish is not None:
                    website_status = website_publish(root, checkpoint=checkpoint)
                store.commit(pack(root), state_generation=generation, claim_generation=claim_generation)
            website_reason = 'not_requested'
            website_http_status = None
            website_evidence = {}
            if mode == 'run' and website_publish is not None:
                try:
                    receipt = json.loads((root / 'monitor/data/health/website-publication.json').read_text())
                    evidence = receipt.get('response_evidence')
                    if isinstance(evidence, dict):
                        allowed = {'content_type': {'json', 'html', 'text', 'other', 'unknown'},
                                   'challenge': {'challenge', 'absent_or_other', 'unknown'},
                                   'cf_ray': {'present', 'absent', 'unknown'},
                                   'body_read': {'complete', 'truncated', 'unknown'},
                                   'body_marker': {'cf_1020_marker', 'token_invalid', 'token_unconfigured',
                                                   'kv_unconfigured', 'other', 'unknown'}}
                        website_evidence = {k: v for k, v in evidence.items()
                                            if k in allowed and isinstance(v, str) and v in allowed[k]}
                    reason = receipt.get('reason')
                    status = receipt.get('http_status')
                    if type(status) is int and 100 <= status <= 599:
                        website_http_status = status
                    if isinstance(reason, str) and reason in {'accepted', 'credential_unavailable', 'binding_invalid', 'snapshot_invalid',
                                  'http_rejected', 'transport_unknown', 'response_invalid', 'ack_mismatch'}:
                        website_reason = reason
                    else:
                        website_reason = 'unknown'
                except (OSError, ValueError, AttributeError):
                    website_reason = 'unknown'
            return {'status': 'evaluated', 'mode': mode, 'healthy': result['healthy'],
                    'snapshot_count': result['snapshot_count'], 'data_status': result['data_status'],
                    'website_publication': website_status, 'website_publication_reason': website_reason,
                    'website_publication_http_status': website_http_status,
                    'website_publication_response_evidence': website_evidence,
                    'source_matches_runtime': manifest['source_revision'] == os.environ.get('MONITOR_SOURCE_REVISION'),
                    'qpk_matches_runtime': manifest['qpk_revision'] == os.environ.get('MONITOR_QPK_REVISION')}
    except Exception:
        # Input/read failures before evaluation have no external effect to
        # replay. Release the exact claim so the next fresh input can recover.
        if claim_generation is not None and not effects_started:
            store.release(claim_generation=claim_generation)
        raise


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
        if mode == 'run' and any(not os.environ.get(key) for key in (
                'TELEGRAM_TOKEN', 'GLOBAL_TELEGRAM_CHAT_ID')):
            raise StateUnavailable('monitor_notification_binding_missing')
        from monitor_website_publish import publish_saved_snapshot

        def publish_website(root, *, checkpoint):
            return publish_saved_snapshot(root, checkpoint=checkpoint,
                                          url=os.environ.get('STRATEGY_HEALTH_SYNC_URL', ''),
                                          secret_version=os.environ.get('STRATEGY_HEALTH_SYNC_SECRET_VERSION', ''))

        result = run_once(store, evaluate, mode=mode, execution_id=os.environ.get('CLOUD_RUN_EXECUTION', ''),
                          refresh=refresh, publish=lambda root, **kwargs: publish_projections(client, uri, root, **kwargs),
                          website_publish=publish_website)
        if result['status'] == 'blocked':
            print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR', **result}))
            return 1
        healthy = result['healthy'] and result['website_publication'] != 'unconfirmed'
        print(json.dumps({'component': 'strategy_health', 'severity': 'INFO' if healthy else 'WARNING', **result}))
        return 0
    except Exception:
        print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR',
                          'status': 'unavailable', 'reason': 'monitor_cycle_failed', 'mode': mode}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
