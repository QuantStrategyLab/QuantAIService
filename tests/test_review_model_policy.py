import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts.review_model_policy import refresh_candidates


POLICY = {
    'standard': {
        'openai': ['gpt-5.4-mini', 'gpt-5.4-mini-2026-03-17'],
        'anthropic': ['claude-sonnet-5-5', 'claude-sonnet-5', 'claude-sonnet-4-6'],
    },
    'complex': {
        'openai': ['gpt-5.4-mini', 'gpt-5.4-mini-2026-03-17'],
        'anthropic': ['claude-sonnet-5-5', 'claude-sonnet-5', 'claude-sonnet-4-6'],
    },
    'rate_caps': {
        'standard': {'openai': [0.75, 4.5], 'anthropic': [3, 15]},
        'complex': {'openai': [0.75, 4.5], 'anthropic': [3, 15]},
    },
}
OPENAI = '''### Standard pricing data
| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-5.4-mini | $0.75 | $4.50 | $0.075 |
| gpt-6-luna | $0.20 | $0.75 | $0.02 |
| gpt-7-mini | $0.74 | $4.49 | $0.07 |
| gpt-8-mini | $0.70 | $7.00 | $0.07 |
| gpt-9-mini-preview | $0.10 | $1.00 | $0.01 |
| gpt-8-pro | $0.01 | $0.02 | $0.01 |

### Batch pricing data
| Model | Input | Output | Cached input |
| --- | --- | --- | --- |
| gpt-8-mini | $0.35 | $3.50 | $0.03 |
'''
CLAUDE = '''## Model pricing
| Model | Base input tokens | 5m cache writes | Output tokens |
| --- | --- | --- | --- |
| Claude Sonnet 5.5 | $2 / MTok | $2.5 / MTok | $10 / MTok |
| Claude Sonnet 6.2 | $3 / MTok | $3.75 / MTok | $15 / MTok |
| Claude Sonnet 6.10 | $3 / MTok | $3.75 / MTok | $15 / MTok |
| Claude Sonnet 7 | $3 / MTok | $3.75 / MTok | Unconfirmed |
| Claude Sonnet 7 Preview | $1 / MTok | $1.25 / MTok | $5 / MTok |
| Claude Opus 7 | $1 / MTok | $1.25 / MTok | $5 / MTok |

## Cloud platform pricing
| Model | Base input tokens | Output tokens |
| --- | --- | --- |
| Claude Sonnet 7 | $1 / MTok | $1 / MTok |
'''


@patch.dict('os.environ', {'OPENAI_PRICING_ORIGIN': 'https://example.org',
                           'ANTHROPIC_PRICING_ORIGIN': 'https://example.org'})
class ReviewModelPolicyTests(unittest.TestCase):
    def test_new_same_family_models_refresh_without_changing_approvals(self):
        self.assertEqual(refresh_candidates('openai', 'standard', POLICY, lambda _: OPENAI),
                         (['gpt-7-mini', 'gpt-6-luna', *POLICY['standard']['openai']], 'official_pricing'))
        self.assertEqual(refresh_candidates('anthropic', 'complex', POLICY, lambda _: CLAUDE),
                         (['claude-sonnet-6-10', 'claude-sonnet-6-2', 'claude-sonnet-5-5'], 'official_pricing'))

    def test_new_rate_above_either_cap_removes_approved_alias_and_snapshot(self):
        for base in ('$0.76 | $4.50', '$0.75 | $4.51'):
            with self.subTest(base=base):
                prices = OPENAI.replace('$0.75 | $4.50', base)
                self.assertEqual(refresh_candidates('openai', 'standard', POLICY, lambda _: prices),
                                 (['gpt-7-mini', 'gpt-6-luna'], 'official_pricing'))

    def test_committed_monthly_audit_policy_has_non_empty_rosters(self):
        committed = json.loads((Path(__file__).resolve().parents[1] / 'config/monthly-audit-models.json').read_text())
        for tier in ('standard', 'complex'):
            for provider in ('openai', 'anthropic'):
                self.assertTrue(committed[tier][provider], f'{tier}.{provider}')

    def test_read_failure_only_preserves_reviewed_baseline(self):
        def unavailable(_):
            raise TimeoutError
        for fetch in (unavailable, lambda _: '<html>temporarily unavailable</html>'):
            self.assertEqual(refresh_candidates('anthropic', 'standard', POLICY, fetch),
                             (POLICY['standard']['anthropic'], 'approved_baseline'))

    def test_empty_roster_stays_empty_if_current_prices_exceed_caps(self):
        self.assertEqual(refresh_candidates('openai', 'complex', POLICY,
                                            lambda _: OPENAI.replace('$0.75', '$1.00').replace('$0.74', '$1.00').replace('$0.20', '$1.00')),
                         ([], 'official_pricing'))
