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
EXAMPLE = ROOT / 'config' / 'quant.example.json'
REQUIRED_SCENARIOS = {
    'pr_code_review',
    'strategy_research_summary',
    'reconciliation_anomaly',
    'signal_context',
    'quick_notification',
}


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
        self.assertEqual(review['providers'], ['dot', 'muse', 'grokbot'])
        self.assertEqual(review['tier'], 'complex')
        self.assertEqual(review['selections']['dot']['model'], 'configured-dot-strong')
        self.assertEqual(review['selections']['dot']['reasoning_effort'], 'high')
        self.assertEqual(review['selections']['muse']['model'], 'configured-muse-strong')
        self.assertEqual(review['selections']['muse']['reasoning_effort'], 'high')

        note = effective_route(self.routing, 'quick_notification')
        self.assertEqual(note['mode'], 'api')
        self.assertEqual(note['providers'], ['gemini'])
        self.assertEqual(note['tier'], 'simple')
        self.assertEqual(note['selections']['gemini']['reasoning_effort'], 'low')

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


if __name__ == '__main__':
    unittest.main()
