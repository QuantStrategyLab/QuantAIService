# Security alert routing

The daily `Security alert routing` workflow lists every public, active, non-fork
repository owned by QuantStrategyLab and Pigbibi. It polls open secret-scanning
alerts and high/critical Dependabot alerts. Repository and alert lists paginate
independently. Any required API failure aborts the poll before notifications;
403 and 404 are never interpreted as an empty required alert list.

The script optionally accepts `--code-scanning`; only code-scanning 404 responses
are treated as disabled. This optional source is off in the scheduled workflow.
The daily schedule runs at 01:17 UTC. GitHub can delay schedules; daily polling
does not provide a one-hour detection SLA.

## Credential readiness

The workflow remains disabled until `vars.SECURITY_ALERT_ROUTING_READY` equals
`true`. Setting this is an operator action after verifying all bindings below.
This change creates no secrets, variables, installations or permissions.

No installed organization App inspected for this change has both required
security permissions. Existing `CROSS_REPO_GITHUB_APP_ID` and
`QUANT_MONITOR_READER_APP_ID` bindings must not be used as security readers:
their Apps do not have the required permissions. The proposed fallback requires
two owner-scoped fine-grained PATs, stored as Actions secrets in this repository:

| Secret name | Resource owner and repository scope | Repository permissions |
|---|---|---|
| `QSL_SECURITY_ALERTS_TOKEN` | QuantStrategyLab; all monitored public repositories | Secret scanning alerts: read; Dependabot alerts: read; Metadata: read |
| `PIGBIBI_SECURITY_ALERTS_TOKEN` | Pigbibi; all monitored public repositories | Secret scanning alerts: read; Dependabot alerts: read; Metadata: read |

API/App permission keys are `secret_scanning_alerts:read` and
`vulnerability_alerts:read`. A fine-grained PAT has one resource owner, so one
owner-scoped PAT cannot cover both owners. Its repository selection must cover
newly added public repositories, too. Optional code scanning additionally needs
Code scanning alerts: read (`security_events:read` for an App).

Reuse the existing Telegram names: `TG_TOKEN`, `GLOBAL_TELEGRAM_CHAT_ID` and
`vars.QUANT_SENTINEL_EXPECTED_BOT_ID_SHA256`. `TG_TOKEN` is already referenced by
the Sentinel workflow; the chat name is the existing Monitor routing contract.
Repository-level name listings do not prove that inherited organization secrets
are available. Verify their existing visibility/bindings before enabling; do not
introduce alternate bot secrets. The maintenance WIF identity is documented as
Secret-version-adder only and must not be repurposed to read Secret payloads.
`getMe` must match the existing expected bot ID hash before any send.

## Delivery and state

New alerts include repository, number, alert/secret type, severity, creation
time, canonical GitHub URL and age in hours. Secret alerts have severity
`unknown` because that API does not supply severity. The first nonempty run
sends one bounded summary. It counts all alerts and includes as many full rows
as Telegram permits; omitted rows remain accessible in repository Security
tabs. Batches larger than ten alerts also use one summary to avoid floods.
All summarized alerts count as first reported. Subsequent runs are idempotent;
alerts still open 24 hours after first report receive one reminder. Closed alert
IDs remain in the state so reopening does not create another initial report.

The state contains only SHA256 alert identities, first-report timestamps and
reminder/pending booleans. It contains no alert fields, repository names, secret
values, locations, bot tokens, chat IDs or message bodies. The workflow stores
only that file in Actions cache, never a Git branch or artifact. Caches are
readable by repository readers: they are not a secret store, and hashed IDs
must not be treated as anonymization. Do not extend this schema with sensitive
metadata. PR runs execute synthetic tests only and never restore/save this cache.
Only the default-branch poll job writes state, serialized by concurrency.

Each send is checkpointed as pending first, then recorded as confirmed only
after Telegram returns HTTP 200 and `ok: true`. Failed runs save the checkpoint,
and pending delivery stops subsequent sends. Reconcile uncertain delivery with
the receiver before repairing the cached state; do not blindly retry or reset
it. A reminder pending entry already has a confirmed first report: mark it
reminded only after receiver confirmation. An initial pending entry needs its
first-report time set to the confirmed delivery time. If confirmed not sent,
remove only that initial pending entry or clear only that reminder's pending
flag before retrying. Never download state into a public artifact for repair.

A Telegram HTTP 4xx response (including 429 rate limiting) is definitive: the
message was not delivered, so that batch's pending marks are rolled back
automatically and the next scheduled run retries without manual repair. HTTP 5xx,
timeouts and other transport errors remain uncertain and keep the pending
checkpoint for operator reconciliation.

Cache eviction or a runner crash before cache save can lose state. A missing
cache starts another single bootstrap summary; it cannot preserve exactly-once
delivery across state loss. Telegram offers no idempotency key for sendMessage.
Monitor workflow failures and cache retention rather than assuming a green unit
test proves delivery. Long-term exact delivery requires durable private storage.

## Data boundary and verification

GitHub's API does not offer a secret-field exclusion parameter. `gh api --jq`
immediately projects each response to an explicit allowlist before the script
receives stdout; raw bodies are never printed, saved or attached. The script
never calls locations endpoints. Never enable `GH_DEBUG`, shell tracing or HTTP
body logging. Logs contain counts and, for Telegram, HTTP status and bot
username only. A logging filter removes known credential strings, token
patterns and sensitive assignments; exceptions are fixed diagnostics.
HTTP redirects are refused and sends are never automatically retried.

Offline verification (no credentials or external calls):

```bash
python -m unittest discover -s tests -p test_security_alert_router.py -v
python scripts/security_alert_router.py --dry-run --fixture tests/fixtures/security-alerts.json
```

For an authorized real API check, use `--dry-run` without `--fixture`. The local
script uses existing gh authentication if owner-specific tokens are absent;
the workflow requires both configured owner-specific tokens. Dry runs print
counts only, never contact Telegram and never update state. The manual workflow
defaults to dry run. No live notification is part of development verification.
