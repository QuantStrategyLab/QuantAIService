import importlib.util
import io
import json
import sys
import types
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


def test_checkpoint_persists_each_transition_before_further_requests(tmp_path):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     job("succeeded", output="private advice")])
    transitions = []

    def checkpoint():
        state = json.loads(next((tmp_path / "actions-state").glob("*.json")).read_text())
        transitions.append((len(opener.calls), state))

    receipt = run(tmp_path, opener, checkpoint=checkpoint)
    assert [count for count, _ in transitions] == [1, 2, 4]
    assert transitions[0][1]["job_id"] is None
    assert transitions[1][1]["job_id"] == ID and "receipt" not in transitions[1][1]
    assert transitions[2][1]["receipt"] == receipt
    assert len({state["deadline"] for _, state in transitions}) == 1
    assert len({state["request_sha256"] for _, state in transitions}) == 1
    assert "private advice" not in json.dumps(transitions)


@pytest.mark.parametrize("fail_at,requests", [(1, 1), (2, 2), (3, 4)])
def test_uncertain_checkpoint_stops_requests_and_never_reports_success(tmp_path, fail_at, requests):
    snapshot(tmp_path)
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     job("succeeded", output="advice")])
    calls = 0

    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == fail_at:
            raise OSError("synthetic uncertain persistence")

    with pytest.raises(OSError, match="synthetic uncertain persistence"):
        run(tmp_path, opener, checkpoint=checkpoint)
    assert calls == fail_at and len(opener.calls) == requests
    state = json.loads(next((tmp_path / "actions-state").glob("*.json")).read_text())
    assert state["job_id"] == (None if fail_at == 1 else ID)


def test_restored_unknown_checkpoint_cannot_submit_on_a_new_runner(tmp_path):
    first = tmp_path / "first"
    snapshot(first)
    durable = {}

    def checkpoint():
        for path in (first / "actions-state").glob("*.json"):
            durable[path.name] = path.read_bytes()

    opener = Opener([{"value": "synthetic-oidc"}, error.URLError("lost submit response")])
    with pytest.raises(error.URLError):
        run(first, opener, checkpoint=checkpoint)
    restored = tmp_path / "restored"
    snapshot(restored)
    state_dir = restored / "actions-state"
    state_dir.mkdir()
    for name, raw in durable.items():
        (state_dir / name).write_bytes(raw)
    second = Opener([])
    with pytest.raises(caller.Rejected, match="submit_outcome_unknown"):
        run(restored, second)
    assert second.calls == []


def test_restored_accepted_job_resumes_with_gets_and_persists_receipt(tmp_path):
    first = tmp_path / "first"
    snapshot(first)
    durable = {}

    def checkpoint():
        for path in (first / "actions-state").glob("*.json"):
            durable[path.name] = path.read_bytes()

    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     error.URLError("lost poll response")])
    with pytest.raises(error.URLError):
        run(first, opener, checkpoint=checkpoint)
    restored = tmp_path / "restored"
    snapshot(restored)
    state_dir = restored / "actions-state"
    state_dir.mkdir()
    for name, raw in durable.items():
        (state_dir / name).write_bytes(raw)
    resumed = Opener([{"value": "synthetic-oidc"}, job("succeeded", output="advice")])
    receipts = []
    receipt = run(restored, resumed, checkpoint=lambda: receipts.append(
        json.loads(next(state_dir.glob("*.json")).read_text())["receipt"]))
    assert receipts == [receipt] and receipt["job_id"] == ID
    assert all(req.method == "GET" for req in resumed.calls)


def test_skipped_diagnosis_does_not_create_or_persist_an_attempt(tmp_path):
    snapshot(tmp_path, [])
    checkpoint = mock.Mock(side_effect=AssertionError("no state to persist"))
    assert run(tmp_path, Opener([]), checkpoint=checkpoint) == {
        "status": "skipped", "reason": "no_data_errors"}
    checkpoint.assert_not_called()
    assert list((tmp_path / "actions-state").glob("*.json")) == []


class StorageFailure(Exception):
    def __init__(self, code):
        self.code = code


def test_storage_boundary_requires_both_forbidden_responses_without_body_reads():
    client = mock.Mock()
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)
    client.list_blobs.side_effect = StorageFailure(403)
    assert caller.verify_storage_bounds(client) == {
        "archive_metadata_denied": True, "object_listing_denied": True}
    client.bucket.assert_called_once_with(caller.CLOUD_BUCKET)
    blob = client.bucket.return_value.blob
    blob.assert_called_once_with("monitor-state/core/current.tar.gz")
    blob.return_value.download_as_bytes.assert_not_called()
    client.list_blobs.assert_called_once_with(
        caller.CLOUD_BUCKET, max_results=1, fields="kind", timeout=20, retry=None)


@pytest.mark.parametrize("code", [401, 404, 429, 500, None])
def test_storage_boundary_does_not_treat_other_failures_as_denial(code):
    client = mock.Mock()
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(code)
    with pytest.raises(caller.Rejected, match="storage_boundary_unverified"):
        caller.verify_storage_bounds(client)
    client.list_blobs.assert_not_called()


@pytest.mark.parametrize("allowed", ["archive", "listing"])
def test_storage_boundary_rejects_unexpected_access_without_reading_data(allowed):
    client = mock.Mock()
    if allowed == "listing":
        client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)
        client.list_blobs.return_value = iter(())
    with pytest.raises(caller.Rejected, match="storage_access_too_broad"):
        caller.verify_storage_bounds(client)
    client.bucket.return_value.blob.return_value.download_as_bytes.assert_not_called()


def test_storage_boundary_forces_lazy_listing_request():
    client = mock.Mock()
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)

    def forbidden_page():
        raise StorageFailure(403)
        yield

    client.list_blobs.return_value = forbidden_page()
    assert caller.verify_storage_bounds(client)["object_listing_denied"] is True


class SavedCloudState:
    """Offline shared-adapter fixture; only the caller protocol is exercised."""

    def __init__(self, files=None):
        self.files = {} if files is None else dict(files)
        self.generation = 10
        self.active = None
        self.events = []
        self.saved = []
        self.fail_checkpoint = None
        self.fail_observation = False
        self.cycle = {"domains": list(caller._read_diagnosis_cycle.__globals__["DOMAINS"]),
                      "as_of": NOW.isoformat(), "data_errors": [ERROR]}

    def observation(self, *, now):
        self.events.append("observation")
        if self.fail_observation:
            raise RuntimeError("synthetic unavailable projection")
        return self.cycle

    def claim(self, execution_id):
        self.events.append(("claim", execution_id))
        if self.active is not None:
            return None
        self.active = 20
        return self.active

    def read(self):
        self.events.append(("read", self.generation))
        return json.dumps(self.files, sort_keys=True).encode(), self.generation

    def restore(self, raw, directory):
        self.events.append("restore")
        directory.mkdir()
        for name, text in json.loads(raw).items():
            (directory / name).write_text(text)

    def checkpoint(self, directory, *, state_generation, claim_generation):
        assert state_generation == self.generation and claim_generation == self.active
        self.events.append(("checkpoint", state_generation, claim_generation))
        self.saved.append({path.name: path.read_text() for path in directory.glob("*.json")})
        if len(self.saved) == self.fail_checkpoint:
            raise RuntimeError("synthetic uncertain checkpoint")
        self.files = self.saved[-1]
        self.generation += 1
        return self.generation

    def release(self, *, claim_generation):
        assert claim_generation == self.active
        self.events.append(("release", claim_generation))
        self.active = None


def cloud_environment():
    return {**environment(), "GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "1"}


def cloud_run(store, opener, **kwargs):
    return caller.diagnose_cloud(store=store, client=mock.Mock(), env=cloud_environment(),
                                 opener=opener, now=NOW, sleep=lambda _: None, **kwargs)


def test_cloud_success_advances_each_generation_under_one_independent_claim():
    store = SavedCloudState()
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     job("succeeded", output="private advice")])
    result = cloud_run(store, opener)
    assert result["status"] == "succeeded" and store.active is None
    assert store.events == [("claim", "actions/123456/1"), ("read", 10), "restore", "observation",
                            ("checkpoint", 10, 20), ("checkpoint", 11, 20),
                            ("checkpoint", 12, 20), ("release", 20)]
    states = [json.loads(item["2026-10-22.json"]) for item in store.saved]
    assert states[0]["job_id"] is None
    assert states[1]["job_id"] == ID and "receipt" not in states[1]
    assert states[2]["receipt"] == result


@pytest.mark.parametrize("fail_at,requests", [(1, 1), (2, 2), (3, 4)])
def test_cloud_uncertain_write_retains_claim_and_stops_later_requests(fail_at, requests):
    store = SavedCloudState()
    store.fail_checkpoint = fail_at
    opener = Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                     job("succeeded", output="advice")])
    with pytest.raises(RuntimeError, match="synthetic uncertain checkpoint"):
        cloud_run(store, opener)
    assert store.active == 20 and len(opener.calls) == requests
    second = Opener([])
    with pytest.raises(caller.Rejected, match="diagnosis_state_busy"):
        cloud_run(store, second)
    assert second.calls == []


def test_cloud_lost_submission_remains_unknown_after_operator_releases_claim():
    store = SavedCloudState()
    with pytest.raises(error.URLError):
        cloud_run(store, Opener([{"value": "synthetic-oidc"}, error.URLError("lost response")]))
    assert store.active == 20 and json.loads(store.files["2026-10-22.json"])["job_id"] is None
    # Explicit operator recovery of the claim does not erase the unknown ledger.
    store.release(claim_generation=20)
    opener = Opener([])
    with pytest.raises(caller.Rejected, match="submit_outcome_unknown"):
        cloud_run(store, opener)
    assert opener.calls == [] and store.active == 20


def test_cloud_pending_job_resumes_with_get_only_without_operator_recovery():
    store = SavedCloudState()
    with pytest.raises(error.URLError):
        cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                                error.URLError("lost poll")]))
    original_deadline = json.loads(store.files["2026-10-22.json"])["deadline"]
    assert store.active is None
    resumed = Opener([{"value": "synthetic-oidc"}, job("succeeded", output="advice")])
    assert cloud_run(store, resumed)["status"] == "succeeded"
    assert all(req.method == "GET" for req in resumed.calls)
    assert json.loads(store.files["2026-10-22.json"])["deadline"] == original_deadline


@pytest.mark.parametrize("operation", ["read", "restore"])
def test_cloud_read_only_failure_releases_claim_and_next_run_can_recover(operation):
    store = SavedCloudState()
    opener = Opener([])
    with mock.patch.object(store, operation, side_effect=RuntimeError("synthetic read-only failure")):
        with pytest.raises(RuntimeError, match="synthetic read-only failure"):
            cloud_run(store, opener)
    assert store.active is None and store.files == {} and opener.calls == []
    assert store.events[-1] == ("release", 20)
    assert cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(),
                                   {"value": "synthetic-oidc"}, job("succeeded", output="advice")]))[
                                       "status"] == "succeeded"


@pytest.mark.parametrize("source", ["changed", "unavailable", "healthy"])
def test_cloud_accepted_job_recovery_ignores_new_observation(source):
    store = SavedCloudState()
    with pytest.raises(error.URLError):
        cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                                error.URLError("lost poll")]))
    original = json.loads(store.files["2026-10-22.json"])
    if source == "changed":
        store.cycle["as_of"] = (NOW + timedelta(minutes=1)).isoformat()
    elif source == "unavailable":
        store.fail_observation = True
    else:
        store.cycle["data_errors"] = []
    store.events.clear()
    opener = Opener([{"value": "synthetic-oidc"}, job("succeeded", output="advice")])
    result = cloud_run(store, opener)
    assert result["observed_at"] == original["observed_at"]
    assert result["request_sha256"] == original["request_sha256"]
    assert "observation" not in store.events and store.active is None
    assert all(req.method == "GET" for req in opener.calls)


@pytest.mark.parametrize("terminal", [True, False])
def test_cross_day_accepted_job_has_one_read_after_original_deadline_without_new_submit(terminal):
    store = SavedCloudState()
    with pytest.raises(error.URLError):
        cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                                error.URLError("lost poll")]))
    original = json.loads(store.files["2026-10-22.json"])
    opener = Opener([{"value": "synthetic-oidc"},
                     job("succeeded", output="advice") if terminal else job("running")])
    args = dict(store=store, client=mock.Mock(), env=cloud_environment(), opener=opener,
                now=NOW + timedelta(days=1), sleep=mock.Mock(side_effect=AssertionError("no extended polling")))
    if terminal:
        assert caller.diagnose_cloud(**args)["status"] == "succeeded"
    else:
        with pytest.raises(caller.Rejected, match="audit_job_timeout"):
            caller.diagnose_cloud(**args)
    assert len(opener.calls) == 2 and all(req.method == "GET" for req in opener.calls)
    assert store.active is None and set(store.files) == {"2026-10-22.json"}
    assert json.loads(store.files["2026-10-22.json"])["deadline"] == original["deadline"]


def test_pending_job_cannot_be_polled_against_another_service():
    store = SavedCloudState()
    with pytest.raises(error.URLError):
        cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(), {"value": "synthetic-oidc"},
                                error.URLError("lost poll")]))
    original = dict(store.files)
    opener = Opener([])
    with pytest.raises(caller.Rejected, match="pending_request_changed"):
        caller.diagnose_cloud(store=store, client=mock.Mock(),
                              env={**cloud_environment(), "CODEX_AUDIT_SERVICE_URL": "https://other.invalid"},
                              now=NOW, opener=opener)
    assert store.files == original and store.active is None and opener.calls == []


def test_cloud_daily_receipt_skips_without_reading_unavailable_observation():
    store = SavedCloudState()
    assert cloud_run(store, Opener([{"value": "synthetic-oidc"}, job(),
                                   {"value": "synthetic-oidc"}, job("succeeded", output="advice")]))[
                                       "status"] == "succeeded"
    store.fail_observation = True
    store.events.clear()
    opener = Opener([])
    assert cloud_run(store, opener) == {"status": "skipped", "reason": "daily_attempt_limit"}
    assert "observation" not in store.events and not opener.calls and store.active is None


def test_cloud_source_unavailable_releases_state_without_requesting_audit():
    store = SavedCloudState()
    store.fail_observation = True
    opener = Opener([])
    with pytest.raises(RuntimeError, match="synthetic unavailable projection"):
        cloud_run(store, opener)
    assert store.events == [("claim", "actions/123456/1"), ("read", 10), "restore", "observation",
                            ("release", 20)]
    assert store.active is None and opener.calls == []


def test_cloud_preserves_cross_day_unknown_records_without_submission():
    original = '{ "job_id": null, "private_extension": {"unknown": true} }\n'
    store = SavedCloudState({"2026-10-21.json": original})
    opener = Opener([])
    with pytest.raises(caller.Rejected, match="previous_outcome_unresolved"):
        cloud_run(store, opener)
    assert store.files == {"2026-10-21.json": original} and opener.calls == []


def test_cloud_healthy_schedule_releases_claim_without_writing_an_attempt():
    store = SavedCloudState()
    store.cycle["data_errors"] = []
    opener = Opener([])
    assert cloud_run(store, opener) == {"status": "skipped", "reason": "no_data_errors"}
    assert store.saved == [] and store.active is None and opener.calls == []


@pytest.mark.parametrize("key,value", [("GITHUB_RUN_ID", "../bad"),
                                       ("GITHUB_RUN_ATTEMPT", ""),
                                       ("GITHUB_WORKFLOW_REF", "other/workflow")])
def test_cloud_bad_execution_identity_has_zero_cloud_or_audit_io(key, value):
    store, client = SavedCloudState(), mock.Mock()
    env = {**cloud_environment(), key: value}
    with pytest.raises(caller.Rejected):
        caller.diagnose_cloud(store=store, client=client, env=env)
    assert store.events == [] and client.mock_calls == []


def test_cloud_verification_refreshes_adc_and_cas_writes_unchanged_ledger_without_audit():
    original = '{ "job_id": null, "extra": 1 }\n'
    store = SavedCloudState({"2026-10-21.json": original})
    client = mock.Mock()
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)
    client.list_blobs.side_effect = StorageFailure(403)
    refresh = mock.Mock(side_effect=lambda: store.events.append("refresh"))
    opener = Opener([])
    receipt = caller.diagnose_cloud(store=store, client=client, refresh=refresh, verify_state_only=True,
                                    env=cloud_environment(), now=NOW, opener=opener)
    assert receipt == {"status": "verified", "mode": "cloud_state_only", "audit_requests": 0,
                       "archive_metadata_denied": True, "object_listing_denied": True,
                       "adc_refresh_verified": True, "checkpoint_unchanged": True,
                       "unresolved_attempts": 1, "legacy_pending_attempts": 1,
                       "native_pending_attempts": 0, "unknown_submission_attempts": 1}
    assert store.events[-4:] == ["refresh", ("checkpoint", 10, 20), ("read", 11), ("release", 20)]
    assert store.files == {"2026-10-21.json": original} and opener.calls == []
    refresh.assert_called_once_with()


def test_cloud_verification_uncertain_checkpoint_retains_claim_without_audit():
    store, client = SavedCloudState(), mock.Mock()
    store.fail_checkpoint = 1
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)
    client.list_blobs.side_effect = StorageFailure(403)
    opener = Opener([])
    with pytest.raises(RuntimeError, match="synthetic uncertain checkpoint"):
        caller.diagnose_cloud(store=store, client=client, refresh=mock.Mock(), verify_state_only=True,
                              env=cloud_environment(), now=NOW, opener=opener)
    assert store.active == 20 and opener.calls == []


def test_cloud_cli_sanitizes_shared_state_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(caller, "diagnose_cloud", mock.Mock(side_effect=RuntimeError("private-object-body")))
    path = tmp_path / "receipt.json"
    assert caller.main(["--cloud", "--verify-state-only", "--receipt", str(path)]) == 1
    assert json.loads(path.read_text()) == {"status": "unavailable", "reason": "diagnosis_unavailable"}
    assert "private-object-body" not in capsys.readouterr().out


@pytest.mark.parametrize("reason", ["diagnosis_observation_unavailable", "diagnosis_observation_stale",
                                    "state_read_failed", "checkpoint_outcome_unknown", "private-object-body"])
def test_cloud_cli_preserves_fixed_state_classifications_only(tmp_path, monkeypatch, reason):
    class StateUnavailable(RuntimeError):
        pass

    module = types.SimpleNamespace(StateUnavailable=StateUnavailable)
    monkeypatch.setitem(sys.modules, "monitor_cloud_state", module)
    monkeypatch.setattr(caller, "diagnose_cloud", mock.Mock(side_effect=StateUnavailable(reason)))
    path = tmp_path / "receipt.json"
    assert caller.main(["--cloud", "--receipt", str(path)]) == 1
    expected = reason if reason in caller.CLOUD_FAILURE_REASONS else "diagnosis_unavailable"
    assert json.loads(path.read_text()) == {"status": "unavailable", "reason": expected}


def test_state_verification_requires_manual_dispatch_before_cloud_io():
    store, client = SavedCloudState(), mock.Mock()
    env = {**cloud_environment(), "GITHUB_EVENT_NAME": "schedule"}
    with pytest.raises(caller.Rejected, match="state_verification_requires_manual_dispatch"):
        caller.diagnose_cloud(store=store, client=client, env=env, verify_state_only=True)
    assert store.events == [] and client.mock_calls == []


def test_cloud_caller_uses_real_shared_json_contract_and_preserves_original_bytes(tmp_path):
    shared = importlib.import_module("monitor_diagnosis_state")
    original = tmp_path / "original"
    original.mkdir()
    (original / "2026-10-21.json").write_bytes(b'{ "job_id": null, "extra": {"unknown":true} }\n')
    raw = shared.encode_checkpoints(original)
    state = shared.DiagnosisState(
        mock.Mock(), checkpoint_uri=f"gs://{caller.CLOUD_BUCKET}/{caller.CLOUD_LEDGER_PREFIX}/checkpoints.json",
        observation_uri=f"gs://{caller.CLOUD_BUCKET}/{caller.CLOUD_OBSERVATION}")
    backend = mock.Mock()
    backend.claim.return_value = 20
    backend.read.side_effect = [(raw, 10), (raw, 11)]
    backend.checkpoint.return_value = 11
    state.store = backend
    state.observations = mock.Mock()
    cycle = SavedCloudState().cycle
    observation = {"schema_version": shared.OBSERVATION_SCHEMA, "status": "available", "cycle": cycle}
    state.observations.read.return_value = (json.dumps(observation).encode(), 5)
    client = mock.Mock()
    client.bucket.return_value.blob.return_value.reload.side_effect = StorageFailure(403)
    client.list_blobs.side_effect = StorageFailure(403)
    opener = Opener([])
    result = caller.diagnose_cloud(store=state, client=client, refresh=mock.Mock(), verify_state_only=True,
                                  env=cloud_environment(), now=NOW, opener=opener)
    assert result["status"] == "verified" and opener.calls == []
    backend.claim.assert_called_once_with({
        "schema_version": "quant.monitor-diagnosis-claim.v1", "execution_id": "actions/123456/1"})
    backend.checkpoint.assert_called_once_with(raw, state_generation=10, claim_generation=20)
    backend.release.assert_called_once_with(claim_generation=20)


@pytest.mark.parametrize("failure", ["unavailable", "stale", "old_time", "invalid_classification"])
def test_cloud_real_projection_rejects_bad_source_and_releases_claim_without_audit(failure):
    shared = importlib.import_module("monitor_diagnosis_state")
    state = shared.DiagnosisState(
        mock.Mock(), checkpoint_uri=f"gs://{caller.CLOUD_BUCKET}/{caller.CLOUD_LEDGER_PREFIX}/checkpoints.json",
        observation_uri=f"gs://{caller.CLOUD_BUCKET}/{caller.CLOUD_OBSERVATION}")
    state.store = mock.Mock()
    state.store.claim.return_value = 20
    state.store.read.return_value = (json.dumps({"schema_version": shared.CHECKPOINT_SCHEMA, "files": {}}).encode(), 10)
    state.observations = mock.Mock()
    cycle = SavedCloudState().cycle
    observation = {"schema_version": shared.OBSERVATION_SCHEMA, "status": "available", "cycle": cycle}
    if failure in {"unavailable", "stale"}:
        observation.update(status=failure, cycle=None)
    elif failure == "old_time":
        cycle["as_of"] = (NOW - timedelta(hours=3)).isoformat()
    else:
        cycle["data_errors"] = [{**ERROR, "code": "invented"}]
    state.observations.read.return_value = (json.dumps(observation).encode(), 5)
    opener = Opener([])
    with pytest.raises(shared.StateUnavailable):
        caller.diagnose_cloud(store=state, client=mock.Mock(), env=cloud_environment(), now=NOW, opener=opener)
    state.store.release.assert_called_once_with(claim_generation=20)
    state.store.checkpoint.assert_not_called()
    assert opener.calls == []


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


NATIVE_ID = '42509a19-b704-4a4b-a66c-f163a15699f1'


class NativeOpener:
    def __init__(self):
        self.calls = []
        self.payload = None
        self.lost_submit = False
        self.status = 'completed'
        self.owner = caller.REPOSITORY

    def open(self, req, timeout):
        self.calls.append(req)
        if 'actions.githubusercontent.com' in req.full_url:
            return io.BytesIO(b'{"value":"synthetic-native-oidc"}')
        if req.method == 'POST':
            self.payload = json.loads(req.data)
            if self.lost_submit:
                raise TimeoutError('synthetic lost acknowledgement')
            status = 'queued'
        else:
            status = self.status
        value = {'id': NATIVE_ID, 'owner': self.owner, 'provider': 'dot', 'request': self.payload,
                 'status': status, 'result_kind': 'advisory', 'output': {'diagnosis': 'synthetic advisory'}}
        return io.BytesIO(json.dumps(value).encode())


def native_environment():
    return {**environment(), 'AI_SERVICE_DIAGNOSIS_READY': 'true',
            'AI_SERVICE_URL': 'https://native.invalid', 'AI_SERVICE_AUDIENCE': 'https://native.invalid/mcp',
            'AI_SERVICE_DIAGNOSIS_MODEL': 'configured-dot'}


def native_run(root, opener, **kwargs):
    return caller.diagnose(root, root / 'actions-state', env=native_environment(), opener=opener,
                           now=NOW, monotonic=lambda: 0, sleep=lambda _: None, **kwargs)


def test_native_diagnosis_uses_common_task_contract_and_advisory_receipt(tmp_path):
    snapshot(tmp_path)
    opener = NativeOpener()
    receipt = native_run(tmp_path, opener)
    assert receipt['protocol'] == 'native_tasks_v1'
    assert receipt['task_id'] == NATIVE_ID and receipt['advisory_only']
    assert receipt['model_verification'] == 'unavailable' and not receipt['execution_authority_granted']
    assert [r.method for r in opener.calls] == ['GET', 'POST', 'GET', 'GET']
    assert parse.parse_qs(parse.urlsplit(opener.calls[0].full_url).query)['audience'] == ['https://native.invalid/mcp']
    assert opener.calls[1].get_header('Idempotency-key').startswith('monitor-diagnosis:')
    assert opener.payload['mode'] == 'agent' and opener.payload['model'] == 'configured-dot'
    assert 'Do not place orders' in opener.payload['objective']
    assert native_run(tmp_path, NativeOpener()) == {'status': 'skipped', 'reason': 'daily_attempt_limit'}


def test_native_lost_acknowledgement_does_not_submit_again(tmp_path):
    snapshot(tmp_path)
    opener = NativeOpener()
    opener.lost_submit = True
    with pytest.raises(TimeoutError):
        native_run(tmp_path, opener)
    retry = NativeOpener()
    with pytest.raises(caller.Rejected, match='submit_outcome_unknown'):
        native_run(tmp_path, retry)
    assert not retry.calls


def test_native_worker_cannot_return_another_project_receipt(tmp_path):
    snapshot(tmp_path)
    opener = NativeOpener()
    opener.owner = 'OtherOwner/Private'
    with pytest.raises(caller.Rejected, match='invalid_native_receipt'):
        native_run(tmp_path, opener)
    assert len(opener.calls) == 2


def test_native_pending_expired_budget_reads_only_original_task_once(tmp_path):
    snapshot(tmp_path)
    opener = NativeOpener()
    native_run(tmp_path, opener)
    path = tmp_path / 'actions-state' / (NOW.date().isoformat() + '.json')
    state = json.loads(path.read_text())
    state.pop('receipt')
    state['deadline'] = NOW.timestamp() - 1
    path.write_text(json.dumps(state))
    opener.calls = []
    opener.status = 'running'
    with pytest.raises(caller.Rejected, match='native_task_timeout'):
        native_run(tmp_path, opener)
    assert [r.method for r in opener.calls] == ['GET', 'GET']
    assert opener.calls[1].full_url.endswith('/v1/tasks/' + NATIVE_ID)


def test_enabling_native_route_preserves_existing_legacy_job_recovery(tmp_path):
    state_dir = tmp_path / 'actions-state'
    state_dir.mkdir()
    state = {'job_id': ID, 'request_sha256': 'a' * 64, 'service_sha256': caller.digest('https://audit.invalid'),
             'route': {k: job()[k] for k in ('provider', 'research_stage', 'model', 'reasoning_effort')},
             'deadline': NOW.timestamp() - 1, 'observed_at': NOW.isoformat()}
    (state_dir / (NOW.date().isoformat() + '.json')).write_text(json.dumps(state))
    opener = Opener([{'value': 'synthetic-oidc'}, job('succeeded', output='legacy advice')])
    result = native_run(tmp_path, opener)
    assert result['job_id'] == ID
    assert [r.method for r in opener.calls] == ['GET', 'GET']
    assert opener.calls[1].full_url == 'https://audit.invalid' + caller.JOBS + '/' + ID
