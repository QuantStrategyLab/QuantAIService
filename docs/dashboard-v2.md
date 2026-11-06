# V2 dashboard health and quota API

[Runtime overview](runtime-baseline.md) · [Migration index](upgrade-v2/README.md)

The task server exposes `GET /v2/ai/health` and `GET /v2/ai/quota` behind its
existing HTTPS proxy. These are read-only JSON resources, so they use HTTP rather
than adding operational tools to the worker MCP surface. No V1 AI gateway module,
route, provider client or static-token authentication is restored.

## Identity and project boundary

Both endpoints use the existing `Authorization: Bearer <jwt>` resource-server
verification, configured issuer, audience, subject/claim constraints and admitted
project. Configure a **single-project**, non-provider dashboard grant with
`scopes: ["dashboard:read"]`; the JWT must also contain `dashboard:read` in its
space-separated `scope` claim. The Quant configuration guard accepts this scope
and resource discovery advertises it. Existing task scopes do not grant dashboard
access. Provider-bound worker credentials are rejected even if the scope is added.
No task submit/work scope is needed.

The optional `?project=example-org/example-repo` selector must match the admitted
grant; absent selectors use its one project. Duplicate or empty selectors fail.
There is no all-project aggregation or account-wide authorization implied by this
API. Each dashboard deployment should select its explicitly admitted project.
Missing/invalid authentication returns 401, denied scope/project/provider returns
403, unsupported methods return 405, and GET bodies return 400. Successful
responses return 200, including explicit unknown/unavailable observations. HTTP
responses use `Cache-Control: no-store`. The retired `/v1/ai/health` and
`/v1/ai/quota` remain unavailable (404 for an admitted caller).

## Health

Example response with synthetic values:

```json
{
  "status": "healthy",
  "p95_ms": 95.0,
  "total": 100,
  "errors": 0,
  "window_seconds": 300,
  "sample_limit": 10000,
  "uptime_seconds": 3600.0,
  "latency_profile": "task_http",
  "as_of": 1000.0
}
```

`p95_ms` is one server-computed nearest-rank percentile over **raw HTTP request
samples pooled across task routes**, not a maximum or average of route P95s.
The interval is `(now - 300 seconds, now]`, retaining at most the most recent
10,000 requests per project in this process. Duration uses a monotonic clock,
from handler entry through dispatch/error handling, before response serialization
and network writes. Only authenticated, admitted-project `/v1/tasks` and
`/v1/tasks/...` requests are attributed; these are current V2 runtime task routes
despite their existing URL version. HTTP 5xx responses count as errors. Dashboard
polling, discovery, MCP calls and asynchronous task execution are excluded.
Authentication failures and invalid/unauthorized project selectors add no sample.

`total` and `errors` cover the same retained samples. `status` is `unknown` with
`p95_ms: null` and `total: 0` after startup or when no samples remain; zero latency
is a valid number. Otherwise status is `degraded` if there is any sampled 5xx,
else `healthy`. This describes the observed task HTTP surface, not provider,
organization or background-task health. No latency threshold is inferred.
Samples reset on restart and are process-local; do not combine replica P95s.
`uptime_seconds` is process observation uptime, not deployment availability.
`as_of` is Unix epoch seconds.

## Quota observation input

The V2 runtime has no provider billing/subscription quota client. It cannot infer
an account limit or reset from task counts, costs or remaining percentages.
An optional service configuration property, `dashboard_quota_path`, names an
absolute path **outside the checkout** to a trusted local observation file.
Configuration checks validate only the path shape and do not read the file or
credentials. No collector, credential access or remote provider request is added.
Before operational use, an independently authorized collector must supply fresh,
explicit provider values; this change alone does not make live quota available.

The file must be a regular file owned by the service UID with no group/other
permission bits (normally mode 0600), at most 1 MB. Final-component symlinks are
rejected. Keep its parent directory private and atomically replace the file under
the same permissions. Its versioned input format is:

```json
{
  "version": 1,
  "projects": {
    "example-org/example-repo": {
      "example-provider": {
        "remaining": 18.4,
        "limit": 60,
        "unit": "usd",
        "observed_at": 990,
        "window_end": 2000
      }
    }
  }
}
```

The producer controls project visibility and must supply only quotas that this
project may read. Provider keys must match the selected project's configured
`providers` route values. Unconfigured provider keys and other projects are
never returned. Each provider has one explicitly selected quota window; the
producer must choose the binding window when a subscription has several limits.
`remaining` and `limit` are finite nonnegative numbers in the same `unit`, with
`remaining <= limit`; valid units are `usd`, `requests`, `tokens`, and `percent`.
For `percent`, the explicit limit must be 100 and the dashboard must display `%`,
not a currency. Never translate a percentage into a dollar allowance.
`observed_at` and `window_end` are Unix epoch seconds, with
`observed_at <= now < window_end` and `window_end > observed_at`. An observation
is fresh only while `now - observed_at < 300` seconds. Future observations,
missing fields, booleans, nonfinite values and invalid units are unavailable.

## Quota output

At synthetic server time 1000, the input above yields:

```json
{
  "status": "available",
  "as_of": 1000,
  "providers": {
    "example-provider": {
      "status": "available",
      "remaining": 18.4,
      "limit": 60,
      "unit": "usd",
      "observed_at": 990,
      "window_end": 2000
    }
  }
}
```

Each configured provider always has `remaining`, `limit`, `unit`, `observed_at`
and `window_end`. They are all `null` unless its status is `available`.
Provider status is `not_configured` when no path is configured, `unavailable`
for absent/unreadable/malformed/insecure input or a missing/invalid provider
observation, and `stale` for an otherwise valid observation at least 300 seconds
old or with an expired window. No raw file contents, exceptions or paths are
returned. One invalid provider does not hide other valid provider observations.
Top-level status is `available` if every configured provider is available,
`partial` if only some are, otherwise `unavailable` (also for no providers).
There is no quota enforcement or financial authority in this read API.

## Cloudflare dashboard integration

G2 must add the two V2 paths to its proxy allowlist and polling, retain the
existing server-side Bearer header pattern, and use a valid project-bound V2 JWT
with the scope above. The legacy static dashboard token is not a V2 credential;
JWT issuance/refresh and operational configuration need separate authorized
integration. Render health `p95_ms` directly, showing unknown for `null`. Render
`providers[name].remaining / limit` only for `available`, with the supplied unit,
and convert `window_end * 1000` for JavaScript dates. Show missing/stale states
explicitly. Do not fall back to V1 or derive limits, reset times or aggregate P95
in the browser. This PR does not change the Worker, its other retired V1 panels,
identity configuration or any deployment.
