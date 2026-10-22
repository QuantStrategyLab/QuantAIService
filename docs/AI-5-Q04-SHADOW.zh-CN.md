# AI-5：Q04 七天只读影子检查

唯一目标是 QuantAIService v2。`config/ai-hook-shadow.json` 只存变量引用，严格拒绝第二目标、正式模式和额外配置字段。所需 URL、audience、profile、model 和只读凭据必须全部由运行环境注入；没有默认值。各调用仓库使用同一份绑定契约，不在代码中放组织、域名、账户或实际 secret 名称。

`AI_HOOK_READ_TOKEN` 是进程内凭据别名，不是 secret 存储中的名称。工作流通过 `vars.AI_HOOK_READ_TOKEN_SECRET` 选择已有 secret；不创建凭据、不改 IAM、不扩大授权。若没有现成且获准的凭据，该次检查记 `blocked_configuration`，退出码 2，不联网、不提交任务。组织变量的实际注入与权限验收尚未完成；绑定文件不是配置已生效的证明。

## 能力边界

- `research_hook.observe` 只发送固定 MCP `initialize`，核对服务名与 v2 版本；可选读取一个**已存在**的 task，不能创建、取消、跟进或执行 task，也不调用 worker 工具。
- 输出仅含状态、计时与布尔标志，不记录 URL、身份、模型配置、凭据、task ID、task 输出或远端异常正文。HTTPS transport 不跟随重定向并拒绝非公网地址。
- 研究仓库的 Q18 hook 保持返回 `unavailable`，不使用传入的 client；月度 hook 只落本地影子记录，不提交 Dot、不关闭 Issue、不通知交易动作。此处不启用其他已有工作流或任何正式 AI 动作。
- 这不是 Python 沙箱或服务端全部能力的证明；范围是这几个 hook 的可达调用。已有服务和工作流的其他入口不在此获得任何新增权限。

## 启动与每日检查

启动时间、七天结束时间及 D0–D7 检查点以 `q04-shadow-plan-20261022.json` 为准。D0 为启动记录，D1–D7 分别是每满 24 小时的检查点。GitHub 工作流每天 UTC 13:00（新加坡 21:00）保存一次记录，也可人工触发同一只读工作流。合并前的本地启动记录与合并后的 Actions 运行应分别保留，不能互相冒充。

每日检查：

1. 核对 `day`、`checked_at` 与计划，不把重复采样算作额外一天。
2. 查看 `blocked_configuration`、认证失败或服务身份不符；不尝试备用服务或补发任务。
3. 确认 `ai_submitted=false`、`formal_actions_enabled=false`；如有反例停止该检查。
4. 记录当天是否只有服务身份检查、是否读到已有 task；两者都不等于 Q04 原生协议验收。
5. 保存 artifact 链接、退出码、失败原因和人工复查结论。缺失日保留缺失，不补造成功。

记录模板：

| 日次 | 应检查时间 | 实际检查时间 | 运行链接/本地记录 | 退出码 | 绑定/身份/已有任务状态 | 违规调用数 | 人工结论 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D0–D7 各一行 | 从计划取 | 待填 | 待填 | 待填 | 待填 | 待核 | 待核 |

七天未满统一写“**影子进行中**”。到期后只输出 `shadow_elapsed_review_required`，不再访问服务，也不自动开启正式动作。工作流保留为无业务请求的到期检查；可由维护者随后停用该工作流。七天经过、每日检查齐全、绑定配置可用、Q04 原生协议验收、用户正式启用决定是不同事项。

## 尚未验收

原 `run_historical_diagnosis_rehearsal` 要求明确的合成输入与 `offline.fixture` subscription creation 证据。本检查**不伪造 native subscription、不提交新任务**，始终 `q04_protocol_accepted=false`。因此已启动的是记录与观察计划，不是原生 Q04 成功执行，更不是七天影子验收完成。缺少组织配置或原生运行材料时，七天结束仍应报告未完成，正式启用仍须用户另行决定。

当前服务端 MCP `initialize` 要求 `tasks:work`；仅有 `tasks:read` 的现成身份会被拒绝。独立审查已用真实 dispatcher 复现。本变更不修改该授权规则，也不向观察凭据增加 worker 权限，因此此情况必须保持 `observation_unavailable`，不能当作认证或服务身份已通过。只读观察的真实接通仍有此协议兼容阻塞；配置补齐也不会自动消除此项。
