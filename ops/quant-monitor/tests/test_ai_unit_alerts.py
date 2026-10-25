import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ai_unit_alerts.py"
SPEC = importlib.util.spec_from_file_location("ai_unit_alerts", SCRIPT)
ALERTS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ALERTS)


class AIUnitAlertsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state.json"
        self.state = ALERTS.load(self.path)
        transport = types.ModuleType("quant_platform_kit.notifications.telegram")
        transport.parse_telegram_chat_ids = lambda raw: tuple(raw.split(",")) if raw else ()
        self.transport = transport
        self.modules = patch.dict(sys.modules, {"quant_platform_kit.notifications.telegram": transport})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.environment = patch.dict(os.environ, {"TELEGRAM_TOKEN": "test-only", "GLOBAL_TELEGRAM_CHAT_ID": "fixture-chat"})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def event(self, invocation, failed=True, unit="fixture.service"):
        ALERTS.observe(self.state, unit=unit, invocation=invocation, failed=failed,
                       reason="exit-code" if failed else "completed-successfully",
                       run_url="https://github.com/fixture/repo/actions/runs/1", observed_at="2026-10-22T12:00:00Z")

    def test_threshold_counts_distinct_invocations_and_survives_restart(self):
        self.event("first")
        self.event("first")
        self.assertEqual(self.state["outbox"], [])
        ALERTS.save(self.path, self.state)
        self.state = ALERTS.load(self.path)
        self.event("second")
        self.event("third")
        self.assertEqual(len(self.state["outbox"]), 1)
        self.assertEqual(self.state["units"]["fixture.service"]["failures"], 3)

    def test_success_breaks_streak_and_recovery_only_follows_alert(self):
        self.event("one")
        self.event("two", False)
        self.event("three")
        self.assertEqual(self.state["outbox"], [])
        self.event("four")
        self.event("five", False)
        self.event("five", False)
        self.event("six", False)
        self.assertEqual([item["kind"] for item in self.state["outbox"]], ["failure", "recovery"])
        self.event("seven")
        self.event("eight")
        self.assertEqual(len(self.state["outbox"]), 3)

    def test_units_have_independent_streaks(self):
        self.event("one")
        self.event("two", unit="other.service")
        self.assertFalse(self.state["outbox"])

    def test_stable_daemon_later_failure_in_same_invocation_is_counted(self):
        self.event("daemon-first", False)
        self.event("daemon-first", True)
        self.event("daemon-first", True)
        self.event("daemon-second", True)
        self.assertEqual(self.state["units"]["fixture.service"]["failures"], 2)
        self.assertEqual(len(self.state["outbox"]), 1)

    def test_delivered_receipts_deduplicate_and_never_store_route_or_token(self):
        self.event("one")
        self.event("two")
        calls = []

        def sender(text, chat, token):
            calls.append(text)
            return {"status": "delivered", "message_id": 123, "date": 456}

        self.assertEqual(ALERTS.deliver(self.path, self.state, sender), 0)
        self.state = ALERTS.load(self.path)
        self.assertEqual(ALERTS.deliver(self.path, self.state, sender), 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("fixture-chat", self.path.read_text())
        self.assertNotIn("test-only", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_unknown_delivery_and_crash_claim_do_not_resend(self):
        self.event("one")
        self.event("two")

        def crash(*args):
            raise RuntimeError("simulated process death")

        with self.assertRaises(RuntimeError):
            ALERTS.deliver(self.path, self.state, crash)
        self.state = ALERTS.load(self.path)
        self.assertEqual(ALERTS.deliver(self.path, self.state, crash), 2)
        receipt = next(iter(self.state["outbox"][0]["receipts"].values()))
        receipt["status"] = "unknown"
        self.assertEqual(ALERTS.deliver(self.path, self.state, crash), 2)

    def test_explicit_rejection_can_retry_and_route_change_is_refused(self):
        self.event("one")
        self.event("two")
        self.assertEqual(ALERTS.deliver(self.path, self.state, lambda *args: {"status": "rejected"}), 2)
        self.assertEqual(ALERTS.deliver(self.path, self.state, lambda *args: {"status": "delivered"}), 0)
        with patch.dict(os.environ, {"GLOBAL_TELEGRAM_CHAT_ID": "different-fixture"}):
            with self.assertRaises(ValueError):
                ALERTS.deliver(self.path, self.state)

    def test_send_requires_real_message_id_date_and_matching_chat(self):
        def transport(**kwargs):
            self.assertEqual(kwargs["chat_ids"], ["fixture-chat"])
            kwargs["opener"](object(), 10)
            return True

        self.transport.send_telegram_message = transport
        for payload, expected in [
            ({"ok": True}, "unknown"),
            ({"ok": False}, "rejected"),
            ({"ok": True, "result": {"message_id": 1, "date": 2, "chat": {"id": "other"}}}, "unknown"),
            ({"ok": True, "result": {"message_id": 1, "date": 2, "chat": {"id": "fixture-chat"}}}, "delivered"),
        ]:
            response = types.SimpleNamespace(status=200, read=lambda: json.dumps(payload).encode())
            manager = unittest.mock.MagicMock()
            manager.__enter__.return_value = response
            with patch("urllib.request.urlopen", return_value=manager):
                receipt = ALERTS.send("body", "fixture-chat", "test-only")
            self.assertEqual(receipt["status"], expected)

    def test_corrupt_state_is_not_silently_reset(self):
        self.path.write_text("broken")
        with self.assertRaises(ValueError):
            ALERTS.load(self.path)

    def test_secret_is_read_only_when_pending_and_never_in_command_arguments(self):
        with patch.dict(os.environ, {"TELEGRAM_TOKEN": "", "TG_TOKEN": "", "QUANT_SENTINEL_GCP_PROJECT": "fixture-project"}):
            self.event("one")
            self.event("two")
            with patch.object(ALERTS.subprocess, "run", return_value=types.SimpleNamespace(stdout="fixture-secret\n")) as command:
                self.assertEqual(ALERTS.deliver(self.path, self.state, lambda *args: {"status": "delivered"}), 0)
            self.assertNotIn("fixture-secret", str(command.call_args))
            self.assertNotIn("fixture-secret", self.path.read_text())
            with patch.object(ALERTS.subprocess, "run", side_effect=AssertionError("no repeat read")):
                self.assertEqual(ALERTS.deliver(self.path, self.state), 0)

    def test_journal_failure_duplicates_and_oneshot_success(self):
        records = []
        for index, (invocation, failed) in enumerate([("a" * 32, True), ("a" * 32, True), ("b" * 32, True), ("c" * 32, False)]):
            records.append(json.dumps({"__CURSOR": str(index), "_PID": "1", "UNIT": "fixture.service",
                                       "INVOCATION_ID": invocation, "UNIT_RESULT": "exit-code" if failed else "success",
                                       "MESSAGE_ID": "failed" if failed else ALERTS.STARTED,
                                       "__REALTIME_TIMESTAMP": "1792670400000000"}))
        properties = types.SimpleNamespace(stdout="Id=fixture.service\nType=oneshot\nActiveState=inactive\n")
        journal = types.SimpleNamespace(stdout="\n".join(records) + "\n-- cursor: final\n")
        with patch.object(ALERTS.subprocess, "run", side_effect=[properties, journal]):
            ALERTS.journal_events(self.state, ["fixture.service"])
        self.assertEqual([item["kind"] for item in self.state["outbox"]], ["failure", "recovery"])
        self.assertEqual(self.state["cursor"], "final")

    def test_restarting_daemon_does_not_reset_streak_before_stability_window(self):
        self.event("a" * 32)
        properties = types.SimpleNamespace(stdout="Id=fixture.service\nType=simple\nActiveState=active\nInvocationID=" + "b" * 32 + "\nActiveEnterTimestampMonotonic=900000000\n")
        journal = types.SimpleNamespace(stdout=json.dumps({"_PID": "1", "UNIT": "fixture.service", "INVOCATION_ID": "b" * 32,
                                                          "MESSAGE_ID": ALERTS.STARTED, "__REALTIME_TIMESTAMP": "1792670400000000"}))
        with patch.object(ALERTS.subprocess, "run", side_effect=[properties, journal]), patch.object(ALERTS.time, "monotonic", return_value=1000):
            ALERTS.journal_events(self.state, ["fixture.service"])
        self.event("b" * 32)
        self.assertEqual(self.state["outbox"][0]["kind"], "failure")
        properties.stdout = properties.stdout.replace("b" * 32, "c" * 32)
        with patch.object(ALERTS.subprocess, "run", side_effect=[properties, types.SimpleNamespace(stdout="")]), patch.object(ALERTS.time, "monotonic", return_value=1300):
            ALERTS.journal_events(self.state, ["fixture.service"])
        self.assertEqual(self.state["outbox"][-1]["kind"], "recovery")

    def test_workflow_reads_actual_failure_then_actual_success(self):
        runs = [{"id": number, "head_branch": "test", "event": "workflow_dispatch", "status": "completed",
                 "conclusion": conclusion, "html_url": f"https://github.com/fixture/repo/actions/runs/{number}",
                 "updated_at": "2026-10-22T12:00:00Z"} for number, conclusion in [(1, "failure"), (2, "success")]]
        jobs = {"jobs": [{"steps": [{"name": "AI4 controlled failure (exit 42)", "conclusion": "failure"}]}]}
        with patch.object(ALERTS, "github_json", side_effect=[{"workflow_runs": runs}, jobs]):
            ALERTS.workflow_events(self.state, "fixture/repo", "fixture.yml", "test")
        self.assertEqual([item["kind"] for item in self.state["outbox"]], ["failure", "recovery"])
        self.assertEqual("controlled-test-exit-42", self.state["outbox"][0]["reason"])
        self.assertIn("/runs/1", self.state["outbox"][0]["text"])
        with patch.object(ALERTS, "github_json", return_value={"workflow_runs": runs}):
            ALERTS.workflow_events(self.state, "fixture/repo", "fixture.yml", "test")
        self.assertEqual(len(self.state["outbox"]), 2)


if __name__ == "__main__":
    unittest.main()
