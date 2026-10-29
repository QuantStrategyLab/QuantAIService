import base64
import hashlib
import pytest
from ai_service.image_command import execute
from ai_service.transport import json_body

PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jrN8AAAAASUVORK5CYII='
ENV = {'AI_SERVICE_MODEL': 'configured-dot', 'AI_SERVICE_PROFILE': 'default',
       'GITHUB_REPOSITORY': 'Synthetic/Project', 'GITHUB_RUN_ID': '123'}


class Client:
    def __init__(self):
        self.posts = []
        self.lookups = []
        self.request = None
        self.lost = False
        self.owner = ENV['GITHUB_REPOSITORY']

    def submit(self, request, key):
        self.posts.append((request, key))
        self.request = request
        if self.lost:
            raise TimeoutError('synthetic unknown')
        return {'id': 'synthetic-task'}

    def lookup(self, key, digest):
        self.lookups.append((key, digest))
        return {'id': 'synthetic-task'}

    def wait(self, task_id, timeout_seconds):
        return {'id': task_id, 'owner': self.owner, 'request': self.request,
                'status': 'completed', 'result_kind': 'advisory', 'output': {'answer': '样例'}}


def test_image_command_keeps_schema_and_actual_request_binding(tmp_path):
    image = tmp_path / 'test.png'
    image.write_bytes(base64.b64decode(PNG))
    client = Client()
    output = execute('测试图片', [image], {'type': 'object'}, state_path=tmp_path/'state',
                     timeout=60, env=ENV, client=client)
    assert output == {'answer': '样例'}
    assert client.request['materials'] == [{'type': 'image', 'mime_type': 'image/png', 'base64': PNG}]
    execute('测试图片', [image], {'type': 'object'}, state_path=tmp_path/'state', timeout=60, env=ENV, client=client)
    assert len(client.posts) == 1


def test_image_command_unknown_uses_frozen_lookup_never_another_post(tmp_path):
    client = Client()
    client.lost = True
    kwargs = dict(state_path=tmp_path/'state', timeout=60, env=ENV, client=client)
    with pytest.raises(TimeoutError):
        execute('中文测试', [], {'type': 'object'}, **kwargs)
    execute('中文测试', [], {'type': 'object'}, **kwargs)
    assert len(client.posts) == 1
    assert client.lookups[0][1] == hashlib.sha256(json_body(client.request)).hexdigest()
    client.owner = 'Other/Project'
    with pytest.raises(ValueError, match='binding'):
        execute('中文测试', [], {'type': 'object'}, **kwargs)
    with pytest.raises(ValueError, match='changed'):
        execute('other material', [], {'type': 'object'}, **kwargs)
