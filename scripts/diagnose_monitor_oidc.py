#!/usr/bin/env python3
"""Actions-only, read-only diagnostic caller for the existing audit service.

Consumes saved observations, never runs the monitor, publishes, or repairs.
New diagnoses use the common native task contract after explicit configuration.
Accepted legacy jobs retain their exact recovery route until drained.
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
CLOUD_PROJECT = "longbridgequant"
CLOUD_BUCKET = "qsl-runtime-logs-shared"
CLOUD_OBSERVATION = "monitor-state/core/diagnosis-observation.json"
CLOUD_LEDGER_PREFIX = "monitor-state/diagnosis-actions"
CLOUD_FAILURE_REASONS = frozenset({
    "diagnosis_observation_unavailable", "diagnosis_observation_stale", "diagnosis_observation_invalid",
    "diagnosis_observation_invalid_or_stale", "diagnosis_state_invalid", "diagnosis_checkpoint_invalid",
    "diagnosis_checkpoint_name_invalid", "diagnosis_checkpoint_too_large", "diagnosis_checkpoint_source_missing",
    "diagnosis_checkpoint_destination_not_empty", "state_read_failed", "workspace_too_large",
    "claim_outcome_unknown", "checkpoint_outcome_unknown", "release_outcome_unknown",
})


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


def json_request(opener, url, token, payload=None, *, idempotency_key=None):
    req = request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
                          headers={"Authorization": "Bearer " + token,
                                   "Accept": "application/json", "Content-Type": "application/json",
                                   "User-Agent": "quant-monitor-operational-diagnosis/1.0"},
                          method="GET" if payload is None else "POST")
    if idempotency_key is not None:
        req.add_header('Idempotency-Key', idempotency_key)
    with opener.open(req, timeout=30) as response:
        body = response.read(LIMIT + 1)
    if len(body) > LIMIT:
        raise Rejected("response_too_large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise Rejected("invalid_response")
    return value


def oidc_token(env, opener, *, audience=AUDIENCE):
    url = https_url(env["ACTIONS_ID_TOKEN_REQUEST_URL"])
    if not (url.hostname.endswith(".actions.githubusercontent.com")):
        raise Rejected("invalid_oidc_origin")
    query = [(k, v) for k, v in parse.parse_qsl(url.query) if k != "audience"]
    url = parse.urlunsplit(url._replace(query=parse.urlencode(query + [("audience", audience)])))
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


def identity_available(env, *, include_healthy=False):
    """Check the original Actions identity before accessing mutable state."""
    if not all(env.get(k, "").strip() for k in (
            "ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN")):
        return False
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_REF") != "refs/heads/main"
            or env.get("GITHUB_WORKFLOW_REF") != WORKFLOW_REF
            or env.get("GITHUB_EVENT_NAME") not in {"schedule", "workflow_dispatch"}):
        raise Rejected("invalid_workflow_identity")
    if include_healthy and env["GITHUB_EVENT_NAME"] != "workflow_dispatch":
        raise Rejected("healthy_observation_requires_manual_dispatch")
    return True


def verify_storage_bounds(client):
    """Require actual 403s without downloading the protected archive or names."""
    operations = (
        lambda: client.bucket(CLOUD_BUCKET).blob("monitor-state/core/current.tar.gz").reload(
            timeout=20, retry=None),
        # Iteration makes the request; the field mask excludes object names and
        # contents even if the identity unexpectedly has listing permission.
        lambda: list(client.list_blobs(CLOUD_BUCKET, max_results=1, fields="kind",
                                      timeout=20, retry=None)),
    )
    for operation in operations:
        try:
            operation()
        except Exception as exc:
            if getattr(exc, "code", None) != 403:
                raise Rejected("storage_boundary_unverified") from exc
        else:
            raise Rejected("storage_access_too_broad")
    return {"archive_metadata_denied": True, "object_listing_denied": True}


def unresolved_attempts(state_dir):
    attempts = []
    for path in sorted(state_dir.glob("*.json")):
        state = json.loads(path.read_text())
        if not isinstance(state, dict):
            raise Rejected("invalid_saved_attempt")
        if not state.get("receipt"):
            attempts.append((path, state))
    return attempts


def native_diagnosis(root, state_dir, path, pending, *, env, opener, now, monotonic,
                     sleep, persist, include_healthy, prepare_observation):
    """Use the common task contract; the caller retains observation and claim ownership."""
    service = env.get('AI_SERVICE_URL', '').rstrip('/')
    https_url(service, base=True)
    audience = service + '/mcp'
    if env.get('AI_SERVICE_AUDIENCE') != audience:
        raise Rejected('invalid_native_audience')
    if pending:
        path, state = pending[0]
        if not state.get('job_id'):
            raise Rejected('submit_outcome_unknown')
        if state.get('service_sha256') != digest(service):
            raise Rejected('pending_request_changed')
    else:
        if path.exists():
            return {'status': 'skipped', 'reason': 'daily_attempt_limit'}
        if prepare_observation is not None:
            prepare_observation()
        paths = sorted((root / 'data/health').glob('cycle_*.json'))
        if not paths:
            raise Rejected('latest_cycle_unavailable')
        observed_at, errors = _read_diagnosis_cycle(paths[-1], now=now, max_age=timedelta(hours=2))
        if not errors and not include_healthy:
            return {'status': 'skipped', 'reason': 'no_data_errors'}
        model, profile = env.get('AI_SERVICE_DIAGNOSIS_MODEL', ''), env.get('AI_SERVICE_DIAGNOSIS_PROFILE', 'default')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', model) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', profile):
            raise Rejected('native_route_not_configured')
        observation = {'observed_at': observed_at.isoformat(), 'latest_cycle_at': observed_at.isoformat(),
                       'current_state': 'still_observed' if errors else 'no_data_errors'}
        payload = {'mode': 'agent', 'model': model, 'profile': profile, 'timeout_seconds': 600,
                   'objective': _operational_diagnosis_prompt(errors, observation=observation)
                   + '\nUse only this saved classification. Empty incidents mean no data errors were recorded, '
                   'not account or trading health. No external tools, repairs, notifications, deployments or orders. '
                   'Return advisory JSON with a diagnosis string only.',
                   'materials': [{'source': 'saved-monitor-classification', 'observed_at': observed_at.isoformat()}],
                   'output_schema': {'type': 'object', 'properties': {'diagnosis': {'type': 'string', 'minLength': 1}},
                                     'required': ['diagnosis'], 'additionalProperties': False}}
        state = {'protocol': 'native_tasks_v1', 'request_sha256': digest(payload),
                 'service_sha256': digest(service), 'observed_at': observed_at.isoformat(),
                 'job_id': None, 'deadline': now.timestamp() + 660,
                 'route': {'provider': 'dot', 'model': model, 'profile': profile}}
        token = oidc_token(env, opener, audience=audience)
        persist(path, state)
        task = json_request(opener, service + '/v1/tasks', token, payload,
                            idempotency_key='monitor-diagnosis:' + path.stem + ':' + state['request_sha256'])
        if (not re.fullmatch(r'[0-9a-f-]{36}', str(task.get('id', '')))
                or task.get('owner') != REPOSITORY or task.get('provider') != 'dot'
                or task.get('result_kind') != 'advisory' or digest(task.get('request')) != state['request_sha256']):
            raise Rejected('invalid_native_receipt')
        state['job_id'] = task['id']
        persist(path, state)
    if (not re.fullmatch(r'[0-9a-f-]{36}', str(state.get('job_id', '')))
            or not re.fullmatch(r'[0-9a-f]{64}', str(state.get('request_sha256', '')))
            or type(state.get('deadline')) not in (int, float) or not math.isfinite(state['deadline'])):
        raise Rejected('invalid_native_receipt')
    deadline = monotonic() + max(0, min(660, state['deadline'] - now.timestamp()))
    first_poll = True
    while first_poll or monotonic() < deadline:
        first_poll = False
        task = json_request(opener, service + '/v1/tasks/' + state['job_id'], oidc_token(env, opener, audience=audience))
        if (task.get('id') != state['job_id'] or task.get('owner') != REPOSITORY
                or task.get('provider') != 'dot' or task.get('result_kind') != 'advisory'
                or digest(task.get('request')) != state['request_sha256']):
            raise Rejected('native_task_identity_changed')
        if task.get('status') == 'completed':
            output = task.get('output')
            if not isinstance(output, dict) or set(output) != {'diagnosis'} or not isinstance(output['diagnosis'], str) or not output['diagnosis'].strip():
                raise Rejected('empty_diagnosis_output')
            text = output['diagnosis']
            receipt = {'status': 'succeeded', 'protocol': 'native_tasks_v1', 'job_id': state['job_id'],
                       'task_id': state['job_id'], **state['route'], 'model_verification': 'unavailable',
                       'task': TASK, 'mode': 'review_only', 'sandbox': 'read-only', 'source_repository': REPOSITORY,
                       'audience': audience, 'observed_at': state['observed_at'], 'request_sha256': state['request_sha256'],
                       'output_sha256': hashlib.sha256(text.encode()).hexdigest(), 'output_length': len(text),
                       'advisory_only': True, 'execution_authority_granted': False}
            persist(path, {**state, 'receipt': receipt})
            return receipt
        if task.get('status') in {'failed', 'cancelled', 'invalid_output'}:
            persist(path, {**state, 'receipt': {'status': task['status'], 'protocol': 'native_tasks_v1',
                                             'advisory_only': True, 'execution_authority_granted': False}})
            raise Rejected('native_task_failed')
        if task.get('status') == 'outcome_unknown':
            raise Rejected('native_task_outcome_unknown')
        if task.get('status') not in {'queued', 'submitting', 'running', 'cancel_requested'}:
            raise Rejected('invalid_native_receipt')
        if monotonic() < deadline:
            sleep(5)
    raise Rejected('native_task_timeout')


def diagnose(root, state_dir, *, include_healthy=False, env=None, opener=None,
             now=None, monotonic=time.monotonic, sleep=time.sleep, checkpoint=None,
             prepare_observation=None):
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
    if not identity_available(env, include_healthy=include_healthy):
        return {"status": "skipped", "reason": "no_oidc"}
    service = env.get("CODEX_AUDIT_SERVICE_URL", "").rstrip("/")
    now = now or datetime.now(timezone.utc)
    opener = opener or request.build_opener(NoRedirect())
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # The file and lock are separate from the monitor's own attempt/alert ledgers.
    with (state_dir / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = state_dir / (now.date().isoformat() + ".json")
        pending = unresolved_attempts(state_dir)
        native_pending = pending and pending[0][1].get('protocol') == 'native_tasks_v1'
        if native_pending or (not pending and env.get('AI_SERVICE_DIAGNOSIS_READY') == 'true'):
            return native_diagnosis(root, state_dir, path, pending, env=env, opener=opener, now=now,
                                    monotonic=monotonic, sleep=sleep, persist=persist,
                                    include_healthy=include_healthy, prepare_observation=prepare_observation)
        https_url(service, base=True)
        for previous, state in pending:
            if previous != path and not state.get("job_id"):
                raise Rejected("previous_outcome_unresolved")
        # Recover the oldest accepted job before looking at any new observation.
        # A newer cycle, midnight or expired polling budget cannot authorize POST.
        if pending:
            path, state = pending[0]
            # An ambiguous POST is never automatically resubmitted, even on rerun.
            if not state.get("job_id"):
                raise Rejected("submit_outcome_unknown")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(state.get("request_sha256", "")))
                    or state.get("service_sha256") != digest(service)):
                raise Rejected("pending_request_changed")
            check_job({"job_id": state["job_id"], "status": "queued", "task": TASK,
                       "source_repository": REPOSITORY, **state["route"]})
            if type(state.get("deadline")) not in (float, int) or not math.isfinite(state["deadline"]):
                raise Rejected("invalid_pending_deadline")
        else:
            if path.exists():
                return {"status": "skipped", "reason": "daily_attempt_limit"}
            if prepare_observation is not None:
                prepare_observation()
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
        first_poll = True
        while first_poll or monotonic() < deadline:
            first_poll = False
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
            if monotonic() < deadline:
                sleep(5)
        raise Rejected("audit_job_timeout")


def diagnose_cloud(*, include_healthy=False, verify_state_only=False, env=None,
                   client=None, store=None, refresh=None, now=None, opener=None,
                   monotonic=time.monotonic, sleep=time.sleep):
    """Use the shared minimal JSON adapter and this consumer's independent claim."""
    env = os.environ if env is None else env
    if not identity_available(env, include_healthy=include_healthy):
        return {"status": "skipped", "reason": "no_oidc"}
    if not verify_state_only:
        https_url((env.get("AI_SERVICE_URL", "") if env.get("AI_SERVICE_DIAGNOSIS_READY") == "true" else env.get("CODEX_AUDIT_SERVICE_URL", "")).rstrip("/"), base=True)
    elif env["GITHUB_EVENT_NAME"] != "workflow_dispatch":
        raise Rejected("state_verification_requires_manual_dispatch")
    if (not re.fullmatch(r"[0-9]{1,24}", env.get("GITHUB_RUN_ID", ""))
            or not re.fullmatch(r"[0-9]{1,6}", env.get("GITHUB_RUN_ATTEMPT", ""))):
        raise Rejected("invalid_execution_identity")
    if client is None:
        import google.auth
        from google.auth.transport.requests import Request
        from google.cloud import storage

        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        client = storage.Client(project=CLOUD_PROJECT, credentials=credentials)

        def refresh():
            credentials.refresh(Request())
    if store is None:
        from monitor_diagnosis_state import DiagnosisState

        store = DiagnosisState(
            client, checkpoint_uri=f"gs://{CLOUD_BUCKET}/{CLOUD_LEDGER_PREFIX}/checkpoints.json",
            observation_uri=f"gs://{CLOUD_BUCKET}/{CLOUD_OBSERVATION}")
    now = now or datetime.now(timezone.utc)
    # Qualification still validates its source first. Normal execution restores
    # pending jobs first; polling one does not depend on a fresh observation.
    if verify_state_only:
        store.observation(now=now)
    bounds = verify_storage_bounds(client) if verify_state_only else {}
    claim = store.claim("actions/" + env["GITHUB_RUN_ID"] + "/" + env["GITHUB_RUN_ATTEMPT"])
    if claim is None:
        raise Rejected("diagnosis_state_busy")
    with tempfile.TemporaryDirectory(prefix="actions-diagnosis-") as directory:
        root = Path(directory)
        state_dir = root / "actions-state"
        retain_claim = False
        try:
            raw, generation = store.read()
            store.restore(raw, state_dir)
            retain_claim = any(not state.get("job_id") for _, state in unresolved_attempts(state_dir))

            def checkpoint():
                nonlocal generation, retain_claim
                # Set before I/O: a lost response may conceal a successful write.
                retain_claim = True
                generation = store.checkpoint(state_dir, state_generation=generation, claim_generation=claim)
                retain_claim = any(not state.get("job_id") for _, state in unresolved_attempts(state_dir))

            def prepare_observation():
                observed = store.observation(now=now)
                cycle_path = root / "data/health/cycle_observation.json"
                cycle_path.parent.mkdir(parents=True)
                save(cycle_path, observed)

            if verify_state_only:
                if refresh is None:
                    raise Rejected("credential_refresh_unavailable")
                refresh()
                # Save unchanged bytes; never obtain an audit token or submit.
                checkpoint()
                restored_raw, restored_generation = store.read()
                if restored_raw != raw or restored_generation != generation:
                    retain_claim = True
                    raise Rejected("checkpoint_verification_failed")
                pending = unresolved_attempts(state_dir)
                drain = {"unresolved_attempts": len(pending),
                         "legacy_pending_attempts": sum(s.get('protocol') != 'native_tasks_v1' for _, s in pending),
                         "native_pending_attempts": sum(s.get('protocol') == 'native_tasks_v1' for _, s in pending),
                         "unknown_submission_attempts": sum(not s.get('job_id') for _, s in pending)}
                result = {"status": "verified", "mode": "cloud_state_only", **bounds, **drain,
                          "adc_refresh_verified": True, "checkpoint_unchanged": True,
                          "audit_requests": 0}
            else:
                result = diagnose(root, state_dir, include_healthy=include_healthy, env=env, now=now,
                                  opener=opener, monotonic=monotonic, sleep=sleep, checkpoint=checkpoint,
                                  prepare_observation=prepare_observation)
        except Exception:
            # Read-only failures can recover next run. Ambiguous submission or
            # checkpoint outcomes must still be reconciled before exact release.
            if not retain_claim:
                store.release(claim_generation=claim)
            raise
        store.release(claim_generation=claim)
        return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--cloud", action="store_true")
    parser.add_argument("--verify-state-only", action="store_true")
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--include-healthy", action="store_true")
    args = parser.parse_args(argv)
    if args.cloud:
        if args.root is not None or args.state_dir is not None:
            parser.error("--cloud cannot be combined with local paths")
    elif args.root is None or args.state_dir is None or args.verify_state_only:
        parser.error("provide --root and --state-dir, or use --cloud")
    try:
        if args.cloud:
            result = diagnose_cloud(include_healthy=args.include_healthy, verify_state_only=args.verify_state_only)
        else:
            result = diagnose(args.root, args.state_dir, include_healthy=args.include_healthy)
    except error.HTTPError as exc:
        result = {"status": "unavailable", "reason": "http_error", "http_status": exc.code}
    except Rejected as exc:
        result = {"status": "unavailable", "reason": str(exc)}
    except Exception as exc:
        state_error = getattr(sys.modules.get("monitor_cloud_state"), "StateUnavailable", ())
        reason = (str(exc) if args.cloud and isinstance(exc, state_error)
                  and str(exc) in CLOUD_FAILURE_REASONS else "diagnosis_unavailable")
        result = {"status": "unavailable", "reason": reason}
    # No response body, exception details, prompt, URL, token or model text is logged.
    save(args.receipt, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] in {"succeeded", "skipped", "verified"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
