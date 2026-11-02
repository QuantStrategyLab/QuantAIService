"""Quant scenario registry is config data for the shared extension contract."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from quant_ai_service.scenario_registry import (
    EXTENSION_SCHEMA_VERSION,
    RegistryError,
    effective_scenario,
    load_registry,
    mask_account_ids,
    validate_registry_document,
)


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / 'config' / 'quant-scenario-registry.example.json'
REQUIRED_SCENARIOS = {
    'pr_code_review',
    'strategy_research_summary',
    'reconciliation_anomaly',
    'signal_context',
    'quick_notification',
}


class MaskAccountIdTests(unittest.TestCase):
    def test_masks_account_like_tokens_for_logs(self):
        text = 'acct_fake_001 cloud 123456789012'
        masked = mask_account_ids(text)
        self.assertNotIn('acct_fake_001', masked)
        self.assertNotIn('123456789012', masked)
        self.assertIn('[redacted-account]', masked)

    def test_leaves_ordinary_project_and_provider_names(self):
        text = 'example-org/ExampleResearch agent:grokbot'
        self.assertEqual(mask_account_ids(text), text)


class RegistryDataTests(unittest.TestCase):
    def test_example_loads_as_extension_contract_data(self):
        registry = load_registry(EXAMPLE)
        self.assertEqual(registry['schema_version'], EXTENSION_SCHEMA_VERSION)
        self.assertIn('assumption', registry)
        self.assertEqual(set(registry['scenarios']), REQUIRED_SCENARIOS)
        self.assertEqual(set(registry['complexity_tiers']), {'simple', 'standard', 'complex'})
        self.assertTrue(registry['categories'])
        # No Python defaults: the example file is the source of truth.
        self.assertNotIn('acct_', json.dumps(registry))

    def test_category_parents_form_a_tree(self):
        registry = load_registry(EXAMPLE)
        for name, category in registry['categories'].items():
            parent = category['parent']
            if parent is None:
                continue
            self.assertIn(parent, registry['categories'], msg=name)
            self.assertNotEqual(parent, name)

    def test_effective_scenario_inherits_category_without_hardcoded_names(self):
        registry = load_registry(EXAMPLE)
        route = effective_scenario(registry, 'quick_notification')
        self.assertEqual(route['scenario'], 'quick_notification')
        self.assertEqual(route['category'], 'quant.notify')
        self.assertEqual(route['provider_preference'], ['api', 'dot', 'grokbot'])
        self.assertEqual(route['default_complexity'], 'simple')
        self.assertEqual(route['reasoning_effort'], 'low')
        self.assertEqual(route['model_profile'], 'cheap')
        self.assertEqual(route['models']['api'], 'configured-api-fast')

        review = effective_scenario(registry, 'pr_code_review')
        self.assertEqual(review['default_complexity'], 'complex')
        self.assertEqual(review['reasoning_effort'], 'high')
        self.assertEqual(review['model_profile'], 'strongest')
        self.assertEqual(review['provider_preference'], ['dot', 'grokbot', 'api'])

    def test_new_category_and_scenario_are_config_only(self):
        raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        raw['categories']['quant.ledger'] = {
            'parent': 'quant',
            'label': 'Ledger checks',
            'matching_hints': ['ledger', 'posting'],
            'provider_preference': ['dot', 'api'],
            'tier_overrides': {'default_complexity': 'standard'},
        }
        raw['scenarios']['ledger_spot_check'] = {
            'category': 'quant.ledger',
            'label': 'Ledger spot check',
            'matching_hints': ['spot-check', 'ledger-diff'],
            'provider_preference': [],
            'tier_overrides': {},
        }
        registry = validate_registry_document(raw)
        route = effective_scenario(registry, 'ledger_spot_check')
        self.assertEqual(route['category'], 'quant.ledger')
        self.assertEqual(route['provider_preference'], ['dot', 'api'])
        self.assertEqual(route['default_complexity'], 'standard')
        self.assertEqual(route['reasoning_effort'], 'medium')

    def test_rejects_unknown_parent_provider_or_tier(self):
        raw = json.loads(EXAMPLE.read_text(encoding='utf-8'))
        bad = copy.deepcopy(raw)
        bad['categories']['quant.review']['parent'] = 'missing'
        with self.assertRaises(RegistryError):
            validate_registry_document(bad)
        bad = copy.deepcopy(raw)
        bad['scenarios']['pr_code_review']['provider_preference'] = ['dot', 'unknown']
        with self.assertRaises(RegistryError):
            validate_registry_document(bad)
        bad = copy.deepcopy(raw)
        bad['complexity_tiers']['extreme'] = {
            'reasoning_effort': 'xhigh', 'model_profile': 'strongest',
        }
        with self.assertRaises(RegistryError):
            validate_registry_document(bad)

    def test_provider_bindings_align_with_project_policy_shape(self):
        registry = load_registry(EXAMPLE)
        quant = json.loads((ROOT / 'config' / 'quant.example.json').read_text(encoding='utf-8'))
        project = next(iter(quant['projects'].values()))
        for logical, binding in registry['provider_bindings'].items():
            route = binding['mode'] if binding['profile'] == 'default' else f"{binding['mode']}:{binding['profile']}"
            if logical == 'api':
                # API stays unbound until authorized; fail-closed at runtime.
                self.assertNotIn(route, project['providers'])
                continue
            self.assertEqual(project['providers'].get(route), binding['policy_provider'])

    def test_module_does_not_embed_quant_scenario_tables(self):
        source = (ROOT / 'src' / 'quant_ai_service' / 'scenario_registry.py').read_text(encoding='utf-8')
        for name in REQUIRED_SCENARIOS:
            self.assertNotIn(name, source)
        self.assertNotIn('quick_notification', source)
        self.assertNotIn('provider_preference\": [\"api\"', source)


if __name__ == '__main__':
    unittest.main()
