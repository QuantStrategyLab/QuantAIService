"""Portable monitor state over Azure Blob Storage, with exclusive leases and ETags."""
from __future__ import annotations

from email.utils import formatdate
import json
import os
import re
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from monitor_cloud_state import CloudState, MAX_ARCHIVE, StateUnavailable

CURRENT = 'current.tar.gz'
PROJECTIONS = frozenset({'diagnosis-observation.json', 'strategy-findings.json'})
MAX_PROJECTION = 2 * 1024 * 1024
FEDERATION_TOKEN_TYPE = 'urn:ietf:params:oauth:token-type:jwt'
FEDERATION_AUDIENCE = re.compile(r'//iam\.googleapis\.com/projects/[0-9]+/locations/global/'
                                 r'workloadIdentityPools/[a-z0-9-]+/providers/[a-z0-9-]+')
AZURE_APP_RESOURCE = re.compile(r'api://[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}')
CONTAINER = re.compile(r'https://[a-z0-9]{3,24}\.blob\.core\.windows\.net/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]')


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def managed_identity_response(resource):
    endpoint = os.environ.get('IDENTITY_ENDPOINT', '')
    header = os.environ.get('IDENTITY_HEADER', '')
    target = urlsplit(endpoint)
    if (not header or target.scheme != 'http'
            or target.hostname not in {'localhost', '127.0.0.1', '::1', '169.254.169.254'}
            or target.username or target.password or target.fragment or target.query):
        raise StateUnavailable('azure_identity_unavailable')
    params = {'resource': resource, 'api-version': '2019-08-01'}
    if client_id := os.environ.get('MONITOR_AZURE_MANAGED_IDENTITY_CLIENT_ID'):
        params['client_id'] = client_id
    request = Request(endpoint + '?' + urlencode(params), headers={'X-IDENTITY-HEADER': header})
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=10) as response:
            data = json.loads(response.read(16385))
        token = data['access_token']
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError('invalid access token')
        return data
    except Exception:
        raise StateUnavailable('azure_identity_unavailable') from None


def storage_token():
    """Use the runner's short-lived token or Container Apps managed identity."""
    return (os.environ.get('MONITOR_AZURE_BLOB_TOKEN')
            or managed_identity_response('https://storage.azure.com/')['access_token'])


def google_subject_response():
    """Supply the Google auth executable protocol using the same managed identity."""
    audience = os.environ.get('GOOGLE_EXTERNAL_ACCOUNT_AUDIENCE', '')
    resource = os.environ.get('MONITOR_GCP_FEDERATION_RESOURCE', '')
    if (os.environ.get('GOOGLE_EXTERNAL_ACCOUNT_TOKEN_TYPE') != FEDERATION_TOKEN_TYPE
            or not FEDERATION_AUDIENCE.fullmatch(audience) or not AZURE_APP_RESOURCE.fullmatch(resource)):
        raise StateUnavailable('azure_federation_config_invalid')
    data = managed_identity_response(resource)
    return {'version': 1, 'success': True, 'token_type': FEDERATION_TOKEN_TYPE,
            'id_token': data['access_token'], 'expiration_time': int(data['expires_on'])}


class AzureBlobState:
    def __init__(self, container_url, token, *, state_name=CURRENT, opener=None):
        if not CONTAINER.fullmatch(container_url):
            raise ValueError('monitor_state_uri_invalid')
        if state_name not in PROJECTIONS | {CURRENT}:
            raise ValueError('monitor_state_name_invalid')
        self.url = container_url
        self.token = token
        self.state_name = state_name
        self.open = opener or build_opener(NoRedirect()).open

    def _request(self, name, method, *, body=None, headers=None, lease=False, limit=0):
        url = f'{self.url}/{name}' + ('?comp=lease' if lease else '')
        request = Request(url, data=body, method=method, headers={
            'Authorization': 'Bearer ' + self.token,
            'x-ms-version': '2023-11-03',
            'x-ms-date': formatdate(usegmt=True),
            **(headers or {}),
        })
        with self.open(request, timeout=60 if limit else 25) as response:
            return response.headers, response.read(limit + 1) if limit else b''

    def read(self, name=None):
        if name is not None and name != self.state_name:
            raise ValueError('monitor_state_name_mismatch')
        limit = MAX_ARCHIVE if self.state_name == CURRENT else MAX_PROJECTION
        try:
            headers, raw = self._request(self.state_name, 'GET', limit=limit)
            etag = headers['ETag']
            if not raw or len(raw) > limit or not etag:
                raise ValueError('invalid blob response')
            return raw, etag
        except Exception:
            raise StateUnavailable('state_read_failed') from None

    def claim(self, record):
        owner = str(uuid4())
        try:
            headers, _ = self._request(CURRENT, 'PUT', body=b'', lease=True, headers={
                'x-ms-lease-action': 'acquire', 'x-ms-lease-duration': '-1', 'x-ms-proposed-lease-id': owner,
            })
        except HTTPError as exc:
            if exc.code == 409 and exc.headers.get('x-ms-error-code') == 'LeaseAlreadyPresent':
                return None
            raise StateUnavailable('claim_outcome_unknown') from None
        except OSError:
            raise StateUnavailable('claim_outcome_unknown') from None
        if headers['x-ms-lease-id'] != owner:
            raise StateUnavailable('claim_outcome_unknown')
        return owner

    def checkpoint(self, raw, *, state_generation, claim_generation):
        if len(raw) > MAX_ARCHIVE:
            raise StateUnavailable('state_write_too_large')
        try:
            headers, _ = self._request(CURRENT, 'PUT', body=raw, headers={
                'If-Match': state_generation, 'x-ms-lease-id': claim_generation,
                'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'application/gzip',
            })
            if not headers['ETag']:
                raise ValueError('missing blob ETag')
            return headers['ETag']
        except Exception:
            raise StateUnavailable('checkpoint_outcome_unknown') from None

    def release(self, *, claim_generation):
        try:
            self._request(CURRENT, 'PUT', body=b'', lease=True, headers={
                'x-ms-lease-action': 'release', 'x-ms-lease-id': claim_generation,
            })
        except Exception:
            raise StateUnavailable('release_outcome_unknown') from None

    def commit(self, raw, *, state_generation, claim_generation):
        generation = self.checkpoint(raw, state_generation=state_generation, claim_generation=claim_generation)
        self.release(claim_generation=claim_generation)
        return generation

    def write_projection(self, name, raw, *, claim_generation):
        if name not in PROJECTIONS or len(raw) > MAX_PROJECTION:
            raise StateUnavailable('projection_invalid')
        try:
            self._request(CURRENT, 'HEAD', headers={'x-ms-lease-id': claim_generation})
            try:
                headers, _ = self._request(name, 'HEAD')
            except HTTPError as exc:
                if exc.code != 404 or exc.headers.get('x-ms-error-code') != 'BlobNotFound':
                    raise
                condition = {'If-None-Match': '*'}
            else:
                if not headers['ETag']:
                    raise ValueError('missing blob ETag')
                condition = {'If-Match': headers['ETag']}
            self._request(name, 'PUT', body=raw, headers={
                'x-ms-blob-type': 'BlockBlob', 'Content-Type': 'application/json', **condition,
            })
        except Exception:
            raise StateUnavailable('projection_write_failed') from None


def open_monitor_state(azure_url_env, gcs_uri_env, *, state_name=CURRENT):
    azure_url = os.environ.get(azure_url_env)
    if azure_url:
        if not CONTAINER.fullmatch(azure_url):
            raise ValueError('monitor_state_uri_invalid')
        return AzureBlobState(azure_url, storage_token(), state_name=state_name)
    from google.cloud import storage

    return CloudState(storage.Client(), os.environ[gcs_uri_env], state_name=state_name)


if __name__ == '__main__':
    try:
        result = google_subject_response()
    except Exception:
        result = {'version': 1, 'success': False, 'code': 'AZURE_IDENTITY_UNAVAILABLE',
                  'message': 'Azure federation configuration or identity is unavailable'}
    print(json.dumps(result))
