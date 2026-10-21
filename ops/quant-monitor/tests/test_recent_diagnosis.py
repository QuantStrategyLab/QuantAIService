import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


spec = importlib.util.spec_from_file_location(
    "recent_health_cycle", Path(__file__).resolve().parents[1] / "scripts/health_cycle.py"
)
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)
NOW = datetime(2026, 9, 11, 4, 40, tzinfo=timezone.utc)
ERROR = {"domain": "crypto", "code": "drift_data_unavailable", "error_type": "ValueError"}


def save(root, stamp, errors):
    directory = root / "data/health"
    directory.mkdir(parents=True, exist_ok=True)
    date = datetime.fromisoformat(stamp)
    path = directory / f"cycle_{date.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"as_of": stamp, "domains": list(health.DOMAINS), "data_errors": errors}))
    return path


def test_recovered_error_is_diagnosed_with_source_time(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-11T04:00:00+00:00", [{**ERROR, "private_detail": "secret-must-not-leak"}])
    save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    run = Mock(return_value={"status": "succeeded"})
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    result = health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert result["status"] == "succeeded"
    assert run.call_count == 1
    assert run.call_args.args[1] == [ERROR]
    observation = run.call_args.kwargs["observation"]
    assert observation == {
        "observed_at": "2026-09-11T04:00:00+00:00",
        "latest_cycle_at": "2026-09-11T04:30:00+00:00",
        "current_state": "not_observed_in_latest",
    }
    assert run.call_args.kwargs["attempt_date"] == "2026-09-11"
    prompt = health._operational_diagnosis_prompt([ERROR], observation=observation)
    assert "not_observed_in_latest" in prompt and "secret-must-not-leak" not in prompt


@pytest.mark.parametrize("latest_errors", [[ERROR], []])
def test_existing_attempt_is_skipped_without_losing_other_recent_error(tmp_path, monkeypatch, latest_errors):
    other = {**ERROR, "domain": "us_equity"}
    save(tmp_path, "2026-09-11T03:00:00+00:00", [other])
    save(tmp_path, "2026-09-11T04:00:00+00:00", [ERROR])
    save(tmp_path, "2026-09-11T04:30:00+00:00", latest_errors)
    health._record_operational_diagnosis_attempt(tmp_path / "data/diagnosis-consumer", health._operational_diagnosis_fingerprint([ERROR]))
    run = Mock(return_value={"status": "succeeded"})
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert run.call_count == 1 and run.call_args.args[1] == [other]


def test_current_cycle_still_must_be_fresh_even_with_old_error(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-11T00:00:00+00:00", [ERROR])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


def test_expired_error_and_invalid_current_cycle_never_trigger(tmp_path, monkeypatch):
    save(tmp_path, "2026-09-10T04:00:00+00:00", [ERROR])
    path = save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "skipped"
    path.write_text("{}")
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


@pytest.mark.parametrize("kind", ["future", "symlink", "corrupt_recent", "corrupt_budget", "oversized", "too_many"])
def test_invalid_sources_or_budget_do_not_submit(tmp_path, monkeypatch, kind):
    old = save(tmp_path, "2026-09-11T04:00:00+00:00", [ERROR])
    latest = save(tmp_path, "2026-09-11T04:30:00+00:00", [])
    if kind == "future":
        save(tmp_path, "2026-09-11T05:00:00+00:00", [])
    elif kind == "symlink":
        latest.unlink()
        latest.symlink_to(old)
    elif kind == "corrupt_recent":
        old.write_text("[]")
    elif kind == "corrupt_budget":
        state_root = tmp_path / "data/diagnosis-consumer"
        health._write_alert_state(state_root, {"operational_diagnosis_last_attempt_date": "invalid"})
    elif kind == "oversized":
        old.write_text(" " * (1024 * 1024 + 1))
    else:
        from datetime import timedelta
        for index in range(513):
            save(tmp_path, (NOW - timedelta(seconds=index + 1)).isoformat(), [])
    run = Mock()
    monkeypatch.setattr(health, "_run_operational_diagnosis", run)
    assert health.diagnose_recent_cycles(tmp_path, now=NOW)["status"] == "rejected"
    run.assert_not_called()


def test_no_gateway_configuration_does_not_spend_daily_attempt(tmp_path, monkeypatch):
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_URL", raising=False)
    monkeypatch.delenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", raising=False)
    root = tmp_path / "data/diagnosis-consumer"
    result = health._run_operational_diagnosis(
        root,
        [ERROR],
        health._operational_diagnosis_fingerprint([ERROR]),
        attempt_date="2026-09-11",
    )
    assert result == {"status": "deferred", "reason": "ai_task_service_not_configured"}
    assert health._load_operational_diagnosis_state(root) == {}


def test_actual_cli_uses_recent_consumer_without_model_or_recollection(
    tmp_path, monkeypatch, capsys
):
    import runpy
    import sys
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    save(tmp_path, (now - timedelta(minutes=40)).isoformat(), [ERROR])
    save(tmp_path, (now - timedelta(minutes=10)).isoformat(), [])
    monkeypatch.setenv("QUANT_MONITOR_ROOT", str(tmp_path))
    monkeypatch.delenv("QUANT_MONITOR_DIAGNOSIS_BINDING", raising=False)
    monkeypatch.setattr(sys, "argv", [str(spec.origin), "--diagnose-recent"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_path(spec.origin, run_name="__main__")
    assert exit_info.value.code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reason"] == "ai_task_service_not_configured"
    assert result["observation"]["current_state"] == "not_observed_in_latest"
    assert (
        health._load_operational_diagnosis_state(tmp_path / "data/diagnosis-consumer")
        == {}
    )


@pytest.mark.parametrize("outcome", ["succeeded", "deferred", "unknown"])
def test_daily_attempt_limit_survives_new_error_and_process_state_reload(
    tmp_path, monkeypatch, outcome
):
    f = NativeFixture(tmp_path, monkeypatch)
    if outcome == "unknown":
        f.lose_submit = True
    result = f.run()
    if outcome == "succeeded":
        f.finish(result["job_id"])
        assert f.run()["status"] == "succeeded"
    assert f.run([{**ERROR, "domain": "us_equity"}]) == {
        "status": "skipped",
        "reason": "daily_attempt_limit",
    }
    assert f.posts == 1 and "private" not in json.dumps(result)


def test_next_day_can_diagnose_different_unattempted_error(tmp_path, monkeypatch):
    f = NativeFixture(tmp_path, monkeypatch)
    health._record_operational_diagnosis_attempt(
        f.root,
        health._operational_diagnosis_fingerprint([ERROR]),
        attempt_date="2026-09-10",
    )
    other = [{**ERROR, "domain": "us_equity"}]
    first = f.run(other)
    f.finish(first["job_id"])
    assert f.run(other)["status"] == "succeeded" and f.posts == 1


@pytest.mark.parametrize(
    "call,stage,error,category",
    [
        (
            "_load_operational_diagnosis_state",
            "diagnosis_state_processing",
            OSError,
            "io_error",
        ),
        (
            "_read_diagnosis_cycle",
            "latest_cycle_input_processing",
            ValueError,
            "value_error",
        ),
        ("_read_diagnosis_cycle", "latest_cycle_input_processing", OSError, "io_error"),
        (
            "_run_operational_diagnosis",
            "diagnosis_processing",
            AttributeError,
            "attribute_error",
        ),
        ("_run_operational_diagnosis", "diagnosis_processing", TypeError, "type_error"),
    ],
)
def test_recent_diagnosis_identifies_caught_stage_without_details(
    tmp_path, monkeypatch, capsys, call, stage, error, category
):
    save(tmp_path, "2026-09-11T04:30:00+00:00", [ERROR])
    failed = Mock(side_effect=error("private-marker /private/account token"))
    monkeypatch.setattr(health, call, failed)
    result = health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert result == {
        "status": "rejected",
        "reason": "recent_cycles_or_state_unavailable",
        "failure_stage": stage,
        "failure_category": category,
    }
    assert failed.call_count == 1
    output = capsys.readouterr()
    assert "private" not in json.dumps(result) + output.out + output.err


def test_recent_cycle_validation_is_distinct_from_latest_and_unknown_types_stay_generic(
    tmp_path, monkeypatch
):
    class PrivateAccountError(ValueError):
        pass

    save(tmp_path, "2026-09-11T04:30:00+00:00", [ERROR])
    read = Mock(side_effect=[(NOW, [ERROR]), PrivateAccountError("private-marker")])
    monkeypatch.setattr(health, "_read_diagnosis_cycle", read)
    result = health.diagnose_recent_cycles(tmp_path, now=NOW)
    assert result["failure_stage"] == "recent_cycle_input_processing"
    assert result["failure_category"] == "unknown_error"
    assert "private" not in json.dumps(
        result
    ) and "PrivateAccountError" not in json.dumps(result)
    monkeypatch.setattr(
        health,
        "_read_diagnosis_cycle",
        Mock(side_effect=RuntimeError("private-marker")),
    )
    with pytest.raises(RuntimeError):
        health.diagnose_recent_cycles(tmp_path, now=NOW)


@pytest.mark.parametrize(
    "call,stage",
    [
        ("_load_operational_diagnosis_state", "diagnosis_state_processing"),
        ("_save_native_diagnosis", "diagnosis_attempt_persistence"),
    ],
)
def test_diagnosis_state_failures_preserve_admission_and_no_retries(
    tmp_path, monkeypatch, call, stage
):
    f = NativeFixture(tmp_path, monkeypatch)
    monkeypatch.setattr(health, call, Mock(side_effect=OSError("private-marker")))
    result = f.run()
    assert result == {
        "status": "deferred",
        "reason": "dedupe_state_unavailable",
        "failure_stage": stage,
        "failure_category": "io_error",
    }
    assert f.calls == []


@pytest.mark.parametrize("outcome", ["unknown", "unavailable"])
def test_diagnosis_failure_projection_keeps_original_attempt_and_delivery_identity(
    tmp_path, monkeypatch, outcome
):
    f = NativeFixture(tmp_path, monkeypatch)
    deliveries = {"c" * 64: {"b" * 64: {"status": "unknown", "attempts": 1}}}
    health._write_alert_state(f.root, {"deliveries": deliveries})
    if outcome == "unknown":
        f.lose_submit = True
    else:
        f.mutate = lambda record: {**record, "owner": "other/project"}
    result = f.run()
    assert result["status"] == "unavailable" and "private" not in json.dumps(result)
    state = health._load_operational_diagnosis_state(f.root)
    assert state["deliveries"] == deliveries
    assert state["operational_diagnosis_last_attempt_date"] == "2026-09-11"
    assert state["operational_diagnosis_attempts"] == [
        health._operational_diagnosis_fingerprint([ERROR])
    ]
    assert "failure_stage" not in json.dumps(
        state
    ) and "failure_category" not in json.dumps(state)
    f.run(attempt_date="2026-10-01")
    assert f.posts == 1


# One selected native handler; the SDK and task runtime are real, transports are local.
class NativeFixture:
    def __init__(self, root, monkeypatch, mode="agent", provider="dot"):
        import copy
        from ai_service.client import TaskClient
        from ai_service.runtime import Policy, TaskRuntime, ProviderReply

        self.time = 1000.0
        monkeypatch.setattr("ai_service.runtime.time.time", lambda: self.time)
        fixture = self

        class Provider:
            callback_driven = mode == "agent"

            def submit(self, request, **kwargs):
                self.request = request
                return {"task_id": kwargs["task_id"]}

            def cancel(self, handle):
                pass

            def poll(self, handle):
                return ProviderReply("completed", fixture.output())

        fake = Provider()
        fake.mode = mode
        self.runtime = TaskRuntime(
            root / "runtime.sqlite",
            {provider: fake},
            {
                "QuantStrategyLab/QuantAIService": Policy(
                    providers={mode + ":diagnosis": provider},
                    models={provider: {"synthetic-model"}},
                )
            },
        )
        self.calls = []
        self.mutate = lambda record: record
        self.lose_submit = False

        class Transport:
            def request(_, url, *, method="GET", headers=None, body=b""):
                path = url.split("https://quant.invalid")[1]
                fixture.calls.append((method, path))
                if method == "POST" and path == "/v1/tasks":
                    record = fixture.runtime.submit(
                        "QuantStrategyLab/QuantAIService",
                        headers["Idempotency-Key"],
                        json.loads(body),
                    )
                    if fixture.lose_submit:
                        raise TimeoutError("private-marker")
                elif method == "GET":
                    record = fixture.runtime.get(
                        "QuantStrategyLab/QuantAIService", path.rsplit("/", 1)[1]
                    )
                elif method == "POST" and path.endswith("/cancel"):
                    record = fixture.runtime.cancel(
                        "QuantStrategyLab/QuantAIService", path.split("/")[-2]
                    )
                else:
                    raise AssertionError("unexpected transport operation")
                return SimpleNamespace(
                    status=200,
                    body=json.dumps(fixture.mutate(copy.deepcopy(record))).encode(),
                )

        self.client = TaskClient(
            "https://quant.invalid", "synthetic-token", transport=Transport()
        )
        self.binding = dict(
            schema_version="quant.monitor-diagnosis-native.v1",
            project="QuantStrategyLab/QuantAIService",
            resource=self.client.resource,
            audience="quant-test",
            mode=mode,
            provider=provider,
            profile="diagnosis",
            model="synthetic-model",
            worker="synthetic-worker" if mode == "agent" else None,
        )
        self.root = root / "consumer"
        self.kwargs = dict(
            config_loader=lambda: self.binding,
            client_factory=lambda config: self.client,
            clock=lambda: self.time,
            attempt_date="2026-09-11",
        )

    def run(self, errors=None, **kwargs):
        errors = [ERROR] if errors is None else errors
        return health._run_operational_diagnosis(
            self.root,
            errors,
            health._operational_diagnosis_fingerprint(errors),
            **{**self.kwargs, **kwargs},
        )

    def output(self):
        return dict(
            schema_version="quant.monitor-diagnosis.v1",
            likely_cause="Synthetic missing artifact",
            evidence_to_inspect=["Synthetic receipt"],
            safe_next_checks=["Read-only verification"],
            advisory_only=True,
            execution_authority_granted=False,
        )

    def finish(self, task_id, output=None):
        self.runtime.tick()
        if self.binding["mode"] == "agent":
            self.runtime.claim(
                self.binding["project"],
                task_id,
                self.binding["provider"],
                self.binding["worker"],
                "attempt-1",
            )
            self.runtime.report(
                self.binding["project"],
                task_id,
                self.binding["provider"],
                self.binding["worker"],
                "attempt-1",
                1,
                "completed",
                output or self.output(),
            )
        else:
            self.runtime.tick()

    @property
    def posts(self):
        return sum(item == ("POST", "/v1/tasks") for item in self.calls)


@pytest.mark.parametrize(
    "mode,provider", [("agent", "dot"), ("agent", "grok"), ("api", "api")]
)
def test_native_single_handler_create_readback_consume_and_daily_limit(
    tmp_path, monkeypatch, mode, provider
):
    f = NativeFixture(tmp_path, monkeypatch, mode, provider)
    first = f.run()
    assert first["reason"] == "ai_task_pending"
    task_id = first["job_id"]
    f.finish(task_id)
    assert f.run() == {"status": "succeeded", "job_id": task_id}
    assert f.run() == {"status": "skipped", "reason": "daily_attempt_limit"}
    assert f.run([{**ERROR, "domain": "us_equity"}])["reason"] == "daily_attempt_limit"
    assert f.posts == 1
    assert health._alert_state_path(f.root).stat().st_mode & 0o777 == 0o600
    record = f.runtime.get(f.binding["project"], task_id)
    assert record["request"]["mode"] == mode and record["result_kind"] == "advisory"
    assert "Do not place orders" in record["request"]["objective"]


def test_native_unknown_across_day_restart_never_reposts(tmp_path, monkeypatch):
    f = NativeFixture(tmp_path, monkeypatch)
    f.lose_submit = True
    first = f.run()
    assert first["reason"] == "ai_outcome_unknown"
    assert f.run(attempt_date="2026-10-01")["reason"] == "ai_outcome_unknown"
    assert f.posts == 1 and "private" not in json.dumps(first)
    assert health._load_operational_diagnosis_state(f.root)[
        "operational_diagnosis_attempts"
    ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("owner", "Pigbibi/PersonalAIService"),
        ("provider", "other"),
        ("revision", 2),
        ("request", {}),
        ("worker", "wrong-worker"),
        ("attempt", "wrong-attempt"),
        ("id", "wrong-id"),
    ],
)
def test_native_original_identity_and_material_are_fixed(
    tmp_path, monkeypatch, field, value
):
    f = NativeFixture(tmp_path, monkeypatch)
    first = f.run()
    f.runtime.tick()
    f.runtime.claim(
        f.binding["project"], first["job_id"], "dot", "synthetic-worker", "attempt-1"
    )
    assert f.run()["reason"] == "ai_task_pending"  # freeze original attempt
    f.finish(first["job_id"])
    f.mutate = lambda record: {**record, field: value}
    assert f.run()["status"] == "unavailable"
    assert f.run(attempt_date="2026-10-01")["status"] == "unavailable"
    assert f.posts == 1


@pytest.mark.parametrize("kind", ["schema", "cancel", "deadline", "cancel_unknown"])
def test_native_invalid_output_and_cancel_deadline_never_adopt_or_replay(
    tmp_path, monkeypatch, kind
):
    f = NativeFixture(tmp_path, monkeypatch)
    first = f.run()
    if kind == "schema":
        f.finish(first["job_id"], {"wrong": "shape"})
        assert f.run()["status"] == "unavailable"
    else:
        f.runtime.tick()
        f.runtime.claim(
            f.binding["project"],
            first["job_id"],
            "dot",
            "synthetic-worker",
            "attempt-1",
        )
        if kind == "deadline":
            f.time += 601
        if kind == "cancel_unknown":
            original = f.client.cancel

            def lost(task_id):
                original(task_id)
                raise TimeoutError("private-marker")

            f.client.cancel = lost
        result = f.run(cancel=kind != "deadline")
        assert result["status"] == "unavailable" and "cancelled" not in json.dumps(
            result
        )
        f.run(cancel=True)
        assert sum(path.endswith("/cancel") for method, path in f.calls) == 1
    assert f.posts == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("project", "Pigbibi/PersonalAIService"),
        ("provider", ""),
        ("mode", "codex"),
        ("worker", None),
        ("resource", "https://wrong.invalid"),
    ],
)
def test_native_invalid_binding_does_not_spend_or_call(
    tmp_path, monkeypatch, field, value
):
    f = NativeFixture(tmp_path, monkeypatch)
    f.binding[field] = value
    assert f.run()["reason"] == "ai_task_service_not_configured"
    assert f.calls == [] and health._load_operational_diagnosis_state(f.root) == {}


def test_native_binding_drift_and_intent_persistence_failure_stop_before_submit(
    tmp_path, monkeypatch
):
    f = NativeFixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        health, "_write_alert_state", Mock(side_effect=OSError("private-marker"))
    )
    assert f.run()["reason"] == "dedupe_state_unavailable" and f.calls == []
    monkeypatch.undo()
    f = NativeFixture(tmp_path / "second", monkeypatch)
    f.run()
    f.binding["audience"] = "different"
    assert f.run()["status"] == "unavailable"
    assert f.posts == 1 and len(f.calls) == 1


def test_native_recent_entry_resumes_pending_before_daily_gate_without_new_collection(
    tmp_path, monkeypatch
):
    f = NativeFixture(tmp_path, monkeypatch)
    f.root = tmp_path / "data/diagnosis-consumer"
    first = f.run()
    f.finish(first["job_id"])
    original = health._run_operational_diagnosis
    monkeypatch.setattr(
        health,
        "_run_operational_diagnosis",
        lambda *args, **kwargs: original(
            *args,
            **kwargs,
            config_loader=lambda: f.binding,
            client_factory=lambda _: f.client,
            clock=lambda: f.time,
        ),
    )
    assert health.diagnose_recent_cycles(tmp_path, now=NOW) == {
        "status": "succeeded",
        "job_id": first["job_id"],
    }
    assert f.posts == 1


def test_native_concurrent_invocations_create_only_one_original_task(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    f = NativeFixture(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: f.run(), range(2)))
    assert {item["job_id"] for item in results} == {results[0]["job_id"]}
    assert f.posts == 1


@pytest.mark.parametrize(
    "corrupt", ["partial", "digest", "request", "deadline", "legacy_pending"]
)
def test_native_corrupt_or_legacy_state_never_creates_replacement(
    tmp_path, monkeypatch, corrupt
):
    f = NativeFixture(tmp_path, monkeypatch)
    f.run()
    state = health._load_operational_diagnosis_state(f.root)
    fp = health._operational_diagnosis_fingerprint([ERROR])
    record = state["operational_diagnosis_native"][fp]
    if corrupt == "partial":
        del record["claim"]
    elif corrupt == "digest":
        record["request_sha256"] = "0" * 64
    elif corrupt == "request":
        record["request"]["objective"] = "altered"
        record["request_sha256"] = health._diagnosis_digest(record["request"])
    elif corrupt == "deadline":
        record["deadline"] = "unknown"
    else:
        state = {"operational_diagnosis_pending": {fp: "old-id"}}
    health._write_alert_state(f.root, state)
    before = list(f.calls)
    assert f.run()["status"] == "unavailable"
    assert f.calls == before and f.posts == 1


def test_native_explicit_file_binding_is_private_and_no_oidc_is_no_attempt(
    tmp_path, monkeypatch
):
    f = NativeFixture(tmp_path, monkeypatch)
    path = tmp_path / "binding.json"
    path.write_text(json.dumps(f.binding))
    path.chmod(0o600)
    monkeypatch.setenv("QUANT_MONITOR_DIAGNOSIS_BINDING", str(path))
    result = f.run(config_loader=None)
    assert result["reason"] == "ai_task_pending" and f.posts == 1
    path.chmod(0o644)
    assert f.run(config_loader=None)["reason"] == "ai_task_service_not_configured"
    path.chmod(0o600)
    fresh = tmp_path / "new-consumer"
    result = health._run_operational_diagnosis(
        fresh, [ERROR], health._operational_diagnosis_fingerprint([ERROR])
    )
    assert result["reason"] == "ai_task_service_not_configured"
    assert health._load_operational_diagnosis_state(fresh) == {}


def test_native_expiry_during_final_checkpoint_cannot_be_consumed(tmp_path, monkeypatch):
    f = NativeFixture(tmp_path, monkeypatch)
    first = f.run()
    f.finish(first['job_id'])
    original = health._save_native_diagnosis
    def expiry_during_checkpoint(root, state):
        original(root, state)
        if any(record['phase'] == 'completed' for record in state['operational_diagnosis_native'].values()):
            f.time += 601
    monkeypatch.setattr(health, '_save_native_diagnosis', expiry_during_checkpoint)
    result = f.run()
    assert result == {'status': 'unavailable', 'reason': 'ai_task_stop_requested'}
    assert f.posts == 1
    assert sum(path.endswith('/cancel') for method, path in f.calls) == 1
    assert f.run()['status'] == 'unavailable'


def test_native_unknown_survives_alert_clear_and_late_completed_after_cancel_is_hold(tmp_path, monkeypatch):
    f = NativeFixture(tmp_path, monkeypatch)
    first = f.run()
    f.runtime.tick()
    f.runtime.claim(f.binding['project'], first['job_id'], 'dot', 'synthetic-worker', 'attempt-1')
    assert f.run(cancel=True)['reason'] == 'ai_task_stop_requested'
    f.runtime.report(f.binding['project'], first['job_id'], 'dot', 'synthetic-worker', 'attempt-1',
                     1, 'completed', f.output())
    health._clear_alert(f.root)
    assert f.run()['status'] == 'unavailable'
    assert f.posts == 1
    assert sum(path.endswith('/cancel') for method, path in f.calls) == 1
