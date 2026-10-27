"""Publish the existing website snapshot contract once. See ../AGENTS.md."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from monitor_cloud_state import StateUnavailable


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def publish_snapshot(path, *, url, token, opener=None):
    target = urllib.parse.urlsplit(url)
    if (target.scheme != 'https' or not target.hostname or target.username or target.password
            or target.query or target.fragment or target.path != '/api/internal/sync-strategy-health' or not token):
        raise StateUnavailable('monitor_website_binding_invalid')
    raw = path.read_bytes()
    if len(raw) > 2 * 1024 * 1024 or json.loads(raw).get('schema_version') != 'strategy_health_dashboard.v1':
        raise StateUnavailable('monitor_website_snapshot_invalid')
    request = urllib.request.Request(url, data=raw, method='POST',
                                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            body = response.read(65537)
            if response.status != 200 or len(body) > 65536 or json.loads(body).get('ok') is not True:
                raise ValueError()
    except Exception as exc:
        raise StateUnavailable('monitor_website_publish_unconfirmed') from exc

