import base64
import hashlib
from pathlib import Path
import tempfile
import unittest
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



class ImageCommandTests(unittest.TestCase):
    def test_image_binding_and_same_id_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / 'test.png'
            image.write_bytes(base64.b64decode(PNG))
            client = Client()
            kwargs = dict(state_path=root/'state', timeout=60, env=ENV, client=client)
            output = execute('测试图片', [image], {'type': 'object'}, **kwargs)
            self.assertEqual(output, {'answer': '样例'})
            self.assertEqual(client.request['materials'], [{'type': 'image', 'mime_type': 'image/png', 'base64': PNG}])
            execute('测试图片', [image], {'type': 'object'}, **kwargs)
            self.assertEqual(len(client.posts), 1)

    def test_unknown_lookup_never_posts_again_and_binding_is_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            client = Client()
            client.lost = True
            kwargs = dict(state_path=Path(directory)/'state', timeout=60, env=ENV, client=client)
            with self.assertRaises(TimeoutError):
                execute('中文测试', [], {'type': 'object'}, **kwargs)
            execute('中文测试', [], {'type': 'object'}, **kwargs)
            self.assertEqual(len(client.posts), 1)
            self.assertEqual(client.lookups[0][1], hashlib.sha256(json_body(client.request)).hexdigest())
            client.owner = 'Other/Project'
            with self.assertRaisesRegex(ValueError, 'binding'):
                execute('中文测试', [], {'type': 'object'}, **kwargs)
            with self.assertRaisesRegex(ValueError, 'changed'):
                execute('other material', [], {'type': 'object'}, **kwargs)
