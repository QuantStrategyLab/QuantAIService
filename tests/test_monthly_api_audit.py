import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from ai_service.transport import HttpReply, TransportError
from scripts import monthly_api_audit as audit


ENV = {
    'OPENAI_API_KEY': 'synthetic-openai-key',
    'ANTHROPIC_API_KEY': 'synthetic-anthropic-key',
    'GEMINI_API_KEY': 'synthetic-gemini-key',
}


def metadata(model):
    return HttpReply(200, json.dumps({'id': model}).encode())


def gemini_metadata(model, methods=('generateContent',)):
    return HttpReply(200, json.dumps({'name': f'models/{model}', 'supportedGenerationMethods': list(methods)}).encode())


def review(name, transport, candidates=None, env=ENV, probe=False):
    if candidates is None:
        if name == 'openai':
            candidates = ['gpt-synthetic']
        elif name == 'gemini':
            candidates = ['gemini-4-hydrogen']
        else:
            candidates = ['claude-synthetic']
    return audit.review(name, audit.PROBE, env, transport, candidates=candidates, tier='simple', probe=probe)


class MonthlyApiAuditTests(unittest.TestCase):
    def test_probe_checks_model_and_does_not_export_the_output(self):
        for name, meta, receipt in (
            ('openai', metadata('gpt-synthetic-2026-11-01'),
             {'model': 'gpt-synthetic-2026-11-01', 'status': 'completed',
              'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'AUDIT_PROBE_OK'}]}]}),
            ('anthropic', metadata('claude-synthetic'),
             {'model': 'claude-synthetic', 'stop_reason': 'end_turn',
              'content': [{'type': 'text', 'text': '{"expected":2,"observed":1}'}]}),
            ('gemini', gemini_metadata('gemini-4-hydrogen'),
             {'modelVersion': 'gemini-4-hydrogen',
              'candidates': [{'finishReason': 'STOP', 'content': {'parts': [
                  {'thought': True, 'text': 'thinking'},
                  {'text': '{"expected":2,"observed":1}'},
              ]}}]}),
        ):
            with self.subTest(provider=name):
                transport = Mock()
                transport.request.side_effect = [meta, HttpReply(200, json.dumps(receipt).encode())]
                result = review(name, transport, probe=True)
                self.assertEqual(result, {'status': 'completed', 'model': receipt.get('model') or receipt['modelVersion'],
                                          'model_verification': 'provider_reported', 'selection': 'account_catalog'})
                self.assertEqual(transport.request.call_args_list[0].kwargs['method'], 'GET')
                call = transport.request.call_args
                expected_url = (audit.PROVIDERS['gemini'][0] + 'gemini-4-hydrogen:generateContent'
                                if name == 'gemini' else audit.PROVIDERS[name][0])
                self.assertEqual(call.args[0], expected_url)
                self.assertIn(audit.PROBE, call.kwargs['body'].decode())
                if name == 'openai':
                    self.assertIs(json.loads(call.kwargs['body'])['store'], False)
                if name == 'gemini':
                    payload = json.loads(call.kwargs['body'])
                    self.assertEqual(payload['generationConfig']['thinkingConfig'], {'thinkingLevel': 'LOW'})
                    self.assertEqual(call.kwargs['headers']['x-goog-api-key'], 'synthetic-gemini-key')

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

    def test_gemini_catalog_and_receipt_guards(self):
        transport = Mock()
        transport.request.side_effect = [
            HttpReply(404, b'{"error":{"status":"NOT_FOUND"}}'),
            gemini_metadata('gemini-2.5-flash', methods=('embedContent',)),
        ]
        self.assertEqual(review('gemini', transport, candidates=['gemini-retired', 'gemini-2.5-flash']),
                         {'status': 'invalid_catalog_receipt'})
        transport2 = Mock()
        transport2.request.side_effect = [
            gemini_metadata('gemini-4-hydrogen'),
            HttpReply(200, b'{"modelVersion":"gemini-4-hydrogen","promptFeedback":{"blockReason":"SAFETY"}}'),
        ]
        self.assertEqual(review('gemini', transport2),
                         {'status': 'incomplete', 'selection': 'account_catalog'})

    def _run_main(self, argv, *, env, transport_side_effect):
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / 'report.json'
            out_path = Path(tmpdir) / 'out.json'
            report_path.write_text(json.dumps({'body': 'evidence payload'}), encoding='utf-8')
            resolved_argv = [arg.replace('{REPORT}', str(report_path)).replace('{OUT}', str(out_path)) for arg in argv]
            mock_transport = Mock()
            mock_transport.request.side_effect = transport_side_effect
            with patch.dict(audit.os.environ, env, clear=True), \
                    patch.object(audit, 'PublicHTTPS', return_value=mock_transport), \
                    patch.object(audit, 'refresh_candidates', side_effect=lambda name, _tier, _pol: ([
                        'gpt-6-luna' if name == 'openai' else 'claude-sonnet-5-5' if name == 'anthropic' else 'gemini-4-hydrogen'
                    ], 'approved_baseline')), \
                    contextlib.redirect_stdout(io.StringIO()):
                rc = audit.main(resolved_argv)
            return rc, json.loads(out_path.read_text(encoding='utf-8')), mock_transport

    def test_report_dual_ai_fallback_without_gemini_key_makes_zero_gemini_calls(self):
        replies = [
            metadata('gpt-6-luna'),
            HttpReply(200, b'{"model":"gpt-6-luna","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"openai ok"}]}]}'),
            metadata('claude-sonnet-5-5'),
            HttpReply(200, b'{"model":"claude-sonnet-5-5","stop_reason":"end_turn","content":[{"type":"text","text":"claude ok"}]}'),
        ]
        rc, doc, transport = self._run_main(
            ['--report', '{REPORT}', '--output', '{OUT}'],
            env={'GITHUB_RUN_ATTEMPT': '1', 'OPENAI_API_KEY': 'sk-o', 'ANTHROPIC_API_KEY': 'sk-a'},
            transport_side_effect=replies,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(doc['audit_mode'], 'dual_ai')
        self.assertEqual(doc['arbitration_policy'], 'union_of_blockers_no_majority_override')
        self.assertTrue(doc['complete'])
        self.assertFalse(doc['quality_accepted'])
        self.assertEqual(doc['reviews']['gemini'], {'status': 'skipped_unconfigured', 'optional': True})
        self.assertEqual(transport.request.call_count, 4)

    def test_report_triple_ai_and_union_of_blockers_no_majority_override(self):
        replies = [
            metadata('gpt-6-luna'),
            HttpReply(200, b'{"model":"gpt-6-luna","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"openai ok"}]}]}'),
            metadata('claude-sonnet-5-5'),
            HttpReply(200, b'{"model":"claude-sonnet-5-5","stop_reason":"end_turn","content":[{"type":"text","text":"claude ok"}]}'),
            gemini_metadata('gemini-4-hydrogen'),
            HttpReply(503, b'gemini down'),
        ]
        rc, doc, transport = self._run_main(
            ['--report', '{REPORT}', '--output', '{OUT}'],
            env={'GITHUB_RUN_ATTEMPT': '1', 'OPENAI_API_KEY': 'sk-o', 'ANTHROPIC_API_KEY': 'sk-a', 'GEMINI_API_KEY': 'sk-g'},
            transport_side_effect=replies,
        )
        self.assertEqual(rc, 1)
        self.assertEqual(doc['audit_mode'], 'triple_ai')
        self.assertEqual(doc['arbitration_policy'], 'union_of_blockers_no_majority_override')
        self.assertFalse(doc['complete'])
        self.assertEqual(doc['reviews']['gemini']['status'], 'provider_rejected')
        self.assertEqual(transport.request.call_count, 6)

    def test_probe_provider_gemini_and_all(self):
        rc_gem, doc_gem, transport_gem = self._run_main(
            ['--probe', '--probe-provider', 'gemini', '--output', '{OUT}'],
            env={'GITHUB_RUN_ATTEMPT': '1', 'GEMINI_API_KEY': 'sk-g'},
            transport_side_effect=[
                gemini_metadata('gemini-4-hydrogen'),
                HttpReply(200, b'{"modelVersion":"gemini-4-hydrogen","candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"{\\"expected\\":2,\\"observed\\":1}"}]}}]}'),
            ],
        )
        self.assertEqual(rc_gem, 0)
        self.assertEqual(set(doc_gem['reviews']), {'gemini'})
        self.assertEqual(doc_gem['audit_mode'], 'single_provider')
        self.assertEqual(transport_gem.request.call_count, 2)

        rc_all, doc_all, transport_all = self._run_main(
            ['--probe', '--probe-provider', 'all', '--output', '{OUT}'],
            env={'GITHUB_RUN_ATTEMPT': '1', 'OPENAI_API_KEY': 'sk-o', 'ANTHROPIC_API_KEY': 'sk-a', 'GEMINI_API_KEY': 'sk-g'},
            transport_side_effect=[
                metadata('gpt-6-luna'),
                HttpReply(200, b'{"model":"gpt-6-luna","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"AUDIT_PROBE_OK"}]}]}'),
                metadata('claude-sonnet-5-5'),
                HttpReply(200, b'{"model":"claude-sonnet-5-5","stop_reason":"end_turn","content":[{"type":"text","text":"{\\"expected\\":2,\\"observed\\":1}"}]}'),
                gemini_metadata('gemini-4-hydrogen'),
                HttpReply(200, b'{"modelVersion":"gemini-4-hydrogen","candidates":[{"finishReason":"STOP","content":{"parts":[{"text":"{\\"expected\\":2,\\"observed\\":1}"}]}}]}'),
            ],
        )
        self.assertEqual(rc_all, 0)
        self.assertEqual(doc_all['audit_mode'], 'triple_ai')
        self.assertEqual(set(doc_all['reviews']), {'openai', 'anthropic', 'gemini'})
        self.assertEqual(transport_all.request.call_count, 6)
