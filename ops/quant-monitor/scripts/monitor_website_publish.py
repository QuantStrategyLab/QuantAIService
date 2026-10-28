"""Publish the existing website snapshot contract once. See ../AGENTS.md."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
import urllib.error
import urllib.parse
import urllib.request

from monitor_cloud_state import StateUnavailable


class PublicationError(StateUnavailable):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def read_sync_token(secret_version, *, session=None):
    """Read only an explicitly bound version through the runtime's own ADC."""
    if not re.fullmatch(r'projects/[1-9][0-9]*/secrets/[A-Za-z0-9_-]+/versions/[1-9][0-9]*', secret_version):
        raise StateUnavailable('monitor_website_binding_invalid')
    owned_session = session is None
    if owned_session:
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        session = AuthorizedSession(credentials)
    try:
        with session.get('https://secretmanager.googleapis.com/v1/' + secret_version + ':access',
                         timeout=20, allow_redirects=False) as response:
            if response.status_code != 200 or len(response.content) > 65536:
                raise ValueError()
            value = response.json()
            if value.get('name') != secret_version:
                raise ValueError()
            token = base64.b64decode(value['payload']['data'], validate=True).decode('utf-8')
            if not token or any(character.isspace() for character in token):
                raise ValueError()
            return token
    except Exception as exc:
        raise StateUnavailable('monitor_website_credential_unavailable') from exc
    finally:
        if owned_session:
            session.close()


def publish_saved_snapshot(root, *, checkpoint, url, secret_version, session=None, opener=None):
    """A website failure is independent of computation and notification delivery."""
    snapshot = root / 'monitor/data/health/strategy_health_dashboard.v1.json'
    path = root / 'monitor/data/health/website-publication.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'schema_version': 'quant.monitor-website-publication.v1', 'status': 'unconfirmed',
               'snapshot_sha256': None, 'recorded_at': datetime.now(timezone.utc).isoformat()}
    try:
        receipt['snapshot_sha256'] = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    except OSError:
        pass
    path.write_text(json.dumps(receipt, sort_keys=True))
    # Never request credentials or send HTTP after an uncertain state write.
    checkpoint()
    try:
        token = read_sync_token(secret_version, session=session)
    except Exception:
        receipt['reason'] = 'credential_unavailable'
    else:
        try:
            publish_snapshot(snapshot, url=url, token=token, opener=opener)
        except PublicationError as exc:
            reason = str(exc)
            receipt['reason'] = reason if reason in {'binding_invalid', 'snapshot_invalid', 'http_rejected',
                                                      'transport_unknown', 'response_invalid', 'ack_mismatch'} else 'transport_unknown'
        except Exception:
            receipt['reason'] = 'transport_unknown'
        else:
            receipt['status'] = 'accepted'
            receipt['reason'] = 'accepted'
    path.write_text(json.dumps(receipt, sort_keys=True))
    return receipt['status']


def publish_snapshot(path, *, url, token, opener=None):
    try:
        target = urllib.parse.urlsplit(url)
    except ValueError:
        raise PublicationError('binding_invalid') from None
    if (target.scheme != 'https' or not target.hostname or target.username or target.password
            or target.query or target.fragment or target.path != '/api/internal/sync-strategy-health' or not token):
        raise PublicationError('binding_invalid')
    try:
        raw = path.read_bytes()
        value = json.loads(raw)
        if len(raw) > 2 * 1024 * 1024 or not isinstance(value, dict) or value.get('schema_version') != 'strategy_health_dashboard.v1':
            raise ValueError()
    except (OSError, ValueError, TypeError):
        raise PublicationError('snapshot_invalid') from None
    request = urllib.request.Request(url, data=raw, method='POST',
                                      headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    opener = opener or urllib.request.build_opener(NoRedirect())
    try:
        with opener.open(request, timeout=20) as response:
            if response.status != 200:
                raise PublicationError('http_rejected')
            body = response.read(65537)
    except urllib.error.HTTPError:
        raise PublicationError('http_rejected') from None
    except PublicationError:
        raise
    except Exception:
        raise PublicationError('transport_unknown') from None
    try:
        if len(body) > 65536:
            raise ValueError()
        value = json.loads(body)
    except (ValueError, TypeError):
        raise PublicationError('response_invalid') from None
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise PublicationError('ack_mismatch')
