"""Project-scoped HTTP observations and explicitly supplied provider quotas."""
from __future__ import annotations

from collections import deque
import json
import math
import os
from pathlib import Path
import stat
import threading
import time


WINDOW_SECONDS = 300
MAX_SAMPLES = 10_000
MAX_SNAPSHOT_BYTES = 1_000_000


def number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value) and value >= 0
    except OverflowError:
        return False


class Dashboard:
    def __init__(self, quota_path=None, *, clock=time.time, monotonic=time.monotonic):
        self.quota_path = Path(quota_path) if quota_path is not None else None
        self.clock, self.monotonic = clock, monotonic
        self.started = monotonic()
        self.samples = {}
        self.lock = threading.Lock()

    def record(self, project, elapsed_ms, status):
        if not number(elapsed_ms):
            return
        with self.lock:
            samples = self.samples.setdefault(project, deque(maxlen=MAX_SAMPLES))
            samples.append((self.monotonic(), elapsed_ms, status >= 500))

    def health(self, project):
        now = self.monotonic()
        with self.lock:
            samples = self.samples.get(project, deque())
            while samples and samples[0][0] <= now - WINDOW_SECONDS:
                samples.popleft()
            latencies = sorted(sample[1] for sample in samples)
            errors = sum(sample[2] for sample in samples)
        count = len(latencies)
        return {'status': ('degraded' if errors else 'healthy') if count else 'unknown',
                'p95_ms': latencies[math.ceil(count * .95) - 1] if count else None,
                'total': count, 'errors': errors, 'window_seconds': WINDOW_SECONDS,
                'sample_limit': MAX_SAMPLES, 'uptime_seconds': max(0, now - self.started),
                'latency_profile': 'task_http', 'as_of': self.clock()}

    def quotas(self, project, providers):
        now = self.clock()
        missing = 'not_configured' if self.quota_path is None else 'unavailable'
        observations = {}
        if self.quota_path is not None:
            try:
                # Open the final component without following links, and validate the
                # descriptor actually read (atomic replacement is safe).
                fd = os.open(self.quota_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                            or info.st_uid != os.geteuid() or info.st_size > MAX_SNAPSHOT_BYTES):
                        raise ValueError('untrusted quota observation')
                    raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
                if len(raw) > MAX_SNAPSHOT_BYTES:
                    raise ValueError('quota observation too large')
                snapshot = json.loads(raw)
                if snapshot['version'] != 1 or type(snapshot['version']) is not int:
                    raise ValueError('unknown quota observation version')
                observations = snapshot['projects'].get(project, {})
                if not isinstance(observations, dict):
                    raise ValueError('invalid quota observations')
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                observations = {}
        rows = {}
        for provider in sorted(providers):
            row = {'status': missing, 'remaining': None, 'limit': None,
                   'window_end': None, 'observed_at': None, 'unit': None}
            observation = observations.get(provider)
            if observation is not None:
                try:
                    limit, remaining, end, observed = (observation[key] for key in
                        ('limit', 'remaining', 'window_end', 'observed_at'))
                    unit = observation['unit']
                    if (not all(number(value) for value in (limit, remaining, end, observed))
                            or remaining > limit or end <= observed or observed > now
                            or unit not in {'usd', 'requests', 'tokens', 'percent'}
                            or (unit == 'percent' and limit != 100)):
                        raise ValueError('invalid provider quota')
                    if now - observed >= WINDOW_SECONDS or end <= now:
                        row['status'] = 'stale'
                    else:
                        row.update(status='available', remaining=remaining, limit=limit,
                                   window_end=end, observed_at=observed, unit=unit)
                except (ValueError, KeyError, TypeError):
                    row['status'] = 'unavailable'
            rows[provider] = row
        available = sum(row['status'] == 'available' for row in rows.values())
        status = 'available' if rows and available == len(rows) else 'partial' if available else 'unavailable'
        return {'status': status, 'as_of': now, 'providers': rows}
