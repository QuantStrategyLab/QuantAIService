# Documentation index

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## Current task runtime

- [Independent runtime: English overview](runtime-baseline.en.md) · [完整中文基线](runtime-baseline.zh-CN.md): this repository owns the runtime and wheel; no cross-owner core installation.
- [Worker project and owner scope](worker-project-scoping.md): exact projects, explicit service owner, token scopes and connection boundaries. Read the version-specific owner-wide section together with the earlier project-list section.
- [Scenario contracts](scenario-contracts.md): offline schemas, templates, defaults and producer binding.
- [V2 guide index](upgrade-v2/README.md): current configuration references versus dated migration material.

## Monitoring operations

- [Quant Monitor by task](../ops/quant-monitor/README.md#reading-by-task): operation, notification and deployment sections. Monitoring is evidence-only; deployment has separate qualification requirements.

The Actions-only [saved-observation diagnosis caller](../scripts/diagnose_monitor_oidc.py)
keeps its existing protected-main workflow identity and local CLI. Its optional
`diagnose(..., checkpoint=...)` callback lets a shared-state adapter durably save
the submission intent, accepted job and final receipt under one retained claim.
The callback must raise on an uncertain write; the caller then stops before the
next request or success response. This hook alone does not migrate the workflow:
the adapter must restore the original observations and all daily checkpoints
(including unknown outcomes), hold the common state claim, and have approved
storage access before a hosted runner can replace the source host.

The hosted diagnosis workflow uses the existing hashed Linux runtime lock and
direct Google Workload Identity Federation. `QUANT_MONITOR_GCP_WIF_PROVIDER`
binds the approved provider; the generated refreshable ADC file stays within the
job. The audit service still receives its separate `quant-codex-audit` token
from the original protected-main workflow. The `--cloud` CLI consumes only the
shared `DiagnosisState` observation projection and this consumer's independent
JSON ledger/claim. It retains the claim on an uncertain attempt or state write;
operator recovery must preserve every daily checkpoint before releasing it.

Manual `verify_state_only=true` runs no audit request. It verifies that archive
metadata and object listing are denied, refreshes ADC, saves the unchanged
ledger with a generation precondition, reads it back and releases the exact
claim. Its receipt records only bounded results. This qualifies state access,
not a real diagnosis or natural scheduled event. Pause the old workflow and
drain its jobs before importing its final VPS ledger; adopt the hosted source
and verify state access before restoring its hourly schedule.

## Historical architecture

- [V1 AI Gateway architecture](architecture.md): former Analyze/Execute/Review interfaces and components, preserved for historical interpretation; not the V2 connection/API guide.
- [AI autonomy draft](ai_autonomy_architecture.md): **HISTORICAL_DRAFT_NOT_CURRENT_QSL_AUTHORITY**. Earlier audit/repair ideas do not define current runtime qualification, automatic merge permission or P0–P6 authority.

Source documentation and migration history do not establish deployed state, OAuth validity, event delivery or real-business acceptance. Provider/tool rights remain independently controlled. See [LICENSE](../LICENSE) and [task-service MIT license](../LICENSE.task-service).
