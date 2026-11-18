"""Unit tests for OpenAI and Anthropic structured-output API providers and wiring."""
from __future__ import annotations

import base64
import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_service.inference import AnthropicAPIProvider, OpenAIAPIProvider
from ai_service.runtime import Policy, SubmissionRejected, TaskRuntime
from ai_service.server import build_application
from ai_service.transport import HttpReply

_PNG_1X1 = base64.b64encode(
    b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4'
    b'\x00\x00\x00\rIDATx\x9cc```\x00\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82'
).decode()


class OpenAIAPIProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = {
            'mode': 'api',
            'model': 'gpt-5-mini-test',
            'objective': 'Audit report',
            'materials': [{'item': 'alpha'}],
            'output_schema': {
                'type': 'object',
                'properties': {'summary': {'type': 'string'}},
                'required': ['summary'],
                'additionalProperties': False,
            },
        }
        self.response = {
            'id': 'resp_123',
            'status': 'completed',
            'model': 'gpt-5-mini-test',
            'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"summary":"ok"}'}]}],
            'usage': {'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18},
        }
        self.calls = []

    def runtime(self, reply_body, *, status=200, key='synthetic-openai-key'):
        class Transport:
            def request(inner, url, **kwargs):
                self.calls.append((url, kwargs))
                return HttpReply(status, reply_body)

        provider = OpenAIAPIProvider(
            lambda: key, model='gpt-5-mini-test', api_usage_authorized=True, transport=Transport(),
        )
        return TaskRuntime(
            Path(self.tmp.name) / 'openai.sqlite', {'openai': provider},
            {'owner/repo': Policy({'api': 'openai'}, {'openai': {'gpt-5-mini-test'}})},
        )

    def run_task(self, runtime, request=None, key='k1'):
        task = runtime.submit('owner/repo', key, request or self.request)
        runtime.tick()
        runtime.tick()
        return runtime.get('owner/repo', task['id'])

    def test_requires_authorization_fixed_model_and_bounds(self):
        with self.assertRaises(ValueError):
            OpenAIAPIProvider(lambda: 'k', model='gpt-5-mini-test')
        for kwargs in (
            {'model': ''},
            {'model': 'bad model'},
            {'max_output_tokens': 0},
            {'max_output_tokens': 32769},
            {'max_output_tokens': True},
            {'timeout_seconds': 0},
            {'timeout_seconds': 181},
        ):
            kw = {'model': 'gpt-5-mini-test', 'api_usage_authorized': True, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                OpenAIAPIProvider(lambda: 'k', **kw)
        OpenAIAPIProvider(lambda: 'k', model='gpt-5-mini-test', api_usage_authorized=True,
                          max_output_tokens=32768, timeout_seconds=180)

    def test_json_response_images_effort_usage_and_store_false(self):
        runtime = self.runtime(json.dumps(self.response).encode())
        req = {
            **self.request,
            'materials': [
                {'item': 'alpha'},
                {'type': 'image', 'mime_type': 'image/png', 'base64': _PNG_1X1},
            ],
        }
        record = self.run_task(runtime, req, key='k-json')
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['output'], {'summary': 'ok'})
        self.assertEqual(record['handle']['model_verification'], 'provider_reported')
        self.assertEqual(record['handle']['usage'], {'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18})
        url, kwargs = self.calls[0]
        self.assertEqual(url, 'https://api.openai.com/v1/responses')
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-openai-key')
        payload = json.loads(kwargs['body'])
        self.assertFalse(payload['store'])
        self.assertEqual(payload['input'][0]['content'][2]['type'], 'input_image')

        routed_reply = runtime.providers['openai'].submit(
            {**req, 'reasoning_effort': 'medium'}, task_id='t-effort', owner='owner/repo',
        )
        self.assertEqual(routed_reply['status'], 'completed')
        self.assertEqual(json.loads(self.calls[1][1]['body'])['reasoning'], {'effort': 'medium'})

    def test_sse_stream_response_and_error_paths(self):
        sse = (
            'data: {"type":"response.output_text.delta","delta":"{"}\n\n'
            + 'data: ' + json.dumps({'type': 'response.completed', 'response': self.response}) + '\n\n'
            + 'data: [DONE]\n\n'
        ).encode()
        runtime = self.runtime(sse)
        self.assertEqual(self.run_task(runtime, key='k-sse')['output'], {'summary': 'ok'})
        self.assertEqual(self.run_task(self.runtime(b'err', status=503), key='k-503')['status'], 'outcome_unknown')
        self.assertEqual(self.run_task(self.runtime(b'limit', status=429), key='k-429')['status'], 'failed')

    def test_rejects_oversized_or_too_many_images_before_network(self):
        provider = OpenAIAPIProvider(lambda: 'k', model='gpt-5-mini-test', api_usage_authorized=True)
        oversized = {'type': 'image', 'mime_type': 'image/png', 'base64': 'A' * 300_004}
        with self.assertRaises(SubmissionRejected):
            provider.submit({**self.request, 'materials': [oversized]}, task_id='t1', owner='owner/repo')
        four_images = [{'type': 'image', 'mime_type': 'image/png', 'base64': _PNG_1X1}] * 4
        with self.assertRaises(SubmissionRejected):
            provider.submit({**self.request, 'materials': four_images}, task_id='t2', owner='owner/repo')


class AnthropicAPIProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.request = {
            'mode': 'api',
            'model': 'claude-sonnet-test',
            'objective': 'Translate document',
            'materials': [{'text': 'hello'}],
            'output_schema': {
                'type': 'object',
                'properties': {'translated': {'type': 'string'}},
                'required': ['translated'],
                'additionalProperties': False,
            },
        }
        self.response = {
            'id': 'msg_123',
            'model': 'claude-sonnet-test',
            'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'id': 'toolu_1', 'name': 'record_result', 'input': {'translated': '你好'}}],
            'usage': {'input_tokens': 14, 'output_tokens': 6},
        }
        self.calls = []

    def runtime(self, reply_body, *, status=200):
        class Transport:
            def request(inner, url, **kwargs):
                self.calls.append((url, kwargs))
                return HttpReply(status, reply_body)

        provider = AnthropicAPIProvider(
            lambda: 'synthetic-anthropic-key', model='claude-sonnet-test',
            api_usage_authorized=True, transport=Transport(),
        )
        return TaskRuntime(
            Path(self.tmp.name) / 'claude.sqlite', {'claude': provider},
            {'owner/repo': Policy({'api': 'claude'}, {'claude': {'claude-sonnet-test'}})},
        )

    def test_tool_choice_structured_output_image_and_wiring(self):
        runtime = self.runtime(json.dumps(self.response).encode())
        req = {
            **self.request,
            'materials': [{'type': 'image', 'mime_type': 'image/png', 'base64': _PNG_1X1}],
        }
        task = runtime.submit('owner/repo', 'k1', req)
        runtime.tick()
        runtime.tick()
        record = runtime.get('owner/repo', task['id'])
        self.assertEqual(record['status'], 'completed')
        self.assertEqual(record['output'], {'translated': '你好'})
        self.assertEqual(record['handle']['model_verification'], 'provider_reported')
        self.assertEqual(record['handle']['usage'], {'input_tokens': 14, 'output_tokens': 6})
        url, kwargs = self.calls[0]
        self.assertEqual(url, 'https://api.anthropic.com/v1/messages')
        self.assertEqual(kwargs['headers']['x-api-key'], 'synthetic-anthropic-key')
        self.assertEqual(kwargs['headers']['anthropic-version'], '2023-06-01')
        self.assertNotIn('output_config', json.loads(kwargs['body']))

        routed_reply = runtime.providers['claude'].submit(
            {**req, 'reasoning_effort': 'high'}, task_id='t-effort', owner='owner/repo',
        )
        self.assertEqual(routed_reply['status'], 'completed')
        self.assertEqual(json.loads(self.calls[1][1]['body'])['output_config'], {'effort': 'high'})
        with self.assertRaises(SubmissionRejected):
            runtime.providers['claude'].submit(
                {**req, 'reasoning_effort': 'xhigh'}, task_id='t-bad-effort', owner='owner/repo',
            )

        with tempfile.TemporaryDirectory() as d:
            cfg = {
                'enabled': True,
                'resource': 'https://service.example/mcp',
                'issuers': {},
                'grants': [],
                'state_path': str(Path(d) / 'tasks.sqlite'),
                'providers': {
                    'muse': {'type': 'muse', 'enabled': True},
                    'grokbot': {'type': 'grok_webhook', 'enabled': False},
                    'claudeduet': {'type': 'claudeduet', 'enabled': False},
                    'dot': {'type': 'dot_events', 'enabled': False},
                    'openai': {'type': 'openai_api', 'enabled': True, 'api_usage_authorized': True,
                               'model': 'gpt-5-mini-test', 'key_env': 'TEST_OPENAI_KEY'},
                    'claude': {'type': 'anthropic_api', 'enabled': True, 'api_usage_authorized': True,
                               'model': 'claude-sonnet-test', 'key_env': 'TEST_ANTHROPIC_KEY'},
                },
                'projects': {
                    'owner/repo': {
                        'providers': {'agent': 'muse', 'api': 'openai', 'api:claude': 'claude'},
                        'models': {'muse': ['m'], 'openai': ['gpt-5-mini-test'], 'claude': ['claude-sonnet-test']},
                    },
                },
            }
            with patch.dict(os.environ, {'TEST_OPENAI_KEY': 'ok', 'TEST_ANTHROPIC_KEY': 'ak'}):
                app = build_application(copy.deepcopy(cfg))
            self.assertIn('muse', app.runtime.providers)
            self.assertNotIn('dot', app.runtime.providers)
            self.assertNotIn('grokbot', app.runtime.providers)
            self.assertIsInstance(app.runtime.providers['openai'], OpenAIAPIProvider)
            self.assertIsInstance(app.runtime.providers['claude'], AnthropicAPIProvider)


if __name__ == '__main__':
    unittest.main()
