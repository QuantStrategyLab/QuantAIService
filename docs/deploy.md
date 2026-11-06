# Deploy the Worker with GitHub Actions

The **Deploy Worker to production** workflow is manual (`workflow_dispatch`) and
accepts only the `main` branch. It checks out the dispatch commit, installs the
lockfile, runs Worker tests and builds before reading deployment credentials.
Concurrent deployments are serialized. Wrangler is pinned to `4.156.2` in the
committed workflow; all external Actions use full commit SHAs.

## One-time owner setup

1. Create the GitHub Environment `production`. Add the owner as a required
   reviewer, disable protection-rule bypass where supported, and restrict deployment branches
   to `main`. Verify that the repository's visibility and GitHub plan actually
   enforce required reviewers **before dispatching**.

   An environment name alone does not create an approval gate. If reviewer
   protection is unavailable, leave deployment disabled until a gate is available.
2. Add environment secrets `CLOUDFLARE_API_TOKEN` and
   `CLOUDFLARE_ACCOUNT_ID`. Limit the token to the existing target account and
   Worker. It needs **Workers Scripts:Edit**, **Account Settings:Read** (account
   read) and **Workers KV Storage:Edit** for `OAUTH_KV`.

   Durable Object binding and migration operations use Workers Scripts permissions. If the private config
   additionally binds D1, add **D1:Edit**; if it manages zone routes, add
   **Workers Routes:Edit** and **Zone:Read** for only those zones. Do not add
   unrelated product permissions.

   See [Cloudflare's setup guide](https://developers.cloudflare.com/workers/ci-cd/external-cicd/github-actions/)
   and [Durable Objects permissions](https://developers.cloudflare.com/workers/authorization/durable-objects/).
3. Set environment variable `WORKER_NAME` to the exact existing target Worker.
   Add environment secret `WORKER_CONFIG_JSON` as a JSON object containing the
   approved production values described below. This is configuration data, not
   a place for API tokens or Worker secret values.
4. Optionally add environment secret `SMOKE_TASKS_TOKEN`: a valid, short-lived,
   project-scoped bearer token with an existing explicit `tasks:list` grant.
   Existing `tasks:read` or `tasks:submit` grants do not imply list access.

   Keep required runtime secrets in the existing Cloudflare Worker configuration.
   This workflow does not mint grants, obtain tokens or change Worker secrets.

## Private configuration required

The committed `cloudflare/task-service/wrangler.example.jsonc` is the base configuration;
there is no committed production config. Copy approved values from the existing
private configuration into `WORKER_CONFIG_JSON`. Top-level keys replace the
corresponding complete sections; nested policy objects are not merged. Include:

- `name`: the same exact value as `WORKER_NAME`.
- `vars`: the complete production variable map, including `PUBLIC_ORIGIN`,
  `PROJECT_ID`, `TASK_STORE_NAME`, `AUTH_CLIENTS`, `ADMISSIONS_JSON`,
  `PROJECT_CONTEXTS`, `PROJECT_POLICIES`, `AGENT_PROVIDERS`, organization fields,
  the GitHub OAuth client ID if used.
- Preserve any existing workload audience/grants and provider or webhook
  configuration within `vars`. Empty example maps are not production values.
- `kv_namespaces`: existing `OAUTH_KV` namespace ID and any additional bindings.
- `durable_objects`: the committed `TASK_STATE` / `PersonalTaskStore` binding.
- `services`: existing `TASK_SERVICE` self-binding, with `service` equal to `name`
  and `entrypoint` equal to `TaskService`, plus any other existing bindings.
- `migrations`: the exact existing migration history and tags. Never infer a new
  initial migration from the example on an existing production Worker.
- `routes`: the complete existing route list, or explicitly `[]` if none.
- `workers_dev`: the existing boolean setting; do not infer routing defaults.
- Any additional existing production binding sections, such as `d1_databases`,
  if present in the private config. None is declared in the committed base.

The actual origin/route hostnames, KV ID, project/store IDs, OAuth client ID,
client/admission/policy maps, provider destinations, existing migration history
and routing settings are **not available from the committed example**.

Additional private bindings cannot be inventoried from this repository. The owner must
compare the complete existing private config before enabling this workflow.
The account ID comes only from `CLOUDFLARE_ACCOUNT_ID`, not the config JSON.
No account IDs, private hostnames or tokens belong in Git.

The helper requires complete sections, rejects unresolved `CONFIGURE_` and
example-host placeholders, checks the target and bindings, and writes a temporary
mode-0600 config. Executable paths and `observability` come from the committed
base: Workers Logs stay enabled with sampling 1, invocation logs disabled and no
traces added. The built entry point is resolved to its absolute path.

Variables are applied from the complete approved map (`keep_vars: false`); existing Worker
secret values remain managed separately. Private scalar values are masked,
Wrangler output is suppressed, and config/log files are removed even on failure.

## Run and verify

After separate PR review and merge approval, open **Actions → Deploy Worker to
production → Run workflow**, select `main`, and have the required reviewer verify
the dispatch commit and target before approving `production`. There are no
push-triggered deployments and no arbitrary checkout-ref input. Do not run an
unreviewed workflow branch with production secrets.

After deployment, smoke checks perform only `GET /healthz` and, when the optional
token is set, `GET /v1/tasks?limit=1` and `GET /v1/tasks/stats`. Responses must be
successful JSON; health must equal `{"status":"ok"}`.

Redirects are rejected so
credentials cannot follow another destination. Bodies, tokens, URLs and private
exceptions are never printed.

`/healthz` is liveness only; it does not assert provider readiness or task completion. Optional task checks show an explicit
skip when the token is absent.

A failed smoke check fails the job **after** deployment; it does not roll back.
A failed deployment command can have an unknown remote result. Inspect the
existing Worker through the approved owner channel before deciding on another
manual run. This workflow creates no secrets and performs no secret updates.
