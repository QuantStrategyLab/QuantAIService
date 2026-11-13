# Quant task routing (`task.routing.v1`)

[简体中文](scenario-provider-routing.zh-CN.md) · [Documentation index](README.md)

QuantAIService uses the `task.routing.v1` registry contract shared with the
private routing service. Quant categories and scenarios are **data** under
each project policy:

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

Python in this repository validates the document and inherits category
defaults. It does not hard-code business scenario ids. Live fail-closed
provider selection runs in the Cloudflare task-service Worker
(`cloudflare/task-service/src/routing.mjs`) when a project policy includes
`routing` and the submit body carries `scenario`. Without `routing` on the
policy, `scenario` / `complexity` / `reasoning_effort` remain rejected.

## Files

| Path | Role |
| --- | --- |
| [`config/quant.example.json`](../config/quant.example.json) | Example project policy with `routing` |
| [`src/quant_ai_service/task_routing.v1.schema.json`](../src/quant_ai_service/task_routing.v1.schema.json) | Compatible `task.routing.v1` schema with stricter QAS checks |
| [`src/quant_ai_service/task_routing.py`](../src/quant_ai_service/task_routing.py) | Offline load / referential checks / complexity helper |
| [`cloudflare/task-service/src/routing.mjs`](../cloudflare/task-service/src/routing.mjs) | Worker runtime: validate registry, resolve complexity, decide provider |

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
| `pr_code_review` | quant_review | agent | muse → grokbot → dot | pull-request, code-review, architecture-audit |
| `strategy_research_summary` | quant_research | agent | muse → grokbot → dot | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | muse → grokbot → dot | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | agent | muse → grokbot → dot | signal-context, feature-note |
| `quick_notification` | quant_notify | api | chatgpt-plan → gemini | short-alert, status-ping |

The example explicitly sets `matching.default_complexity` to `complex` for
`pr_code_review` and `reconciliation_anomaly`, `standard` for
`strategy_research_summary` and `signal_context`, and `simple` for
`quick_notification`. Omit `matching.default_complexity` to inherit from the
category chain; an explicit `null` is invalid. Image / size floors use
`matching.min_with_image`, `matching.size_thresholds` and global `heuristics` with raise-only semantics from the shared contract.

Agent and API modes stay separate, and both pools are wired end-to-end
(`providers`, `models`, scenario lists, `complexity_tiers`). Agent scenarios
prefer `muse → grokbot → dot`. API `quick_notification` prefers
`chatgpt-plan → gemini` (ChatGPT plan OAuth first, Gemini API key backup);
legacy profiles expose `api` → `chatgpt-plan` and `api:gemini` → `gemini`.
`claudeduet` is never listed. While Muse remains unavailable it is skipped —
do not cancel both paid agents until Muse readiness is live.

## Complexity tier table

| Tier | Provider | Model (example alias) | Reasoning effort |
| --- | --- | --- | --- |
| simple | dot / muse / grokbot / chatgpt-plan / gemini | `*-fast` / `configured-chatgpt-plan-fast` / `configured-api-fast` | low |
| standard | dot / muse / grokbot / chatgpt-plan / gemini | `configured-*` / `configured-chatgpt-plan` / `configured-api` | medium |
| complex | dot / muse / grokbot / chatgpt-plan / gemini | `*-strong` / `configured-chatgpt-plan-strong` / `configured-api` | high |

`pr_code_review` sets `tier_overrides.complex` to the strong models explicitly
(including `muse`). Aliases are synthetic; production names stay in private
`models` allowlists. Real account ids never appear in the registry; diagnostics
mask account-like tokens.

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

Unready providers are skipped at submit; if none remain, submit is denied.
Dot readiness still requires a live event subscription. In the example,
`gemini` stays `enabled: false` / `api_usage_authorized: false` until
separately authorized. Shipping this runtime does not deploy services, rotate
secrets or enable providers.
