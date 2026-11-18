"""Quant task.routing.v1 data plugs into the shared registry contract."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from quant_ai_service.server import validate_configuration
from quant_ai_service.task_routing import (
    SCHEMA_VERSION,
    RoutingError,
    effective_route,
    load_routing,
    mask_account_ids,
    resolve_complexity,
    validate_project_routing,
    validate_routing,
)


ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / 'tests' / 'fixtures' / 'legacy-provider-config.json'
REQUIRED_SCENARIOS = {
    'pr_code_review',
    'strategy_research_summary',
    'reconciliation_anomaly',
    'signal_context',
    'quick_notification',
}



class SharedComplexityContractTests(unittest.TestCase):
    def test_shared_complexity_cases(self):
        fixtures = ROOT / 'tests' / 'fixtures' / 'routing-complexity-cases.json'
        registry = next(iter(json.loads(EXAMPLE.read_text())['projects'].values()))['routing']
        for sample in json.loads(fixtures.read_text()):
            with self.subTest(case=sample['name']):
                routing = {**registry, 'scenarios': {
                    'shared_probe': {'mode': 'agent', 'providers': ['muse'], 'matching': sample['matching']},
                }}
                payload = {'objective': sample['text'] * sample['repeat'], 'materials': sample['materials']}
                actual = resolve_complexity(routing, 'shared_probe', requested=sample['requested'],
                                          material_bytes=len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')),
                                          material_count=len(payload['materials']), has_image=bool(payload['materials'] and payload['materials'][0].get('type') == 'image'))
                self.assertEqual(actual, sample['expected'])


class MaskAccountIdTests(unittest.TestCase):
    def test_masks_account_like_tokens(self):
        text = 'acct_fake_001 cloud 123456789012'
        masked = mask_account_ids(text)
        self.assertNotIn('acct_fake_001', masked)
        self.assertNotIn('123456789012', masked)
        self.assertIn('[redacted-account]', masked)


class TaskRoutingDataTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        self.project = next(iter(self.config['projects'].values()))
        self.routing = self.project['routing']

    def test_example_routing_uses_task_routing_v1(self):
        data = load_routing(self.routing)
        self.assertEqual(data['schema_version'], SCHEMA_VERSION)
        self.assertEqual(set(data['scenarios']), REQUIRED_SCENARIOS)
        self.assertEqual(set(data['complexity_tiers']), {'simple', 'standard', 'complex'})
        self.assertIn('heuristics', data)

    def test_validate_against_project_policy_shape(self):
        validate_project_routing(self.routing, self.project)
        validate_configuration(self.config)

    def test_category_defaults_and_tier_overrides(self):
        review = effective_route(self.routing, 'pr_code_review')
        self.assertEqual(review['category'], 'quant_review')
        self.assertEqual(review['mode'], 'agent')
        self.assertEqual(review['providers'], ['muse', 'grokbot', 'dot'])
        self.assertEqual(review['tier'], 'complex')
        self.assertEqual(review['selections']['dot']['model'], 'configured-dot-strong')
        self.assertEqual(review['selections']['dot']['reasoning_effort'], 'high')
        self.assertEqual(review['selections']['muse']['model'], 'configured-muse-strong')
        self.assertEqual(review['selections']['muse']['reasoning_effort'], 'high')

        note = effective_route(self.routing, 'quick_notification')
        self.assertEqual(note['mode'], 'api')
        self.assertEqual(note['providers'], ['chatgpt-plan'])
        self.assertEqual(note['tier'], 'simple')
        self.assertEqual(note['selections']['chatgpt-plan']['reasoning_effort'], 'low')
        self.assertEqual(note['selections']['chatgpt-plan']['model'], 'configured-chatgpt-plan-fast')

    def test_complexity_raises_only_from_matching_and_heuristics(self):
        self.assertEqual(resolve_complexity(self.routing, 'strategy_research_summary'), 'standard')
        self.assertEqual(
            resolve_complexity(self.routing, 'strategy_research_summary', material_bytes=60000),
            'complex',
        )
        self.assertEqual(
            resolve_complexity(
                self.routing, 'pr_code_review', requested='simple', has_image=True,
            ),
            'complex',
        )

    def test_new_category_and_scenario_are_config_only(self):
        routing = copy.deepcopy(self.routing)
        routing['categories']['quant_ledger'] = {
            'parent': 'quant',
            'default_complexity': 'standard',
        }
        routing['scenarios']['ledger_spot_check'] = {
            'category': 'quant_ledger',
            'mode': 'agent',
            'providers': ['dot'],
            'matching': {'hints': ['ledger', 'spot-check']},
        }
        validate_project_routing(routing, self.project)
        route = effective_route(routing, 'ledger_spot_check')
        self.assertEqual(route['category'], 'quant_ledger')
        self.assertEqual(route['tier'], 'standard')
        self.assertEqual(route['selections']['dot']['reasoning_effort'], 'medium')

    def test_rejects_unknown_schema_parent_or_unadmitted_provider(self):
        bad = copy.deepcopy(self.routing)
        bad['schema_version'] = 'personal-ai.scenario-registry.extension.v1'
        with self.assertRaises(RoutingError):
            validate_routing(bad)
        bad = copy.deepcopy(self.routing)
        bad['categories']['quant_review']['parent'] = 'missing'
        with self.assertRaises(RoutingError):
            validate_routing(bad)
        bad = copy.deepcopy(self.routing)
        bad['scenarios']['pr_code_review']['providers'] = ['dot', 'missing']
        with self.assertRaises(RoutingError):
            validate_project_routing(bad, self.project)

    def test_account_ids_masked_on_effective_route(self):
        route = effective_route(self.routing, 'signal_context', account_id='acct_fake_001')
        self.assertEqual(route['account_id'], '[redacted-account]')
        self.assertNotIn('acct_fake_001', json.dumps(route))

    def test_module_does_not_hardcode_quant_scenario_ids(self):
        source = (ROOT / 'src' / 'quant_ai_service' / 'task_routing.py').read_text(encoding='utf-8')
        for name in REQUIRED_SCENARIOS:
            self.assertNotIn(f"'{name}'", source)
            self.assertNotIn(f'"{name}"', source)


class RuntimeAdaptiveRoutingTests(unittest.TestCase):
    def test_auto_mode_and_runtime_parity(self):
        import os
        import tempfile
        from unittest.mock import patch

        from ai_service.gemini import matches_model
        from ai_service.server import build_application
        from ai_service.transport import HttpReply

        self.assertTrue(matches_model('gpt-5.4-mini', 'gpt-5.4-mini-2026-03-17'))
        self.assertTrue(matches_model('claude-sonnet-4-6', 'claude-sonnet-4-6-20260514'))

        schema = {
            'type': 'object',
            'properties': {'summary': {'type': 'string'}},
            'required': ['summary'],
            'additionalProperties': False,
        }
        with tempfile.TemporaryDirectory() as tmp:
            cfg = {
                'enabled': True,
                'owner': 'example-org',
                'resource': 'https://service.example/mcp',
                'issuers': {},
                'grants': [],
                'state_path': str(Path(tmp) / 'tasks.sqlite'),
                'providers': {
                    'muse': {'type': 'muse', 'enabled': True},
                    'openai': {
                        'type': 'openai_api', 'enabled': True, 'api_usage_authorized': True,
                        'model': 'gpt-5.4-mini', 'key_env': 'OPENAI_KEY',
                        'tier_models': {'simple': ['gpt-5.4-mini'], 'standard': ['gpt-5.4-mini', 'gpt-5.4-mini-alt']},
                    },
                    'claude': {
                        'type': 'anthropic_api', 'enabled': True, 'api_usage_authorized': True,
                        'model': 'claude-sonnet-4-6', 'key_env': 'ANTHROPIC_KEY',
                    },
                },
                'projects': {
                    'example-org/QuantRepo': {
                        'providers': {'agent': 'muse', 'api': 'openai', 'api:claude': 'claude'},
                        'models': {
                            'muse': ['configured-muse-fast', 'configured-muse', 'configured-muse-strong'],
                            'openai': ['gpt-5.4-mini', 'gpt-5.4-mini-alt', 'gpt-5.4'],
                            'claude': ['claude-haiku-4-5', 'claude-sonnet-4-6', 'claude-opus-4-6'],
                        },
                        'routing': {
                            'schema_version': 'task.routing.v1',
                            'categories': {'quant_research': {'parent': None, 'default_complexity': 'standard'}},
                            'scenarios': {
                                'strategy_research_summary': {
                                    'category': 'quant_research',
                                    'mode': 'auto',
                                    'providers': ['openai', 'claude', 'muse'],
                                },
                            },
                            'complexity_tiers': {
                                'simple': {
                                    'openai': {'model': 'gpt-5.4-mini', 'reasoning_effort': 'low'},
                                    'claude': {'model': 'claude-haiku-4-5', 'reasoning_effort': 'low'},
                                    'muse': {'model': 'configured-muse-fast', 'reasoning_effort': 'low'},
                                },
                                'standard': {
                                    'openai': {'model': 'gpt-5.4-mini', 'reasoning_effort': 'medium'},
                                    'claude': {'model': 'claude-sonnet-4-6', 'reasoning_effort': 'medium'},
                                    'muse': {'model': 'configured-muse', 'reasoning_effort': 'medium'},
                                },
                                'complex': {
                                    'openai': {'model': 'gpt-5.4', 'reasoning_effort': 'high'},
                                    'claude': {'model': 'claude-opus-4-6', 'reasoning_effort': 'high'},
                                    'muse': {'model': 'configured-muse-strong', 'reasoning_effort': 'high'},
                                },
                            },
                        },
                    },
                },
            }

            class MockTransport:
                def request(self, url, **_kwargs):
                    if url.endswith('/gpt-5.4-mini'):
                        return HttpReply(404, b'')
                    if url.endswith('/gpt-5.4-mini-alt'):
                        return HttpReply(200, json.dumps({'id': 'gpt-5.4-mini-alt'}).encode())
                    return HttpReply(200, json.dumps({
                        'id': 'resp_q', 'status': 'completed', 'model': 'gpt-5.4-mini-alt-2026-03-17',
                        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"summary":"ok"}'}]}],
                    }).encode())

            with patch.dict(os.environ, {'OPENAI_KEY': 'sk-test', 'ANTHROPIC_KEY': 'ak-test'}):
                app = build_application(cfg)
                app.runtime.providers['openai']._transport = MockTransport()
                app.event_buses['muse'].has_active = lambda owner: owner == 'example-org/QuantRepo'

                api_task = app.runtime.submit('example-org/QuantRepo', 'q-api', {
                    'scenario': 'strategy_research_summary',
                    'objective': 'summarize daily drift',
                    'output_schema': schema,
                })
                self.assertEqual(api_task['provider'], 'openai')
                self.assertEqual(api_task['request']['mode'], 'api')
                self.assertEqual(api_task['request']['model'], 'gpt-5.4-mini-alt')
                app.runtime.tick()
                app.runtime.tick()
                self.assertEqual(app.runtime.get('example-org/QuantRepo', api_task['id'])['status'], 'completed')

                agent_task = app.runtime.submit('example-org/QuantRepo', 'q-agent', {
                    'mode': 'auto',
                    'scenario': 'strategy_research_summary',
                    'complexity': 'complex',
                    'objective': 'deep backtest review',
                    'output_schema': schema,
                })
                self.assertEqual(agent_task['provider'], 'muse')
                self.assertEqual(agent_task['request']['mode'], 'agent')
                self.assertEqual(agent_task['request']['model'], 'configured-muse-strong')

                with self.assertRaises(PermissionError):
                    app.runtime.submit('example-org/QuantRepo', 'q-bad', {
                        'mode': 'api',
                        'scenario': 'strategy_research_summary',
                        'complexity': 'complex',
                        'objective': 'mismatch mode',
                        'output_schema': schema,
                    })


if __name__ == '__main__':
    unittest.main()
