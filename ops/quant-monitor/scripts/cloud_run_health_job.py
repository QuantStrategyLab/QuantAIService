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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from monitor_cloud_state import CloudState, PROJECTS, StateUnavailable, pack, restore


def paper_watchdog_errors(*, client=None, now=None):
    """Read four exact heartbeat keys; never list or read financial objects."""
    bucket_name = os.environ.get('PAPER_WATCHDOG_BUCKET', '')
    if not bucket_name:
        return []
    now = now or datetime.now(timezone.utc)
    try:
        activation = datetime.fromisoformat(os.environ['PAPER_WATCHDOG_ACTIVATED_AT'])
        if activation.utcoffset() is None:
            raise ValueError('invalid activation')
        if now < activation + timedelta(minutes=45):
            return []
        from google.api_core.exceptions import NotFound
        if client is None:
            from google.cloud import storage
            client = storage.Client()
        bucket = client.bucket(bucket_name)
        for offset in range(4):
            tick = now - timedelta(minutes=15*offset)
            tick = tick.replace(minute=tick.minute//15*15, second=0, microsecond=0)
            key = 'alerts/watch-heartbeat/' + tick.strftime('%Y%m%dT%H%MZ') + '.json'
            blob = bucket.blob(key)
            try:
                blob.reload(timeout=5, retry=None)
                if blob.size is None or blob.size > 4096:
                    raise ValueError('invalid heartbeat')
                row = json.loads(blob.download_as_bytes(if_generation_match=blob.generation, timeout=5, retry=None))
            except NotFound:
                continue
            observed = datetime.fromisoformat(row['at'])
            if observed.utcoffset() is None:
                raise ValueError('invalid heartbeat')
            if now-timedelta(minutes=45) <= observed <= now:
                return []
        return ['paper_watchdog_heartbeat_missing']
    except Exception:
        return ['paper_watchdog_heartbeat_unavailable']


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
                                   'body_marker': {'cf_1020_marker', 'plain_error_1010', 'plain_error_1020',
                                                   'token_invalid', 'token_unconfigured',
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
                              findings_dispatch=record_findings, diagnosis_enabled=False,
                              operational_errors=paper_watchdog_errors() if effects_enabled else ())
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


FAILURE_CODES = frozenset({
    'monitor_notification_binding_missing', 'monitor_inputs_stale_or_invalid', 'monitor_inputs_scope_invalid',
    'monitor_input_reader_unavailable',
    'state_read_failed', 'claim_outcome_unknown', 'checkpoint_outcome_unknown', 'release_outcome_unknown',
    'state_write_too_large', 'workspace_invalid', 'workspace_link_not_portable', 'workspace_manifest_invalid',
    'workspace_member_not_allowed', 'workspace_too_large',
})


def safe_failure(exc):
    if isinstance(exc, StateUnavailable):
        code = str(exc)
        return {'failure_type': 'state_unavailable', 'failure_code': code if code in FAILURE_CODES else 'unknown'}
    if isinstance(exc, TimeoutError):
        kind = 'timeout'
    elif isinstance(exc, OSError):
        kind = 'os_error'
    elif isinstance(exc, ValueError):
        kind = 'value_error'
    else:
        kind = 'other'
    return {'failure_type': kind, 'failure_code': 'unknown'}


def refresh_cycle_inputs(input_store, root, *, mode):
    from monitor_inputs import apply_inputs, mint_reader_token, run_adapter
    if mode == 'run' and os.environ.get('MONITOR_INPUT_REFRESH_IN_CYCLE') == 'true':
        raw_config = os.environ.pop('MONITOR_INPUT_READER_CONFIG_JSON', '')
        required = ('CLOUD_RUN_EXECUTION',)
        try:
            config = json.loads(raw_config)
        except (TypeError, ValueError):
            raise StateUnavailable('monitor_input_reader_unavailable') from None
        fields = {'private_key', 'archive_uri', 'stream_id', 'account_scope_sha256', 'app_id', 'installation_id'}
        if (not isinstance(config, dict) or set(config) != fields
                or any(not isinstance(config[k], str) or not config[k] for k in fields)
                or any(not os.environ.get(name) for name in required) or not shutil.which('gh')):
            raise StateUnavailable('monitor_input_reader_unavailable')
        token = mint_reader_token(config['app_id'], config['installation_id'], config['private_key'])
        values = {'GH_TOKEN': token, 'NO_COLOR': '1',
                  'BINANCE_REPORT_ARCHIVE_URI': config['archive_uri'],
                  'BINANCE_REPORT_ARCHIVE_STREAM_ID': config['stream_id'],
                  'BINANCE_REPORT_ARCHIVE_ACCOUNT_SCOPE_SHA256': config['account_scope_sha256']}
        previous = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            summary = run_adapter(input_store, execution_id=os.environ['CLOUD_RUN_EXECUTION'], source_kind='cloud_run')
            if isinstance(summary, dict):
                readiness = summary.get('source_readiness')
                counts = summary.get('source_counts')
                if (readiness in ('ready', 'unavailable', 'unknown') and isinstance(counts, dict)
                        and set(counts) == {'ready', 'error', 'unknown'}
                        and all(type(counts[key]) is int and 0 <= counts[key] <= 100 for key in counts)):
                    try:
                        print(json.dumps({'component': 'monitor_input_refresh', 'stage': 'source_summary',
                            'scope': 'source_reads_only', 'source_readiness': readiness,
                            'source_counts': {key: counts[key] for key in ('ready', 'error', 'unknown')}}), flush=True)
                    except (OSError, ValueError):
                        pass  # Logging must not affect refresh or delivery.
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    raw, _ = input_store.read()
    apply_inputs(root, raw)


def main():
    mode = os.environ.get('MONITOR_MODE', 'preview')
    from google.cloud import storage

    try:
        client = storage.Client()
        uri = os.environ['MONITOR_STATE_GCS_URI']
        store = CloudState(client, uri)
        def refresh(root):
            refresh_cycle_inputs(CloudState(client, os.environ['MONITOR_INPUT_GCS_URI']), root, mode=mode)
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
    except Exception as exc:
        print(json.dumps({'component': 'strategy_health', 'severity': 'ERROR',
                          'status': 'unavailable', 'reason': 'monitor_cycle_failed', 'mode': mode,
                          **safe_failure(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
