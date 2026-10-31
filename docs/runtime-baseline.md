# Independent task runtime — English overview

[完整中文基线](runtime-baseline.zh-CN.md) · [Documentation index](README.md)

QuantAIService owns its `src/ai_service` runtime, dependencies and eight scenario templates. The offline SDK validates templates under the [scenario contracts](scenario-contracts.md). The Quant wheel supplies the compatible `ai_service.client` import; it does not install or download a PersonalAIService core package. Use separate virtual environments for the services because their compatibility namespace is the same.

The initial runtime/tests were copied with owner authorization from revision `f9bdf0fb55e7195d459520155bfa01652e793a5e`, including the safe MCP logging change. Preserve the provenance and MIT terms in [LICENSE.task-service](../LICENSE.task-service). Subsequent maintenance belongs to this repository; there is no synchronization task or cross-owner runtime dependency. Service discovery reports the Quant package and version.

Configure the service `owner` explicitly and apply the version-specific [worker project/owner constraints](worker-project-scoping.md#quant-service-configuration). The example service configuration is disabled and contains placeholders; preparing documentation does not activate it or qualify any connection. Task read/work scopes transport tasks and advisory results; they do not grant trading, account, risk-budget, merge or deployment authority. Product-side provider and tool access remains separate.

The baseline describes wheel installation, dependency checks, service/Quant admission tests and safe logging. Logs exclude tokens, task material, raw exceptions and private identifiers. It does not copy Personal deployment configuration. A rollback restores matching code and configuration without rewriting the state database.

Local checks, CI and historical migration records cannot establish current deployment, login, subscription or business readiness. This summary preserves the complete baseline and both repository [MIT licenses](../LICENSE).
