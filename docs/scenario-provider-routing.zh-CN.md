# Quant 任务路由（`task.routing.v1`）

[English](scenario-provider-routing.md) · [文档索引](README.md)

QuantAIService 复用 PersonalAIService 的任务路由注册方言 `task.routing.v1`
（见 [Pigbibi/PersonalAIService#88](https://github.com/Pigbibi/PersonalAIService/pull/88)
及其 `docs/task-routing.md`）。Quant 的类别与场景是各项目策略下的**数据**：

`projects[<id>].routing` / Worker `PROJECT_POLICIES[project].routing`

本仓库 Python 只做文档校验与类别默认值继承，不硬编码业务场景 id，也不做线上
fail-closed provider 选择；后者仍由共享 PersonalAIService 运行时负责。

## 文件

| 路径 | 作用 |
| --- | --- |
| [`config/quant.example.json`](../config/quant.example.json) | 含 `routing` 的示例项目策略 |
| [`src/quant_ai_service/task_routing.v1.schema.json`](../src/quant_ai_service/task_routing.v1.schema.json) | 精确的 `task.routing.v1` schema |
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

`matching.default_complexity: null` 表示沿类别链继承。图片 / 体积下限使用
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

## 仅通过配置新增类别

1. 增加 `categories` 条目（`parent`、`default_complexity`）。
2. 增加带 `category`、`mode`、有序 `providers` 及可选 `matching` /
   `tier_overrides` 的 scenarios。
3. 确保每个偏好 provider 都是项目 `providers` 映射中的值，且各档模型已在对应
   `models` 白名单中。
4. 运行 `python -m unittest discover -s tests/task_service -p test_task_routing.py -v`。

无需修改 Python 场景表。

## 就绪与运维

未就绪 provider 由 PersonalAIService 跳过；若列表耗尽则拒绝提交。示例中
`gemini` 保持 `enabled: false` / `api_usage_authorized: false`，直至另行授权。
本变更不部署服务、不轮换密钥、不启用 provider。
