import json
import os
import unittest
from unittest.mock import patch

from ai_service.client import TaskClient, github_token
from ai_service.transport import HttpReply


class ClientTests(unittest.TestCase):
    def test_lookup_sends_only_the_frozen_key_and_digest_by_get(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append((url, kwargs))
                return HttpReply(200, b'{"id":"original-task"}')
        client = TaskClient('https://service.example', 'synthetic', transport=Transport())
        self.assertEqual(client.lookup('original-key', 'a' * 64), {'id': 'original-task'})
        self.assertEqual(len(calls), 1)
        url, arguments = calls[0]
        self.assertEqual(url, 'https://service.example/v1/tasks/lookup')
        self.assertEqual(arguments['method'], 'GET')
        self.assertEqual(arguments['body'], b'')
        self.assertEqual(arguments['headers']['Idempotency-Key'], 'original-key')
        self.assertEqual(arguments['headers']['X-Request-SHA256'], 'a' * 64)

    def test_lookup_unknown_never_posts_retries_or_accepts_an_error_body_id(self):
        for status in (400, 401, 403, 404, 409, 500):
            with self.subTest(status=status):
                calls = []
                class Transport:
                    def request(self, url, *, _calls=calls, _status=status, **kwargs):
                        _calls.append(kwargs['method'])
                        return HttpReply(_status, b'{"id":"untrusted-id","private":"do-not-echo"}')
                client = TaskClient('https://service.example', 'synthetic', transport=Transport())
                with self.assertRaises(RuntimeError) as error:
                    client.lookup('frozen-key', 'a' * 64)
                self.assertNotIn('untrusted-id', str(error.exception))
                self.assertNotIn('do-not-echo', str(error.exception))
                self.assertEqual(calls, ['GET'])

    def test_lookup_transport_unknown_is_not_retried(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(kwargs['method'])
                raise TimeoutError('synthetic unknown')
        client = TaskClient('https://service.example', 'synthetic', transport=Transport())
        with self.assertRaises(TimeoutError):
            client.lookup('frozen-key', 'a' * 64)
        self.assertEqual(calls, ['GET'])

    def test_lookup_rejects_invalid_binding_before_token_or_transport(self):
        def forbidden(*_, **__):
            raise AssertionError('token or transport must not be called')
        client = TaskClient('https://service.example', forbidden)
        with patch.object(client.transport, 'request', forbidden):
            for key, digest in [(None, 'a' * 64), ('', 'a' * 64), ('x' * 201, 'a' * 64),
                    ('bad\r\nkey', 'a' * 64), ('key', None), ('key', 'a' * 63),
                    ('key', 'A' * 64), ('key', 'g' * 64)]:
                with self.subTest(key=key, digest=digest), self.assertRaises(ValueError):
                    client.lookup(key, digest)

    def test_lookup_rejects_nonminimal_or_invalid_id_responses(self):
        for reply in [None, [], {}, {'id': ''}, {'id': 1}, {'id': 'bad\nvalue'},
                      {'id': 'x' * 201}, {'id': 'task', 'request': 'private'}]:
            with self.subTest(reply=reply):
                class Transport:
                    def request(self, url, *, _reply=reply, **kwargs):
                        return HttpReply(200, json.dumps(_reply).encode())
                client = TaskClient('https://service.example', 'synthetic', transport=Transport())
                with self.assertRaises(ValueError):
                    client.lookup('frozen-key', 'a' * 64)

    def test_github_identity_destination_cannot_be_caller_selected_private_host(self):
        for url in ['https://attacker.example/token', 'http://pipelines.actions.githubusercontent.com/token',
                    'https://pipelines.actions.githubusercontent.com.attacker.example/token']:
            with patch.dict(os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': url, 'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic'}):
                with self.assertRaises(ValueError):
                    github_token('https://service.example/mcp')

    def test_github_audience_is_encoded_and_overrides_old_query_value(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(url)
                return HttpReply(200, b'{"value":"synthetic-token"}')
        with patch.dict(os.environ, {'ACTIONS_ID_TOKEN_REQUEST_URL': 'https://pipelines.actions.githubusercontent.com/token?audience=old',
                                   'ACTIONS_ID_TOKEN_REQUEST_TOKEN': 'synthetic'}):
            self.assertEqual(github_token('https://service.example/mcp', transport=Transport()), 'synthetic-token')
        self.assertNotIn('audience=old', calls[0])
        self.assertIn('audience=https%3A%2F%2Fservice.example%2Fmcp', calls[0])

    def test_wait_returns_unknown_without_resubmitting_and_errors_redact_upstream(self):
        calls = []
        class Transport:
            def request(self, url, **kwargs):
                calls.append(kwargs['method'])
                return HttpReply(200, b'{"status":"outcome_unknown"}')
        client = TaskClient('https://service.example', 'synthetic-token', transport=Transport())
        self.assertEqual(client.wait('task')['status'], 'outcome_unknown')
        self.assertEqual(calls, ['GET'])

    def test_image_response_limit_is_explicit_and_bounded(self):
        client = TaskClient('https://service.example', 'synthetic', max_response_bytes=16_000_000)
        self.assertEqual(client.transport.max_response_bytes, 16_000_000)
        for limit in [True, 0, 16_000_001, 2.5]:
            with self.assertRaises(ValueError):
                TaskClient('https://service.example', 'synthetic', max_response_bytes=limit)


if __name__ == '__main__':
    unittest.main()
