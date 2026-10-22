#!/usr/bin/env python3
"""Observe completed AI unit invocations; reuse the QuantSentinel transport."""

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error


DEFAULT_UNITS = (
    "quant-ai.service", "codex-gateway.service", "codex-audit-service.service",
    "codex-quant.service", "codex-daily-briefing.service",
    "codex-org-health-refresh.service", "codex-router-observer.service",
)
STARTED = "39f53479d3a045ac8e11786248231fbf"


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(state, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load(path):
    if not path.exists():
        return {"schema_version": 1, "units": {}, "outbox": [], "cursor": None}
    state = json.loads(path.read_text(encoding="utf-8"))
    if state.get("schema_version") != 1:
        raise ValueError("unsupported_state_schema")
    return state


def observe(state, *, unit, invocation, failed, reason, run_url, observed_at, threshold=2):
    record = state["units"].setdefault(unit, {"failures": 0, "episode": None})
    if record.get("last_invocation") == invocation:
        return
    record["last_invocation"] = invocation
    record["observed_at"] = observed_at
    record["last_run_url"] = run_url
    record["last_reason"] = reason
    kind = None
    if failed:
        record["failures"] += 1
        if record["failures"] >= threshold and record["episode"] is None:
            record["episode"] = invocation
            kind = "failure"
    else:
        record["failures"] = 0
        if record["episode"] is not None:
            kind = "recovery"
            record["episode"] = None
    if kind:
        text = (
            f"quant-monitor AI {kind}\nunit: {unit}\n"
            f"consecutive_failures: {record['failures']}\nreason: {reason}\n"
            f"run: {run_url}\nevent_at: {observed_at}\n"
            "scope: observation only; no trading or service restart"
        )
        state["outbox"].append({
            "id": hashlib.sha256(f"{unit}:{invocation}:{kind}".encode()).hexdigest(),
            "kind": kind, "unit": unit, "event_at": observed_at, "text": text,
            "receipts": {},
        })


def journal_events(state, units):
    result = subprocess.run(
        ["systemctl", "show", *units, "--property=Id,Type,ActiveState,InvocationID,ActiveEnterTimestampMonotonic"],
        capture_output=True, text=True, timeout=15, check=True,
    )
    properties = {}
    for block in result.stdout.strip().split("\n\n"):
        values = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        properties[values.get("Id")] = values
    command = ["journalctl", "--no-pager", "--quiet", "--output=json", "--show-cursor"]
    for unit in units:
        command.extend(["-u", unit])
    command += ["--after-cursor", state["cursor"]] if state["cursor"] else ["--since", "1 hour ago"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=True)
    cursor = state["cursor"]
    for line in result.stdout.splitlines():
        if line.startswith("-- cursor: "):
            cursor = line.removeprefix("-- cursor: ")
            continue
        if not line.startswith("{"):
            continue
        event = json.loads(line)
        cursor = event.get("__CURSOR", cursor)
        unit = event.get("UNIT")
        invocation = event.get("INVOCATION_ID", "")
        if event.get("_PID") != "1" or unit not in units or not re.fullmatch(r"[0-9a-f]{32}", invocation):
            continue
        result_code = event.get("UNIT_RESULT")
        failed = bool(result_code and result_code != "success") or event.get("JOB_RESULT") == "failed"
        if not failed and event.get("MESSAGE_ID") != STARTED:
            continue
        if not failed and properties.get(unit, {}).get("Type") != "oneshot":
            continue
        reason = result_code or ("job-failed" if failed else "completed-successfully")
        if not re.fullmatch(r"[a-z-]{1,40}", reason):
            reason = "unit-failed" if failed else "completed-successfully"
        timestamp = datetime.fromtimestamp(int(event["__REALTIME_TIMESTAMP"]) / 1e6, timezone.utc).isoformat()
        observe(
            state, unit=unit, invocation=invocation, failed=failed, reason=reason,
            run_url=f"journalctl -u {unit} INVOCATION_ID={invocation}", observed_at=timestamp,
        )
    state["cursor"] = cursor
    for unit, values in properties.items():
        invocation = values.get("InvocationID", "")
        entered = int(values.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
        if (values.get("Type") != "oneshot" and values.get("ActiveState") == "active"
                and entered > 0 and time.monotonic() - entered >= 300
                and re.fullmatch(r"[0-9a-f]{32}", invocation)):
            observe(state, unit=unit, invocation=invocation, failed=False, reason="active-stable-300s",
                    run_url=f"journalctl -u {unit} INVOCATION_ID={invocation}", observed_at=now())


def github_json(endpoint):
    result = subprocess.run(["gh", "api", endpoint], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def workflow_events(state, repo, workflow, branch):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("invalid_repository")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.yml", workflow):
        raise ValueError("invalid_workflow")
    from urllib.parse import quote

    endpoint = f"repos/{repo}/actions/workflows/{workflow}/runs?branch={quote(branch, safe='')}&per_page=100"
    response = github_json(endpoint)
    runs = response["workflow_runs"]
    if response.get("total_count", 0) > 100:
        raise ValueError("workflow_history_limit_exceeded")
    latest = state.get("workflow_run", 0)
    for run in sorted(runs, key=lambda item: item["id"]):
        if run["id"] <= latest:
            continue
        if run["status"] != "completed":
            break
        if run["head_branch"] != branch or run["event"] != "workflow_dispatch":
            raise ValueError("unexpected_workflow_origin")
        if run["conclusion"] not in ("success", "failure"):
            raise ValueError("workflow_conclusion_unavailable")
        expected_url = f"https://github.com/{repo}/actions/runs/{run['id']}"
        if run["html_url"] != expected_url:
            raise ValueError("unexpected_workflow_run_url")
        failed = run["conclusion"] == "failure"
        reason = "workflow-failed" if failed else "workflow-success"
        if failed:
            jobs = github_json(f"repos/{repo}/actions/runs/{run['id']}/jobs")["jobs"]
            if any(step.get("name") == "AI4 controlled failure (exit 42)" and step.get("conclusion") == "failure"
                   for job in jobs for step in job.get("steps", [])):
                reason = "controlled-test-exit-42"
        observe(
            state, unit=f"acceptance:{repo}:{workflow}:{branch}", invocation=str(run["id"]),
            failed=failed, reason=reason, run_url=expected_url,
            observed_at=run["updated_at"], threshold=1,
        )
        state["workflow_run"] = run["id"]


def send(text, chat, token):
    from quant_platform_kit.notifications.telegram import send_telegram_message
    from urllib.request import urlopen

    acknowledgement = {}

    def opener(request, timeout):
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            if 400 <= error.code < 500:
                acknowledgement["status"] = "rejected"
            raise
        payload = json.loads(raw)
        message = payload.get("result", {})
        if payload.get("ok") is False:
            acknowledgement["status"] = "rejected"
        elif (payload.get("ok") is True and isinstance(message.get("message_id"), int)
              and isinstance(message.get("date"), int) and str(message.get("chat", {}).get("id")) == chat):
            acknowledgement.update(status="delivered", message_id=message["message_id"], date=message["date"])
        buffered = io.BytesIO(raw)
        buffered.status = status
        return buffered

    try:
        send_telegram_message(bot_token=token, chat_ids=[chat], text=text, opener=opener, printer=lambda *args, **kwargs: None)
    except Exception:
        pass
    return {"status": "unknown", **acknowledgement, "recorded_at": now()}


def deliver(path, state, sender=send):
    from quant_platform_kit.notifications.telegram import parse_telegram_chat_ids

    chats = parse_telegram_chat_ids(os.environ.get("GLOBAL_TELEGRAM_CHAT_ID"))
    if not chats:
        raise ValueError("telegram_route_unavailable")
    route = sorted(hashlib.sha256(chat.encode()).hexdigest() for chat in chats)
    if state.get("route") not in (None, route):
        raise ValueError("telegram_route_changed")
    state["route"] = route
    token = None
    for notification in state["outbox"]:
        for chat in chats:
            key = hashlib.sha256(chat.encode()).hexdigest()
            previous = notification["receipts"].get(key, {})
            if previous.get("status") in ("delivered", "claimed", "unknown"):
                continue
            if token is None:
                token = os.environ.get("TELEGRAM_TOKEN") or os.environ.get("TG_TOKEN")
                if not token:
                    project = os.environ.get("QUANT_SENTINEL_GCP_PROJECT")
                    secret = os.environ.get("QUANT_SENTINEL_TELEGRAM_SECRET_NAME", "quant-sentinel-telegram-bot-token")
                    if not project:
                        raise ValueError("telegram_project_unavailable")
                    result = subprocess.run(
                        ["gcloud", "secrets", "versions", "access", "latest", "--secret", secret, "--project", project],
                        capture_output=True, text=True, timeout=20, check=True,
                    )
                    token = result.stdout.strip()
                    if not token:
                        raise ValueError("telegram_token_unavailable")
            notification["receipts"][key] = {"status": "claimed", "recorded_at": now()}
            save(path, state)
            receipt = sender(notification["text"], chat, token)
            notification["receipts"][key] = receipt
            save(path, state)
            print(json.dumps({"event": notification["id"], "kind": notification["kind"], **receipt}))
    return 2 if any(receipt.get("status") != "delivered" for item in state["outbox"]
                    for receipt in item["receipts"].values()) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--units", nargs="+", default=list(DEFAULT_UNITS))
    parser.add_argument("--workflow-repo")
    parser.add_argument("--workflow-file")
    parser.add_argument("--workflow-branch")
    arguments = parser.parse_args()
    arguments.state.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with arguments.state.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(arguments.state)
        if arguments.workflow_repo:
            if not arguments.workflow_file or not arguments.workflow_branch:
                raise ValueError("workflow_scope_required")
            workflow_events(state, arguments.workflow_repo, arguments.workflow_file, arguments.workflow_branch)
        else:
            journal_events(state, arguments.units)
        save(arguments.state, state)
        return deliver(arguments.state, state)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as failure:
        print(json.dumps({"status": "unavailable", "error_type": type(failure).__name__}))
        raise SystemExit(1) from None
