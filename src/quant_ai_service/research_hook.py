"""Bounded Q04 shadow observations; no task submission or business actions."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
from urllib.parse import quote, urlsplit, urlunsplit

from ai_service.transport import PublicHTTPS, json_body


TARGET = "quant-ai-service-v2"
ENVIRONMENT_FIELDS = {"url", "audience", "profile", "model", "read_token"}
TASK_STATUSES = {"queued", "submitting", "running", "completed", "failed", "cancelled",
                 "invalid_output", "outcome_unknown"}


class BindingError(ValueError):
    pass


def load_binding(path, environment):
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
        if (not isinstance(config, dict)
                or set(config) != {"schema", "service_target", "mode", "purpose", "environment"}
                or config["schema"] != "ai-hook-binding.v1"
                or config["service_target"] != TARGET or config["mode"] != "shadow"
                or config["purpose"] != "research"
                or not isinstance(config["environment"], dict)
                or set(config["environment"]) != ENVIRONMENT_FIELDS):
            raise BindingError("invalid_binding")
        values = {}
        for field, variable in config["environment"].items():
            if not isinstance(variable, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", variable):
                raise BindingError("invalid_binding")
            value = environment.get(variable)
            if (not isinstance(value, str) or not value.strip() or value != value.strip()
                    or any(ord(character) < 32 for character in value)):
                raise BindingError("missing_binding_value")
            values[field] = value
        resource = urlsplit(values["url"])
        if (resource.scheme != "https" or not resource.hostname or resource.username
                or resource.password or resource.query or resource.fragment
                or resource.port not in (None, 443) or not resource.path.rstrip("/").endswith("/mcp")):
            raise BindingError("invalid_binding")
        values["url"] = values["url"].rstrip("/")
        return values
    except (OSError, ValueError, TypeError, KeyError):
        raise BindingError("binding_unavailable") from None


def observe(binding, *, task_id=None, transport=None):
    """Only MCP initialize and an optional GET of an already existing task."""
    transport = transport or PublicHTTPS(timeout=15, max_response_bytes=262144)
    try:
        if task_id is not None and (not isinstance(task_id, str)
                                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id)):
            raise ValueError("invalid task identity")
        headers = {"Authorization": "Bearer " + binding["read_token"], "Content-Type": "application/json"}
        response = transport.request(binding["url"], method="POST", headers=headers, body=json_body({
            "jsonrpc": "2.0", "id": "ai5-shadow", "method": "initialize", "params": {
                "protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "q04-shadow-reader", "version": "1"}}}))
        if response.status != 200:
            raise ValueError("service unavailable")
        envelope = json.loads(response.body)
        info = envelope["result"]["serverInfo"]
        if (envelope.get("id") != "ai5-shadow" or envelope.get("jsonrpc") != "2.0"
                or "error" in envelope or info.get("name") != "quant-ai-service"
                or not isinstance(info.get("version"), str)
                or re.fullmatch(r"2\.\d+\.\d+(?:[a-zA-Z0-9.+-]*)?", info["version"]) is None):
            raise ValueError("not the v2 service")
        result = {"status": "identity_only", "service_identity_verified": True,
                  "existing_task_read": False, "q04_protocol_accepted": False}
        if task_id is not None:
            resource = urlsplit(binding["url"])
            task_url = urlunsplit((resource.scheme, resource.netloc, "/v1/tasks/" + quote(task_id, safe=""), "", ""))
            response = transport.request(task_url,
                                         method="GET", headers=headers, body=b"")
            if response.status != 200:
                raise ValueError("existing task unavailable")
            task = json.loads(response.body)
            if task.get("id") != task_id or task.get("status") not in TASK_STATUSES:
                raise ValueError("task read mismatch")
            result.update(status="existing_task_observed", existing_task_read=True)
        return result
    except Exception:
        return {"status": "observation_unavailable", "service_identity_verified": False,
                "existing_task_read": False, "q04_protocol_accepted": False}


def start_plan(now):
    if now.tzinfo is None:
        raise ValueError("timezone required")
    return {"schema": "q04-shadow-plan.v1", "service_target": TARGET,
            "mode": "shadow", "formal_actions_enabled": False,
            "started_at": now.isoformat(), "ends_at": (now + timedelta(days=7)).isoformat(),
            "daily_checkpoints": [(now + timedelta(days=day)).isoformat() for day in range(8)]}


def checkpoint(plan, binding_path, environment, *, now, task_id=None, transport=None):
    started = datetime.fromisoformat(plan["started_at"])
    if (plan != start_plan(started) or now.tzinfo is None or now < started):
        raise ValueError("invalid shadow plan")
    elapsed = now >= started + timedelta(days=7)
    result = {"schema": "q04-shadow-checkpoint.v1", "checked_at": now.isoformat(),
              "day": min((now - started).days, 7), "service_target": TARGET,
              "phase": "shadow_elapsed_review_required" if elapsed else "shadow_in_progress",
              "formal_actions_enabled": False, "ai_submitted": False,
              "q04_protocol_accepted": False, "service_identity_verified": False,
              "existing_task_read": False}
    if elapsed:
        return result | {"status": "window_elapsed_no_automatic_activation"}
    try:
        binding = load_binding(binding_path, environment)
    except BindingError:
        return result | {"status": "blocked_configuration"}
    return result | observe(binding, task_id=task_id, transport=transport)


def write_record(path, value, *, exclusive=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | (os.O_EXCL if exclusive else os.O_TRUNC)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--binding", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--start", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    try:
        if args.start:
            write_record(args.plan, start_plan(now), exclusive=True)
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        result = checkpoint(plan, args.binding, os.environ, now=now,
                            task_id=os.environ.get("AI_HOOK_EXISTING_TASK_ID") or None)
        write_record(args.output, result)
    except (OSError, ValueError, TypeError, KeyError):
        raise SystemExit("shadow record unavailable") from None
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(2 if result["status"] in {"blocked_configuration", "observation_unavailable"} else 0)


if __name__ == "__main__":
    main()
