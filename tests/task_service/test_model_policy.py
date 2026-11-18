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


class ModelPolicyTests(unittest.TestCase):
    def test_parses_official_tables_and_respects_rate_caps(self):
        policy = json.loads((ROOT / 'config' / 'api-tier-models.json').read_text(encoding='utf-8'))
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

    def test_unavailable_pricing_keeps_approved_baseline_and_cli_runs_check(self):
        policy = json.loads((ROOT / 'config' / 'api-tier-models.json').read_text(encoding='utf-8'))
        updated, report = refresh_policy(policy, fetch=lambda _url: (_ for _ in ()).throw(OSError('offline')))
        self.assertEqual(updated['tier_models'], policy['tier_models'])
        self.assertEqual(report['openai']['simple']['source'], 'approved_baseline')

        with tempfile.TemporaryDirectory() as tmp:
            policy_path = Path(tmp) / 'api-tier-models.json'
            out_path = Path(tmp) / 'out.json'
            policy_path.write_text(json.dumps(policy), encoding='utf-8')
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
