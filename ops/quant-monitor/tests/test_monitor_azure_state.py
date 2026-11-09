"""Offline checks for the Azure Blob state contract."""
from email.message import Message
import io
from pathlib import Path
import sys
from urllib.error import HTTPError

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from monitor_cloud_state import StateUnavailable
from monitor_azure_state import AzureBlobState

URL = 'https://synthetic.blob.core.windows.net/input-state'


class Reply(io.BytesIO):
    def __init__(self, body=b'', **headers):
        super().__init__(body)
        self.headers = Message()
        for key, value in headers.items():
            self.headers[key] = value


def http_error(code, reason):
    headers = Message()
    headers['x-ms-error-code'] = reason
    return HTTPError(URL, code, reason, headers, io.BytesIO())


def test_blob_claim_and_commit_use_the_same_lease_and_read_version():
    calls = []
    def open_request(request, *, timeout):
        headers = {k.lower(): v for k, v in request.header_items()}
        calls.append((request.method, request.full_url, headers, request.data))
        if headers.get('x-ms-lease-action') == 'acquire':
            return Reply(**{'x-ms-lease-id': headers['x-ms-proposed-lease-id']})
        if request.method == 'GET':
            return Reply(b'archive', ETag='"read-version"')
        return Reply(ETag='"new-version"')
    store = AzureBlobState(URL, 'synthetic', opener=open_request)
    lease = store.claim({'execution_id': 'test-cycle'})
    assert store.read() == (b'archive', '"read-version"')
    assert store.commit(b'new-archive', state_generation='"read-version"', claim_generation=lease) == '"new-version"'
    acquire, read, write, release = calls
    assert acquire[1].endswith('/current.tar.gz?comp=lease')
    assert acquire[2]['x-ms-lease-duration'] == '-1'
    assert write[2]['if-match'] == '"read-version"' and write[2]['x-ms-lease-id'] == lease
    assert release[2]['x-ms-lease-action'] == 'release' and release[2]['x-ms-lease-id'] == lease
    assert all(call[2]['authorization'] == 'Bearer synthetic' for call in calls)


def test_conflicts_and_uncertain_write_keep_the_lease():
    calls = []
    def occupied(*_args, **_kwargs):
        raise http_error(409, 'LeaseAlreadyPresent')
    assert AzureBlobState(URL, 'synthetic', opener=occupied).claim({}) is None

    def fail(request, **_kwargs):
        calls.append(request)
        raise http_error(412, 'ConditionNotMet')
    with pytest.raises(StateUnavailable, match='checkpoint_outcome_unknown'):
        AzureBlobState(URL, 'synthetic', opener=fail).commit(
            b'archive', state_generation='"stale"', claim_generation='lease')
    assert len(calls) == 1


def test_projection_creation_is_conditional_and_requires_current_lease():
    calls = []
    def open_request(request, **_kwargs):
        headers = {k.lower(): v for k, v in request.header_items()}
        calls.append((request.method, request.full_url, headers))
        if request.method == 'HEAD' and request.full_url.endswith('/diagnosis-observation.json'):
            raise http_error(404, 'BlobNotFound')
        return Reply()
    AzureBlobState(URL, 'synthetic', opener=open_request).write_projection(
        'diagnosis-observation.json', b'{}', claim_generation='core-lease')
    assert calls[0][0] == 'HEAD' and calls[0][2]['x-ms-lease-id'] == 'core-lease'
    assert calls[-1][2]['if-none-match'] == '*'


@pytest.mark.parametrize('url', [URL + '?sas=secret', URL.replace('https:', 'http:'),
                                 URL + '/another', URL.replace('blob.core.windows.net', 'example.test')])
def test_only_explicit_azure_container_urls_can_receive_bearer_token(url):
    with pytest.raises(ValueError, match='monitor_state_uri_invalid'):
        AzureBlobState(url, 'synthetic')


def test_google_federation_helper_requires_expected_protocol_and_azure_app_uri(monkeypatch):
    import monitor_azure_state as azure

    requests = []
    resource = 'api://12345678-1234-1234-1234-123456789abc'
    monkeypatch.setenv('GOOGLE_EXTERNAL_ACCOUNT_TOKEN_TYPE', azure.FEDERATION_TOKEN_TYPE)
    monkeypatch.setenv('GOOGLE_EXTERNAL_ACCOUNT_AUDIENCE',
                       '//iam.googleapis.com/projects/123/locations/global/workloadIdentityPools/test/providers/test')
    monkeypatch.setenv('MONITOR_GCP_FEDERATION_RESOURCE', resource)
    def request(audience):
        requests.append(audience)
        return {'access_token': 'synthetic-jwt', 'expires_on': '1790000000'}
    monkeypatch.setattr(azure, 'managed_identity_response', request)
    assert azure.google_subject_response() == {
        'version': 1, 'success': True, 'token_type': azure.FEDERATION_TOKEN_TYPE,
        'id_token': 'synthetic-jwt', 'expiration_time': 1790000000,
    }
    monkeypatch.setenv('MONITOR_GCP_FEDERATION_RESOURCE', 'https://storage.azure.com/')
    with pytest.raises(StateUnavailable, match='azure_federation_config_invalid'):
        azure.google_subject_response()
    assert requests == [resource]
