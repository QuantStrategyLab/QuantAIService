# Quant 任务路由（`task.routing.v1`）

[English](scenario-provider-routing.md) · [文档索引](README.md)

QuantAIService 使用与私有路由服务共享的 `task.routing.v1` 注册契约。Quant 的类别与场景是各项目策略下的**数据**：

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

本仓库 Python 做文档校验与类别默认值继承，不硬编码业务场景 id。线上
fail-closed provider 选择由 Cloudflare task-service Worker
（`cloudflare/task-service/src/routing.mjs`）在项目策略含 `routing` 且提交体
带 `scenario` 时执行。策略无 `routing` 时仍拒绝 `scenario` /
`complexity` / `reasoning_effort`。

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
| `pr_code_review` | quant_review | agent | muse → grokbot → dot | pull-request, code-review, architecture-audit |
| `strategy_research_summary` | quant_research | agent | muse → grokbot → dot | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | muse → grokbot → dot | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | agent | muse → grokbot → dot | signal-context, feature-note |
| `quick_notification` | quant_notify | api | chatgpt-plan | short-alert, status-ping |

示例显式设置 `matching.default_complexity`：`pr_code_review` 和
`reconciliation_anomaly` 为 `complex`，`strategy_research_summary` 和
`signal_context` 为 `standard`，`quick_notification` 为 `simple`。
省略该字段时沿类别链继承；显式 `null` 无效。图片 / 体积下限使用
`matching.min_with_image`、`matching.size_thresholds` 与全局 `heuristics`，语义为
共享契约中的只升不降。

Quant 场景与 PersonalAIService **不是同一套目录**，只共用 `task.routing.v1` 合同。Personal 短 API 示例是 Gemini；Quant 短 API 示例是 `chatgpt-plan`，并且 Quant 还有 OpenAI / Claude API 与双 AI 审计走本注册表之外的路径。

Agent 与 API 模式分离：

- **Agent** 场景用有序偏好（`muse → grokbot → dot`），未就绪则跳过；Muse 接上后打头。列表不放 `claudeduet`。**两边付费 agent 都停之前必须先让 Muse 就绪**。
- **API** 场景只绑定**一个** provider。`quick_notification` 对应 `chatgpt-plan`（OpenAI plan OAuth / Responses）；模型与推理档位由复杂度分层智能填充。API **没有**后备链——Gemini 走遗留 `api:gemini`（或另开场景），不挂第二偏好。Quant 另外还有 **OpenAI API**、**Anthropic Claude API** 以及 **双 AI 审计**（Codex 主审 + 独立 GPT/Claude 复审）走 AiGateway / 审计路径；那不是 PersonalAIService 的场景目录，也不写成这里的 API 有序列表。

## 复杂度档位表

| 档位 | Provider | 模型（示例别名） | Reasoning effort |
| --- | --- | --- | --- |
| simple | dot / muse / grokbot / chatgpt-plan / gemini | `*-fast` / `configured-chatgpt-plan-fast` / `configured-api-fast` | low |
| standard | dot / muse / grokbot / chatgpt-plan / gemini | `configured-*` / `configured-chatgpt-plan` / `configured-api` | medium |
| complex | dot / muse / grokbot / chatgpt-plan / gemini | `*-strong` / `configured-chatgpt-plan-strong` / `configured-api` | high |

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

提交时跳过未就绪 provider；若列表耗尽则拒绝提交。Dot 就绪仍要求有效的
事件订阅。示例中 `gemini` 保持 `enabled: false` /
`api_usage_authorized: false`，直至另行授权。合入本运行时不等于部署服务、
轮换密钥或启用 provider。
