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


def bounded_http_status(value):
    return value if type(value) is int and 100 <= value <= 599 else None


class PublicationError(StateUnavailable):
    def __init__(self, reason, http_status=None, response_evidence=None):
        super().__init__(reason)
        self.http_status = bounded_http_status(http_status)
        self.response_evidence = response_evidence


def classify_rejection(response):
    """Project bounded response markers only; no response text or headers leave memory."""
    result = {'content_type': 'unknown', 'challenge': 'unknown', 'cf_ray': 'unknown',
              'body_marker': 'unknown', 'body_read': 'unknown'}
    try:
        headers = response.headers
        content_type = str(headers.get('Content-Type', '')).split(';', 1)[0].strip().lower()
        result['content_type'] = {'application/json': 'json', 'text/html': 'html',
                                  'text/plain': 'text'}.get(content_type, 'other' if content_type else 'unknown')
        challenge = headers.get('cf-mitigated')
        result['challenge'] = 'challenge' if challenge == 'challenge' else 'absent_or_other'
        result['cf_ray'] = 'present' if headers.get('cf-ray') else 'absent'
    except Exception:
        return result
    try:
        body = response.read(4097)
        if len(body) > 4096:
            result['body_read'] = 'truncated'
        elif result['content_type'] == 'json':
            result['body_read'] = 'complete'
            value = json.loads(body)
            error = value.get('error') if isinstance(value, dict) else None
            result['body_marker'] = {
                'strategy health sync token is invalid': 'token_invalid',
                'strategy health sync token is not configured': 'token_unconfigured',
                'strategy health KV is not configured': 'kv_unconfigured',
            }.get(error, 'other') if isinstance(error, str) else 'other'
        elif result['content_type'] == 'text' and (match := re.fullmatch(
                rb'error code:[ \t]*(1010|1020)', body.strip(), re.I)):
            result['body_read'] = 'complete'
            code = match.group(1).decode('ascii')
            result['body_marker'] = 'plain_error_' + code
        elif result['content_type'] == 'html' and re.search(rb'error\s+code\s*:\s*1020\b', body, re.I):
            result['body_read'] = 'complete'
            result['body_marker'] = 'cf_1020_marker'
        else:
            result['body_read'] = 'complete'
            result['body_marker'] = 'other'
    except Exception:
        result['body_marker'] = 'unknown'
    return result


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
               'snapshot_sha256': None, 'http_status': None, 'recorded_at': datetime.now(timezone.utc).isoformat()}
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
            receipt['http_status'] = bounded_http_status(publish_snapshot(snapshot, url=url, token=token, opener=opener))
        except PublicationError as exc:
            receipt['http_status'] = bounded_http_status(exc.http_status)
            if exc.response_evidence is not None:
                receipt['response_evidence'] = exc.response_evidence
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
    http_status = None
    try:
        with opener.open(request, timeout=20) as response:
            http_status = bounded_http_status(response.status)
            if response.status != 200:
                raise PublicationError('http_rejected', response.status, classify_rejection(response))
            body = response.read(65537)
    except urllib.error.HTTPError as exc:
        try:
            evidence = classify_rejection(exc)
        finally:
            try:
                exc.close()
            except Exception:
                pass
        raise PublicationError('http_rejected', exc.code, evidence) from None
    except PublicationError:
        raise
    except Exception:
        raise PublicationError('transport_unknown', http_status) from None
    try:
        if len(body) > 65536:
            raise ValueError()
        value = json.loads(body)
    except (ValueError, TypeError):
        raise PublicationError('response_invalid', 200) from None
    if not isinstance(value, dict) or value.get('ok') is not True:
        raise PublicationError('ack_mismatch', 200)
    return 200
