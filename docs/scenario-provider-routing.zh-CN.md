# Quant 任务路由（`task.routing.v1`）

[English](scenario-provider-routing.md) · [文档索引](README.md)

QuantAIService 使用与私有路由服务共享的 `task.routing.v1` 注册契约。Quant 的类别与场景是各项目策略下的**数据**：

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

本仓库 Python 同样执行注册表校验、类别默认值继承与场景路由，且不硬编码业务场景 id。当项目策略含 `routing` 且提交体带 `scenario` 时，Python（`ai_service.routing`）与 Cloudflare task-service Worker（`cloudflare/task-service/src/routing.mjs`）均会按同一契约完成复杂度评估与 fail-closed provider 选择；策略无 `routing` 时仍拒绝 `scenario` / `complexity` / `reasoning_effort`。

## 文件

| 路径 | 作用 |
| --- | --- |
| [`config/quant.example.json`](../config/quant.example.json) | 含 `routing` 的示例项目策略 |
| [`src/quant_ai_service/task_routing.v1.schema.json`](../src/quant_ai_service/task_routing.v1.schema.json) | 兼容路由契约并保留 QAS 检查的 `task.routing.v1` schema |
| [`src/quant_ai_service/task_routing.py`](../src/quant_ai_service/task_routing.py) | 离线加载 / 引用检查 / 复杂度辅助 |
| [`cloudflare/task-service/src/routing.mjs`](../cloudflare/task-service/src/routing.mjs) | Worker 运行时：校验注册表、解析复杂度、选定 provider |

## 类别树

| 类别 | 父级 | 默认复杂度 |
| --- | --- | --- |
| `quant` | — | standard |
| `quant_review` | quant | complex |
| `quant_research` | quant | standard |
| `quant_ops` | quant | complex |
| `quant_signals` | quant | standard |
| `quant_notify` | quant | simple |

## 场景表

| 场景 | 类别 | Mode | providers | 匹配提示（示例） |
| --- | --- | --- | --- | --- |
| `pr_code_review` | quant_review | agent | muse | pull-request, code-review, architecture-audit |
| `strategy_backtest_review` | quant_review | agent | muse | strategy-backtest, backtest-review |
| `strategy_research_summary` | quant_research | auto | openai, claude, gemini, muse | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | muse | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | auto | openai, claude, gemini, muse | signal-context, feature-note |
| `report_audit` | quant_notify | api | openai, claude, gemini | report-audit, audit-summary |
| `document_translation` | quant_notify | api | openai, claude, gemini | document-translation, translate |
| `quick_notification` | quant_notify | api | openai, claude, gemini | short-alert, status-ping |

示例显式设置 `matching.default_complexity`：`pr_code_review`、`strategy_backtest_review` 和 `reconciliation_anomaly` 为 `complex`，`strategy_research_summary`、`signal_context` 和 `report_audit` 为 `standard`，`document_translation` 与 `quick_notification` 为 `simple`。
省略该字段时沿类别链继承；显式 `null` 无效。图片 / 体积下限使用 `matching.min_with_image`、`matching.size_thresholds` 与全局 `heuristics`，语义为共享契约中的只升不降。

Quant 与 PersonalAIService 共用 `task.routing.v1` 契约，各自维护场景目录。独立的定期月审（`scripts/monthly_api_audit.py`）基于冻结证据分别调用 OpenAI 和 Anthropic（在配置 `GEMINI_API_KEY` 时纳入 Gemini 作为可选第三独立审计方，`api` 模式）生成独立结论。

各场景按配置与复杂度档位解析执行模式：

- **`agent` 场景**（`pr_code_review`、`strategy_backtest_review`、`reconciliation_anomaly`）：由 Muse（`muse`）执行，`grokbot`、`claudeduet`、`dot` 等槽位默认停用并保留框架设计。
- **`api` 场景**（`quick_notification`、`report_audit`、`document_translation`）：按优先级从 `openai`（`openai_api`）、`claude`（`anthropic_api`）、`gemini`（`gemini_api`）选择就绪后端。当 API provider 配置了 `tier_models` 时，Python 与 Worker 运行时会在提交阶段用项目凭据校验候选模型可用性，并接受精确模型 ID 或服务商日期快照。配合 [`config/api-tier-models.json`](../config/api-tier-models.json) 与 `scripts/refresh_api_models.py` 可定期依据官方定价更新各档候选清单。
- **`auto` 场景**（`strategy_research_summary`、`signal_context`）：支持提交时省略 `mode` 或传 `mode: "auto"`（并支持可选 `mode_by_tier`）；默认 `simple` 与 `standard` 档选择 `api` 后端，`complex` 档升级至 `muse`（`agent`）。

## 复杂度档位表

| 档位 | Provider | 模型（示例别名） | Reasoning effort |
| --- | --- | --- | --- |
| simple | muse / openai / claude / gemini | `configured-muse-fast` / `CONFIGURE_APPROVED_*` / `configured-api` | low |
| standard | muse / openai / claude / gemini | `configured-muse` / `CONFIGURE_APPROVED_*` / `configured-api` | medium |
| complex | muse / openai / claude / gemini | `configured-muse-strong` / `CONFIGURE_APPROVED_*` / `configured-api` | high |

`pr_code_review` 在 `tier_overrides.complex` 中显式指定强模型（含 `muse`）。
别名为合成占位；生产模型名保留在私有 `models` 白名单。注册表不得出现真实账户
id；诊断日志对账户类 token 脱敏。

## 校验契约

Reasoning effort 仅允许 `low`、`medium`、`high`。Heuristics 与场景体积阈值
必须是 >= 1 的整数。合并默认值后须满足
`size_simple_max < size_standard < size_complex`，每个场景的有效阈值须满足
`standard < complex`。类别 id 匹配 `^[A-Za-z][A-Za-z0-9_-]{0,63}$`；
档位 provider id（含覆盖配置）匹配 `^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$`。

QAS 还检查显式 schema 版本、类别的 `parent` 和 `default_complexity`、
provider 在各档位的完整配置以及项目 provider/model 白名单。兼容性测试使用独立编写的
`tests/fixtures/routing_contract.py`，检查上述契约子集的示例和无效值。
该 fixture 不代表已验证与私有路由服务的完整运行时兼容性。

## 仅通过配置新增类别

1. 增加 `categories` 条目（`parent`、`default_complexity`）。
2. 增加带 `category`、`mode`、有序 `providers` 及可选 `matching` /
   `tier_overrides` 的 scenarios。
3. 确保每个偏好 provider 都是项目 `providers` 映射中的值，且各档模型已在对应
   `models` 白名单中。
4. 运行 `python -m unittest discover -s tests/task_service -p test_task_routing.py -v`。

无需修改 Python 场景表。

## 就绪与运维

提交时跳过未就绪或与当前档位模式不匹配的 provider；若列表耗尽则拒绝提交。Dot 就绪仍要求有效的事件订阅；Muse 就绪依赖有效订阅或符合项目策略的轮询执行端。示例中 API 后端与停用的 Agent 槽位保持关闭，直至显式配置凭据与批准型号清单。

Worker 路由单测 `cloudflare/task-service/test/routing.mjs` 使用 Quant 场景 id
（`quick_notification`、`pr_code_review` 等），不用 Personal 的 `example_*` /
`gemini-text` 目录。
