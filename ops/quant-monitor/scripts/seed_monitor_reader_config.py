"""One protected transfer of existing input-reader configuration; no values are logged."""
import base64
import json
import os
import re

import google.auth
from google.auth.transport.requests import AuthorizedSession

from monitor_inputs import mint_reader_token, reader_installation_id


def seed(environ, *, session_factory=AuthorizedSession):
    resource = environ.get('MONITOR_READER_CONFIG_TARGET', '')
    app_id = environ.get('QUANT_MONITOR_READER_APP_ID', '')
    config = {
        'private_key': environ.get('QUANT_MONITOR_READER_APP_PRIVATE_KEY', ''),
        'archive_uri': environ.get('QUANT_MONITOR_BINANCE_REPORT_ARCHIVE_URI', ''),
        'stream_id': environ.get('QUANT_MONITOR_BINANCE_STREAM_ID', ''),
        'account_scope_sha256': environ.get('QUANT_MONITOR_BINANCE_REPORT_SCOPE_SHA256', ''),
        'app_id': app_id,
    }
    if (not re.fullmatch(r'projects/[a-z][a-z0-9-]{4,28}[a-z0-9]/secrets/[A-Za-z0-9_-]+', resource)
            or any(not isinstance(value, str) or not value for value in config.values())):
        return 'preflight_failed'
    try:
        installation_id = reader_installation_id(app_id, config['private_key'])
        mint_reader_token(app_id, installation_id, config['private_key'])
        config['installation_id'] = installation_id
        payload = base64.b64encode(json.dumps(config, separators=(',', ':')).encode()).decode()
        credentials, _ = google.auth.default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        session = session_factory(credentials)
    except Exception:
        return 'preflight_failed'
    try:
        response = session.post('https://secretmanager.googleapis.com/v1/' + resource + ':addVersion',
                                json={'payload': {'data': payload}}, timeout=30, allow_redirects=False)
        return 'created' if response.status_code == 200 else 'unconfirmed'
    except Exception:
        return 'unconfirmed'


def main():
    result = seed(os.environ)
    print(json.dumps({'status': result}, separators=(',', ':')))
    return 0 if result == 'created' else 1


if __name__ == '__main__':
    raise SystemExit(main())
