# Quant task routing (`task.routing.v1`)

[简体中文](scenario-provider-routing.zh-CN.md) · [Documentation index](README.md)

QuantAIService uses the `task.routing.v1` registry contract shared with the
private routing service. Quant categories and scenarios are **data** under
each project policy:

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

Python validates the document, inherits category defaults, and runs scenario routing when a project policy defines `routing`. It does not hard-code business scenario ids. Live fail-closed provider selection runs identically in Python (`ai_service.routing`) and the Cloudflare task-service Worker (`cloudflare/task-service/src/routing.mjs`) when a project policy includes `routing` and the submit body carries `scenario`. Without `routing` on the policy, `scenario` / `complexity` / `reasoning_effort` remain rejected.

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

| Scenario | Category | Mode | Providers | Matching hints (examples) |
| --- | --- | --- | --- | --- |
| `pr_code_review` | quant_review | agent | muse | pull-request, code-review, architecture-audit |
| `strategy_backtest_review` | quant_review | agent | muse | strategy-backtest, backtest-review |
| `strategy_research_summary` | quant_research | auto | openai, claude, gemini, muse | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | muse | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | auto | openai, claude, gemini, muse | signal-context, feature-note |
| `report_audit` | quant_notify | api | openai, claude | report-audit, audit-summary |
| `document_translation` | quant_notify | api | openai, claude | document-translation, translate |
| `quick_notification` | quant_notify | api | openai, claude, gemini | short-alert, status-ping |

The example explicitly sets `matching.default_complexity` to `complex` for
`pr_code_review`, `strategy_backtest_review`, and `reconciliation_anomaly`, `standard` for
`strategy_research_summary`, `signal_context`, and `report_audit`, and `simple` for
`document_translation` and `quick_notification`. Omit `matching.default_complexity` to inherit from the
category chain; an explicit `null` is invalid. Image / size floors use
`matching.min_with_image`, `matching.size_thresholds` and global `heuristics` with raise-only semantics from the shared contract.

Quant scenarios are independent of PersonalAIService's business scenarios; only the
`task.routing.v1` contract is shared. Separate scheduled Dual-API audits
(`scripts/monthly_api_audit.py`) invoke OpenAI and Anthropic independently in `api`
mode using their own frozen evidence pipeline.

Execution modes resolve cleanly per scenario and complexity tier:

- **`agent` scenarios** (`pr_code_review`, `strategy_backtest_review`, `reconciliation_anomaly`): route exclusively to Muse (`muse`). Other agent slots (`grokbot`, `claudeduet`, `dot`) remain disabled in configuration while keeping the adapter framework intact.
- **`api` scenarios** (`quick_notification`, `report_audit`, `document_translation`): route through `openai` (`openai_api`), `claude` (`anthropic_api`), and `gemini` (`gemini_api`) according to preference order. When `tier_models` is set on an API provider, Python and Worker runtimes verify candidate model availability with provider credentials and accept exact or provider-dated snapshots. See [`config/api-tier-models.json`](../config/api-tier-models.json) and `scripts/refresh_api_models.py` for pricing-bounded tier roster updates.
- **`auto` scenarios** (`strategy_research_summary`, `signal_context`): accept omitted or `"auto"` `mode` at submit time (with optional `mode_by_tier`); `simple` and `standard` tiers select an `api` backend, while `complex` payloads escalate to `muse` (`agent`).

## Complexity tier table

| Tier | Provider | Model (example alias) | Reasoning effort |
| --- | --- | --- | --- |
| simple | muse / openai / claude / gemini | `configured-muse-fast` / `CONFIGURE_APPROVED_*` / `configured-api` | low |
| standard | muse / openai / claude / gemini | `configured-muse` / `CONFIGURE_APPROVED_*` / `configured-api` | medium |
| complex | muse / openai / claude / gemini | `configured-muse-strong` / `CONFIGURE_APPROVED_*` / `configured-api` | high |

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

Unready providers (and providers whose mode disagrees with the resolved tier mode) are skipped at submit; if none remain, submit is denied.
Muse requires a live subscription or an eligible polling worker. Set the API provider and approved model for the selected runtime.

Worker routing unit tests under `cloudflare/task-service/test/routing.mjs` use
Quant scenario ids (`quick_notification`, `pr_code_review`, …), not Personal’s
`example_*` / `gemini-text` catalog.
