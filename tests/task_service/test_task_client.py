import json
import http.client
import os
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from ai_service.client import USER_AGENT, TaskClient, github_token
from ai_service.transport import HttpReply, TransportError


class ClientTests(unittest.TestCase):
    def test_real_http_get_has_no_entity_body_or_content_length(self):
        # Exercise http.client's wire encoding without a socket or credentials.
        # An empty bytes body emits Content-Length: 0, which the deployed
        # Cloudflare request path rejected; None is an ordinary bodyless GET.
        for label, call in [('lookup', lambda client: client.lookup('original-key', 'a' * 64)),
                            ('get', lambda client: client.get('original-task')),
                            ('events', lambda client: client.request('GET', '/v1/tasks/original-task/events'))]:
            with self.subTest(request=label):
                writes, connections = [], []

                class Wire(http.client.HTTPConnection):
                    def send(self, data):
                        writes.append(data)

                    def getresponse(self):
                        return SimpleNamespace(status=200, read=lambda _limit: b'{"id":"original-task"}')

                def connect(*_args, **_kwargs):
                    connections.append(Wire('service.example'))
                    return connections[-1]

                with patch('ai_service.transport.public_destination',
                           side_effect=lambda url: (urlsplit(url), [(socket.AF_INET, '1.1.1.1')])), \
                        patch('ai_service.transport._PinnedConnection', side_effect=connect):
                    call(TaskClient('https://service.example', 'synthetic-only'))
                request = b''.join(writes)
                header, _, body = request.partition(b'\r\n\r\n')
                self.assertEqual(len(connections), 1)
                self.assertTrue(header.startswith(b'GET /v1/tasks'))
                self.assertNotIn(b'Content-Length:', request)
                self.assertNotIn(b'Transfer-Encoding:', request)
                self.assertIn(b'User-Agent: ' + USER_AGENT.encode(), header)
                self.assertEqual(body, b'')

    def test_real_http_post_keeps_json_entity_and_declares_its_length(self):
        writes = []

        class Wire(http.client.HTTPConnection):
            def send(self, data):
                writes.append(data)

            def getresponse(self):
                return SimpleNamespace(status=200, read=lambda _limit: b'{"status":"queued"}')

        with patch('ai_service.transport.public_destination',
                   side_effect=lambda url: (urlsplit(url), [(socket.AF_INET, '1.1.1.1')])), \
                patch('ai_service.transport._PinnedConnection', return_value=Wire('service.example')):
            client = TaskClient('https://service.example', 'synthetic-only')
            self.assertEqual(client.submit({'mode': 'agent'}, 'synthetic-key'), {'status': 'queued'})
        request = b''.join(writes)
        header, _, body = request.partition(b'\r\n\r\n')
        self.assertTrue(request.startswith(b'POST /v1/tasks HTTP/1.1\r\n'))
        self.assertIn(b'Content-Length: ' + str(len(body)).encode(), header)
        self.assertNotIn(b'Transfer-Encoding:', request)
        self.assertIn(b'Content-Type: application/json', header)
        self.assertIn(b'Idempotency-Key: synthetic-key', header)
        self.assertIn(b'User-Agent: ' + USER_AGENT.encode(), header)
        self.assertEqual(json.loads(body), {'mode': 'agent'})

    def test_real_http_lost_reply_is_not_retried(self):
        attempts, writes = [], []

        class Wire(http.client.HTTPConnection):
            def send(self, data):
                writes.append(data)

            def getresponse(self):
                raise TimeoutError('synthetic lost reply')

        def connect(*_args, **_kwargs):
            attempts.append(1)
            return Wire('service.example')

        with patch('ai_service.transport.public_destination',
                   side_effect=lambda url: (urlsplit(url), [(socket.AF_INET, '1.1.1.1')])), \
                patch('ai_service.transport._PinnedConnection', side_effect=connect):
            with self.assertRaises(TransportError):
                TaskClient('https://service.example', 'synthetic-only').lookup('original-key', 'a' * 64)
        self.assertEqual(attempts, [1])
        self.assertEqual(len(writes), 1)

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
        self.assertIsNone(arguments['body'])
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
