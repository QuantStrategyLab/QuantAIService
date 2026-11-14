# Named agent providers

`grokbot` (Grok Bot), `claudeduet` (Claude Duet) and `muse` (Muse Wonder agent)
are separate provider identities. Existing `dot`, `grok`, `claude` and API routes
keep their meanings. Names are not interchangeable worker credentials or model
attestations. Agent mode stays separate from API mode (`gemini_api` and similar).

| Provider | Configuration type | Example enablement | Invocation |
| --- | --- | --- | --- |
| `grokbot` | `grok_webhook` | `true` | Existing Grok Bot Routine webhook |
| `claudeduet` | `claudeduet` | `false` | Scaffold only; no verified trigger protocol |
| `muse` | `muse` | `false` | Scaffold only; Cloudflare MCP claim identity reserved; no outbound launch host |

The enclosing Python example service remains `enabled: false`. Grok Bot is
explicitly enabled at the provider level as requested, but this does not deploy
it, create a Routine, authorize usage or establish a connection.

Replace the placeholder URL with an approved HTTPS Routine endpoint and bind an approved
Secret using `AI_SERVICE_GROKBOT_WEBHOOK_KEY`; do not put its value in config.
Without that credential the Python application refuses startup; the Worker
rejects submission before outbound activity. There is no fallback to Dot or API.

Claude Duet is a different product from Claude Code Routines. The repositories
and the [Duet getting-started documentation](https://support.claude.com/en/articles/17631495-get-started-with-claude-duet)
do not establish a service-compatible trigger/receipt contract. Its reserved
type accepts disabled registration only.

Setting it to `enabled: true` fails
closed in both runtimes, without using Claude Code Routine tokens or inference
API keys. No speculative Duet endpoint or Secret is configured.

Confirm a supported trigger, scoped MCP connection, cancellation and delivery semantics
before implementing a live adapter. The existing `claude_routine` adapter stays
available for its original Claude Code purpose.

`muse` is the reserved long-conversation agent identity for Muse Wonder. It is
not an API-mode model slot and must not share Gemini or ChatGPT plan routes.
Shared surface with Dot / GrokBot / ClaudeDuet is the **Cloudflare Worker** task
service (OAuth, queue, MCP claim/report). Each named agent keeps its own outbound
wake channel; they must not share a Cursor Cloud Agents launch path.

Muse currently has **no verified Cloudflare-compatible outbound trigger**. Its
reserved type accepts disabled registration only. Setting `enabled: true` fails
closed in both runtimes. Do not bind `url`, `key_env`, Cursor API keys, or
`AI_SERVICE_MUSE_READY` for Muse. Scenario routing always reports muse as
`unavailable` until a non-Cursor, service-compatible trigger exists.

## Routing and admission

Python examples register `agent:grokbot` -> `grokbot`,
`agent:claudeduet` -> `claudeduet` and `agent:muse` -> `muse`, with separate
`configured-grokbot`, `configured-claudeduet` and `configured-muse*` model
allowlists. Requests use `mode: "agent"` and the approved `profile`; they cannot
choose a provider directly. The default `agent` -> `dot` and existing
`agent:second-opinion` -> `grok` are preserved.

Placeholder worker grants use distinct subjects and `azp` clients, with only
`tasks:read` and `tasks:work`. They are examples, not live admissions.

For the Worker, `AGENT_PROVIDERS` in `cloudflare/task-service/wrangler.example.jsonc`
contains the registrations. The other admission maps intentionally stay
empty. An operator must separately provide all of the following through the
existing protected configuration process:

- `PROJECT_POLICIES[project].providers["agent:grokbot"] = "grokbot"` and
  `models.grokbot = ["configured-grokbot"]` (analogous reserved routes for Duet
  and Muse).
- `PROJECT_CONTEXTS[projectId].providers` containing the exact provider identity.
- A distinct `AUTH_CLIENTS[clientId]` entry with that provider, the MCP resource
  and read/work scopes; explicit same-owner `projects` only where required.

- The matching `ADMISSIONS_JSON` entry binding organization, project, user,
  owner, client and provider. Caller identities remain submit/read only.

No worker gains `events:subscribe` for these names: event delivery stays with
Dot; Grok Bot uses Routine webhooks; Muse stays claim-identity scaffold only.
Existing project/provider checks reject
legacy `grok` or `claude` workers claiming a new provider's task. Registering an
OAuth public client alone does not grant task access. This change runs no
registration or grant-writing tooling.

## Scenario contracts and verification

These names are accepted in the instance and task schemas wherever other agent
workers are accepted, including the agent phase of `protocol_probe`. API-only
phase, tool and budget restrictions remain. Offline compilation does not enable
a provider or change its policy.

The current pilot adapter and candidate helper
remain restricted to a single Dot stage; do not switch an existing pilot by
changing its worker name alone.

Grok Bot reuses [Routine webhook semantics](https://cursor.com/help/grok-bot/routines):
only task identifiers/revisions are sent, HTTP 200 means started, and completion
requires the assigned worker's current claim and schema-valid report.

Unknown
acceptance is never retried or rerouted. Cancellation requires worker confirmation.
Model verification remains `unavailable` for native trigger receipts.

Offline regressions cover schema compilation, enablement, missing credentials,
scoped claim/report, cross-provider/project denial and no retry after ambiguous
receipts. They use synthetic credentials and intercepted transports only; they
do not verify real connections, billing, latency or business completion.

The native three-role briefing consumer also accepts the new provider names
in its protected role binding. Distinct worker identities and current readback
remain required; a name alone never yields approval, and Duet / Muse remain
unavailable in the production runtime until an adapter is verified.
Keep `muse` disabled until a Cloudflare-compatible Muse Wonder trigger exists
and MCP worker identity plus cancel/receipt checks are verified. Never wire Muse
through Cursor Cloud Agents.
