# Monthly audit evidence / 月审证据汇总

## Scope / 范围

`Monthly Audit Evidence Report` collects existing GitHub issues, comments and
workflow runs from `QuantStrategyLab/HkEquitySnapshotPipelines` and
`QuantStrategyLab/UsEquitySnapshotPipelines`. Both source repositories are public;
the workflow uses its existing `GITHUB_TOKEN` to read public evidence and create
only a central `monthly-review` issue in QuantAIService. When `MONTHLY_API_AUDIT_READY=true`, a scheduled run also sends the public evidence to the
configured OpenAI and Anthropic APIs (plus Gemini when `GEMINI_API_KEY` is configured) for independent advisory reviews under a strict `union_of_blockers_no_majority_override` arbitration policy.
Existing repository secrets and model variables configure these paid calls.

本任务按月汇总两个公开源仓库的现有报告。设置 `MONTHLY_API_AUDIT_READY=true` 后，定时运行会各自独立调用 GPT 与 Claude（并在配置 `GEMINI_API_KEY` 时纳入 Gemini 作为可选第三独立审计方，采用“任一阻塞即阻塞、不以多数票覆盖”的 `union_of_blockers_no_majority_override` 原则），
将独立建议与模型回执写入审计工件。源问题仍由源 issue 跟踪，
`quality_accepted` 始终为 `false`。

## Schedule / 时间

- Cron: `17 4 3 * *`, each month's third day at 04:17 UTC / 12:17 Asia/Singapore.
- First expected run after the October 2026 change: **2026-11-03 12:17 SGT**,
  only after the workflow has been merged into the default branch.
- GitHub schedules may be delayed; an inactive public repository may have its
  scheduled workflows disabled after 60 days. Check the actual run, not cron alone.

每月 3 日给源仓库留出产出时间。缺少源 issue、run、审阅评论或读取失败时，在汇总中
明确标注“证据不足”，不伪造补审。成功 run 只证明源工作流执行成功。

## Manual validation / 手动验证

```bash
PYTHONPATH=src python3 -m unittest tests.test_monthly_audit_report tests.test_monthly_api_audit -v
python3 scripts/monthly_audit_report.py --month 2026-11
gh workflow run monthly-orchestrator.yml --ref main -f month=2026-11 -f dry_run=true
```

The script is read-only unless `--publish` is explicitly supplied. A scheduled
run publishes; manual runs default to dry-run. The workflow uploads the report
JSON for 90 days and writes a job summary, including dry-runs. The issue preserves
source body excerpts, comment excerpts and full-text SHA256 hashes at observation
time. Hashes do not prove historical completeness or remediation. Long texts are
explicitly truncated; read linked sources for the full text.

`api_probe=true` submits one synthetic request per selected provider (`both` for OpenAI + Anthropic, `all` for OpenAI + Anthropic + Gemini, or a single named provider) from `main`.
It only publishes a verification artifact and incurs provider usage. When the
probes report the configured models successfully, set
`MONTHLY_API_AUDIT_READY=true` to activate scheduled inference. In `--report`
mode, `GEMINI_API_KEY` is optional: when unset, Gemini records
`skipped_unconfigured` with zero external requests and runs in `dual_ai` mode;
when configured, all three providers must complete (`triple_ai`, no 2-vs-1
majority override). Each scheduled audit stores provider outputs separately from
the evidence report. Automatic reruns skip paid requests and retain the
original run's results.

已有同月 issue（含已关闭项）时只复用链接，不覆盖正文、不追加重复评论、不重开或关闭
任何 issue。重跑的新读取结果留在该 run 的 artifact / summary。若首次汇总缺少源报告，
原汇总不会自动升级为“质量通过”；后续仍需人工审阅和补充证据。

## References / 参考

- [Workflow](../.github/workflows/monthly-orchestrator.yml)
- [Source report-only contract](https://github.com/QuantStrategyLab/UsEquitySnapshotPipelines/blob/main/docs/snapshot-ai-audit-automation.md)
- [Retired v1](upgrade-v2/retired-v1.md)
- [GitHub schedule limitations](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)
