# Named agent providers

`grokbot` (Grok Bot), `claudeduet` (Claude Duet) and `muse` (Muse Wonder agent)
are separate provider identities. Existing `dot`, `grok`, `claude` and API routes
keep their meanings. Names are not interchangeable worker credentials or model
attestations. Agent mode stays separate from API mode (`gemini_api` and similar).

| Provider | Configuration type | Example enablement | Invocation |
| --- | --- | --- | --- |
| `grokbot` | `grok_webhook` | omitted | Existing Grok Bot Routine webhook |
| `claudeduet` | `claudeduet` | omitted | Scaffold only; no verified trigger protocol |
| `muse` | `muse` | `true` | Cloudflare event subscription or scheduled MCP polling |

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

The Cloudflare Worker supports an independently scoped Muse webhook
subscription and scheduled polling. Keep the example provider disabled until
its project and worker grant pass end-to-end acceptance. Cloudflare does not
use Cursor Cloud Agents or require a Muse API key.

### Scheduled polling

A personal Muse cloud schedule can call `poll_muse_tasks` using the project’s
Muse OAuth grant. The response contains up to eight claimable tasks or pending
tasks already claimed by that worker, ordered by deadline. Continue with the
existing claim and report tools. Set `muse_poll_seconds` on an approved entry
in `PROJECT_POLICIES` to select scheduled polling. The setting bounds the age
of the last successful poll. New tasks need a deadline longer than this window.
Enable it after measuring that the scheduling delay plus execution time fits
the caller’s deadline.

## Routing and admission

The default examples route `agent` and `agent:muse` to Muse. Projects admit
its model aliases and OAuth worker explicitly. Other Agent adapters remain
available for explicit configuration. API profiles have their own provider
and model allowlists.

Placeholder worker grants use distinct subjects and `azp` clients, with only
`tasks:read` and `tasks:work`. They are examples, not live admissions.

For the Worker, `AGENT_PROVIDERS` in `cloudflare/task-service/wrangler.example.jsonc`
contains the registrations. The other admission maps intentionally stay
empty. An operator must separately provide all of the following through the
existing protected configuration process:

- A Muse route such as `PROJECT_POLICIES[project].providers["agent:muse"] = "muse"`
  and the corresponding `models.muse` allowlist.
- `PROJECT_CONTEXTS[projectId].providers` containing the exact provider identity.
- A distinct `AUTH_CLIENTS[clientId]` entry with that provider, the MCP resource
  and read/work scopes; explicit same-owner `projects` only where required.

- The matching `ADMISSIONS_JSON` entry binding organization, project, user,
  owner, client and provider. Caller identities remain submit/read only.

Dot and Muse worker grants use `events:subscribe` for scoped queue delivery.
Grok Bot uses Routine webhooks. Existing project/provider checks reject legacy `grok` or `claude` workers claiming a new provider's task. Registering an
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
Enable a Muse project route only after verifying its delivery mode, MCP worker
identity, cancellation and result receipt.
