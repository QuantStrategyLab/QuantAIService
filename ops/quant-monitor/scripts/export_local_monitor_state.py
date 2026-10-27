"""Manual self-hosted input adapter. See ../AGENTS.md.

Read only the configured monitor data and strategy data roots. No broker,
notification, model, systemd mutation or credential-file reads. Upload directly
from the approved source host; never route the archive through a developer PC.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from monitor_cloud_state import DATA, MAX_EXPANDED, PROJECTS, SCHEMA, StateUnavailable, allowed, pack


def capture(monitor: Path, projects: dict[str, Path], *, source_revision: str, qpk_revision: str):
    if set(projects) != PROJECTS:
        raise ValueError('monitor_project_bindings_required')
    roots = [(monitor / 'data' / name, 'monitor/data/' + name) for name in sorted(DATA)]
    roots += [(path, 'projects/' + name + '/data') for name, path in sorted(projects.items())]
    # Root bindings are explicit paths, not trust obtained by resolving an
    # arbitrary symlink. Artifact links may point back into monitor data.
    trusted = tuple(path.parent.resolve() / path.name for path in [monitor / 'data', *projects.values()])
    observed, total, files = {}, 0, 0
    with tempfile.TemporaryDirectory(prefix='monitor-export-') as directory:
        destination = Path(directory)

        def walk(source, relative, ancestors):
            nonlocal total, files
            actual = source.resolve(strict=True)
            if not any(actual.is_relative_to(root) for root in trusted):
                raise StateUnavailable('input_link_outside_data')
            if actual in ancestors:
                raise StateUnavailable('input_link_cycle')
            info = actual.stat()
            observed[actual] = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(source.iterdir()):
                    walk(child, relative + '/' + child.name, ancestors | {actual})
                return
            if source.name == 'health_cycle.json.lock':
                return
            if not stat.S_ISREG(info.st_mode) or not allowed(relative):
                raise StateUnavailable('input_member_not_allowed')
            total += info.st_size
            files += 1
            if total > MAX_EXPANDED:
                raise StateUnavailable('input_too_large')
            before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            data = actual.read_bytes()
            after = actual.stat()
            if before != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                raise StateUnavailable('input_changed_during_capture')
            observed[actual] = before
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)

        for path, relative in roots:
            if path.exists():
                walk(path, relative, set())
        manifest = {'schema_version': SCHEMA, 'source_revision': source_revision, 'qpk_revision': qpk_revision,
                    'captured_at': datetime.now(timezone.utc).isoformat(), 'source_kind': 'self_hosted'}
        (destination / 'manifest.json').write_text(json.dumps(manifest))
        for path, before in observed.items():
            info = path.stat()
            if before != (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns):
                raise StateUnavailable('input_changed_during_capture')
        return pack(destination), files


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        # This operator-owned file carries only protected path/resource bindings;
        # ADC consumes credentials separately. Do not place tokens in the config.
        config = json.loads(args.config.read_text())
        raw, count = capture(Path(config['monitor_root']),
                             {key: Path(value) for key, value in config['project_data_roots'].items()},
                             source_revision=config['source_revision'], qpk_revision=config['qpk_revision'])
        from monitor_cloud_state import CloudState

        # Validate the URI without creating a storage client or copying the
        # source host's credentials. Its existing approved gcloud client uploads.
        class BindingOnly:
            def bucket(self, name):
                return name
        CloudState(BindingOnly(), config['state_uri'])
        accounts = json.loads(subprocess.run(['gcloud', 'auth', 'list', '--format=json'],
                                             capture_output=True, check=True, timeout=20).stdout)
        account = next(row['account'] for row in accounts if hashlib.sha256(row['account'].encode()).hexdigest()
                       == config['gcloud_account_sha256'])
        uri = config['state_uri'].rstrip('/') + '/current.tar.gz'
        subprocess.run(['gcloud', 'storage', 'cp', '--account', account, '--if-generation-match=0', '-', uri],
                       input=raw, capture_output=True, timeout=90, check=True)
        metadata = json.loads(subprocess.run(['gcloud', 'storage', 'objects', 'describe', uri, '--account', account,
                                              '--format=json'], capture_output=True, timeout=30, check=True).stdout)
        if int(metadata['size']) != len(raw):
            raise StateUnavailable('upload_size_mismatch')
        print(json.dumps({'status': 'seeded', 'files': count, 'bytes': len(raw),
                          'sha256': hashlib.sha256(raw).hexdigest(), 'generation': int(metadata['generation'])}))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'reason': 'source_capture_or_upload_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
