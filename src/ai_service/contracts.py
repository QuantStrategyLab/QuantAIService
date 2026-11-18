"""Stable, dependency-free home for the SDK's shared wire/data contracts.

The Cloudflare Worker mirrors the scope names in its auth module, so these
values are cross-runtime contracts: keep this module depending only on the
standard library, so the more volatile modules can depend on it.
"""
from __future__ import annotations

import hashlib
import json

# Ordered so OAuth resource metadata (scopes_supported) stays byte-stable.
SCOPES = ('tasks:read', 'tasks:submit', 'tasks:work', 'events:subscribe', 'offline_access')
SCOPE_SET = frozenset(SCOPES)

# Allowed role scopes. The offline pilot config producer and the runtime grant
# validator must agree on these, or the producer emits a config auth rejects.
DOT_WORKER_SCOPES = frozenset({'tasks:read', 'tasks:work', 'events:subscribe'})
SUBMITTER_SCOPES = frozenset({'tasks:read', 'tasks:submit'})


def compact_json(value, *, sort_keys=False, ensure_ascii=False, allow_nan=True):
    """Compact JSON for request digests, size checks and one-line records."""
    return json.dumps(value, ensure_ascii=ensure_ascii, sort_keys=sort_keys,
                      allow_nan=allow_nan, separators=(',', ':'))


def request_digest(value):
    """sha256 of the canonical request JSON."""
    return hashlib.sha256(compact_json(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
