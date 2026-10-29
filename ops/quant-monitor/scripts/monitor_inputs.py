"""GitHub input adapter and portable input boundary. See ../AGENTS.md.

No broker, model or Telegram operation. Bodies remain in the cloud runner.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

from monitor_cloud_state import DATA_PROJECTS, StateUnavailable, pack, restore


_READER_REPOSITORIES = ('CnEquityStrategies', 'HkEquityStrategies', 'UsEquityStrategies',
                        'CryptoStrategies', 'BinancePlatform')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def mint_reader_token(app_id, installation_id, private_key, *, opener=None, now=None):
    """Mint the existing reader App's bounded token without exposing key or response."""
    import jwt

    if not (re.fullmatch(r'[1-9][0-9]*', app_id or '')
            and re.fullmatch(r'[1-9][0-9]*', installation_id or '') and private_key):
        raise StateUnavailable('monitor_input_reader_unavailable')
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    try:
        assertion = jwt.encode({'iat': now - 60, 'exp': now + 540, 'iss': app_id},
                               private_key, algorithm='RS256')
        body = json.dumps({'repositories': list(_READER_REPOSITORIES),
                           'permissions': {'actions': 'read'}}).encode()
        request = Request('https://api.github.com/app/installations/' + installation_id + '/access_tokens',
                          data=body, method='POST', headers={
                              'Authorization': 'Bearer ' + assertion,
                              'Accept': 'application/vnd.github+json',
                              'Content-Type': 'application/json',
                              'X-GitHub-Api-Version': '2022-11-28',
                              'User-Agent': 'QuantStrategyLab-Monitor/1.0',
                          })
        with (opener or build_opener(_NoRedirect())).open(request, timeout=20) as response:
            raw = response.read(65537)
            if response.status != 201 or len(raw) > 65536:
                raise ValueError('invalid response')
            result = json.loads(raw)
        token = result.get('token') if isinstance(result, dict) else None
        permissions = result.get('permissions') if isinstance(result, dict) else None
        repositories = result.get('repositories') if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or '\n' in token or '\r' in token:
            raise ValueError('invalid token')
        if (not isinstance(permissions, dict) or permissions.get('actions') != 'read'
                or not isinstance(repositories, list)
                or len(repositories) != len(_READER_REPOSITORIES)
                or {item.get('full_name') for item in repositories if isinstance(item, dict)}
                != {'QuantStrategyLab/' + name for name in _READER_REPOSITORIES}):
            raise ValueError('invalid token scope')
        return token
    except Exception as exc:
        raise StateUnavailable('monitor_input_reader_unavailable') from exc


def input_member(name):
    parts = Path(name).parts
    return (name == 'manifest.json'
            or len(parts) >= 4 and parts[:3] == ('monitor', 'data', 'lifecycle-artifacts')
            or len(parts) >= 5 and parts[:3] == ('monitor', 'data', 'lifecycle-store')
            and parts[3] in {'backtest', 'live_runs'}
            or len(parts) >= 4 and parts[0] == 'projects' and parts[1] in DATA_PROJECTS and parts[2] == 'data')


def input_archive(root, *, refreshed=False, source_kind='github_actions'):
    """Flatten only explicit data roots and their managed artifact links."""
    from export_local_monitor_state import capture

    manifest = json.loads((root / 'manifest.json').read_text())
    raw, _ = capture(root / 'monitor', {p: root / 'projects' / p / 'data' for p in DATA_PROJECTS},
                     source_revision=manifest['source_revision'], qpk_revision=manifest['qpk_revision'])
    with tempfile.TemporaryDirectory(prefix='monitor-input-filter-') as directory:
        target = Path(directory)
        restore(raw, target)
        for path in sorted(target.rglob('*'), reverse=True):
            if path.is_file() and not input_member(path.relative_to(target).as_posix()):
                path.unlink()
            elif path.is_dir() and not any(path.iterdir()):
                path.rmdir()
        if refreshed:
            if source_kind not in {'github_actions', 'cloud_run'}:
                raise ValueError('monitor_input_source_invalid')
            manifest.update(captured_at=datetime.now(timezone.utc).isoformat(), source_kind=source_kind)
        (target / 'manifest.json').write_text(json.dumps(manifest))
        return pack(target)


def apply_inputs(root, raw, *, now=None):
    now = now or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix='monitor-input-read-') as directory:
        source = Path(directory)
        manifest = restore(raw, source)
        try:
            stamp = datetime.fromisoformat(manifest['captured_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None or not -timedelta(minutes=5) <= now - stamp <= timedelta(hours=2):
                raise ValueError()
        except (ValueError, TypeError, KeyError) as exc:
            raise StateUnavailable('monitor_inputs_stale_or_invalid') from exc
        for path in source.rglob('*'):
            if path.is_file() and not input_member(path.relative_to(source).as_posix()):
                raise StateUnavailable('monitor_inputs_scope_invalid')
        # Replace only input-owned trees; alert, drift, monitor and diagnosis
        # history remain owned by the computing runtime.
        names = ['monitor/data/lifecycle-artifacts', 'monitor/data/lifecycle-store/backtest',
                 'monitor/data/lifecycle-store/live_runs']
        names += ['projects/' + p + '/data' for p in DATA_PROJECTS]
        for name in names:
            target, origin = root / name, source / name
            if target.exists():
                shutil.rmtree(target)
            if origin.exists():
                shutil.copytree(origin, target)
        current = json.loads((root / 'manifest.json').read_text())
        current['input_captured_at'] = manifest['captured_at']
        (root / 'manifest.json').write_text(json.dumps(current))


def refresh_binance_report_archive(root, *, client, uri, stream_id, account_scope_sha256, write=True):
    """Read the bound archive into the claimed temporary local input store."""
    from binance_report_input import prepare_archive_records, valid_stream_id
    from quant_platform_kit.strategy_lifecycle.performance_store import PerformanceStore
    from sync_binance_live_runs import BinanceLiveRunsError, DOMAIN, STRATEGY_PROFILE

    parsed = urlsplit(uri)
    if (parsed.scheme != 'gs' or not parsed.netloc or not parsed.path.strip('/')
            or parsed.query or parsed.fragment or parsed.username or parsed.password
            or not valid_stream_id(stream_id)
            or not re.fullmatch(r'[0-9a-f]{64}', account_scope_sha256)):
        raise BinanceLiveRunsError('archive_binding_invalid')
    prefix = parsed.path.strip('/') + '/'
    bucket = client.bucket(parsed.netloc)
    local_root = root / 'monitor/data/lifecycle-store'
    stream_root = local_root / 'live_runs' / DOMAIN / STRATEGY_PROFILE / 'streams' / stream_id
    existing = []
    if stream_root.exists():
        for path in sorted(stream_root.rglob('*.json')):
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BinanceLiveRunsError('existing_record_read_failed') from exc
            if not isinstance(value, dict):
                raise BinanceLiveRunsError('existing_record_invalid')
            existing.append(value)

    def list_page(token):
        iterator = client.list_blobs(bucket, prefix=prefix, page_token=token, page_size=1000)
        page = next(iterator.pages, None)
        objects = [] if page is None else [
            {'name': blob.name, 'generation': str(blob.generation)} for blob in page
            if blob.name.endswith('.json')
        ]
        return objects, iterator.next_page_token

    def read_object(name, generation):
        return bucket.blob(name, generation=int(generation)).download_as_bytes(
            if_generation_match=int(generation), timeout=60, retry=None,
        )

    additions = prepare_archive_records(list_page, read_object, prefix=prefix, stream_id=stream_id,
                                       account_scope_sha256=account_scope_sha256, existing_records=existing)
    if write:
        store = PerformanceStore(local_root=local_root)
        for record in additions:
            store.save_live_run_record(STRATEGY_PROFILE, DOMAIN, record, stream_id=stream_id)
    return len(additions)


def refresh_inputs(root):
    import sync_binance_live_runs
    import sync_lifecycle_artifacts

    values = {'QUANT_MONITOR_ROOT': str(root / 'monitor'), 'QUANT_PROJECTS_ROOT': str(root / 'projects'),
              'LIFECYCLE_LOCAL_ROOT': str(root / 'monitor/data/lifecycle-store'), 'LIFECYCLE_PERFORMANCE_BUCKET': ''}
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        # Portable archives flatten managed links. Regenerate only these active
        # aliases; immutable version history and live records stay intact.
        for domain, config in sync_lifecycle_artifacts.DOMAIN_CONFIGS.items():
            for path in (root / 'monitor/data/lifecycle-artifacts/current' / domain,
                         root / 'monitor/data/lifecycle-store/backtest' / domain,
                         root / 'projects' / config['snapshot_repository'] / 'data/output'):
                if path.is_symlink():
                    path.unlink()
                elif path.exists():
                    shutil.rmtree(path)
        uri = os.environ.get('BINANCE_REPORT_ARCHIVE_URI', '').strip()
        stream = os.environ.get('BINANCE_REPORT_ARCHIVE_STREAM_ID', '').strip()
        scope = os.environ.get('BINANCE_REPORT_ARCHIVE_ACCOUNT_SCOPE_SHA256', '').strip()
        configured = bool(uri or stream or scope)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sync_lifecycle_artifacts.main([])
            if not configured:
                sync_binance_live_runs.run_from_environment()
        status_path = root / 'monitor/data/lifecycle-artifacts/binance-report-source.json'
        if uri or stream or scope:
            from google.cloud import storage
            try:
                try:
                    client = storage.Client()
                except Exception as exc:
                    raise sync_binance_live_runs.BinanceLiveRunsError('archive_client_unavailable') from exc
                refresh_binance_report_archive(root, client=client, uri=uri,
                                               stream_id=stream, account_scope_sha256=scope)
            except sync_binance_live_runs.BinanceLiveRunsError as exc:
                source_status = {'status': 'error', 'reason': exc.reason}
            else:
                source_status = {'status': 'ready'}
            source_status.update(schema_version='quant_monitor_binance_report_source.v1',
                                 stream_id=stream, as_of=datetime.now(timezone.utc).isoformat())
            sync_binance_live_runs._write_json(status_path, source_status)
        elif status_path.exists():
            status_path.unlink()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_adapter(store, *, verify_only=False, execution_id=None, source_kind='github_actions'):
    if verify_only:
        raw, _ = store.read()
        with tempfile.TemporaryDirectory(prefix='monitor-input-verify-') as directory:
            root = Path(directory)
            restore(raw, root)
            if any(not input_member(p.relative_to(root).as_posix()) for p in root.rglob('*') if p.is_file()):
                raise StateUnavailable('monitor_inputs_scope_invalid')
        return {'status': 'verified', 'writes': 0, 'audit_requests': 0}
    if execution_id is None:
        execution_id = os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']
    if not execution_id or source_kind not in {'github_actions', 'cloud_run'}:
        raise ValueError('monitor_input_execution_identity_invalid')
    claim = store.claim({'schema_version': 'quant.monitor-input-claim.v1',
                         'execution_id': execution_id})
    if claim is None:
        raise StateUnavailable('monitor_inputs_active_or_uncertain')
    try:
        raw, generation = store.read()
        with tempfile.TemporaryDirectory(prefix='monitor-input-refresh-') as directory:
            root = Path(directory)
            restore(raw, root)
            refresh_inputs(root)
            prepared = input_archive(root, refreshed=True, source_kind=source_kind)
    except Exception:
        # Source reads and local preparation have no external write to replay.
        # A known claim can be released so fresh inputs can recover next time.
        store.release(claim_generation=claim)
        raise
    # Once a conditional write is attempted its outcome may be unknown. Keep
    # the claim on failure, as in the computing/diagnosis state adapters.
    store.commit(prepared, state_generation=generation, claim_generation=claim)
    return {'status': 'refreshed', 'audit_requests': 0}


def _archive_error_details(exc):
    cause = exc.__cause__
    if cause is None:
        return {}
    known_types = {'Forbidden', 'Unauthorized', 'NotFound', 'BadRequest', 'TooManyRequests',
                   'ServiceUnavailable', 'GoogleAPICallError', 'RetryError', 'TimeoutError', 'OSError'}
    error_type = type(cause).__name__
    result = {'error_type': error_type if error_type in known_types else 'Other'}
    code = getattr(cause, 'code', None)
    if isinstance(code, int) and not isinstance(code, bool) and 400 <= code <= 599:
        result['http_status'] = int(code)
    return result


def main():
    from google.cloud import storage
    from monitor_cloud_state import CloudState

    if os.environ.get('VERIFY_BINANCE_REPORT_ARCHIVE_ONLY') == 'true':
        from sync_binance_live_runs import BinanceLiveRunsError
        try:
            with tempfile.TemporaryDirectory(prefix='binance-report-verify-') as directory:
                count = refresh_binance_report_archive(
                    Path(directory), client=storage.Client(),
                    uri=os.environ.get('BINANCE_REPORT_ARCHIVE_URI', ''),
                    stream_id=os.environ.get('BINANCE_REPORT_ARCHIVE_STREAM_ID', ''),
                    account_scope_sha256=os.environ.get('BINANCE_REPORT_ARCHIVE_ACCOUNT_SCOPE_SHA256', ''),
                    write=False,
                )
            print(json.dumps({'status': 'verified', 'records': count}))
            return 0
        except BinanceLiveRunsError as exc:
            reason = exc.reason if re.fullmatch(r'[a-z_]{1,64}', exc.reason) else 'archive_verification_failed'
            print(json.dumps({'status': 'unavailable', 'reason': reason, **_archive_error_details(exc)}))
            return 1
        except Exception:
            print(json.dumps({'status': 'unavailable', 'reason': 'archive_verification_failed'}))
            return 1
    try:
        result = run_adapter(CloudState(storage.Client(), os.environ['MONITOR_INPUT_GCS_URI']),
                             verify_only=os.environ.get('VERIFY_STATE_ONLY') == 'true')
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'reason': 'monitor_input_adapter_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
