# QuantAIService

[Documentation index](docs/README.md) — current task runtime, V2 configuration, monitoring and historical architecture.

An independently packaged, project-scoped task runtime for AI work.

English | [简体中文](README.zh-CN.md)

## Status

Experimental. Use at your own risk. This project is not financial advice and
does not guarantee any trading result. It is an operations tool; it does not
place orders and does not grant trading authority.

## What this is

QuantAIService runs project-scoped AI tasks. API tasks and persistent assistant
tasks share one contract, and each repository that uses the service owns its own
runtime, schemas, prompts, and source validation. The package provides the task
server (`quant-ai-service`), a client (`ai-task`), and an offline scenario SDK
for `ScenarioTaskSpec/v1` and `ScenarioInstance/v1` validated with JSON Schema.

## What this is not

It does not own strategy prompts, source validation, execution, or adoption;
those stay in the business repositories that own them. It is not a shared core
for the repository family. It does not place orders or authorize work by itself.

## Quick start

Requires Python 3.10 or newer.

```bash
python -m pip install .
python -m unittest discover -s tests/task_service -v
```

Run the task server with your own configuration. The bundled
`config/quant.example.json` is disabled by default and is a starting point only.

## Configuration

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `AI_SERVICE_URL` | Yes | none | Base URL of the task service. |
| `AI_SERVICE_TOKEN` | No | none | Bearer token used by clients. |
| `AI_SERVICE_AUDIENCE` | No | none | Audience for the workload-identity token. Required by the `client_from_env()` helper; optional for the `ai-task` CLI when `AI_SERVICE_TOKEN` is set. |

With the `ai-task` CLI, set `AI_SERVICE_URL` and then either
`AI_SERVICE_TOKEN` or `AI_SERVICE_AUDIENCE`. The `client_from_env()` helper
used by the scenario pilot CLI instead requires `AI_SERVICE_URL` and
`AI_SERVICE_AUDIENCE`; `AI_SERVICE_TOKEN` stays optional, and when unset the
helper requests a GitHub workload-identity token for that audience.

Server configuration lives in a JSON file based on
[`config/quant.example.json`](config/quant.example.json). It defines the
listening resource, the SQLite state path, accepted token issuers, and the
provider backends. Replace every `CONFIGURE_*` placeholder with your own value.

## Repository layout

```
src/        task server, client, and scenario schemas
config/     example service configuration
deploy/     deployment templates
ops/        monitoring helpers
docs/       design notes and contracts
tests/      unit and contract tests
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). AI assistants and maintainers also follow [AGENTS.md](AGENTS.md).

## Security

See [SECURITY.md](SECURITY.md). Do not open a public issue for a vulnerability.
Keep credentials, private data, and external service tokens out of Git and logs.

## License

MIT. See [LICENSE](LICENSE). The task-service component carries its own MIT
license in [`LICENSE.task-service`](LICENSE.task-service).

## Native diagnostic caller verification

The existing operational-diagnosis workflow accepts `verify_native_only` for a
manual synthetic connection test. It uses the production caller's OIDC,
submission, polling and response binding with an explicitly synthetic input.
It does not authenticate to cloud storage, read real observations, or change the
production daily-attempt ledger. Normal scheduled diagnosis remains unchanged.
The test is single-attempt; an uncertain submission is inspected through its
saved task state rather than automatically submitted again.

Inline bitmap materials use `type=image`, `mime_type` and Base64 data. Native
workers receive standard MCP image blocks in `get_task`; the original request
and result binding remain unchanged. `python -m ai_service.image_command` adapts
existing read-only prompt/image/schema commands to explicitly configured task
routes. Unknown submissions retain their state and use read-only lookup.
