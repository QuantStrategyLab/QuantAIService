# QuantAIService

示例配置的 Agent 任务由已启用的主 Agent provider 执行。API 任务走组织单独配置的后端，不依赖 Agent provider 订阅。启用 API 后端前先配置已批准的模型与凭据。

[文档索引](docs/README.md)：当前任务运行时、V2 配置、监控与历史架构。

独立打包、按项目隔离的 AI 任务运行时。

[English](README.md) | 简体中文

## 状态

实验性项目，使用风险自负。本项目不构成投资建议，也不保证任何交易结果。它是运维工具，不提交订单，也不授予交易权限。

## 简介

QuantAIService 运行按项目隔离的 AI 任务。API 任务和持久助手任务共用一套契约，使用该服务的每个仓库各自维护运行时、schema、提示词和数据验证。本包提供服务端 `quant-ai-service`、客户端 `ai-task`，以及用于 `ScenarioTaskSpec/v1` 和 `ScenarioInstance/v1` 的离线场景 SDK，两者均按 JSON Schema 校验。

## 不适用范围

它不负责策略提示词、数据验证、执行或结果采用，这些由各自的业务仓库负责。它不是本系列仓库的共享核心。它不提交订单，也不会自行授权任何工作。

## 快速开始

需要 Python 3.10 或更高版本。

```bash
python -m pip install .
python -m unittest discover -s tests/task_service -v
```

用你自己的配置运行任务服务端。仓库内的 `config/quant.example.json` 默认关闭，仅作起点。

## 配置

月报分别从[各档已批准的有序型号清单](config/monthly-audit-models.json)选择 GPT 和 Claude。正式月报从 `standard` 档开始，证据达到 50,000 个 UTF-8 字节后升为 `complex`，合成验证使用月报默认档。两家用各自的密钥读取模型元数据。仅有 Responses 权限的 OpenAI 密钥可直接推理，仅在服务商明确返回模型不存在后切换候选。回执记录复杂度、选择方式和服务商确认的实际型号，两家分别保留成功或失败结果。Claude 优先选择单价较低的 Sonnet 5 系列，也可回退到 Sonnet 4.6。

将仓库变量 `OPENAI_PRICING_ORIGIN` 和 `ANTHROPIC_PRICING_ORIGIN` 分别设为下列官方定价站点的 HTTPS 源。每次月报和合成验证都读取 [OpenAI 标准定价](https://developers.openai.com/api/docs/pricing)与 [Claude 基础定价](https://platform.claude.com/docs/en/about-claude/pricing)。GPT mini 和 Claude Sonnet 家族的新稳定型号在输入和输出价格都符合当前档 `rate_caps` 后加入候选。上限单位为每百万 token 的美元价格。服务先尝试版本较新的型号，再尝试已批准的基线。价格资料不可读取时仅使用基线。公开单价超过上限的型号与日期快照一并排除。回执记录此次名单依据。

| 变量 | 必填 | 默认值 | 用途 |
|---|---|---|---|
| `AI_SERVICE_URL` | 是 | 无 | 任务服务的基础 URL。 |
| `AI_SERVICE_TOKEN` | 否 | 无 | 客户端使用的 bearer token。 |
| `AI_SERVICE_AUDIENCE` | 否 | 无 | 工作负载身份 token 使用的 audience。`client_from_env()` 辅助函数要求设置；`ai-task` CLI 在已设置 `AI_SERVICE_TOKEN` 时可省略。 |

使用 `ai-task` CLI 时，设置 `AI_SERVICE_URL`，并在 `AI_SERVICE_TOKEN` 与 `AI_SERVICE_AUDIENCE` 中二选一。场景 pilot CLI 使用的 `client_from_env()` 辅助函数则要求同时设置 `AI_SERVICE_URL` 与 `AI_SERVICE_AUDIENCE`；`AI_SERVICE_TOKEN` 仍为可选，未设置时该辅助函数会以该 audience 申请 GitHub 工作负载身份 token。

服务端配置存放在以 [`config/quant.example.json`](config/quant.example.json) 为模板的 JSON 文件中，定义监听地址、SQLite 状态路径、接受的 token 签发方以及 provider 后端。请把所有 `CONFIGURE_*` 占位符替换为你自己的值。

参考配置默认启用主 Agent provider 槽位（`enabled: true`）用于策略回测与代码审查（`strategy_backtest_review`、`pr_code_review`），将未接通的其余 Agent 槽位保持 `enabled: false` 且不填 URL/Secret，并在 `API_PROVIDERS` / `providers` 中提供默认关闭的 `openai`（`openai_api`）、`claude`（`anthropic_api`）、`gemini`（`gemini_api`）及 `chatgpt-plan` 占位条目，以及 `report_audit`、`document_translation`、`quick_notification`（`mode: "api"`）和复杂度自适应场景 `strategy_research_summary` / `signal_context`（`mode: "auto"`）。各复杂度档位的 API 候选型号配置与定期刷新见 [`config/api-tier-models.json`](config/api-tier-models.json) 与 `scripts/refresh_api_models.py`。

## 仓库结构

```
src/        任务服务端、客户端与场景 schema
config/     示例服务配置
deploy/     部署模板
ops/        监控辅助
docs/       设计说明与契约
tests/      单元与契约测试
```

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。AI 助手与维护者遵循 [AGENTS.md](AGENTS.md)。

## 安全

见 [SECURITY.md](SECURITY.md)。安全漏洞不要开公开 issue，也不要让凭据、私有数据和外部服务 token 进入 Git 或日志。

## 许可证

MIT，见 [LICENSE](LICENSE)。task-service 组件另有自己的 MIT 许可证，见 [`LICENSE.task-service`](LICENSE.task-service)。

Agent provider 槽位的启用与准入规则见 [agent providers](docs/agent-providers.md)。
