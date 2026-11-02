# Quant 任务路由（`task.routing.v1`）

[English](scenario-provider-routing.md) · [文档索引](README.md)

QuantAIService 使用与私有路由服务共享的 `task.routing.v1` 注册契约。Quant 的类别与场景是各项目策略下的**数据**：

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

本仓库 Python 只做文档校验与类别默认值继承，不硬编码业务场景 id，也不做线上
fail-closed provider 选择；后者由私有路由服务负责。
只有 QAS 运行时支持路由契约后，路由配置才会生效。目前随 QAS 提供的运行时
忽略策略中的 routing 数据，并拒绝任务中的 `scenario` 字段；配置校验通过不表示
路由已启用。

## 文件

| 路径 | 作用 |
| --- | --- |
| [`config/quant.example.json`](../config/quant.example.json) | 含 `routing` 的示例项目策略 |
| [`src/quant_ai_service/task_routing.v1.schema.json`](../src/quant_ai_service/task_routing.v1.schema.json) | 兼容路由契约并保留 QAS 检查的 `task.routing.v1` schema |
| [`src/quant_ai_service/task_routing.py`](../src/quant_ai_service/task_routing.py) | 离线加载 / 引用检查 / 复杂度辅助 |

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

| 场景 | 类别 | Mode | 有序 providers | 匹配提示（示例） |
| --- | --- | --- | --- | --- |
| `pr_code_review` | quant_review | agent | dot → grokbot | pull-request, code-review, architecture-audit |
| `strategy_research_summary` | quant_research | agent | dot → grokbot | research-summary, strategy-brief, portfolio-note |
| `reconciliation_anomaly` | quant_ops | agent | dot → grokbot | recon-diff, ops-anomaly, ledger-mismatch |
| `signal_context` | quant_signals | agent | dot | signal-context, feature-note |
| `quick_notification` | quant_notify | api | gemini | short-alert, status-ping |

示例显式设置 `matching.default_complexity`：`pr_code_review` 和
`reconciliation_anomaly` 为 `complex`，`strategy_research_summary` 和
`signal_context` 为 `standard`，`quick_notification` 为 `simple`。
省略该字段时沿类别链继承；显式 `null` 无效。图片 / 体积下限使用
`matching.min_with_image`、`matching.size_thresholds` 与全局 `heuristics`，语义为
共享契约中的只升不降。

## 复杂度档位表

| 档位 | Provider | 模型（示例别名） | Reasoning effort |
| --- | --- | --- | --- |
| simple | dot / grokbot / gemini | `*-fast` / `configured-api-fast` | low |
| standard | dot / grokbot / gemini | `configured-dot` / `configured-grokbot` / `configured-api` | medium |
| complex | dot / grokbot / gemini | `*-strong` / `configured-api` | high |

`pr_code_review` 在 `tier_overrides.complex` 中显式指定强模型。别名为合成占位；
生产模型名保留在私有 `models` 白名单。注册表不得出现真实账户 id；诊断日志对
账户类 token 脱敏。

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

QAS 运行时支持路由契约后，会跳过未就绪 provider；若列表耗尽则拒绝提交。
运行时同步属于独立变更。示例中
`gemini` 保持 `enabled: false` / `api_usage_authorized: false`，直至另行授权。
本变更不部署服务、不轮换密钥、不启用 provider。
