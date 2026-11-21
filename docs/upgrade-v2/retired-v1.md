# V1 入口退役

本次为不保留兼容接口的 2.0 升级。以下旧源码和对应实现测试从活动工作区删除，原版可从 Git 基线 `<pre-v2-baseline-commit-sha>` 查阅；历史业务证据、运行数据和生产配置未删除。

通用任务服务替代 VPS Codex/Cursor 进程、固定 provider 路由、旧 HTTP SDK、模型目录同步和旧代理。原来的定时工程审计、依赖通知、账户诊断、组合提案及固定策略编排不再自动运行。需要这些功能时由调用方提交材料与目标，不能恢复旧 workflow 作为兼容入口。

SOXL、Global ETF、CN 研究入口在对应策略仓；平台候选修复在 LongBridgePlatform；共享 watcher、漂移审查、工程材料审查在 QuantPlatformKit；监控日报的消费和发送仍在 ops/quant-monitor。候选采用、数据准入和金融权限仍由业务方控制。

旧 Cloudflare 看板（`cloudflare/ai-gateway-dash` 与 `publish-ai-gateway-dash.yml`）仅保留历史提交记录，已从活动工作区移除；相关身份配置核对资料保留于本目录。云端旧服务停用和已部署工作流的切换按 V2 迁移进度推进。

删除的源码与测试文件共 212 个。
