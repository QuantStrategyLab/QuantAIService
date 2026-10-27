"""Manual Sentinel Secret maintenance using ADC; see ../AGENTS.md.

Inspect is read-only. Sync appends versions after all targets pass preflight.
The Bot token stays in process memory; uncertain writes are never retried.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
from urllib import request as http

API = 'https://secretmanager.googleapis.com/v1/'
PERMISSION = 'secretmanager.versions.add'
TARGET = re.compile(r'projects/(?:[a-z][a-z0-9-]{4,61}[a-z0-9]|[0-9]+)/secrets/[A-Za-z0-9_-]{1,255}')


class Failure(RuntimeError):
    def __init__(self, code, *, versions_added=0, outcome_unknown=False):
        super().__init__(code)
        self.code = code
        self.versions_added = versions_added
        self.outcome_unknown = outcome_unknown


class NoRedirect(http.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def make_request(credentials, auth_request, *, opener=None):
    opener = opener or http.build_opener(NoRedirect())

    def request(method, url, body):
        headers = {'Content-Type': 'application/json'}
        if url.startswith(API + 'projects/'):
            credentials.before_request(auth_request, method, url, headers)
        elif not url.startswith('https://api.telegram.org/bot') or not url.endswith('/getMe'):
            raise Failure('invalid_endpoint')
        req = http.Request(url, data=json.dumps(body).encode(), headers=headers, method=method)
        with opener.open(req, timeout=20) as response:
            raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise Failure('response_too_large')
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise Failure('invalid_response')
        return value

    return request


def validate_configuration(mode, token, expected_bot_hash, targets):
    if (mode not in {'inspect', 'sync'} or not isinstance(token, str) or not token
            or any(char.isspace() for char in token)
            or not isinstance(expected_bot_hash, str) or not re.fullmatch('[0-9a-f]{64}', expected_bot_hash)
            or not isinstance(targets, list) or len(targets) != 4
            or not all(isinstance(name, str) and TARGET.fullmatch(name) for name in targets)
            or len(set(targets)) != 4):
        raise Failure('invalid_configuration')


def maintain(*, mode, token, expected_bot_hash, targets, request):
    validate_configuration(mode, token, expected_bot_hash, targets)
    try:
        identity = request('POST', 'https://api.telegram.org/bot' + token + '/getMe', {})
        bot = identity.get('result', {})
        if (identity.get('ok') is not True or bot.get('is_bot') is not True
                or type(bot.get('id')) is not int or bot['id'] <= 0):
            raise Failure('bot_check_failed')
        actual = hashlib.sha256(str(bot['id']).encode()).hexdigest()
        if not hmac.compare_digest(actual, expected_bot_hash):
            raise Failure('bot_identity_mismatch')
    except Failure:
        raise
    except Exception:
        raise Failure('bot_check_failed') from None
    for name in targets:
        try:
            permissions = request('POST', API + name + ':testIamPermissions', {'permissions': [PERMISSION]})
            granted = permissions.get('permissions', [])
            if not isinstance(granted, list) or not all(isinstance(value, str) for value in granted):
                raise Failure('permission_check_failed')
            if PERMISSION not in granted:
                raise Failure('target_permission_missing')
        except Failure:
            raise
        except Exception:
            raise Failure('permission_check_failed') from None
    added = 0
    if mode == 'sync':
        payload = {'payload': {'data': base64.b64encode(token.encode()).decode()}}
        for name in targets:
            try:
                version = request('POST', API + name + ':addVersion', payload)
                if not re.fullmatch(re.escape(name) + r'/versions/[0-9]+', str(version.get('name', ''))):
                    raise ValueError('unexpected_version')
            except Exception:
                raise Failure('write_outcome_unknown', versions_added=added, outcome_unknown=True) from None
            added += 1
    return {'status': 'verified' if mode == 'inspect' else 'completed', 'mode': mode,
            'bot_identity_verified': True, 'permissions_verified': len(targets), 'versions_added': added}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['inspect', 'sync'], default='inspect')
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        try:
            targets = json.loads(os.environ.get('QUANT_SENTINEL_SECRET_TARGETS_JSON', ''))
        except ValueError:
            raise Failure('invalid_configuration') from None
        token = os.environ.get('TG_TOKEN', '')
        expected = os.environ.get('QUANT_SENTINEL_EXPECTED_BOT_ID_SHA256', '')
        validate_configuration(args.mode, token, expected, targets)
        import google.auth
        from google.auth.transport.requests import Request

        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        result = maintain(mode=args.mode, token=token, expected_bot_hash=expected,
            targets=targets, request=make_request(credentials, Request()))
    except Failure as error:
        result = {'status': 'failed', 'reason': error.code, 'mode': args.mode,
                  'confirmed_versions_added': error.versions_added, 'write_outcome_unknown': error.outcome_unknown}
    except Exception:
        result = {'status': 'failed', 'reason': 'client_initialization_failed', 'mode': args.mode,
                  'confirmed_versions_added': 0, 'write_outcome_unknown': False}
    args.receipt.write_text(json.dumps(result, sort_keys=True) + '\n')
    print(json.dumps(result, sort_keys=True))
    return 0 if result['status'] in {'verified', 'completed'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
