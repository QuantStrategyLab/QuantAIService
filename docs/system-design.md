# System design — package boundary

[Documentation index](README.md) · [Runtime baseline](runtime-baseline.md)

This page records the current V2 package boundary after the Application / HTTP
split. It does not authorize deployment, trading, merge, or credential access.
Do not commit audit reports, progress logs, or temporary handoffs into git.

## Packages

| Package / module | Role |
|---|---|
| `ai_service.server` | HTTP/MCP **transport**: `ThreadingHTTPServer`, request framing, auth headers, status codes. Re-exports `Application` and wiring helpers for compatibility. |
| `ai_service.application` | **Application** operations: task API, MCP JSON-RPC handlers, access helpers. No socket bind; invoked by the transport. |
| `ai_service.wiring` | Composition root: validate config, assemble runtime/auth/providers, build `Application`. |
| `ai_service.runtime` | Durable task state and policy (not HTTP). |
| `ai_service.*` (providers, auth, …) | Domain adapters used by wiring/application. |
| `quant_ai_service` | Published wheel / CLI entry (`quant-ai-service`), owner routing validation, stable consumer imports. Prefer `ai_service...` for new in-repo code. |

## Import guidance

- New application logic: `from ai_service.application import Application` (or the
  specific mixin module under `application/`).
- New HTTP/process entry work: keep framing in `ai_service.server`; call into
  `application` / `wiring` rather than growing handlers inside `server.py`.
- Compatibility: `from ai_service.server import Application` remains valid and
  must refer to the same class object as `ai_service.application.Application`.
- Consumers and setuptools entry points may keep `quant_ai_service`.

## What stays out of git

Temporary audit reports, Wave progress ledgers, and session handoffs stay
outside the repository (owner workspace / out-of-tree notes only).
