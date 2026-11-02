# Quant scenario registry extension

[简体中文](scenario-provider-routing.zh-CN.md) · [Documentation index](README.md)

QuantAIService contributes **data only** to the shared PersonalAIService scenario
registry. Categories and scenarios are JSON. New quant categories are added by
editing the registry document; this package does not hard-code scenario names or
provider preference tables in Python.

## Assumption (PersonalAIService alignment)

On 2026-11-02 the PersonalAIService pull request titled around “Scenario-based
provider routing” was not readable from this repository’s GitHub identity
(`Pigbibi/PersonalAIService` unresolved / private). This repository therefore
ships:

- schema id `personal-ai.scenario-registry.extension.v1`
- logical provider bindings that map onto existing `PROJECT_POLICIES` route keys
  (`agent`, `agent:<profile>`, `api`) and `PROJECT_CONTEXTS.providers`
- complexity tiers `simple` / `standard` / `complex`

If the shared schema uses different field names, remap them here and keep the
quant category and scenario rows as data. Fail-closed skipping of providers that
are not ready remains in the PersonalAIService registry runtime.

## Files

| Path | Role |
| --- | --- |
| [`config/quant-scenario-registry.example.json`](../config/quant-scenario-registry.example.json) | Example quant registry document (source of truth for tables below) |
| [`src/quant_ai_service/scenario_registry.extension.v1.schema.json`](../src/quant_ai_service/scenario_registry.extension.v1.schema.json) | Extension contract schema |
| [`src/quant_ai_service/scenario_registry.py`](../src/quant_ai_service/scenario_registry.py) | Offline load, schema/referential validation, category inheritance |

The loader never submits tasks, reads secrets, or selects a live provider.

## Category tree

| Category | Parent | Default complexity | Provider preference | Matching hints (examples) |
| --- | --- | --- | --- | --- |
| `quant` | — | standard | dot → grokbot → api | quant, qas, trading-ops |
| `quant.review` | quant | complex | dot → grokbot → api | review, audit, pull-request |
| `quant.research` | quant | standard | dot → api → grokbot | research, strategy, briefing |
| `quant.ops` | quant | complex | dot → grokbot | reconciliation, anomaly, ops |
| `quant.signals` | quant | standard | dot → api | signal, context |
| `quant.notify` | quant | simple | api → dot → grokbot | notification, alert, ping |

An empty `provider_preference` or `tier_overrides` on a scenario means “inherit
from the category chain.”

## Scenario table

| Scenario | Category | Default complexity | Effective preference |
| --- | --- | --- | --- |
| `pr_code_review` | quant.review | complex | dot → grokbot → api |
| `strategy_research_summary` | quant.research | standard | dot → api → grokbot |
| `reconciliation_anomaly` | quant.ops | complex | dot → grokbot |
| `signal_context` | quant.signals | standard | dot → api |
| `quick_notification` | quant.notify | simple | api → dot → grokbot |

## Complexity tier table

| Tier | Reasoning effort | Model profile | Intended use |
| --- | --- | --- | --- |
| `simple` | low | cheap | Short notifications and low-cost text |
| `standard` | medium | balanced | Research summaries and signal context |
| `complex` | high | strongest | PR audits, architecture review, ops anomalies |

Model profile aliases are synthetic placeholders (`configured-dot`,
`configured-api-fast`, …). Production model names stay in private
`PROJECT_POLICIES.models` allowlists. Real account ids are never stored in this
repository; tests use fake ids and `mask_account_ids` redacts account-like tokens
before logs or artifacts.

## Adding a category through config alone

1. Add a `categories` entry with `parent`, `matching_hints`, `provider_preference`
   and `tier_overrides`.
2. Add one or more `scenarios` that reference that category.
3. Leave scenario `provider_preference` empty to inherit, or set an explicit list.
4. Run offline validation: `python -m unittest discover -s tests -p test_scenario_registry.py -v`.

No Python table update is required.

## Security and operations

- Do not put API keys, tokens, or real account ids in the registry document.
- API logical provider remains unbound in `config/quant.example.json` until
  separately authorized; unreadiness is fail-closed in the shared runtime.
- This change does not deploy services, rotate secrets, or enable providers.
