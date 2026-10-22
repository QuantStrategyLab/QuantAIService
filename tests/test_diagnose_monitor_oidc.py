import importlib.util
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from urllib import error, parse

import pytest

SPEC = importlib.util.spec_from_file_location(
    "diagnose_monitor_oidc", Path(__file__).resolve().parents[1] / "scripts/diagnose_monitor_oidc.py")
caller = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(caller)
NOW = datetime(2026, 10, 22, 13, 0, tzinfo=timezone.utc)
ID = "a" * 32
ERROR = {"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}


def environment():
    return {"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": caller.REPOSITORY,
            "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_REF": caller.WORKFLOW_REF,
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://vstoken.actions.githubusercontent.com/token?audience=wrong&x=1",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-request-bearer",
            "CODEX_AUDIT_SERVICE_URL": "https://audit.invalid",
            "CODEX_AUDIT_SERVICE_AUDIENCE": "wrong-override-must-be-ignored",
            "CODEX_AUDIT_SERVICE_TOKEN": "synthetic-static-must-never-be-used"}


def job(status="queued", **changes):
    return {"job_id": ID, "status": status, "task": caller.TASK,
            "source_repository": caller.REPOSITORY, "provider": "codex",
            "research_stage": "drift_analysis", "model": "test-model",
            "reasoning_effort": "high", **changes}


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def open(self, req, timeout):
        self.calls.append(req)
        value = next(self.replies)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())


def snapshot(root, errors=None, age=timedelta()):
    path = root / "data/health/cycle_20261022T130000Z.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"domains": list(caller._read_diagnosis_cycle.__globals__["DOMAINS"]),
                                "as_of": (NOW - age).isoformat(),
                                "data_errors": [ERROR] if errors is None else errors,
                                "sensitive_extra": "must-not-enter-prompt"}))
    return path


def run(root, opener, **kwargs):
    return caller.diagnose(root, root / "actions-state", env=environment(), opener=opener,
                           now=NOW, sleep=lambda _: None, **kwargs)


@pytest.mark.parametrize("missing", ["ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "both"])
def test_no_oidc_ignores_static_token_with_zero_io(tmp_path, missing):
    env = environment()
    for key in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"):
        if missing in (key, "both"):
            env.pop(key)
    opener = Opener([])
    assert caller.diagnose(tmp_path, tmp_path / "state", env=env, opener=opener) == {
        "status": "skipped", "reason": "no_oidc"}
    assert opener.calls == [] and not (tmp_path / "state").exists()
    health = caller._read_diagnosis_cycle.__globals__
    with mock.patch.dict("os.environ", env, clear=True), mock.patch.object(
            caller.request, "urlopen", side_effect=AssertionError("no network")):
        assert health["_run_operational_diagnosis"](tmp_path, [ERROR], "1" * 64) == {
            "status": "skipped", "reason": "no_oidc"}


def test_success_requires_bound_service_output_and_fixed_readonly_contract(tmp_path):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-poll-oidc"},
                     job("succeeded", output="private diagnostic advice")])
    receipt = run(tmp_path, opener)
    assert receipt["status"] == "succeeded" and receipt["job_id"] == ID
    assert receipt["output_length"] == len("private diagnostic advice")
    assert "private diagnostic advice" not in json.dumps(receipt)
    assert receipt["audience"] == "quant-codex-audit"
    for req in (opener.calls[0], opener.calls[2]):
        assert parse.parse_qs(parse.urlsplit(req.full_url).query) == {
            "x": ["1"], "audience": ["quant-codex-audit"]}
    submit = opener.calls[1]
    payload = json.loads(submit.data)
    assert (payload["task"], payload["sandbox"], payload["mode"]) == (
        "operational_data_diagnosis", "read-only", "review_only")
    assert payload["allowed_providers"] == ["codex"]
    assert payload["source_repository"] == caller.REPOSITORY and payload["source_ref"] == "main"
    assert set(payload) == {"task", "mode", "sandbox", "allowed_providers", "research_stage", "complexity",
                            "model", "reasoning_effort", "source_repository", "source_ref", "timeout_seconds", "prompt"}
    assert "must-not-enter-prompt" not in payload["prompt"]
    assert "Do not place orders" in payload["prompt"]
    assert [(r.method, parse.urlsplit(r.full_url).path) for r in opener.calls[1::2]] == [
        ("POST", caller.JOBS), ("GET", caller.JOBS + "/" + ID)]
    assert submit.get_header("Authorization") == "Bearer synthetic-oidc"
    assert run(tmp_path, Opener([])) == {"status": "skipped", "reason": "daily_attempt_limit"}


@pytest.mark.parametrize("changes", [
    {"job_id": "../../deploy"}, {"task": "deploy"}, {"source_repository": "other/repo"},
    {"provider": "cursor"}, {"research_stage": "optimization"}, {"status": "unknown"},
    {"model": "private\ntext"}, {"reasoning_effort": "invalid"},
])
def test_bad_admission_never_polls_or_retries(tmp_path, changes):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(**changes)])
    with pytest.raises(caller.Rejected, match="invalid_job_receipt"):
        run(tmp_path, opener)
    assert len(opener.calls) == 2
    with pytest.raises(caller.Rejected, match="submit_outcome_unknown"):
        run(tmp_path, Opener([]))


@pytest.mark.parametrize("last", [job("failed", error="sensitive backend details"),
                                  job("succeeded", output=""),
                                  job("succeeded", job_id="b" * 32, output="advice"),
                                  job("succeeded", model="changed", output="advice"),
                                  b"not-json-private-details"])
def test_poll_failures_cannot_be_success(tmp_path, last):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"}, last])
    with pytest.raises((caller.Rejected, ValueError)):
        run(tmp_path, opener)
    state = json.loads(next((tmp_path / "actions-state").glob("*.json")).read_text())
    assert "receipt" not in state
    assert len([r for r in opener.calls if r.method == "POST"]) == 1


@pytest.mark.parametrize("code", [302, 401, 403, 429, 500])
def test_http_failures_are_sanitized_and_never_resubmitted(tmp_path, monkeypatch, capsys, code):
    snapshot(tmp_path)
    failure = error.HTTPError("https://sensitive.invalid", code, "private-body", {}, io.BytesIO(b"private-body"))
    opener = Opener([{"value": "synthetic-oidc"}, failure])
    monkeypatch.setattr(caller.request, "build_opener", lambda *args: opener)
    monkeypatch.setenv("IGNORED", "")
    with mock.patch.dict("os.environ", environment(), clear=True), mock.patch.object(
            caller, "datetime") as dt:
        dt.now.return_value = NOW
        receipt = tmp_path / "receipt.json"
        assert caller.main(["--root", str(tmp_path), "--state-dir", str(tmp_path / "actions-state"),
                            "--receipt", str(receipt)]) == 1
    assert json.loads(receipt.read_text()) == {"status": "unavailable", "reason": "http_error", "http_status": code}
    assert "private-body" not in capsys.readouterr().out
    with pytest.raises(caller.Rejected, match="submit_outcome_unknown"):
        run(tmp_path, Opener([]))


def test_no_redirect_can_forward_identity():
    assert caller.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.invalid") is None


def test_previous_unknown_submit_cannot_be_repeated_after_midnight(tmp_path):
    snapshot(tmp_path)
    state = tmp_path / "actions-state"
    state.mkdir()
    (state / "2026-10-21.json").write_text(json.dumps({"job_id": None}))
    opener = Opener([])
    with pytest.raises(caller.Rejected, match="previous_outcome_unresolved"):
        run(tmp_path, opener)
    assert opener.calls == []


def test_pending_read_resumes_original_job_without_another_submit(tmp_path):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     error.URLError("private-poll-error")])
    with pytest.raises(error.URLError):
        run(tmp_path, opener)
    resumed = Opener([{"value": "synthetic-oidc"}, job("succeeded", output="advice")])
    assert run(tmp_path, resumed)["status"] == "succeeded"
    assert all(req.method == "GET" for req in resumed.calls)


@pytest.mark.parametrize("url", ["http://audit.invalid", "https://user:password@audit.invalid",
                                 "https://audit.invalid/deploy", "https://audit.invalid?query=1"])
def test_invalid_service_target_makes_zero_requests(tmp_path, url):
    env = {**environment(), "CODEX_AUDIT_SERVICE_URL": url}
    opener = Opener([])
    with pytest.raises(caller.Rejected):
        caller.diagnose(tmp_path, tmp_path / "state", env=env, opener=opener)
    assert opener.calls == []


@pytest.mark.parametrize("key,value", [("GITHUB_REF", "refs/heads/other"),
                                       ("GITHUB_REPOSITORY", "other/repo"),
                                       ("GITHUB_WORKFLOW_REF", caller.WORKFLOW_REF.replace("main", "other")),
                                       ("GITHUB_EVENT_NAME", "pull_request")])
def test_wrong_workflow_is_rejected_before_requests(tmp_path, key, value):
    env = environment()
    env[key] = value
    opener = Opener([])
    with pytest.raises(caller.Rejected, match="invalid_workflow_identity"):
        caller.diagnose(tmp_path, tmp_path / "state", env=env, opener=opener)
    assert opener.calls == []


def test_stale_or_healthy_schedule_never_calls(tmp_path):
    path = snapshot(tmp_path, [], age=timedelta(hours=3))
    opener = Opener([])
    with pytest.raises(ValueError):
        run(tmp_path, opener, include_healthy=True)
    path.unlink()
    path.parent.rmdir()
    snapshot(tmp_path, [])
    assert run(tmp_path, opener) == {"status": "skipped", "reason": "no_data_errors"}
    assert opener.calls == []


def test_manual_healthy_observation_is_real_and_does_not_invent_incidents(tmp_path):
    snapshot(tmp_path, [])
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"}, job("succeeded", output="limits")])
    assert run(tmp_path, opener, include_healthy=True)["status"] == "succeeded"
    prompt = json.loads(opener.calls[1].data)["prompt"]
    assert '"incidents":[]' in prompt and '"current_state":"no_data_errors"' in prompt


def test_workflow_has_no_trading_deployment_or_static_credentials():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/quant-monitor-operational-diagnosis.yml"
    text = path.read_text()
    assert "id-token: write" in text and "quant-codex-audit" in text
    for forbidden in ("CODEX_AUDIT_SERVICE_TOKEN", "systemctl", "health_check.sh", "deploy", "trade", "pull_request"):
        assert forbidden not in text
