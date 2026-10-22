# GitHub App task comments

This optional outbox binds an explicitly supplied issue to a task at submission. It does not create issues, search task text for links, close issues, or forward materials/results into comments.

Submit through the existing authenticated `/v1/tasks` API with an additional field:

```json
"issue": {"repository": "the-same-owner/the-same-project", "number": 7}
```

`repository` must exactly match the admitted task project; `number` is a positive integer. Only agent tasks accept issue bindings. The binding is immutable and participates in the existing submission digest and idempotency key. A worker cannot change it in claim/report tools. Existing tasks without this field produce no comments.

For App comments, configure an exact service `owner` and a `github_apps` object keyed by admitted worker identity. Each route has exactly `provider`, positive integer `app_id`, positive integer `installation_id`, and opaque `private_key_ref`. Every admitted provider-bound worker needs a route when this feature is enabled. Multiple connections to one provider can share an identical App binding. Conflicting installations, credential references and App/provider bindings are rejected before key access. No production App IDs or worker grants are supplied by this change.

The client rechecks worker/project/provider admission, authenticates the App and its installation owner, then requests an installation token limited to the target repository and `issues:write` (GitHub may also return implicit `metadata:read`). Tokens are cached separately per worker/provider/App/installation/repository and expire early. GitHub endpoints are fixed to `api.github.com`; the existing HTTPS transport never follows redirects or retries writes. Outgoing App credentials do not authenticate incoming MCP requests: existing OAuth/JWT grants and scope checks remain required.

## Key consumption is a separate prerequisite

`build_application(config, github_key_loader=...)` accepts an approved host adapter that resolves an opaque reference into an RS256 signing key in memory. The default is unavailable: no file, environment, PAT, user OAuth or `gh` fallback. A configured App outbox without that adapter records `pending / preflight_unavailable`; it does not sign or make network requests. This change does not install a production loader, choose a key delivery mechanism, transfer keys, modify systemd credentials, or enable live comments. AI-7D-KEYPLAN-01 must establish the approved consumption method before deployment.

## Delivery semantics

A successful first claim and accepted final report enqueue a minimal status comment in the same SQLite transaction as the task update. The durable key binds service owner/project/task/attempt/revision/operation. Comments contain task ID, operation/status, a hash of the attempt, revision, and a stable marker; no objective, materials, output, artifacts or raw exception. Repeated failed reports use the same result and delivery record. Changed report content remains a task conflict.

The dispatcher flushes this outbox separately from task execution. A cross-process file lock covers recovery and delivery. It persists `sending` before a single POST. A successful, matching response records only the comment ID; definite API rejection becomes `rejected` and is not automatically resent. Missing local authentication prerequisites remain `pending`. The public task record exposes `github_comments` with delivery key, state, comment ID and a fixed safe error code, independently of task status. Reads and repeated worker calls observe existing delivery state.

Timeouts, malformed responses, server errors, redirects and interrupted sends become `unknown`. Recovery reads pages of comments (100 per page, at most 100 pages per scan), checking exact body/marker, issue, App ID and bot login. A matching comment becomes `sent`; incomplete scans, changed routing, failed authentication and absent/mutated comments remain `unknown`. No absence of evidence permits another POST. Matching requires GitHub's `performed_via_github_app` attribution; missing attribution conservatively stays unknown. Different queued records get a chance to run even when old unknown records cannot be resolved.

Keep the database and its private directory with existing service backups. Restoring an old database can lose post-backup delivery intents; do not resume a restored outbox until remote comment history is reconciled. There is no cross-system exactly-once guarantee and no automatic retry/reset endpoint for rejected or unknown writes.

## Validation and remaining rollout work

Synthetic RSA keys and fake HTTP responses cover routing for two owners × two providers, scope rejection, missing configuration, issue bindings, transaction rollback, failure deduplication, concurrent senders, restart, pagination, and unknown readback. No real key or GitHub issue is used. The original OAuth, attempt, revision, cancellation, terminal and schema tests remain applicable.

Grok worker configuration is BLOCKED: no verified connection issuer/subject/azp, Routine/key reference or model route is available. Synthetic route coverage does not grant or verify Grok access. The disabled-by-default feature can be reviewed and merged independently. Quant deployment configuration is a separate change card. This change does not deploy or establish worker/App E2E success.

Protocol references: [App JWT](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app), [installation tokens](https://docs.github.com/en/rest/apps/apps#create-an-installation-access-token-for-an-app), [issue comments and pagination](https://docs.github.com/en/rest/issues/comments).
