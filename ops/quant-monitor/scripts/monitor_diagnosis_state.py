"""Minimal observations and independent Actions checkpoints. See ../AGENTS.md.

This interface never reads the lifecycle archive. The caller keeps its original
workflow OIDC for the audit service and uses separately approved GCS identity.
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from monitor_cloud_state import CloudState, StateUnavailable

OBSERVATION_SCHEMA = 'quant.monitor-diagnosis-observation.v1'
CHECKPOINT_SCHEMA = 'quant.monitor-diagnosis-checkpoints.v1'
MAX_BYTES = 2 * 1024 * 1024


def _load(raw):
    try:
        if len(raw) > MAX_BYTES:
            raise ValueError()
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (TypeError, ValueError) as exc:
        raise StateUnavailable('diagnosis_state_invalid') from exc


def _filename(name):
    if not isinstance(name, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}\.json', name):
        raise StateUnavailable('diagnosis_checkpoint_name_invalid')
    date.fromisoformat(name[:-5])


def encode_checkpoints(directory: Path) -> bytes:
    if not directory.is_dir() or directory.is_symlink():
        raise StateUnavailable('diagnosis_checkpoint_source_missing')
    files = {}
    for path in sorted(directory.iterdir()):
        if path.name == 'lock':
            continue
        _filename(path.name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            raise StateUnavailable('diagnosis_checkpoint_invalid')
        raw = path.read_bytes()
        _load(raw)
        files[path.name] = base64.b64encode(raw).decode('ascii')
    raw = json.dumps({'schema_version': CHECKPOINT_SCHEMA, 'files': files}, sort_keys=True).encode()
    if len(raw) > MAX_BYTES:
        raise StateUnavailable('diagnosis_checkpoint_too_large')
    return raw


def restore_checkpoints(raw: bytes, directory: Path):
    value = _load(raw)
    if (set(value) != {'schema_version', 'files'} or value['schema_version'] != CHECKPOINT_SCHEMA
            or not isinstance(value['files'], dict) or len(value['files']) > 10000):
        raise StateUnavailable('diagnosis_checkpoint_invalid')
    if directory.exists() and (directory.is_symlink() or any(directory.iterdir())):
        raise StateUnavailable('diagnosis_checkpoint_destination_not_empty')
    decoded = {}
    try:
        for name, encoded in value['files'].items():
            _filename(name)
            body = base64.b64decode(encoded, validate=True)
            _load(body)
            decoded[name] = body
    except (ValueError, TypeError) as exc:
        raise StateUnavailable('diagnosis_checkpoint_invalid') from exc
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, body in decoded.items():
        path = directory / name
        path.write_bytes(body)
        path.chmod(0o600)


def validate_observation(raw: bytes, *, now: datetime | None = None) -> dict:
    from health_cycle import _read_diagnosis_cycle

    value = _load(raw)
    if set(value) != {'schema_version', 'status', 'cycle'} or value['schema_version'] != OBSERVATION_SCHEMA:
        raise StateUnavailable('diagnosis_observation_invalid')
    if value['status'] not in {'available', 'unavailable', 'stale'}:
        raise StateUnavailable('diagnosis_observation_invalid')
    if value['status'] != 'available':
        raise StateUnavailable('diagnosis_observation_' + value['status'])
    cycle = value['cycle']
    if not isinstance(cycle, dict) or set(cycle) != {'domains', 'as_of', 'data_errors'}:
        raise StateUnavailable('diagnosis_observation_invalid')
    # Reuse the original four-domain, time and fixed-classification checks.
    try:
        with tempfile.TemporaryDirectory(prefix='diagnosis-observation-') as directory:
            path = Path(directory) / 'cycle.json'
            path.write_text(json.dumps(cycle))
            _read_diagnosis_cycle(path, now=now or datetime.now(timezone.utc), max_age=timedelta(hours=2))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise StateUnavailable('diagnosis_observation_invalid_or_stale') from exc
    return cycle


def observation_from_workspace(monitor: Path, *, now: datetime | None = None) -> bytes:
    from health_cycle import _read_diagnosis_cycle

    now = now or datetime.now(timezone.utc)
    try:
        path = sorted((monitor / 'data/health').glob('cycle_*.json'))[-1]
        _, errors = _read_diagnosis_cycle(path, now=now, max_age=timedelta(hours=2))
        original = _load(path.read_bytes())
        cycle = {'domains': original['domains'], 'as_of': original['as_of'], 'data_errors': errors}
        return json.dumps({'schema_version': OBSERVATION_SCHEMA, 'status': 'available', 'cycle': cycle}).encode()
    except (OSError, ValueError, TypeError, IndexError, KeyError, StateUnavailable):
        return json.dumps({'schema_version': OBSERVATION_SCHEMA, 'status': 'unavailable', 'cycle': None}).encode()


class DiagnosisState:
    """Only the observation object and this consumer's independent JSON ledger."""

    def __init__(self, client, *, checkpoint_uri: str, observation_uri: str):
        prefix, name = checkpoint_uri.rsplit('/', 1)
        if name != 'checkpoints.json':
            raise ValueError('diagnosis_checkpoint_uri_invalid')
        self.store = CloudState(client, prefix, state_name=name)
        prefix, name = observation_uri.rsplit('/', 1)
        if name != 'diagnosis-observation.json':
            raise ValueError('diagnosis_observation_uri_invalid')
        self.observations = CloudState(client, prefix, state_name=name)

    def claim(self, execution_id: str):
        if not isinstance(execution_id, str) or not re.fullmatch(r'[A-Za-z0-9_./:-]{1,200}', execution_id):
            raise ValueError('diagnosis_execution_identity_invalid')
        return self.store.claim({'schema_version': 'quant.monitor-diagnosis-claim.v1', 'execution_id': execution_id})

    def read(self):
        return self.store.read()

    def restore(self, raw: bytes, directory: Path):
        restore_checkpoints(raw, directory)

    def observation(self, *, now: datetime | None = None):
        raw, _ = self.observations.read()
        return validate_observation(raw, now=now)

    def checkpoint(self, directory: Path, *, state_generation: int, claim_generation: int):
        return self.store.checkpoint(encode_checkpoints(directory), state_generation=state_generation,
                                     claim_generation=claim_generation)

    def release(self, *, claim_generation: int):
        self.store.release(claim_generation=claim_generation)
