# AiGateway Dashboard

Cloudflare Worker that serves an operations dashboard for the AiGateway service.

## Endpoints

- `/` — HTML dashboard with auto-refresh
- `/api/*` — proxies to AiGateway VPS origin (with `DASHBOARD_API_TOKEN` auth)

## Secrets

| Secret | Purpose |
|--------|---------|
| `GITHUB_OAUTH_CLIENT_ID` | GitHub OAuth App client ID |
| `GITHUB_OAUTH_CLIENT_SECRET` | GitHub OAuth App client secret |
| `DASHBOARD_SESSION_SECRET` | Required dedicated HMAC secret for signed dashboard sessions |
| `DASHBOARD_SESSION_KV` | Optional Workers KV binding used to revoke signed sessions on logout |
| `AI_GATEWAY_ORIGIN_URL` | HTTPS origin of the AiGateway service, for example `https://<vps-host>`. Set with `wrangler secret put`. |
| `DASHBOARD_API_TOKEN` | Static token for read-only API access |

`DASHBOARD_API_TOKEN` must match the token configured on the AiGateway origin
service so the dashboard can read `/v1/ai/*` endpoints.
Dashboard sessions are signed cookies, not Worker in-memory state, so they
survive Worker cold starts and edge isolate changes. When `DASHBOARD_SESSION_KV` is bound, logout revokes the session ID server-side. Revocable sessions fail closed if the KV binding is removed. Enabling the KV binding invalidates sessions issued before the binding existed; users should sign in again after rollout. `DASHBOARD_SESSION_SECRET` must be independent from GitHub OAuth and provider API secrets.

## Display semantics

- `health` shows online service health, not monthly audit quality or strategy health.
- `quota` shows remaining Codex window percentage when available; GPT/Claude rows come from Admin Usage/Cost APIs and may omit cost when the provider does not return it.
- Dashboard visibility does not change approval boundaries: high-risk changes, policy changes, secrets, and live-trading decisions still require human review.

## Operational data diagnosis

`/api/diagnoses` and `/api/diagnoses/<job_id>` are session-protected, read-only
projections of AIAuditBridge operational diagnosis ledger records. Only the explicit
job status and up to 500 characters of successful advisory output are shown; no
job polling, prompts, error details, or action endpoints are exposed. The origin
service must list this repository in its dashboard-repositories allowlist for
this read access.

The submission path is not wired in this repository. The `.github/workflows/`
directory has no `Codex Audit` workflow, so there is no daily schedule or
`operational_diagnosis=true` dispatch, and setting
`AI_OPERATIONAL_DIAGNOSIS_ENABLED` does not create a diagnosis run. The endpoints
above only project ledger records that the origin service already holds.

## Protected Actions publish

`Publish AiGateway dashboard` is a manual-only workflow in
`.github/workflows/publish-ai-gateway-dash.yml`. Run it from the default branch.
It accepts only its pinned, full reviewed source SHA; branches, short SHAs and
other commits fail before checkout. Updating that pin requires a reviewed change.
The Worker serves its dashboard directly from `src/index.mjs`; this directory has
no `package.json` or separate frontend build. Wrangler is pinned in the workflow.

The default `dry_run=true` runs the dashboard tests and Wrangler bundle validation
without Cloudflare credentials or a publish. A non-dry run repeats bundle validation
and uses the existing protected `production` Environment, including its required
reviewer and main-only policy. Only the publish step receives the existing
`CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID` Environment secrets. Credentials
must remain scoped to the approved dashboard target; their presence does not
prove that their account or permissions are appropriate for a release.

After merge, a **separate owner card** must approve the workflow revision, pinned
source SHA and dashboard target before a live publish. This workflow change grants
no deployment permission. The command deploys only the Worker and its embedded
assets described by this directory's `wrangler.jsonc`. It does not synchronize
Worker secrets or deploy the VPS. The existing reviewed Worker configuration is
used as-is; confirm its bindings and target during release preparation.

Runs are serialized and never automatically cancel a running publish. If a run
fails or is interrupted after publish starts, its remote outcome is unknown:
read back the target before considering any separately authorized retry. Raw
Wrangler diagnostics are suppressed to keep private resource identifiers out of
Actions logs. A completed command does not establish runtime acceptance.

## Manual deployment reference

The commands below are operator references and require separate authorization.


```bash
cd cloudflare/ai-gateway-dash
npx wrangler secret put GITHUB_OAUTH_CLIENT_ID
npx wrangler secret put GITHUB_OAUTH_CLIENT_SECRET
npx wrangler secret put AI_GATEWAY_ORIGIN_URL
npx wrangler secret put DASHBOARD_API_TOKEN
npx wrangler secret put DASHBOARD_SESSION_SECRET
npx wrangler deploy
```
