# 组织 AI 任务服务

独立部署的 Cloudflare Worker、OAuth KV 和 SQLite Durable Object。此目录的生产入口使用 Quant 状态空间；Pigbibi 回归夹具使用单独的测试状态类。保留 DO 类名 PersonalTaskStore 仅为存储绑定兼容，组织身份由服务端配置限定。

Dot 是当前 agent 执行方。API 模式尚未接入，收到 API 请求会明确拒绝，不能据此认定整个旧服务迁移完成。Claude、Grok 不启用；未来支持须显式配置。

## 本地验证与部署

运行 `npm ci`、`npm test`、`npm run build`。测试在本地 workerd 中分别验证 Quant、Pigbibi，以及跨组织、错误 owner 拒绝；GitHub 和 webhook 使用合成替身，不证明线上 Dot 完成。

将 wrangler.example.jsonc 复制到受保护配置目录并填入已有资源与批准的项目策略；真实账号、客户端和授权名单不进入仓库。GitHub client secret 通过批准的凭据存储配置。部署由统筹执行 `wrangler deploy --config /absolute/private/wrangler.jsonc`，不依赖默认目录。占位示例不可直接上线。

公有 OAuth 客户端注册脚本要求 `node scripts/register-clients.mjs --config /absolute/private/registration.json`，配置含 organization、PROJECT_ID、PUBLIC_ORIGIN。远端模式另需 --remote 与准确 --namespace-id。已有 .local 注册计划时先读回，禁止盲重；不发 grant 或 token。

生产采用须分别验证 OAuth、active 订阅、同一任务提交/领取/回传/原客户端消费和重连恢复；这些通过后才能切旧调用者并清理 VPS 专属项。
