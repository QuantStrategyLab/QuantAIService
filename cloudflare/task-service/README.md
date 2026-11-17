# Organization AI Task Service

English | [简体中文](README.zh-CN.md)

Independently deployed Cloudflare Worker, OAuth KV, and SQLite Durable Object. Production entrypoints in this directory use the Quant state space; Pigbibi regression fixtures use a separate test state class. Retaining the `PersonalTaskStore` Durable Object class name is solely for storage-binding compatibility; organization identity is scoped by server-side configuration.

Dot is the current agent executor. API mode preserves the same task submission, lookup, event, and idempotency contract, currently supporting the legacy service's Gemini text-JSON protocol; it is disabled by default. Claude and Grok Routine adapters are present and disabled by default.

MCP resource metadata and unauthenticated challenges advertise the union of configured worker role scopes whose `AUTH_CLIENTS` entry matches the exact resource and has a non-null provider, ordered along server-side `SCOPES`; scopes are never inferred from requests, current tokens, or REST callers. This advertisement lets clients request required capabilities during normal authorization rather than granting execution permission: base MCP operations still require `tasks:work`, subscriptions and their management require `events:subscribe`, and existing client, admission, organization, project, and audience checks remain unchanged. REST continues advertising only the minimal scope for the active read or submit action.

Deploying this advertisement fix does not broaden existing grants. Admitted Dot connections discover event capabilities and all six tools, with each tool's `securitySchemes` declaring its required scope. `get_event_subscription` and `list_event_subscriptions` return only the caller's own subscription lifecycle and latest delivery fields (`last_attempt_at`, `last_status`, `last_error_class`, `consecutive_failures`), always returning `redacted-host` for valid `callback_host` URLs without original hostnames, secrets, paths, or query strings; `consecutive_failures` increments from the stored database value on failed delivery and resets to zero on success. Legacy grants scoped only to `tasks:work` still cannot subscribe, list, read, or delete subscriptions: raw event requests return standard OAuth 403 scope challenges, and subscription management tools return errors carrying `_meta["mcp/www_authenticate"]` prompting incremental authorization without writing subscriptions or dispatching webhooks. Normal task operations still require only `tasks:work`.

Clients must authorize the required scopes through the plugin's normal consent flow before verifying event discovery, active subscriptions, and real task outcomes; never mutate KV grants or borrow another client token as a substitute. Updating resource metadata alone does not guarantee the host refreshes cached scopes immediately; verify via actual tool/event discovery, the consent page, and the resulting grant.

API routing is injected via protected server configuration: `PROJECT_POLICIES[project].providers` sets `api` or `api:<profile>` pointing to an explicit provider alias, and `models[alias]` restricts allowed models. In Worker configuration, `API_PROVIDERS[alias]` defines `type: "gemini_api"`, `enabled: true`, `api_usage_authorized: true`, exact `model`, and `key_env` (name of a pre-bound Secret), plus optional `max_output_tokens` (1..8192, default 1024) and `timeout_seconds` (1..60, default 60). Missing any enablement, authorization, model, or credential requirement rejects API calls without affecting Dot. These boolean flags record prior approval only and never grant paid API authority on their own; production enablement and live requests still require explicit authorization.

The API adapter calls only the fixed Google `generateContent` HTTPS endpoint; it never accepts caller-supplied URLs, follows redirects, discovers models, retries inference, or falls back to other paid routes. It accepts text materials only and passes no tools or safety-setting overrides. Responses with mismatched models, abnormal finish reasons, or schema violations are rejected rather than returned as success. HTTP rejections fail explicitly; dropped connections, server errors, or unverified receipts persist `outcome_unknown` so the frozen intent remains queryable without resubmitting a new task. Submitting state is written before remote calls, with network I/O outside SQLite transactions and short Durable Object critical sections. Synchronous API cancellation records a cancel request without claiming remote termination; if a verified complete receipt subsequently arrives, that observed outcome stands.

This implementation ports only the Gemini API vertical slice; legacy ChatGPT-plan OAuth Responses providers are not yet ported to the Worker, so legacy provider migration is not complete.

## GitHub Actions Caller Identity

Machine REST callers may authenticate with GitHub Actions OIDC JWTs. `TaskClient` configures the service URL and token audience separately; the legacy Python service checks `config.resource` for both REST and MCP, so callers targeting `/v1/tasks` must not have their audience rewritten automatically. This entrypoint is distinct from Dot's OAuth and MCP identities and cannot claim tasks, report results, or manage event subscriptions.

Machine grants must explicitly set server-side `GITHUB_WORKLOAD_AUDIENCE` to the caller's exact approved audience (including legacy `/mcp` resource identifiers): a single HTTPS URL with no credentials, query string, fragment, or whitespace. Missing or invalid values fail closed without guessing legacy URLs or accepting dual audiences. Migration may update only the caller service URL while preserving its original token audience; this value validates JWT `aud` only, never issues outbound requests, and never enables MCP. Internal service-bound resources remain fixed at `PUBLIC_ORIGIN + "/v1/tasks"`.

Machine callers are denied by default. Protected variable `GITHUB_WORKLOAD_GRANTS_JSON` accepts only exact pre-existing caller grants reusing Python's `issuer`, `subject`, `identity`, `project`, `scopes`, and `claims` fields. `project` must match the exact repository configured for `PROJECT_ID` in `PROJECT_CONTEXTS`, which supplies owner and organization identity; caller tokens, headers, or parameters cannot override project binding. Never deploy placeholder entries literally; the JSON below illustrates synthetic config structure only:

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

Per [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc) and [fixed OIDC discovery](https://token.actions.githubusercontent.com/.well-known/openid-configuration), only fixed issuer, HTTPS JWKS, and RS256 are accepted; single-string `audience`, `kid`, signature, `exp`/`iat`/`nbf`, `subject`, repository, `workflow_ref`, and branch ref are verified without consulting JWT `jku`/`x5u`. Keys are cached for 5 minutes; unknown `kid` values or key-fetch errors reject rather than falling back to alternative identity sources.

Branch subjects must match the exact repository and ref; if a repository uses immutable subjects, the grant must bind both `repository_owner_id` and `repository_id` alongside GitHub's exact subject string without deriving subjects from repository names. PR, `pull_request_target`, tag, environment subjects, and multi-repository grants are excluded here.

GitHub OIDC tokens omit OAuth scopes, so permissions come from the bounded grant intersected with any explicit token scope claim. GitHub's signed `repository_owner` must match the exact organization owning `project`; `PROJECT_CONTEXTS.ownerId` retains existing internal owner identifiers for RPC compatibility even when distinct from the GitHub organization name. Internal RPC `githubUserId` carries GitHub's numeric `actor_id` for audit logging only and grants no permissions; `provider` stays `null`. Caller-supplied identity, project, organization, and provider headers are stripped before service binding. Business `mode` still follows project policy (Dot or explicitly enabled API), and workload auth never activates providers.

For machine callers, `tasks:read` allows `GET /v1/tasks/lookup`, `GET /v1/tasks/{id}`, and `GET /v1/tasks/{id}/events?after={seq}`; `tasks:submit` allows `POST /v1/tasks`, `POST /v1/tasks/{id}/messages`, and `POST /v1/tasks/{id}/cancel`.

### Read-Only Task Listing and Stats

`GET /v1/tasks` and `GET /v1/tasks/stats` require `tasks:list` explicitly in the server workload grant; `tasks:read` and `tasks:submit` do not imply list access, and token scopes can only narrow grant permissions. Grants carrying only `tasks:list` cannot read task payloads, submit, cancel, append messages, claim, or report tasks. Queries stay restricted to the grant-bound project, and provider identities have no access. OAuth callers require explicit `tasks:list` across client, admission, and grant layers.

Listing accepts optional `since`, `provider`, `status`, `limit`, and `cursor`. `since` is an inclusive UTC/offset ISO 8601 timestamp matching creation time (defaulting to Unix epoch). `limit` defaults to 50 and caps at 200. Ordering is descending by creation time then ID. Pass `next_cursor` back as `cursor` while keeping filter parameters unchanged; `next_cursor: null` marks completion. Cursors use authenticated encryption with a server-persisted key, expose no decodable project/tenant/task identifiers, and bind project, filters, and sort order. Tampered, cross-project, filter-mismatched, or legacy cursors all return a generic HTTP 400 so callers restart from page 1 after upgrades. Task states may advance during pagination; malformed or duplicated parameters return 400, while unknown providers yield an empty list.

Responses return `tasks` and `next_cursor`, with each entry containing only `id`, `provider`, `status`, `created_at`, `claimed_at`, `completed_at`, and `error_code`. Timestamps are UTC ISO strings or `null`. `completed_at` marks entry into any terminal state including `failed` and `cancelled`. New claims record exact `claimed_at`; legacy tasks retain `null` where claim timestamps cannot be recovered reliably, and legacy terminal tasks reuse recorded final update times.

`error_code` returns only `deadline_before_submission`, `deadline_exceeded`, `acceptance_unknown`, `submission_rejected`, or `output_schema_mismatch`; other storage errors map to `unknown_error`, and error-free tasks carry `null`. Endpoints omit prompts, results, handles, caller identities, and credentials.

Stats accepts only optional `since` with identical creation-time semantics, scanning at most 1,001 index rows per call: up to 1,000 matches compute exact counts in a single pass, whereas larger result sets return HTTP 400 without partial stats so callers narrow `since`. `counts` groups by `{provider, status, count}`; `stuck` reports `claimed_not_completed`, `queued_over_threshold`, and `total` (`queued_threshold_minutes` stays 15 via `STUCK_QUEUE_MINUTES`).

`claimed_not_completed` counts explicitly claimed non-terminal tasks including legacy attempted tasks with null `claimed_at`; `failed` and `cancelled` count as terminal. `queued_over_threshold` counts `queued` tasks waiting longer than 15 minutes. Empty match sets yield empty `counts` and zero `stuck` counts.

Both endpoints return `Cache-Control: no-store` without mutating tasks, calling providers, or scheduling alarms. Durable Object SQLite initialization adds nullable lifecycle columns and four composite `(project, created_at, id)` indexes (unfiltered, provider-only, status-only, provider+status), generating a persisted cursor key once during migration rather than rotating keys during queries. Listing seeks directly across the index bounded at `limit + 1` rows. Offline `npm test` uses synthetic IDs, locally signed workload tokens, and mocked JWKS to verify authorization, isolation, filtering, pagination, stats, and migration.

Messages preserve the SDK's JSON `text`, optional `materials`, and stable idempotency keys: duplicate keys with matching bodies avoid bumping revision, mismatched bodies return HTTP 409, and terminal or cancel-requested tasks reject new messages. Events retain the monotonic `seq` cursor returning an empty array when caught up, HTTP 400 for invalid cursors, and HTTP 404 for unknown tasks. Cancel calls require body `{}` and preserve terminal/idempotency rules without relabeling failed tasks as canceled.

Isolation remains scoped at repository `project` level: task owners correspond to repositories, and only grants admitted for that exact project can read tasks/events, append messages, or cancel its tasks, without claiming per-user or per-client isolation inside one project. Machine entrypoints grant no MCP, claim/report, subscription, or admin access; adding REST routes neither creates nor expands live grants, and live traffic still requires verifying active clients, exact audiences, and project mappings.

Production acceptance still requires selecting one real caller with its active grant/audience, server mapping, and restricted config, issuing real GitHub tokens to submit bounded tasks, and completing lookup/consumption via the original client under shared idempotency keys; synthetic `events` submission scripts and offline tests do not close that caller loop.

OAuth access tokens default to `3600` seconds (1 hour) and can be explicitly overridden by protected server setting `OAUTH_ACCESS_TOKEN_TTL_SECONDS` (integer `60..86400`, e.g., `21600`). Clients reuse valid tokens locally and refresh before expiry; token validation and reuse perform zero KV writes and never extend an issued token's expiry, while refreshes continue existing token rotation and two KV writes. Raising TTL extends the window during which a leaked token remains usable, while audience, scope, admission, and revocation checks continue to apply.

## Local Verification and Deployment

Run `npm ci`, `npm test`, and `npm run build`. `npm test` includes OAuth TTL regressions covering default `3600`s and explicit `21600`s issuance, zero-write validation reuse, refresh rotation, expiry rejection, mismatched audience rejection, and revocation. Local `workerd` suites test Quant and Pigbibi paths plus cross-org and wrong-owner rejections using synthetic GitHub, webhook, and Gemini HTTP stubs over real SQLite state; passing offline suites does not prove live Dot or API connectivity.

Machine auth is additionally verified by `node test/workload.mjs` using temporary RSA keys and local JWKS stubs to test cross-org SQLite submissions, lookup, message idempotency/revisions, event cursors, read/submit separation, and identity rejections without outbound network access or shipping test signers into production bundles. Regression suites also run unmodified Python 3 `TaskClient` against the local Worker with socket access disabled, advancing alarms explicitly so cancellation and scheduled delivery (`native.mjs`) are verified deterministically.

Copy `wrangler.example.jsonc` into a protected config directory and populate existing resources and approved project policies; real accounts, clients, and allowlists never enter Git. The example enables Workers Logs (`observability.enabled` and `logs.enabled`, `head_sampling_rate: 1`) while keeping invocation logs off so tokens, signatures, and full callback URLs in headers/bodies do not leak to platform logs. Delivery diagnostics emit sanitized custom JSON (`subscription` short ID, `redacted-host`, event type, task ID, attempt count, HTTP status/error class, duration). Deployments require maintainer execution of `wrangler deploy --config /absolute/private/wrangler.jsonc` outside placeholder directories. Public OAuth client registration requires `node scripts/register-clients.mjs --config /absolute/private/registration.json` containing organization, `PROJECT_ID`, and `PUBLIC_ORIGIN`, plus `--remote` and exact `--namespace-id` remotely; read back existing `.local` plans rather than re-running blindly.

Production adoption separately verifies OAuth, active subscriptions, end-to-end task lifecycle with the original caller, and reconnect recovery before switching legacy callers or retiring VPS components.

## Additional Routine Executors

`AGENT_PROVIDERS` is configured by server provider alias, requiring explicit `enabled: true`, `type`, `url`, and `key_env` wired to Secret bindings; misconfigured, disabled, or credential-missing entries reject before submission. Project policies, model allowlists, and Worker grants still apply independently, so callers cannot select arbitrary providers. Default examples remain disabled:

```json
{"AGENT_PROVIDERS":{"grok":{"type":"grok_webhook","enabled":false,"url":"https://CONFIGURE_ROUTINE_HOST/trigger","key_env":"GROK_ROUTINE_KEY"},"claude":{"type":"claude_routine","enabled":false,"url":"https://api.anthropic.com/v1/claude_code/routines/trig_CONFIGURE/fire","key_env":"CLAUDE_ROUTINE_TOKEN"}}}
```

Claude uses the [official Routine API](https://platform.claude.com/docs/en/api/claude-code/routines-fire) with a dedicated Routine Bearer token, `anthropic-version`, and `text` payload; Grok uses [Routine webhooks](https://cursor.com/help/grok-bot/routines). Triggers pass only task ID, project, revision, change marker, and MCP endpoint without task materials or inference secrets. HTTP 200 confirms dispatch only; completion still requires claim/report by the same admitted provider rather than inferring model identity from trigger receipts.

State persists before trigger calls; timeouts, 5xx responses, or interrupts remain `outcome_unknown` with no automatic re-trigger. Message additions and cancellation notices fire at most once per event, cancellation still awaits worker acknowledgment, and Claude Routine lacks idempotency keys so Dot event retries do not apply. Enabling any Routine, minting tokens, or live testing requires explicit authorization.

## Multi-Project Admission

One organization's Worker can host multiple explicit project contexts within the shared task store using project isolation. `GITHUB_WORKLOAD_GRANTS_JSON` accepts up to 100 exact grants; pre-signature JWT claims select a candidate strictly by unique subject and fixed workflow/ref/repository constraints before verifying signature, audience, and scopes. Project identity comes from the matched server grant and unique context belonging to the original service owner within the organization; callers cannot switch projects via queries or task payloads.

By default, workers stay scoped to their primary project. Accessing extra projects requires listing exact full repository names under `AUTH_CLIENTS[client_id].projects` plus matching `PROJECT_CONTEXTS` and `PROJECT_POLICIES` under the same organization, owner, and provider rules. Additional projects may pass explicit `project`; for single-project cached host schemas, omitted `project` lookups locate only existing task IDs within that worker's admitted project list without scanning other projects, while event subscriptions continue requiring exact `project`. Configuration lists do not substitute for per-caller admission or auto-create native subscriptions; removing a project or connection halts future deliveries.

Before cutting over callers, complete synthetic end-to-end submission/consumption from the original caller plus subscription/claim/report from the original worker connection across each project, update endpoints and switches per project, and never retire the legacy service while callers remain active.

## Unclaimed Dot Task Cancellation

Before a worker claims a Dot task, only persisted events exist with no running execution. Explicit cancellations or expired deadlines move unclaimed tasks directly to `cancelled`, releasing concurrency slots; legacy `cancel_requested` records finish via alarm while preserving original request/error fields. Already-claimed tasks still require worker-confirmed cancellation, and terminal tasks are never overwritten. Unknown-submission and cancellation semantics for other providers stay unchanged.

### Grok Bot and Claude Duet

See [agent providers](../../docs/agent-providers.md) for added aliases, configuration, and admission steps. Although `grokbot` is `enabled: true` in the provider sample, it still requires an active endpoint, Secret bindings, and project/worker admission; `claudeduet` and `muse` remain disabled by default with unverified trigger schemas and fail closed if misconfigured enabled. Claude Duet does not share Claude Code Routine tokens; Muse skips API mode, keeps MCP claim identity scaffolding only, and never spawns from Cursor Cloud Agents.
