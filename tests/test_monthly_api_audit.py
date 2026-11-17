import json
import unittest
from unittest.mock import Mock

from ai_service.transport import HttpReply, TransportError
from scripts import monthly_api_audit as audit


ENV = {
    'OPENAI_API_KEY': 'synthetic-openai-key',
    'ANTHROPIC_API_KEY': 'synthetic-anthropic-key',
}


def metadata(model):
    return HttpReply(200, json.dumps({'id': model}).encode())


def review(name, transport, candidates=None, env=ENV, probe=False):
    if candidates is None:
        candidates = ['gpt-synthetic' if name == 'openai' else 'claude-synthetic']
    return audit.review(name, audit.PROBE, env, transport, candidates=candidates, tier='simple', probe=probe)


class MonthlyApiAuditTests(unittest.TestCase):
    def test_probe_checks_model_and_does_not_export_the_output(self):
        for name, receipt in (
            ('openai', {'model': 'gpt-synthetic-2026-11-01', 'status': 'completed',
                        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'AUDIT_PROBE_OK'}]}]}),
            ('anthropic', {'model': 'claude-synthetic', 'stop_reason': 'end_turn',
                           'content': [{'type': 'text', 'text': '{"expected":2,"observed":1}'}]}),
        ):
            with self.subTest(provider=name):
                transport = Mock()
                transport.request.side_effect = [metadata(receipt['model']), HttpReply(200, json.dumps(receipt).encode())]
                result = review(name, transport, probe=True)
                self.assertEqual(result, {'status': 'completed', 'model': receipt['model'],
                                          'model_verification': 'provider_reported', 'selection': 'account_catalog'})
                self.assertEqual(transport.request.call_args_list[0].kwargs['method'], 'GET')
                call = transport.request.call_args
                self.assertEqual(call.args[0], audit.PROVIDERS[name][0])
                self.assertIn(audit.PROBE, call.kwargs['body'].decode())
                if name == 'openai':
                    self.assertIs(json.loads(call.kwargs['body'])['store'], False)

    def test_http_and_model_failures_do_not_return_provider_content(self):
        transport = Mock()
        transport.request.side_effect = [
            metadata('gpt-synthetic'), HttpReply(429, b'provider diagnostic'),
            metadata('claude-synthetic'), HttpReply(200, b'{"model":"wrong-model"}'),
        ]
        self.assertEqual(review('openai', transport),
                         {'status': 'provider_rejected', 'http_status': 429, 'selection': 'account_catalog'})
        self.assertEqual(review('anthropic', transport),
                         {'status': 'model_mismatch', 'selection': 'account_catalog'})
        self.assertEqual(transport.request.call_count, 4)

    def test_claude_probe_rejects_swapped_counts(self):
        reply = {'model': 'claude-synthetic', 'stop_reason': 'end_turn',
                 'content': [{'type': 'text', 'text': '{"expected":1,"observed":2}'}]}
        transport = Mock()
        transport.request.side_effect = [metadata(reply['model']), HttpReply(200, json.dumps(reply).encode())]
        self.assertEqual(review('anthropic', transport, probe=True),
                         {'status': 'invalid_output', 'selection': 'account_catalog'})

    def test_missing_credentials_make_no_request(self):
        transport = Mock()
        self.assertEqual(review('openai', transport, env={}),
                         {'status': 'configuration_missing'})
        transport.request.assert_not_called()

    def test_catalog_checks_approved_models_in_policy_order(self):
        transport = Mock()
        transport.request.side_effect = [
            HttpReply(404, b'{"error":{"type":"not_found_error"}}'),
            metadata('claude-current'),
            HttpReply(200, b'{"model":"claude-current","stop_reason":"end_turn","content":[{"type":"text","text":"ok"}]}'),
        ]
        result = review('anthropic', transport, candidates=['claude-retired', 'claude-current'])
        self.assertEqual(result['model'], 'claude-current')
        calls = transport.request.call_args_list
        self.assertEqual([call.args[0] for call in calls], [
            'https://api.anthropic.com/v1/models/claude-retired',
            'https://api.anthropic.com/v1/models/claude-current', audit.PROVIDERS['anthropic'][0],
        ])
        self.assertEqual([call.kwargs.get('method', 'POST') for call in calls], ['GET', 'GET', 'POST'])

    def test_responses_only_key_advances_only_after_model_rejection(self):
        transport = Mock()
        transport.request.side_effect = [
            HttpReply(403, b'{}'), HttpReply(404, b'{"error":{"code":"model_not_found"}}'),
            TransportError('ambiguous'),
        ]
        result = review('openai', transport, candidates=['old', 'current', 'another'])
        self.assertEqual(result['status'], 'transport_failed')
        calls = transport.request.call_args_list
        self.assertEqual([json.loads(call.kwargs['body'])['model'] for call in calls[1:]], ['old', 'current'])

    def test_catalog_failure_sends_no_inference(self):
        for response in [HttpReply(401, b'{}'), HttpReply(429, b'{}'), metadata('different')]:
            with self.subTest(response=response):
                transport = Mock()
                transport.request.return_value = response
                self.assertNotEqual(review('anthropic', transport, ['a', 'b'])['status'], 'completed')
                transport.request.assert_called_once()

    def test_real_monthly_starts_at_standard_and_raises_on_utf8_bytes(self):
        self.assertEqual(audit.evidence_tier(audit.PROBE), 'standard')
        self.assertEqual(audit.evidence_tier('two source reviews'), 'standard')
        self.assertEqual(audit.evidence_tier('研' * 17000), 'complex')
