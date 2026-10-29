# Quant deploy config 基线变更计划

状态：PROPOSED；本卡仅建立准确变更方案，不批准或执行服务器配置写入、部署、重启及密钥交付。独立审查通过表示方案可供后续配置变更使用，不表示部署前置已满足。

## 已核实事实

2026-10-23 UTC+8 通过既有 qvps SSH + sudo 只读确认：

- `/etc/ai-services/quant-deploy.json` 不存在。现有 helper `/usr/local/sbin/quant-ai-deploy` SHA256 为 `1f44bedab7d91ed7212892a71e0fdb397eb83bbe8ea0ecdc8767275aa9facee3`，仍要求 root config 和 config hash。不得跳过 `Settings.load` 或 `config_gate`。
- 当前 ExecStart release：`20261014-quant-v2.0.7-4769cf6`。
- 该 release 的 `source-manifest.json` 中 shim source SHA 为 `4769cf6de39012b0691027c35d33aef30edbef3a`，原仓库名 `QuantStrategyLab/AIAuditBridge`；本机 Git 对象独立解析到相同完整 SHA。core 为原 `Pigbibi/AIGateway` 的 `9aeaa93e61856facd01f905f5f5466bd93acfe1f`，版本均为 2.0.7。这是现有旧运行组合，不把当前独立 wheel 当其已采用。
- release 包含 `venv`、`wheels`、`requirements.txt`、`source-manifest.json`、`metadata/prior-target-dropin.conf`。本轮未验证其全部 wheel/venv 内容、drop-in 字节等价或可回滚性。
- 当前业务 config SHA256 `bba1cc8a90f11741aa07b331bd4494bcc1adec2249e2f040a0b58a1fc25ed8f1`；这是当前文件的读值，不是未来候选的 hash。AI-7D-03 实读 owner 缺失，本轮没有修改。当前 github_apps/Grok provider 仍未配置。

## 拟写集与步骤（后续配置变更执行时）

1. 重新只读核对 helper hash、当前 ExecStart、service User/Group、Python 3.12 + venv、数据库实际路径及现有 drop-in。现有 paths 参考 `deploy/vps/ci-deploy/README.md`，不从默认值猜实际配置；保存 root-only 原文件与字节 hash。出现漂移先修订本卡，不按旧值写入。
2. 若当前业务配置仍缺 owner，在服务器本地生成仅增加 `owner=QuantStrategyLab` 的候选，保留所有原单项目 grants/claims/providers/policies；不加入 Grok，不加入 App key 引用，不扩大 owner-wide worker 授权。分别由拟合并的新版本和旧基线的 `--check-config` 验证。用现有脱敏兼容验证证明旧基线保留依赖、DB schema 和新候选可用；任何缺项停止对应操作。
3. 核实 helper 对批准 baseline 的 drop-in 渲染与现有准确字节等价，完整 baseline venv/依赖、健康版本及 DB 兼容性已获证；不调用 rollback/deploy/start/stop 来验证本卡。若不能证明旧基线可恢复，不建立可启用的部署配置。
4. 对最终批准候选的实际字节计算 SHA256，不能复用上面的旧 config hash。审查精确配置 diff、source SHA、baseline、所有路径和新 hash 后，才在单独明确授权的配置变更中安装业务 config 和 `/etc/ai-services/quant-deploy.json`。后者必须 root-owned regular file、0600、root-owned parents，原子写入并保留回滚副本。
5. root config 必须包含核实后的 releases_dir/config_path/db_path/dropin_path/lock_path/python/port、上述完整 baseline_release/baseline_source_sha 和候选 config_sha256；不能把本卡的文字或未知字段写为运行配置，不生成带占位值的 root JSON。
6. 安装后仅运行不改变服务的 Settings.load、精确白名单 readback 和 config hash 对比；只输出路径/权限/hash/基线等非秘密字段。失败立即停止受影响操作，依据已保存字节恢复本次配置更改，不顺带部署、重启或更换密钥。

## 验证与回退

本轮运行既有离线 `tests/test_vps_deploy.py`，覆盖缺文件、所有权/模式、hash drift、基线一致性与原部署保护；只新增文档，不更改 helper 或放宽门槛。未来真实配置写入另记实际命令/退出码及 readback。服务器恢复原业务 config 和 root config 的配置回退不等于发布/数据库回退；不因本卡建立而执行服务切换。

## 尚缺证据

准确候选 config 字节/新 hash、实际 db/Python/drop-in 核对、完整 baseline 兼容验证、配置写入授权及执行回读。App 开启还须 AI-7D-KEYPLAN-01 批准真实 key 消费并独立核实四 App 身份；Grok 连接继续 BLOCKED。不得将上述事项写成“仅差密钥”或“可部署”。
