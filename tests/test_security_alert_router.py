"""Offline security routing regression tests. No external calls or credentials."""

import contextlib
import hashlib
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/security_alert_router.py"
FIXTURE = Path(__file__).parent / "fixtures/security-alerts.json"
spec = importlib.util.spec_from_file_location("security_alert_router", SCRIPT)
router = importlib.util.module_from_spec(spec)
spec.loader.exec_module(router)
NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "state.json"
        self.state = router.load_state(self.path)
        self.alerts = json.loads(FIXTURE.read_text())
        self.sent = []

    def route(self, alerts=None, now=NOW, sender=None):
        router.route(self.alerts if alerts is None else alerts, self.state, now,
                     self.path, sender or self.sent.append)

    def test_bootstrap_summary_dedupe_and_one_reminder(self):
        self.route(self.alerts * 50)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("2 open alerts", self.sent[0])
        self.state = router.load_state(self.path)
        self.route(now=NOW + timedelta(hours=23, minutes=59))
        self.assertEqual(len(self.sent), 1)
        self.route(now=NOW + timedelta(hours=24))
        self.assertEqual(len(self.sent), 3)
        self.route(now=NOW + timedelta(days=3))
        self.assertEqual(len(self.sent), 3)

    def test_reminder_uses_first_report_not_created_at(self):
        self.route()
        self.assertEqual(router.plan(self.alerts, self.state, NOW)[1], [])

    def test_closed_alert_not_reminded_and_reopen_not_new(self):
        self.route()
        self.route([], now=NOW + timedelta(days=2))
        self.assertEqual(len(self.sent), 1)
        new, reminders = router.plan(self.alerts, self.state, NOW + timedelta(days=2))
        self.assertEqual(new, [])
        self.assertEqual(len(reminders), 2)

    def test_large_bootstrap_is_one_bounded_message(self):
        many = [dict(self.alerts[0], number=i) for i in range(1, 201)]
        self.route(many)
        self.assertEqual(len(self.sent), 1)
        self.assertLess(len(self.sent[0]), 4096)
        self.assertEqual(len(self.state["alerts"]), 200)
        self.assertIn("200 open alerts", self.sent[0])

    def test_new_alert_after_bootstrap_has_all_required_fields(self):
        self.route()
        added = dict(self.alerts[0], number=3)
        self.route(self.alerts + [added])
        text = self.sent[-1]
        for value in (added["repo"], "#3", added["type"], added["severity"],
                      "created_at=", "age=24h", "/security/secret-scanning/3"):
            self.assertIn(value, text)

    def test_ambiguous_delivery_is_saved_pending_and_never_retried(self):
        def fail(_text):
            raise router.SafeError("unknown outcome")
        with self.assertRaises(router.SafeError):
            self.route(sender=fail)
        self.state = router.load_state(self.path)
        self.assertTrue(all(e["pending"] for e in self.state["alerts"].values()))
        with self.assertRaises(router.SafeError):
            self.route(now=NOW + timedelta(days=2))
        self.assertEqual(self.sent, [])

    def test_definitive_rejection_rolls_back_pending_so_next_run_retries(self):
        def reject(_text):
            raise router.DeliveryNotSent("rejected")
        with self.assertRaises(router.DeliveryNotSent):
            self.route(sender=reject)
        self.state = router.load_state(self.path)
        self.assertEqual(self.state["alerts"], {})
        self.route(now=NOW + timedelta(hours=1))
        self.assertEqual(len(self.sent), 1)

    def test_rejected_reminder_clears_only_pending_flag(self):
        self.route()
        self.state = router.load_state(self.path)
        def reject(_text):
            raise router.DeliveryNotSent("rejected")
        with self.assertRaises(router.DeliveryNotSent):
            self.route(now=NOW + timedelta(hours=25), sender=reject)
        self.state = router.load_state(self.path)
        self.assertFalse(any(e["pending"] or e["reminded"] for e in self.state["alerts"].values()))
        self.route(now=NOW + timedelta(hours=26))
        self.assertEqual(len(self.sent), 3)

    def test_http_4xx_is_not_sent_but_5xx_and_timeout_stay_uncertain(self):
        telegram = object.__new__(router.Telegram)
        telegram.token, telegram.chat = "synthetic", "synthetic-chat"
        telegram.last_send = 0.0
        for code, expected in ((400, router.DeliveryNotSent), (429, router.DeliveryNotSent),
                               (502, router.SafeError), ):
            with patch.object(router.request, "build_opener") as opener:
                telegram.opener = opener.return_value
                telegram.opener.open.side_effect = HTTPError("x", code, "x", {}, None)
                with self.assertRaises(expected) as caught:
                    telegram.send("synthetic alert")
                self.assertEqual(type(caught.exception) is router.DeliveryNotSent, expected is router.DeliveryNotSent)

    def test_state_contains_only_opaque_ids_and_delivery_timing(self):
        self.route()
        value = json.loads(self.path.read_text())
        self.assertEqual(set(value), {"version", "alerts"})
        for key, entry in value["alerts"].items():
            self.assertRegex(key, r"^[a-f0-9]{64}$")
            self.assertEqual(set(entry), {"first_reported", "reminded", "pending"})
        self.assertNotIn("example-repo", self.path.read_text())

    def test_invalid_state_stops_instead_of_resetting_dedupe(self):
        self.path.write_text('{"unexpected":"value"}')
        with self.assertRaises(router.SafeError):
            router.load_state(self.path)

    def test_dry_run_prints_counts_only_and_never_touches_state_or_telegram(self):
        output = io.StringIO()
        with patch.object(router, "Telegram") as telegram, contextlib.redirect_stdout(output):
            result = router.main(["--dry-run", "--fixture", str(FIXTURE), "--state", str(self.path)])
        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue()),
                         {"repositories": 1, "open": 2, "new": 2, "reminders": 0, "pending": 0})
        telegram.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_projection_never_emits_secret_or_locations_and_selects_owner_token(self):
        fake = "123456789:" + "A" * 35
        result = subprocess.CompletedProcess([], 0, stdout='{"count":0,"items":[]}', stderr="")
        with patch.dict(os.environ, {"GH_DEBUG": "api", "QSL_SECURITY_ALERTS_TOKEN": fake}), \
                patch.object(router.subprocess, "run", return_value=result) as run:
            router.gh_page("repos/QuantStrategyLab/example/secret-scanning/alerts?state=open",
                           '{count:length,items:[.[] | {number,created_at,type:.secret_type}]}',
                           "QuantStrategyLab")
        args = run.call_args
        self.assertNotIn(fake, str(args.args))
        self.assertNotIn("GH_DEBUG", args.kwargs["env"])
        self.assertEqual(args.kwargs["env"]["GH_TOKEN"], fake)
        query = args.args[0][args.args[0].index("--jq") + 1]
        self.assertNotIn("locations", query)
        self.assertNotIn(".secret,", query)

    def test_pagination_continues_after_severity_filtered_empty_page(self):
        # gh emits one projected JSON line per page, following Link headers.
        output = '\n'.join(json.dumps(page) for page in [
            {"count": 100, "items": []}, {"count": 1, "items": ["high"]}])
        result = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
        with patch.object(router.subprocess, "run", return_value=result) as run:
            self.assertEqual(list(router.pages("repos/example/alerts?state=open", ".", "Pigbibi")),
                             ["high"])
        command = run.call_args.args[0]
        self.assertIn("--paginate", command)
        self.assertIn("per_page=100", command[2])
        self.assertNotIn("&page=", command[2])

    def test_disabled_code_scanning_is_optional_but_other_failures_are_errors(self):
        for status in (403, 404):
            result = subprocess.CompletedProcess([], 1, stdout="", stderr=f"gh: hidden (HTTP {status})")
            with patch.object(router.subprocess, "run", return_value=result):
                if status == 404:
                    self.assertIsNone(router.gh_page("repos/example/code-scanning/alerts", ".",
                                                    "Pigbibi", optional=True))
                else:
                    with self.assertRaises(router.SafeError):
                        router.gh_page("repos/example/code-scanning/alerts", ".", "Pigbibi", optional=True)
                with self.assertRaises(router.SafeError):
                    router.gh_page("repos/example/secret-scanning/alerts", ".", "Pigbibi")

    def test_poll_filters_public_repos_and_alert_payloads(self):
        calls = []

        def fake_pages(endpoint, projection, owner, optional=False):
            calls.append((endpoint, projection, owner, optional))
            if endpoint.startswith(("orgs/", "users/")):
                self.assertIn(".private == false", projection)
                self.assertIn(".archived == false", projection)
                self.assertIn(".fork == false", projection)
                return [f"{owner}/example"]
            if "secret-scanning" in endpoint:
                self.assertIn("type:.secret_type", projection)
                return [{"number": 1, "created_at": "2026-01-01T00:00:00Z",
                         "type": "synthetic", "severity": "unknown"}]
            if "dependabot" in endpoint:
                self.assertIn('== "high"', projection)
                self.assertIn('== "critical"', projection)
            return []

        with patch.object(router, "pages", side_effect=fake_pages):
            alerts, repos = router.poll(True)
        self.assertEqual((len(alerts), repos), (2, 2))
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(c[3] for c in calls if "code-scanning" in c[0]))
        self.assertTrue(all(".secret," not in c[1] and "locations" not in c[1] for c in calls))

    def test_partial_poll_failure_never_delivers_or_writes_state(self):
        with patch.object(router, "poll", side_effect=router.SafeError("read failed")), \
                patch.object(router, "Telegram") as telegram, contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(router.main(["--state", str(self.path)]), 1)
        telegram.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_redaction_filters_credentials_and_exception_tracebacks(self):
        fake = "123456789:" + "A" * 35
        record = logging.LogRecord("test", logging.ERROR, "", 1, "url /bot%s chat_id=123", (fake,),
                                   (ValueError, ValueError(fake), None))
        router.RedactionFilter().filter(record)
        self.assertNotIn(fake, record.getMessage())
        self.assertNotIn("chat_id=123", record.getMessage())
        self.assertIsNone(record.exc_info)

    def test_telegram_transport_never_logs_body_or_exception_url(self):
        telegram = object.__new__(router.Telegram)
        telegram.token, telegram.chat = "synthetic", "synthetic-chat"
        telegram.last_send = 0.0
        for failure in (URLError("sensitive-url"),
                        HTTPError("sensitive-url", 403, "sensitive-body", {}, None)):
            with patch.object(router.request, "build_opener") as opener:
                telegram.opener = opener.return_value
                telegram.opener.open.side_effect = failure
                with self.assertRaises(router.SafeError) as caught:
                    telegram.send("synthetic alert")
                self.assertNotIn("sensitive", str(caught.exception))
                self.assertEqual(telegram.opener.open.call_count, 1)

    def test_bot_identity_must_match_existing_hash_before_send(self):
        environment = {"TG_TOKEN": "synthetic", "GLOBAL_TELEGRAM_CHAT_ID": "synthetic-chat",
                       "QUANT_SENTINEL_EXPECTED_BOT_ID_SHA256": hashlib.sha256(b"123").hexdigest()}
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(router.Telegram, "call", return_value={
                    "ok": True, "result": {"id": 123, "username": "synthetic_bot"}}) as call:
            router.Telegram()
            call.assert_called_once_with("getMe", {})
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(router.Telegram, "call", return_value={
                    "ok": True, "result": {"id": 456, "username": "synthetic_bot"}}):
            with self.assertRaises(router.SafeError):
                router.Telegram()

    def test_telegram_refuses_redirects(self):
        self.assertIsNone(router.NoRedirect().redirect_request(None, None, 302, "", {}, "https://example.test"))


if __name__ == "__main__":
    unittest.main()
