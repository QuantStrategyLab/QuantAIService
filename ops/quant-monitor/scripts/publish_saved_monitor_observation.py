"""Self-hosted adapter for saved, bounded diagnostic observations.

See ../AGENTS.md. It never runs the monitor, a broker, a model or a notification.
The transitional producer stops writing when the cloud producer takes ownership.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime
from pathlib import Path

from monitor_cloud_state import CloudState, StateUnavailable
from monitor_diagnosis_state import observation_from_workspace

PRODUCER = 'self_hosted_transition'


def plan_publication(new: bytes, previous: bytes, producer: str | None):
    if producer != PRODUCER:
        return 'producer_changed'
    current, candidate = json.loads(previous), json.loads(new)
    old_cycle, new_cycle = current.get('cycle'), candidate.get('cycle')
    if old_cycle and new_cycle:
        old = datetime.fromisoformat(old_cycle['as_of'].replace('Z', '+00:00'))
        candidate_time = datetime.fromisoformat(new_cycle['as_of'].replace('Z', '+00:00'))
        if old.tzinfo is None or candidate_time.tzinfo is None:
            raise StateUnavailable('observation_time_invalid')
        if old > candidate_time:
            return 'newer_observation_exists'
    if previous == new:
        return 'unchanged'
    return 'publish'


def publish(monitor: Path, client, *, now=None):
    raw = observation_from_workspace(monitor, now=now)
    previous, metadata = client.read()
    producer = (metadata.get('metadata') or metadata.get('custom_metadata')
                or metadata.get('custom_fields') or {}).get('monitor-producer')
    reason = plan_publication(raw, previous, producer)
    if reason != 'publish':
        return {'status': 'skipped', 'reason': reason, 'generation': int(metadata['generation'])}
    generation = client.write(raw, int(metadata['generation']))
    value = json.loads(raw)
    return {'status': 'published', 'generation': generation, 'observation_status': value['status'],
            'observed_at': (value.get('cycle') or {}).get('as_of')}


class GcloudObservation:
    def __init__(self, uri, account_hash):
        prefix, name = uri.rsplit('/', 1)
        if name != 'diagnosis-observation.json':
            raise ValueError('observation_uri_invalid')
        class BindingOnly:
            def bucket(self, name):
                return name
        CloudState(BindingOnly(), prefix, state_name=name)
        self.uri = uri
        accounts = self.call(['auth', 'list', '--format=json'], account=False)
        self.account = next(row['account'] for row in accounts
                            if hashlib.sha256(row['account'].encode()).hexdigest() == account_hash)

    def call(self, args, *, body=None, account=True, raw=False):
        command = ['gcloud', *args]
        if account:
            command += ['--account', self.account]
        result = subprocess.run(command, input=body, capture_output=True, timeout=60)
        if result.returncode:
            raise StateUnavailable('observation_client_failed')
        return result.stdout if raw else json.loads(result.stdout)

    def read(self):
        metadata = self.call(['storage', 'objects', 'describe', self.uri, '--format=json'])
        if int(metadata['size']) > 2 * 1024 * 1024:
            raise StateUnavailable('observation_too_large')
        raw = self.call(['storage', 'cat', self.uri + '#' + str(metadata['generation'])], raw=True)
        return raw, metadata

    def write(self, raw, generation):
        self.call(['storage', 'cp', '--if-generation-match=' + str(generation),
                   '--content-type=application/json', '--custom-metadata=monitor-producer=' + PRODUCER,
                   '-', self.uri], body=raw, raw=True)
        value = self.call(['storage', 'objects', 'describe', self.uri, '--format=json'])
        return int(value['generation'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        result = publish(Path(config['monitor_root']), GcloudObservation(config['observation_uri'], config['gcloud_account_sha256']))
        print(json.dumps(result))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'reason': 'observation_publish_failed'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
