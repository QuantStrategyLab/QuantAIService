# AI-7D-04 Quant deploy config 变更卡

- 类型：独立基础配置变更提案；本 PR 只提交文档，不部署、不写服务器、不迁移密钥。
- 问题：helper 必需的 `/etc/ai-services/quant-deploy.json` 缺失，不能更新 hash 或绕过 gate。
- 目标：核实旧基线与候选配置兼容后建立受保护 root config，并精确 pin 最终候选 hash。
- 当前已核 baseline：`20261014-quant-v2.0.7-4769cf6` / `4769cf6de39012b0691027c35d33aef30edbef3a`，由运行 release manifest 及 Git 对象核实。
- 本次文件：本目录 change-plan.md、change-card.md；App 实现及功能审查在同一 PR 的独立章节记录。
- 风险：旧运行仍依赖历史 shared core；缺 owner、新旧配置/DB 不兼容或 drop-in 不等价会使回滚不可用。没有证明前保持部署阻塞。
- 验证：既有 helper 离线测试；方案经非作者独立审查。当前没有安装/配置成功或 E2E 结论。
- 回退：本次为文档，撤回文档不改变运行。未来配置变更须保留原字节/root config 不存在状态，在不切换服务的条件下恢复。
- 后续授权：任何实际服务器配置写入及部署独立执行，不因本卡合并而获得权限。
- 详细步骤及缺项：见 change-plan.md。禁止以旧 hash、默认路径、短 SHA 或未经批准的 App/Grok 数据补空。
