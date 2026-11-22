"""Offline unit tests for official-pricing API model tier refreshes."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from ai_service.model_policy import refresh_policy, refresh_tier_models, standard_rates

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts'
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import refresh_api_models  # noqa: E402

OPENAI_DOC = """
### Standard pricing data
| Model | Input | Cached input | Output |
| --- | --- | --- | --- |
| gpt-5.5-mini | $0.25 | $0.03 | $2.00 |
| gpt-5.6-mini | $1.50 | $0.10 | $6.00 |
| gpt-5.4-mini | $0.15 | $0.02 | $0.60 |
| gpt-5.4 | $2.50 | $0.25 | $10.00 |
"""

ANTHROPIC_DOC = """
## Model pricing
| Model | Base input tokens | Output tokens |
| --- | --- | --- |
| Claude Haiku 4.5 | $1.00 / MTok | $5.00 / MTok |
| Claude Sonnet 4.7 | $3.00 / MTok | $15.00 / MTok |
| Claude Sonnet 4.6 | $3.00 / MTok | $15.00 / MTok |
| Claude Opus 4.6 | $15.00 / MTok | $75.00 / MTok |
"""

GEMINI_DOC = """
## Gemini 2.6 Flash
| Tier | Input price | Output price |
| --- | --- | --- |
| Paid | $0.20 / 1M tokens | $1.00 / 1M tokens |
## Gemini 2.5 Flash
| Tier | Input price | Output price |
| --- | --- | --- |
| Paid | $0.30 / 1M tokens | $2.50 / 1M tokens |
"""

GEMINI_MARKDOC_DOC = """
## Gemini 4 Hydrogen  {% id="gemini-4-hydrogen" %}
{% tabs %}
  {% tab title="Standard" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free of charge
  * $1.50

  ---

  * Output price (including thinking tokens)
  * Free of charge
  * $7.50
  {% /table %}
  {% /tab %}
  {% tab title="Batch" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Not available
  * $0.10

  ---

  * Output price (including thinking tokens)
  * Not available
  * $0.20
  {% /table %}
  {% /tab %}
{% /tabs %}

## Gemini 3.8 Flash  {% id="gemini-3.8-flash" %}
{% tabs %}
  {% tab title="Standard" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free of charge
  * $0.75 through December 31, 2026.
  $1.50 starting January 1, 2027.

  ---

  * Output price (including thinking tokens)
  * Free of charge
  * $3.75 through December 31, 2026.
  $7.50 starting January 1, 2027.
  {% /table %}
  {% /tab %}
{% /tabs %}

## Gemini 3.8 Live  {% id="gemini-3.8-live" %}
{% tabs %}
  {% tab title="Standard" %}
  {% table %}
  * Feature
  * Free Tier
  * Paid Tier, per 1M tokens in USD

  ---

  * Input price
  * Free of charge
  * $0.50

  ---

  * Output price
  * Free of charge
  * $2.00
  {% /table %}
  {% /tab %}
{% /tabs %}
"""

BASELINE_POLICY = {
    'schema_version': 'api.tier-models.v1',
    'tier_models': {
        'openai': {
            'simple': ['gpt-5.4-mini'],
            'standard': ['gpt-5.4-mini', 'gpt-5.4-mini-2026-03-17'],
            'complex': ['gpt-5.4', 'gpt-5.4-mini'],
        },
        'anthropic': {
            'simple': ['claude-haiku-4-5'],
            'standard': ['claude-sonnet-4-6'],
            'complex': ['claude-opus-4-6', 'claude-sonnet-4-6'],
        },
        'gemini': {
            'simple': ['gemini-2.5-flash-lite'],
            'standard': ['gemini-2.5-flash'],
            'complex': ['gemini-2.5-pro', 'gemini-2.5-flash'],
        },
    },
    'tier_families': {
        'openai': {
            'simple': r'gpt-(\d+(?:\.\d+)*)-(?:nano|mini|luna)',
            'standard': r'gpt-(\d+(?:\.\d+)*)-(?:mini|luna|terra)',
            'complex': r'gpt-(\d+(?:\.\d+)*)(?:-(?:sol|terra))?',
        },
        'anthropic': {
            'simple': r'claude-haiku-(\d+(?:-\d+)*)',
            'standard': r'claude-sonnet-(\d+(?:-\d+)*)',
            'complex': r'claude-opus-(\d+(?:-\d+)*)',
        },
        'gemini': {
            'simple': r'gemini-(\d+(?:\.\d+)*)-flash-lite',
            'standard': r'gemini-(\d+(?:\.\d+)*)-(?:hydrogen|flash)',
            'complex': r'gemini-(\d+(?:\.\d+)*)-(?:neon|hydrogen|pro)',
        },
    },
    'rate_caps': {
        'openai': {'simple': [0.75, 4.5], 'standard': [0.75, 4.5], 'complex': [5.0, 20.0]},
        'anthropic': {'simple': [2.0, 10.0], 'standard': [4.0, 20.0], 'complex': [16.0, 80.0]},
        'gemini': {'simple': [0.35, 2.5], 'standard': [1.5, 7.5], 'complex': [3.5, 20.0]},
    },
}


class ModelPolicyTests(unittest.TestCase):
    def test_parses_official_tables_and_respects_rate_caps(self):
        policy = json.loads(json.dumps(BASELINE_POLICY))
        self.assertIn('gpt-5.5-mini', standard_rates('openai', OPENAI_DOC))
        self.assertIn('claude-sonnet-4-7', standard_rates('anthropic', ANTHROPIC_DOC))
        self.assertIn('gemini-2.6-flash', standard_rates('gemini', GEMINI_DOC))

        openai_std, src_o = refresh_tier_models('openai', 'standard', policy, document=OPENAI_DOC)
        self.assertEqual(src_o, 'official_pricing')
        self.assertEqual(openai_std[0], 'gpt-5.5-mini')
        self.assertNotIn('gpt-5.6-mini', openai_std)
        self.assertIn('gpt-5.4-mini', openai_std)

        anth_std, src_a = refresh_tier_models('anthropic', 'standard', policy, document=ANTHROPIC_DOC)
        self.assertEqual(src_a, 'official_pricing')
        self.assertEqual(anth_std[0], 'claude-sonnet-4-7')

        gem_std, src_g = refresh_tier_models('gemini', 'standard', policy, document=GEMINI_DOC)
        self.assertEqual(src_g, 'official_pricing')
        self.assertEqual(gem_std[0], 'gemini-2.6-flash')

    def test_parses_gemini_markdoc_standard_tab_and_caps_candidates(self):
        rates = standard_rates('gemini', GEMINI_MARKDOC_DOC)
        self.assertEqual(str(rates['gemini-4-hydrogen'][0]), '1.50')
        self.assertEqual(str(rates['gemini-4-hydrogen'][1]), '7.50')
        self.assertEqual(str(rates['gemini-3.8-flash'][0]), '1.50')
        self.assertEqual(str(rates['gemini-3.8-flash'][1]), '7.50')
        self.assertNotIn('gemini-3.8-live', rates)

        many_models_doc = '### Standard pricing data\n| Model | Input | Output |\n| --- | --- | --- |\n' + '\n'.join(
            f'| gpt-6.{minor}-mini | $0.20 | $0.80 |' for minor in range(1, 11)
        )
        candidates, source = refresh_tier_models('openai', 'simple', BASELINE_POLICY, document=many_models_doc)
        self.assertEqual(source, 'official_pricing')
        self.assertEqual(len(candidates), 8)
        self.assertEqual(candidates[0], 'gpt-6.10-mini')

    def test_unavailable_pricing_keeps_approved_baseline_and_cli_runs_check(self):
        policy = json.loads((ROOT / 'config' / 'api-tier-models.json').read_text(encoding='utf-8'))
        for provider, tiers in policy['tier_models'].items():
            for tier, candidates in tiers.items():
                self.assertTrue(1 <= len(candidates) <= 8, f'{provider}.{tier}')
        updated, report = refresh_policy(policy, fetch=lambda _url: (_ for _ in ()).throw(OSError('offline')))
        self.assertEqual(updated['tier_models'], policy['tier_models'])
        self.assertEqual(report['openai']['simple']['source'], 'approved_baseline')

        fixture_policy = json.loads(json.dumps(BASELINE_POLICY))
        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / 'api-tier-models.json'
            out_path = Path(tmp) / 'out.json'
            policy_path.write_text(json.dumps(fixture_policy), encoding='utf-8')
            code = refresh_api_models.main(
                ['--policy', str(policy_path), '--check'],
                fetch=lambda url: OPENAI_DOC if 'openai' in url else ANTHROPIC_DOC if 'claude' in url else GEMINI_DOC,
            )
            self.assertEqual(code, 0)
            self.assertFalse(out_path.exists())

            code = refresh_api_models.main(
                ['--policy', str(policy_path), '--output', str(out_path)],
                fetch=lambda url: OPENAI_DOC if 'openai' in url else ANTHROPIC_DOC if 'claude' in url else GEMINI_DOC,
            )
            self.assertEqual(code, 0)
            written = json.loads(out_path.read_text(encoding='utf-8'))
            self.assertEqual(written['tier_models']['openai']['standard'][0], 'gpt-5.5-mini')


if __name__ == '__main__':
    unittest.main()
