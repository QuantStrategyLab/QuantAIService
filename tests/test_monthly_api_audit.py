import json
import unittest
from unittest.mock import Mock

from ai_service.transport import HttpReply
from scripts import monthly_api_audit as audit


ENV = {
    'OPENAI_MODEL': 'gpt-synthetic', 'OPENAI_API_KEY': 'synthetic-openai-key',
    'ANTHROPIC_MODEL': 'claude-synthetic', 'ANTHROPIC_API_KEY': 'synthetic-anthropic-key',
}


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
                transport.request.return_value = HttpReply(200, json.dumps(receipt).encode())
                result = audit.review(name, audit.PROBE, ENV, transport, probe=True)
                self.assertEqual(result, {'status': 'completed', 'model': receipt['model'],
                                          'model_verification': 'provider_reported'})
                transport.request.assert_called_once()
                call = transport.request.call_args
                self.assertEqual(call.args[0], audit.PROVIDERS[name][0])
                self.assertIn(audit.PROBE, call.kwargs['body'].decode())
                if name == 'openai':
                    self.assertIs(json.loads(call.kwargs['body'])['store'], False)

    def test_http_and_model_failures_do_not_return_provider_content(self):
        transport = Mock()
        transport.request.side_effect = [
            HttpReply(429, b'provider diagnostic'),
            HttpReply(200, b'{"model":"wrong-model"}'),
        ]
        self.assertEqual(audit.review('openai', audit.PROBE, ENV, transport),
                         {'status': 'provider_rejected', 'http_status': 429})
        self.assertEqual(audit.review('anthropic', audit.PROBE, ENV, transport),
                         {'status': 'model_mismatch'})
        self.assertEqual(transport.request.call_count, 2)

    def test_claude_probe_rejects_swapped_counts(self):
        reply = {'model': ENV['ANTHROPIC_MODEL'], 'stop_reason': 'end_turn',
                 'content': [{'type': 'text', 'text': '{"expected":1,"observed":2}'}]}
        transport = Mock()
        transport.request.return_value = HttpReply(200, json.dumps(reply).encode())
        self.assertEqual(audit.review('anthropic', audit.PROBE, ENV, transport, probe=True),
                         {'status': 'invalid_output'})

    def test_missing_credentials_make_no_request(self):
        transport = Mock()
        self.assertEqual(audit.review('openai', audit.PROBE, {}, transport),
                         {'status': 'configuration_missing'})
        transport.request.assert_not_called()
