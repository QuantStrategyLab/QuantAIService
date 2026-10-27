"""Portable monitor data and generation-guarded storage; no provider execution.

See ../AGENTS.md. Bodies stay in the protected runtime. Never print archives,
object names, ledger contents or underlying storage errors.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

SCHEMA = 'quant.monitor-workspace.v1'
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_EXPANDED = 256 * 1024 * 1024
MAX_FILES = 20000
PROJECTS = frozenset({'CnEquityStrategies', 'HkEquityStrategies', 'UsEquityStrategies', 'CryptoStrategies'})
DATA = frozenset({'health', 'alert-state', 'lifecycle-store', 'lifecycle-artifacts', 'diagnosis-consumer', 'strategy-reviews'})


class StateUnavailable(RuntimeError):
    """An unreadable/uncertain state is never replaced by an empty ledger."""


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    parts = path.parts
    if str(path) != name or path.is_absolute() or '..' in parts or '\\' in name:
        return False
    if name == 'manifest.json':
        return True
    if any(part in {'.git', '.venv', '__pycache__'} or
           re.search(r'(?:credential|secret|token|\.env|private.key)', part, re.I) for part in parts):
        return False
    if path.suffix not in {'.json', '.csv'}:
        return False
    return ((len(parts) >= 4 and parts[:2] == ('monitor', 'data') and parts[2] in DATA)
            or (len(parts) >= 4 and parts[0] == 'projects' and parts[1] in PROJECTS and parts[2] == 'data'))


def restore(raw: bytes, root: Path) -> dict:
    """Validate the entire archive before writing any member; never extract links."""
    if len(raw) > MAX_ARCHIVE or any(root.iterdir()):
        raise StateUnavailable('workspace_invalid')
    members, total, manifest = {}, 0, None
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:gz') as archive:
            for entry in archive:
                if (not entry.isfile() or not allowed(entry.name) or entry.name in members
                        or len(members) >= MAX_FILES or entry.size < 0):
                    raise StateUnavailable('workspace_invalid')
                total += entry.size
                if total > MAX_EXPANDED:
                    raise StateUnavailable('workspace_too_large')
                handle = archive.extractfile(entry)
                if handle is None:
                    raise StateUnavailable('workspace_invalid')
                data = handle.read(entry.size + 1)
                if len(data) != entry.size:
                    raise StateUnavailable('workspace_invalid')
                members[entry.name] = data
        manifest = json.loads(members['manifest.json'])
        if (not isinstance(manifest, dict) or manifest.get('schema_version') != SCHEMA
                or not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('source_revision', '')))
                or not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('qpk_revision', '')))):
            raise StateUnavailable('workspace_manifest_invalid')
    except (KeyError, OSError, ValueError, tarfile.TarError) as exc:
        raise StateUnavailable('workspace_invalid') from exc
    for name, data in members.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(0o600)
    return manifest


def pack(root: Path) -> bytes:
    """Only data goes to storage; immutable code and credentials stay outside."""
    output, total, count = io.BytesIO(), 0, 0
    with tarfile.open(fileobj=output, mode='w:gz') as archive:
        for path in sorted(root.rglob('*')):
            if path.is_symlink():
                raise StateUnavailable('workspace_link_not_portable')
            if not path.is_file():
                continue
            name = path.relative_to(root).as_posix()
            if path.name == 'health_cycle.json.lock':
                # A local advisory lock is not portable state. The cloud claim
                # above the cycle provides cross-instance exclusion.
                continue
            if not allowed(name):
                raise StateUnavailable('workspace_member_not_allowed')
            data = path.read_bytes()
            total += len(data)
            count += 1
            if total > MAX_EXPANDED or count > MAX_FILES:
                raise StateUnavailable('workspace_too_large')
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode = len(data), 0o600
            archive.addfile(entry, io.BytesIO(data))
    raw = output.getvalue()
    if len(raw) > MAX_ARCHIVE:
        raise StateUnavailable('workspace_too_large')
    return raw


class CloudState:
    """One explicit GCS prefix; no listing, deletes, retry or expiring locks."""

    def __init__(self, client, uri: str):
        match = re.fullmatch(r'gs://([a-z0-9][a-z0-9._-]+)/([A-Za-z0-9/_-]+)', uri)
        if not match:
            raise ValueError('monitor_state_uri_invalid')
        self.bucket = client.bucket(match[1])
        self.prefix = match[2].rstrip('/')

    def read(self, name: str):
        blob = self.bucket.blob(self.prefix + '/' + name)
        try:
            blob.reload(timeout=20, retry=None)
            if blob.size is None or blob.size > MAX_ARCHIVE:
                raise StateUnavailable('workspace_too_large')
            generation = int(blob.generation)
            return blob.download_as_bytes(if_generation_match=generation, timeout=60, retry=None), generation
        except StateUnavailable:
            raise
        except Exception as exc:
            raise StateUnavailable('state_read_failed') from exc

    def claim(self, record: dict) -> int | None:
        from google.api_core.exceptions import PreconditionFailed

        blob = self.bucket.blob(self.prefix + '/active.json')
        try:
            blob.upload_from_string(json.dumps(record), content_type='application/json',
                                    if_generation_match=0, timeout=20, retry=None)
            return int(blob.generation)
        except PreconditionFailed:
            return None
        except Exception as exc:
            raise StateUnavailable('claim_outcome_unknown') from exc

    def commit(self, raw: bytes, *, state_generation: int, claim_generation: int):
        try:
            self.bucket.blob(self.prefix + '/current.tar.gz').upload_from_string(
                raw, content_type='application/gzip', if_generation_match=state_generation, timeout=60, retry=None,
            )
            self.bucket.blob(self.prefix + '/active.json').delete(
                if_generation_match=claim_generation, timeout=20, retry=None,
            )
        except Exception as exc:
            # Keep the claim when completion is uncertain. A future invocation
            # cannot replay notification/Issue/AI effects from an old snapshot.
            raise StateUnavailable('commit_outcome_unknown') from exc
