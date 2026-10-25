# 组织 AI 任务服务

独立部署的 Cloudflare Worker、OAuth KV 和 SQLite Durable Object。此目录的生产入口使用 Quant 状态空间；Pigbibi 回归夹具使用单独的测试状态类。保留 DO 类名 PersonalTaskStore 仅为存储绑定兼容，组织身份由服务端配置限定。

Dot 是当前 agent 执行方。API 模式保留同一任务提交、查询、事件和幂等合同，当前支持旧服务已有的 Gemini 文本 JSON 协议；默认关闭。Claude、Grok 不启用。

API 路由由受保护的服务端配置注入：项目 `PROJECT_POLICIES[project].providers` 的 `api` 或 `api:<profile>` 指向显式 provider 别名，`models[别名]` 限定模型。另在 Worker 的 `API_PROVIDERS[别名]` 配置 `type: "gemini_api"`、`enabled: true`、`api_usage_authorized: true`、准确 `model` 和 `key_env`（已有 Secret 的绑定名）；可设置 `max_output_tokens`（1..8192，默认1024）与 `timeout_seconds`（1..60，默认60）。缺少任一启用、授权、模型或凭据条件时拒绝 API 请求，不影响 Dot。这里的布尔配置记录既有批准，本身不授予付费授权；生产启用与真实调用仍需相应授权。

API 只调用固定的 Google `generateContent` HTTPS 端点，不接收调用者提供的 URL，不跟随跳转、不发现模型、不重试推理、不回退其他收费通道。仅接收文本数据材料，不提供工具或更改安全设置。收到模型与配置不同、非正常终止或不符合输出 schema 的结果不会作为成功输出。HTTP拒绝记为失败；连接丢失、服务端错误或无法确认的回执保留 `outcome_unknown`，原冻结意图仍可查询，不重发新任务。执行前先持久化 submitting；网络在 SQLite 事务和阻塞整个 DO 的短临界区以外。同步 API 的取消仅表示已请求，未声称远端已停止；若随后收到已核验的完整回执，仍按该事实记录结果。

本实现只移植 Gemini API 纵切，旧 ChatGPT-plan OAuth Responses 提供者尚未迁入 Worker；不能据此认定所有旧 API 提供者或整个迁移已完成。

## 本地验证与部署

运行 `npm ci`、`npm test`、`npm run build`。测试在本地 workerd 中分别验证 Quant、Pigbibi，以及跨组织、错误 owner 拒绝；GitHub、webhook 与 Gemini HTTP 使用合成替身，包含真实 SQLite 状态、输出验证、跨组织、未知回执与不重发回归；不证明线上 Dot 或生产 API 完成。

将 wrangler.example.jsonc 复制到受保护配置目录并填入已有资源与批准的项目策略；真实账号、客户端和授权名单不进入仓库。GitHub client secret 通过批准的凭据存储配置。部署由统筹执行 `wrangler deploy --config /absolute/private/wrangler.jsonc`，不依赖默认目录。占位示例不可直接上线。

公有 OAuth 客户端注册脚本要求 `node scripts/register-clients.mjs --config /absolute/private/registration.json`，配置含 organization、PROJECT_ID、PUBLIC_ORIGIN。远端模式另需 --remote 与准确 --namespace-id。已有 .local 注册计划时先读回，禁止盲重；不发 grant 或 token。

生产采用须分别验证 OAuth、active 订阅、同一任务提交/领取/回传/原客户端消费和重连恢复；这些通过后才能切旧调用者并清理 VPS 专属项。
