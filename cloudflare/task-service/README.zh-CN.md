# 组织 AI 任务服务

独立部署的 Cloudflare Worker、OAuth KV 和 SQLite Durable Object。此目录的生产入口使用 Quant 状态空间；Pigbibi 回归夹具使用单独的测试状态类。保留 DO 类名 PersonalTaskStore 仅为存储绑定兼容，组织身份由服务端配置限定。

Dot 是当前 agent 执行方。API 模式保留同一任务提交、查询、事件和幂等合同，当前支持旧服务已有的 Gemini 文本 JSON 协议；默认关闭。Claude、Grok 不启用。

MCP 的资源 metadata 和未登录 challenge 广告 `AUTH_CLIENTS` 中精确匹配该资源、provider 非 null 的已配置工作角色 scope 并集，顺序沿服务端 `SCOPES`；不根据请求、当前 token 或 REST 调用方推断权限。该广告供客户端在正常授权时申请所需能力，不是执行许可：MCP 基本操作仍要求 `tasks:work`，订阅及其管理仍要求 `events:subscribe`，原 client、admission、组织、项目和 audience 校验保持不变。REST 继续只广告当前读取或提交操作的最小 scope。

部署这个广告修复不会扩大已有 grant。已准入的 Dot 连接可发现事件能力及五个工具，各工具的 `securitySchemes` 明确声明所需 scope。若旧连接仅授予 `tasks:work`，它仍不能执行订阅或读取、删除订阅记录：原生事件请求返回官方 OAuth 403 scope challenge，订阅管理工具返回带 `_meta["mcp/www_authenticate"]` 的错误结果，提示正常补充授权；拒绝时不写订阅、不发送 webhook。普通任务操作仍只要求 `tasks:work`。

需要使用原插件正常授权所需 scope，再核验事件目录、active 订阅及真实任务结果，不能修改 KV grant 或借用其他客户端 token 代替。资源 metadata 更新不保证宿主立即更新已保存的 scope；以工具和事件实际发现、授权页面及最终 grant 为准。

API 路由由受保护的服务端配置注入：项目 `PROJECT_POLICIES[project].providers` 的 `api` 或 `api:<profile>` 指向显式 provider 别名，`models[别名]` 限定模型。另在 Worker 的 `API_PROVIDERS[别名]` 配置 `type: "gemini_api"`、`enabled: true`、`api_usage_authorized: true`、准确 `model` 和 `key_env`（已有 Secret 的绑定名）；可设置 `max_output_tokens`（1..8192，默认1024）与 `timeout_seconds`（1..60，默认60）。缺少任一启用、授权、模型或凭据条件时拒绝 API 请求，不影响 Dot。这里的布尔配置记录既有批准，本身不授予付费授权；生产启用与真实调用仍需相应授权。

API 只调用固定的 Google `generateContent` HTTPS 端点，不接收调用者提供的 URL，不跟随跳转、不发现模型、不重试推理、不回退其他收费通道。仅接收文本数据材料，不提供工具或更改安全设置。收到模型与配置不同、非正常终止或不符合输出 schema 的结果不会作为成功输出。HTTP拒绝记为失败；连接丢失、服务端错误或无法确认的回执保留 `outcome_unknown`，原冻结意图仍可查询，不重发新任务。执行前先持久化 submitting；网络在 SQLite 事务和阻塞整个 DO 的短临界区以外。同步 API 的取消仅表示已请求，未声称远端已停止；若随后收到已核验的完整回执，仍按该事实记录结果。

本实现只移植 Gemini API 纵切，旧 ChatGPT-plan OAuth Responses 提供者尚未迁入 Worker；不能据此认定所有旧 API 提供者或整个迁移已完成。

## GitHub Actions 调用方身份

REST 机器调用方可使用 GitHub Actions OIDC JWT。`TaskClient` 分别配置服务 URL 与 token audience；旧 Python 服务对 REST 和 MCP 都核对同一个 `config.resource`，不能因 REST 路径是 `/v1/tasks` 就替原调用方重写 audience。本入口与 Dot 的 OAuth、MCP 身份分开，不能领取任务、回传结果或管理事件订阅。

配置机器 grant 时必须同时显式设置服务端 `GITHUB_WORKLOAD_AUDIENCE`，值为该原调用方已批准的准确 audience（包括既有 `/mcp` resource 的场合）；必须是单个、不含凭据、query、fragment或空白的 HTTPS URL。缺失或无效均拒绝，不推断旧地址，不同时接受新旧两个 audience。迁移时可仅更改调用方的服务 URL，保留其原 token audience；这里的值只用于 JWT 精确核对，不作为请求 URL，也不会开放 MCP。内部服务绑定的 resource 仍固定为当前 `PUBLIC_ORIGIN + "/v1/tasks"`。

默认不准入任何机器调用。受保护服务端变量 `GITHUB_WORKLOAD_GRANTS_JSON` 只接收一个准确既有调用方的 grant，沿用 Python 的 `issuer`、`subject`、`identity`、`project`、`scopes` 和 `claims` 字段。`project` 必须等于当前 `PROJECT_ID` 在 `PROJECT_CONTEXTS` 中的准确仓库，owner 和组织也由该映射提供；不能用其他仓库的 token、请求头或请求参数更改项目。不得直接复制占位值上线。例如，以下仅表示合成配置的结构：

```json
[
  {
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:example-org/ExampleProject:ref:refs/heads/main",
    "identity": "example-project-workflow",
    "project": "example-org/ExampleProject",
    "scopes": ["tasks:read", "tasks:submit"],
    "claims": {
      "repository": "example-org/ExampleProject",
      "workflow_ref": "example-org/ExampleProject/.github/workflows/ai-task.yml@refs/heads/main",
      "ref": "refs/heads/main"
    }
  }
]
```

按 [GitHub OIDC 参考](https://docs.github.com/en/actions/reference/security/oidc)及[固定发现文档](https://token.actions.githubusercontent.com/.well-known/openid-configuration)，只接受固定 issuer、HTTPS JWKS 与 RS256；验证唯一字符串 audience、kid、签名、exp/iat/nbf、subject、仓库、workflow_ref 和分支 ref，不使用 JWT 的 jku/x5u。密钥缓存5分钟；未知 kid 或取钥失败均拒绝，不回退其他身份源。

普通分支 subject 必须与准确仓库及 ref 一致；若既有仓库使用 immutable subject，必须在 grant 中同时准确绑定 `repository_owner_id`、`repository_id`，并使用 GitHub 实际的精确 subject。不能据仓库名猜测或自动转换 subject。PR、PR target、tag、环境 subject 和多仓 grant 均不进入本次准入。

GitHub 通常不提供 OAuth scope；权限取自有限 grant，如 token 显式带 scope 则只取交集。GitHub 签名的 `repository_owner` 必须等于准确仓库 `project` 的所属组织；`PROJECT_CONTEXTS.ownerId` 是既有内部身份名，可以与 GitHub 组织名不同，进入 RPC 时原样保留，不为迁移改名。内部 RPC 的 `githubUserId` 对机器调用方沿用 GitHub 签名 `actor_id` 的数字形状，仅作身份记录，不能据此授予权限；`provider` 固定为 null。外部提供的身份、项目、组织和 provider 头在进入服务绑定前删除。业务 `mode` 仍由既有项目策略选择 Dot 或已明确启用的 API，本鉴权不会启用任何 provider。

本包开放 `POST /v1/tasks`、`GET /v1/tasks/lookup`、`GET /v1/tasks/{id}`，并保留已存在的 `POST /v1/tasks/{id}/cancel`（JSON body 必须为 `{}`，权限仍为 `tasks:submit`）。保留原幂等键、请求 SHA256、任务 ID、未知任务404与冲突409；取消沿用既有幂等及终态语义，不能把已失败任务改称已取消。

隔离单位沿用原合同的仓库 project：task owner 是仓库，只有当前精确 grant 准入的项目可访问/取消该项目任务，不能跨项目或组织；不声称已提供同一项目内逐人或逐 client 的提交者隔离。SDK 的 message、任务事件读取等其他操作尚未迁入此机器入口，不能据此宣称整个 SDK 已兼容。

生产验收还需核定一个实际原调用程序、其有效原 grant/audience、准确服务端映射及受限配置。由原程序请求真实 GitHub token，提交有限任务后用同一幂等键和任务 ID 完成查询及原客户端消费；单独的 events 合成提交脚本不等于业务调用闭环。离线测试不替代该闭环。

## 本地验证与部署

运行 `npm ci`、`npm test`、`npm run build`。测试在本地 workerd 中分别验证 Quant、Pigbibi，以及跨组织、错误 owner 拒绝；GitHub、webhook 与 Gemini HTTP 使用合成替身，包含真实 SQLite 状态、输出验证、跨组织、未知回执与不重发回归；不证明线上 Dot 或生产 API 完成。

机器入口另运行 `node test/workload.mjs`：本地生成临时 RSA 密钥、模拟固定 JWKS，验证两组织本地 SQLite 提交/读取/lookup、错误身份和越权拒绝；不请求外网 JWT、不调用模型，也不把测试签发能力放进生产 bundle。

将 wrangler.example.jsonc 复制到受保护配置目录并填入已有资源与批准的项目策略；真实账号、客户端和授权名单不进入仓库。GitHub client secret 通过批准的凭据存储配置。部署由统筹执行 `wrangler deploy --config /absolute/private/wrangler.jsonc`，不依赖默认目录。占位示例不可直接上线。

公有 OAuth 客户端注册脚本要求 `node scripts/register-clients.mjs --config /absolute/private/registration.json`，配置含 organization、PROJECT_ID、PUBLIC_ORIGIN。远端模式另需 --remote 与准确 --namespace-id。已有 .local 注册计划时先读回，禁止盲重；不发 grant 或 token。

生产采用须分别验证 OAuth、active 订阅、同一任务提交/领取/回传/原客户端消费和重连恢复；这些通过后才能切旧调用者并清理 VPS 专属项。
