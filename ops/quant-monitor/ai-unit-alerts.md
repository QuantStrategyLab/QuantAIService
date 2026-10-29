# AI unit 连续失败通知

独立 observer 每分钟只读 systemd journal，复用 QuantPlatformKit 的
`send_telegram_message` 和现有量化哨兵 Secret/收件路由。不修改被观察 unit，
不重启业务，不发订单，不改生产 cron。默认 unit 清单在 `DEFAULT_UNITS`。

## 计数和恢复

- 以 systemd 的不同 `INVOCATION_ID` 计数，不把同一失败的多条 journal、
  重复轮询或 `JOB_RESULT` 当作新失败。达到两次后每个失败周期只发一次。
- oneshot 的成功完成打断连续失败；daemon 必须新 invocation 持续 active
  至少 300 秒才算恢复，避免 `Restart=` 的短暂 started 反复清空计数。
- 启动时读取最近一小时，之后持久化 journal cursor；状态损坏或 cursor
  不可用时退出非零，不抹掉状态重新计数。
- 恢复实际发生后发送一次恢复通知。定时观察误差通常不超过 65 秒；正常网络
  下的验收目标为失败事件完成后 15 分钟以内，并非网络中断期间的送达保证。
- 发送前持久化 claim，每个收件路由分别记录脱敏回执；只有 Telegram 返回
  匹配路由的 `message_id` 和 `date` 才记 delivered。该回执不是客户端已读证明。
  HTTP 4xx/明确拒绝可重试；超时、未知结果或中途崩溃不自动重发，防止重复。
  `claimed/unknown` 退出 2，需人工核对消息后再处理该条回执，不得删除整个状态。

## 部署

将脚本放到 root-owned `/opt/quant-ai-unit-alerts/ai_unit_alerts.py`，将两个
`systemd/quant-ai-unit-alerts.*.example` 安装到 `/etc/systemd/system/`。
新 observer 只使用自己的 `/var/lib/quant-ai-unit-alerts/state.json`，
不竞争原有 `/run/quant-monitor`。

`/etc/quant-ai-unit-alerts/route.env` 只复制已批准的
`QUANT_SENTINEL_GCP_PROJECT`、`QUANT_SENTINEL_TELEGRAM_SECRET_NAME`、
`GLOBAL_TELEGRAM_CHAT_ID`，权限 root:ubuntu 0640；不在仓库或回执展示值。
读取与现有 loader 相同的 Secret；token 只从 gcloud 受限客户端的 stdout
进入进程内存，不写文件、不进入 curl 命令参数。不新建 Secret、不修改 IAM。
配置 `PYTHONPATH` 指向当前已审核 QPK 的 `src`，提供既有通知 helper；不升级依赖。

```bash
sudo systemd-analyze verify /etc/systemd/system/quant-ai-unit-alerts.service /etc/systemd/system/quant-ai-unit-alerts.timer
sudo systemctl daemon-reload
sudo systemctl enable --now quant-ai-unit-alerts.timer
sudo systemctl start quant-ai-unit-alerts.service
```

systemd 告警的 run 字段是可精确定位 invocation 的 journalctl 查询，不伪造
GitHub run。验收模式使用 `--workflow-repo/--workflow-file/--workflow-branch`
读取真实 Actions run 和失败 step；该模式单次失败即告警，与 unit 的两次阈值分离。
只观察指定手动测试分支，最多 100 个 run，超过时停止而非跳过历史。
测试状态与生产 unit 状态必须分开；测试完成后移除临时 observer 调度和分支故障。

```bash
python3 -m unittest discover -s ops/quant-monitor/tests -p test_ai_unit_alerts.py -v
```
