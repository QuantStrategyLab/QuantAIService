"""Refresh or validate curated API tier model rosters from pricing policy."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from ai_service.model_policy import refresh_policy


def main(argv: list[str] | None = None, *, fetch=None) -> int:
    parser = argparse.ArgumentParser(description='Refresh approved API tier model rosters')
    parser.add_argument('--policy', type=Path, default=Path('config/api-tier-models.json'))
    parser.add_argument('--output', type=Path, default=None)
    parser.add_argument('--check', action='store_true', help='Validate and summarize without writing files')
    args = parser.parse_args(argv)

    policy = json.loads(args.policy.read_text(encoding='utf-8'))
    updated, report = refresh_policy(policy, fetch=fetch)
    empty_tiers = [
        f'{provider}.{tier}'
        for provider, tiers in updated.get('tier_models', {}).items()
        for tier, candidates in tiers.items()
        if not candidates
    ]
    if not empty_tiers and not args.check:
        target = args.output or args.policy
        target.write_text(json.dumps(updated, indent=2) + '\n', encoding='utf-8')
    payload = {'schema_version': updated.get('schema_version'), 'tiers': report}
    if empty_tiers:
        payload['empty_tiers'] = empty_tiers
    print(json.dumps(payload, sort_keys=True))
    return 1 if empty_tiers else 0


if __name__ == '__main__':
    raise SystemExit(main())
