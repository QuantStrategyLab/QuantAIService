# Quant 场景注册扩展

[English](scenario-provider-routing.md) · [文档索引](README.md)

QuantAIService 向共享的 PersonalAIService 场景注册表只贡献**数据**。类别与场景均为 JSON。新增 quant 类别只需改注册文档，本包不在 Python 中硬编码场景名或 provider 偏好表。

## 假设（与 PersonalAIService 对齐）

2026-11-02 当日，本仓库所用 GitHub 身份无法读取 PersonalAIService 上标题约含 “Scenario-based provider routing” 的拉取请求（`Pigbibi/PersonalAIService` 无法解析 / 私有）。因此本仓库提供：

- schema id：`personal-ai.scenario-registry.extension.v1`
- 逻辑 provider 绑定到既有 `PROJECT_POLICIES` 路由键（`agent`、`agent:<profile>`、`api`）以及 `PROJECT_CONTEXTS.providers`
- 复杂度档位：`simple` / `standard` / `complex`

若共享 schema 字段名不同，在此重映射即可，quant 类别与场景行保持为数据。未就绪 provider 的 fail-closed 跳过由 PersonalAIService 注册运行时负责。

## 文件

| 路径 | 作用 |
| --- | --- |
| [`config/quant-scenario-registry.example.json`](../config/quant-scenario-registry.example.json) | quant 注册示例（下表数据来源） |
| [`src/quant_ai_service/scenario_registry.extension.v1.schema.json`](../src/quant_ai_service/scenario_registry.extension.v1.schema.json) | 扩展契约 schema |
| [`src/quant_ai_service/scenario_registry.py`](../src/quant_ai_service/scenario_registry.py) | 离线加载、schema/引用校验、类别继承 |

加载器不提交任务、不读密钥、不选择线上 provider。

## 类别树

| 类别 | 父级 | 默认复杂度 | Provider 偏好 | 匹配提示（示例） |
| --- | --- | --- | --- | --- |
| `quant` | — | standard | dot → grokbot → api | quant, qas, trading-ops |
| `quant.review` | quant | complex | dot → grokbot → api | review, audit, pull-request |
| `quant.research` | quant | standard | dot → api → grokbot | research, strategy, briefing |
| `quant.ops` | quant | complex | dot → grokbot | reconciliation, anomaly, ops |
| `quant.signals` | quant | standard | dot → api | signal, context |
| `quant.notify` | quant | simple | api → dot → grokbot | notification, alert, ping |

场景上的空 `provider_preference` 或 `tier_overrides` 表示沿类别链继承。

## 场景表

| 场景 | 类别 | 默认复杂度 | 有效偏好 |
| --- | --- | --- | --- |
| `pr_code_review` | quant.review | complex | dot → grokbot → api |
| `strategy_research_summary` | quant.research | standard | dot → api → grokbot |
| `reconciliation_anomaly` | quant.ops | complex | dot → grokbot |
| `signal_context` | quant.signals | standard | dot → api |
| `quick_notification` | quant.notify | simple | api → dot → grokbot |

## 复杂度档位表

| 档位 | reasoning effort | 模型档 | 用途 |
| --- | --- | --- | --- |
| `simple` | low | cheap | 短通知与低成本文本 |
| `standard` | medium | balanced | 研究摘要与信号上下文 |
| `complex` | high | strongest | PR 审计、架构审阅、运维异常 |

模型档别名为合成占位（`configured-dot`、`configured-api-fast` 等）。生产模型名保留在私有 `PROJECT_POLICIES.models` 白名单。真实账户 id 不得写入本仓库；测试使用假 id，日志与制品经 `mask_account_ids` 脱敏。

## 仅通过配置新增类别

1. 在 `categories` 增加带 `parent`、`matching_hints`、`provider_preference`、`tier_overrides` 的条目。
2. 增加引用该类别的一个或多个 `scenarios`。
3. 场景 `provider_preference` 可留空以继承，或给出显式列表。
4. 运行离线校验：`python -m unittest discover -s tests -p test_scenario_registry.py -v`。

无需修改 Python 表。

## 安全与运维

- 注册文档中不得放入 API 密钥、令牌或真实账户 id。
- `config/quant.example.json` 中 API 逻辑 provider 在另行授权前保持未绑定；未就绪时由共享运行时 fail-closed。
- 本变更不部署服务、不轮换密钥、不启用 provider。
