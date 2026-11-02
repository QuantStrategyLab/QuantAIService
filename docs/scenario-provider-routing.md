# Quant task routing (`task.routing.v1`)

[简体中文](scenario-provider-routing.zh-CN.md) · [Documentation index](README.md)

QuantAIService uses the `task.routing.v1` registry contract shared with the
private routing service. Quant categories and scenarios are **data** under
each project policy:

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

Python in this repository only validates the document and inherits category
defaults. It does not hard-code business scenario ids or perform live
fail-closed provider selection; that remains in the private routing service.
Routing only takes effect after the QAS runtime supports the routing contract.
The currently bundled QAS runtime ignores policy routing data and rejects
`scenario` task fields; validating this registry does not enable routing.

## Files

| Path | Role |
| --- | --- |
| [`config/quant.example.json`](../config/quant.example.json) | Example project policy with `routing` |
| [`src/quant_ai_service/task_routing.v1.schema.json`](../src/quant_ai_service/task_routing.v1.schema.json) | Compatible `task.routing.v1` schema with stricter QAS checks |
| [`src/quant_ai_service/task_routing.py`](../src/quant_ai_service/task_routing.py) | Offline load / referential checks / complexity helper |

## Category tree

| Category | Parent | Default complexity |
| --- | --- | --- |
| `quant` | — | standard |
| `quant_review` | quant | complex |
| `quant_research` | quant | standard |
| `quant_ops` | quant | complex |
| `quant_signals` | quant | standard |
| `quant_notify` | quant | simple |

## Scenario table

| Scenario | Category | Mode | Ordered providers | Matching hints (examples) |
| --- | --- | --- | --- | --- |
| `pr_code_review` | quant_review | agent | dot → grokbot | pull-request, code-review, architecture-audit |
| `strategy_research_summary` | quant_research | agent | dot → grokbot | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | dot → grokbot | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | agent | dot | signal-context, feature-note |
| `quick_notification` | quant_notify | api | gemini | short-alert, status-ping |

The example explicitly sets `matching.default_complexity` to `complex` for
`pr_code_review` and `reconciliation_anomaly`, `standard` for
`strategy_research_summary` and `signal_context`, and `simple` for
`quick_notification`. Omit `matching.default_complexity` to inherit from the
category chain; an explicit `null` is invalid. Image / size floors use
`matching.min_with_image`, `matching.size_thresholds` and global `heuristics` with raise-only semantics from the shared contract.

## Complexity tier table

| Tier | Provider | Model (example alias) | Reasoning effort |
| --- | --- | --- | --- |
| simple | dot / grokbot / gemini | `*-fast` / `configured-api-fast` | low |
| standard | dot / grokbot / gemini | `configured-dot` / `configured-grokbot` / `configured-api` | medium |
| complex | dot / grokbot / gemini | `*-strong` / `configured-api` | high |

`pr_code_review` sets `tier_overrides.complex` to the strong models explicitly.
Aliases are synthetic; production names stay in private `models` allowlists.
Real account ids never appear in the registry; diagnostics mask account-like
tokens.

## Validation contract

Reasoning effort is `low`, `medium` or `high`. Heuristics and scenario size
thresholds must be integers >= 1. After applying defaults,
`size_simple_max < size_standard < size_complex`; each scenario's effective
`standard < complex` thresholds must also hold. Category ids match
`^[A-Za-z][A-Za-z0-9_-]{0,63}$`; tier provider ids (including overrides) match
`^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`.

QAS also checks an explicit schema version, category `parent` and
`default_complexity`, complete provider coverage across tiers, and project
provider/model admission. Compatibility tests use the independently written
`tests/fixtures/routing_contract.py` to check the subset above against the
example and rejected values. This fixture does not establish full runtime
compatibility with the private routing service.

## Adding a category through config alone

1. Add a `categories` entry (`parent`, `default_complexity`).
2. Add scenarios with `category`, `mode`, ordered `providers`, optional
   `matching` / `tier_overrides`.
3. Ensure every preferred provider is a value in the project `providers` map and
   every tier model is in that provider’s `models` allowlist.
4. Run `python -m unittest discover -s tests/task_service -p test_task_routing.py -v`.

No Python scenario table update is required.

## Readiness and operations

After the QAS runtime supports the routing contract, unready providers are
skipped; if none remain, submit is denied. This runtime sync is a separate change. In the
example, `gemini` stays `enabled: false` /
`api_usage_authorized: false` until separately authorized. This change does not
deploy services, rotate secrets or enable providers.
