# Documentation index

[English README](../README.md) · [中文 README](../README.zh-CN.md)

## Current task runtime

- [Independent runtime: English overview](runtime-baseline.en.md) · [完整中文基线](runtime-baseline.zh-CN.md): this repository owns the runtime and wheel; no cross-owner core installation.
- [Worker project and owner scope](worker-project-scoping.md): exact projects, explicit service owner, token scopes and connection boundaries. Read the version-specific owner-wide section together with the earlier project-list section.
- [Scenario contracts](scenario-contracts.md): offline schemas, templates, defaults and producer binding.
- [V2 guide index](upgrade-v2/README.md): current configuration references versus dated migration material.

## Monitoring operations

- [Quant Monitor by task](../ops/quant-monitor/README.md#reading-by-task): operation, notification and deployment sections. Monitoring is evidence-only; deployment has separate qualification requirements.

## Historical architecture

- [V1 AI Gateway architecture](architecture.md): former Analyze/Execute/Review interfaces and components, preserved for historical interpretation; not the V2 connection/API guide.
- [AI autonomy draft](ai_autonomy_architecture.md): **HISTORICAL_DRAFT_NOT_CURRENT_QSL_AUTHORITY**. Earlier audit/repair ideas do not define current runtime qualification, automatic merge permission or P0–P6 authority.

Source documentation and migration history do not establish deployed state, OAuth validity, event delivery or real-business acceptance. Provider/tool rights remain independently controlled. See [LICENSE](../LICENSE) and [task-service MIT license](../LICENSE.task-service).
