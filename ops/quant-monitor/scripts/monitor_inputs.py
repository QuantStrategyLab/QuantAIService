"""GitHub input adapter and portable input boundary. See ../AGENTS.md.

No broker, model or Telegram operation. Bodies remain in the cloud runner.
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

from monitor_cloud_state import DATA_PROJECTS, StateUnavailable, pack, restore


def input_member(name):
    parts = Path(name).parts
    return (name == 'manifest.json'
            or len(parts) >= 4 and parts[:3] == ('monitor', 'data', 'lifecycle-artifacts')
            or len(parts) >= 5 and parts[:3] == ('monitor', 'data', 'lifecycle-store')
            and parts[3] in {'backtest', 'live_runs'}
            or len(parts) >= 4 and parts[0] == 'projects' and parts[1] in DATA_PROJECTS and parts[2] == 'data')


def input_archive(root, *, refreshed=False):
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
            manifest.update(captured_at=datetime.now(timezone.utc).isoformat(), source_kind='github_actions')
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
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            sync_lifecycle_artifacts.main([])
            sync_binance_live_runs.run_from_environment()
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def run_adapter(store, *, verify_only=False):
    if verify_only:
        raw, _ = store.read()
        with tempfile.TemporaryDirectory(prefix='monitor-input-verify-') as directory:
            root = Path(directory)
            restore(raw, root)
            if any(not input_member(p.relative_to(root).as_posix()) for p in root.rglob('*') if p.is_file()):
                raise StateUnavailable('monitor_inputs_scope_invalid')
        return {'status': 'verified', 'writes': 0, 'audit_requests': 0}
    claim = store.claim({'schema_version': 'quant.monitor-input-claim.v1',
                         'execution_id': os.environ['GITHUB_RUN_ID'] + ':' + os.environ['GITHUB_RUN_ATTEMPT']})
    if claim is None:
        raise StateUnavailable('monitor_inputs_active_or_uncertain')
    raw, generation = store.read()
    with tempfile.TemporaryDirectory(prefix='monitor-input-refresh-') as directory:
        root = Path(directory)
        restore(raw, root)
        refresh_inputs(root)
        store.commit(input_archive(root, refreshed=True), state_generation=generation, claim_generation=claim)
    return {'status': 'refreshed', 'audit_requests': 0}


def main():
    from google.cloud import storage
    from monitor_cloud_state import CloudState

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
