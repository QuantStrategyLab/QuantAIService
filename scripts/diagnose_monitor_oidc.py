#!/usr/bin/env python3
"""Actions-only, read-only diagnostic caller for the existing audit service.

Consumes saved observations, never runs the monitor, publishes, or repairs.
The V2 native task client remains separate from this deliberately narrow caller.
See ../AGENTS.md for operating and authorization boundaries.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from urllib import error, parse, request

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/quant-monitor/scripts"))
from health_cycle import _operational_diagnosis_prompt, _read_diagnosis_cycle

AUDIENCE = "quant-codex-audit"
REPOSITORY = "QuantStrategyLab/QuantAIService"
WORKFLOW_REF = REPOSITORY + "/.github/workflows/quant-monitor-operational-diagnosis.yml@refs/heads/main"
JOBS = "/v1/ai/execute/jobs"
TASK = "operational_data_diagnosis"
LIMIT = 2 * 1024 * 1024


class Rejected(ValueError):
    pass


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def https_url(value, *, base=False):
    url = parse.urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.fragment or (base and (url.query or url.path not in {"", "/"}))):
        raise Rejected("invalid_https_url")
    return url


def json_request(opener, url, token, payload=None):
    req = request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token,
                                   "Accept": "application/json", "Content-Type": "application/json"},
                          method="GET" if payload is None else "POST")
    with opener.open(req, timeout=30) as response:
        body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise Rejected("response_too_large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise Rejected("invalid_response")
    return value


def oidc_token(env, opener):
    url = https_url(env["ACTIONS_ID_TOKEN_REQUEST_URL"])
    if not (url.hostname.endswith(".actions.githubusercontent.com")):
        raise Rejected("invalid_oidc_origin")
    query = [(k, v) for k, v in parse.parse_qsl(url.query) if k != "audience"]
    url = parse.urlunsplit(url._replace(query=parse.urlencode(query + [("audience", AUDIENCE)])))
    value = json_request(opener, url, env["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]).get("value")
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise Rejected("invalid_oidc_response")
    return value


def save(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".diagnosis-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def check_job(job, job_id=None, route=None):
    if (not isinstance(job.get("job_id"), str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{24,96}", job["job_id"])
            or (job_id is not None and job["job_id"] != job_id)
            or job.get("task") != TASK or job.get("source_repository") != REPOSITORY
            or job.get("provider") != "codex" or job.get("research_stage") != "drift_analysis"
            or not isinstance(job.get("model"), str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", job["model"])
            or job.get("reasoning_effort") not in {"low", "medium", "high", "xhigh"}
            or job.get("status") not in {"queued", "running", "succeeded", "failed"}):
        raise Rejected("invalid_job_receipt")
    actual = {k: job[k] for k in ("provider", "research_stage", "model", "reasoning_effort")}
    if route is not None and actual != route:
        raise Rejected("job_route_changed")
    return actual


def diagnose(root, state_dir, *, include_healthy=False, env=None, opener=None,
             now=None, monotonic=time.monotonic, sleep=time.sleep, checkpoint=None):
    """Diagnose a saved observation, optionally persisting each state transition.

    A portable-state adapter must acquire its shared claim before restoring the
    observation and state_dir. Its zero-argument checkpoint callback persists
    the updated directory durably while retaining that claim, and raises if the
    write is uncertain. It runs before submission, before polling an accepted
    job and before returning success. The adapter owns final claim release;
    this hook neither grants storage access nor makes the CLI portable itself.
    """
    def persist(path, value):
        save(path, value)
        if checkpoint is not None:
            checkpoint()

    env = os.environ if env is None else env
    if not all(env.get(k, "").strip() for k in (
            "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        return {"status": "skipped", "reason": "no_oidc"}
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF
            or env.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}):
        raise Rejected("invalid_workflow_identity")
    if include_healthy and env["GITHUB_EVENT_NAME"] != "workflow_dispatch":
        raise Rejected("healthy_observation_requires_manual_dispatch")
    service = env.get("CODEX_AUDIT_SERVICE_URL", "").rstrip("/")
    https_url(service, base=True)
    now = now or datetime.now(timezone.utc)
    paths = sorted((root / "data/health").glob("cycle_*.json"))
    if not paths:
        raise Rejected("latest_cycle_unavailable")
    observed_at, errors = _read_diagnosis_cycle(paths[-1], now=now, max_age=timedelta(hours=2))
    if not errors and not include_healthy:
        return {"status": "skipped", "reason": "no_data_errors"}
    observation = {"observed_at": observed_at.isoformat(), "latest_cycle_at": observed_at.isoformat(),
                   "current_state": "still_observed" if errors else "no_data_errors"}
    payload = {"task": TASK, "mode": "review_only", "sandbox": "read-only",
               "allowed_providers": ["codex"], "research_stage": "drift_analysis",
               "complexity": "high", "model": "auto", "reasoning_effort": "auto",
               "source_repository": REPOSITORY, "source_ref": "main", "timeout_seconds": 600,
               "prompt": _operational_diagnosis_prompt(errors, observation=observation)
               + "\nThis is a real saved observation. An empty incident list means no data errors "
               "were recorded; do not invent an incident or infer trading/account health. "
               "Use only this supplied classification and state its limits; no external tools or actions."}
    opener = opener or request.build_opener(NoRedirect())
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The file and lock are separate from the monitor's own attempt/alert ledgers.
    with (state_dir / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / (now.date().isoformat() + ".json")
        for previous in state_dir.glob("*.json"):
            if previous != path and not json.loads(previous.read_text()).get("receipt"):
                raise Rejected("previous_outcome_unresolved")
        if path.exists():
            state = json.loads(path.read_text())
            if state.get("receipt"):
                return {"status": "skipped", "reason": "daily_attempt_limit"}
            # An ambiguous POST is never automatically resubmitted, even on rerun.
            if not state.get("job_id"):
                raise Rejected("submit_outcome_unknown")
            if state.get("request_sha256") != digest(payload) or state.get("service_sha256") != digest(service):
                raise Rejected("pending_request_changed")
            check_job({"job_id": state["job_id"], "status": "queued", "task": TASK,
                       "source_repository": REPOSITORY, **state["route"]})
            if type(state.get("deadline")) not in (float, int) or not math.isfinite(state["deadline"]):
                raise Rejected("invalid_pending_deadline")
        else:
            state = {"request_sha256": digest(payload), "service_sha256": digest(service),
                     "observed_at": observed_at.isoformat(), "job_id": None,
                     "deadline": now.timestamp() + 660}
            # Fetch identity before spending the attempt, but persist before POST.
            token = oidc_token(env, opener)
            persist(path, state)
            job = json_request(opener, service + JOBS, token, payload)
            route = check_job(job)
            state.update(job_id=job["job_id"], route=route)
            persist(path, state)
        deadline = monotonic() + max(0, min(660, state["deadline"] - now.timestamp()))
        while monotonic() < deadline:
            job = json_request(opener, service + JOBS + "/" + state["job_id"], oidc_token(env, opener))
            check_job(job, state["job_id"], state["route"])
            if job["status"] == "failed":
                raise Rejected("audit_job_failed")
            if job["status"] == "succeeded":
                output = job.get("output")
                if not isinstance(output, str) or not output.strip():
                    raise Rejected("empty_diagnosis_output")
                receipt = {"status": "succeeded", "job_id": job["job_id"], **state["route"],
                           "task": TASK, "mode": "review_only", "sandbox": "read-only",
                           "source_repository": REPOSITORY, "audience": AUDIENCE,
                           "observed_at": state["observed_at"], "request_sha256": state["request_sha256"],
                           "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                           "output_length": len(output), "advisory_only": True,
                           "execution_authority_granted": False}
                persist(path, {**state, "receipt": receipt})
                return receipt
            sleep(5)
        raise Rejected("audit_job_timeout")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--include-healthy", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = diagnose(args.root, args.state_dir, include_healthy=args.include_healthy)
    except error.HTTPError as exc:
        result = {"status": "unavailable", "reason": "http_error", "http_status": exc.code}
    except Rejected as exc:
        result = {"status": "unavailable", "reason": str(exc)}
    except Exception:
        result = {"status": "unavailable", "reason": "diagnosis_unavailable"}
    # No response body, exception details, prompt, URL, token or model text is logged.
    save(args.receipt, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"succeeded", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
