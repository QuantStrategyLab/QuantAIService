import ast
from datetime import datetime, timedelta, timezone
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_service.auth import Principal
from ai_service.runtime import Policy, TaskRuntime
from ai_service.server import Application
from ai_service.transport import HttpReply
from quant_ai_service import research_hook as hook


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)


def environment():
    return {"AI_SERVICE_URL": "https://service.example/mcp", "AI_SERVICE_AUDIENCE": "private-audience",
            "AI_SERVICE_PROFILE": "private-profile", "AI_SERVICE_MODEL": "private-model",
            "AI_HOOK_READ_TOKEN": "private-token"}


class Transport:
    def __init__(self, *, name="quant-ai-service", version="2.0.13"):
        self.calls = []
        self.name, self.version = name, version

    def request(self, url, *, method, headers, body):
        self.calls.append((url, method, json.loads(body) if body else None))
        if method == "POST":
            assert url == "https://service.example/mcp"
            assert json.loads(body)["method"] == "initialize"
            result = {"jsonrpc": "2.0", "id": "ai5-shadow", "result": {
                "serverInfo": {"name": self.name, "version": self.version}}}
        else:
            assert method == "GET" and url == "https://service.example/v1/tasks/existing-task"
            result = {"id": "existing-task", "status": "completed", "output": {"secret": "never-return-this"}}
        return HttpReply(200, json.dumps(result).encode())


class ShadowTests(unittest.TestCase):
    def test_workflow_uses_only_dynamic_secret_references_for_binding_values(self):
        workflow = (ROOT / ".github/workflows/q04-shadow.yml").read_text()
        for value, selector in (("AI_SERVICE_URL", "AI_HOOK_URL_SECRET"),
                                ("AI_SERVICE_AUDIENCE", "AI_HOOK_AUDIENCE_SECRET"),
                                ("AI_SERVICE_PROFILE", "AI_HOOK_PROFILE_SECRET"),
                                ("AI_SERVICE_MODEL", "AI_HOOK_MODEL_SECRET"),
                                ("AI_HOOK_READ_TOKEN", "AI_HOOK_READ_TOKEN_SECRET"),
                                ("AI_HOOK_EXISTING_TASK_ID", "AI_HOOK_EXISTING_TASK_ID_SECRET")):
            self.assertIn(value + ": ${{ secrets[vars." + selector + "] }}", workflow)
            self.assertNotIn(value + ": ${{ vars.", workflow)

    def binding(self, env=None):
        return hook.load_binding(ROOT / "config/ai-hook-shadow.json", environment() if env is None else env)

    def test_every_binding_value_is_required_before_any_request(self):
        for missing in environment():
            for invalid in (None, "", "  ", "\nprivate-value"):
                with self.subTest(missing=missing, invalid=invalid):
                    env = environment()
                    env[missing] = invalid
                    transport = Transport()
                    result = hook.checkpoint(hook.start_plan(NOW), ROOT / "config/ai-hook-shadow.json", env,
                                             now=NOW, transport=transport)
                    self.assertEqual(result["status"], "blocked_configuration")
                    self.assertEqual(transport.calls, [])
                    self.assertNotIn("private-value", json.dumps(result))

    def test_rejects_second_target_live_mode_or_inline_binding(self):
        original = json.loads((ROOT / "config/ai-hook-shadow.json").read_text())
        for change in ({"fallback": "other"}, {"service_target": "other-service"},
                       {"mode": "live"}, {"purpose": "trading"},
                       {"environment": {"url": "https://service.example"}}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "binding.json"
                path.write_text(json.dumps(original | change))
                with self.assertRaisesRegex(hook.BindingError, "binding_unavailable"):
                    hook.load_binding(path, environment())

    def test_rejects_unsafe_resource_without_network(self):
        for resource in ("http://service.example/mcp", "https://user@service.example/mcp",
                         "https://service.example/mcp?token=value", "https://service.example/mcp#fragment",
                         "https://service.example:8443/mcp", "https://service.example/other"):
            with self.subTest(resource=resource), self.assertRaises(hook.BindingError):
                self.binding(environment() | {"AI_SERVICE_URL": resource})

    def test_only_initialization_and_existing_task_read_are_reachable(self):
        transport = Transport()
        result = hook.observe(self.binding(), task_id="existing-task", transport=transport)
        self.assertEqual([call[1] for call in transport.calls], ["POST", "GET"])
        self.assertEqual(result["status"], "existing_task_observed")
        self.assertFalse(result["q04_protocol_accepted"])
        for value in environment().values():
            self.assertNotIn(value, json.dumps(result))
        self.assertNotIn("never-return-this", json.dumps(result))

    def test_different_service_or_major_version_never_reads_task(self):
        for name, version in (("personal-ai-service", "2.0.0"), ("quant-ai-service", "1.0.0"),
                              ("quant-ai-service", "development")):
            with self.subTest(name=name, version=version):
                transport = Transport(name=name, version=version)
                result = hook.observe(self.binding(), task_id="existing-task", transport=transport)
                self.assertEqual(result["status"], "observation_unavailable")
                self.assertEqual(len(transport.calls), 1)

    def test_task_identity_cannot_select_another_route(self):
        for task_id in ("../orders", "task/cancel", "x?token=value", "", 42):
            with self.subTest(task_id=task_id):
                transport = Transport()
                self.assertEqual(hook.observe(self.binding(), task_id=task_id, transport=transport)["status"],
                                 "observation_unavailable")
                self.assertEqual(transport.calls, [])

    def test_transport_failure_never_leaks_secret_or_retries(self):
        class Failed:
            calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                raise RuntimeError("private-token")
        transport = Failed()
        result = hook.observe(self.binding(), transport=transport)
        self.assertEqual(transport.calls, 1)
        self.assertNotIn("private-token", json.dumps(result))

    def dispatch_observation(self, scopes):
        project = "fixture-org/research"
        principal = Principal("fixture-reader", project, frozenset(scopes))
        with tempfile.TemporaryDirectory() as tmp:
            runtime = TaskRuntime(Path(tmp) / "runtime.sqlite", {}, {
                project: Policy({"api": "fixture"}, {"fixture": {"configured"}})})
            app = Application(runtime, None)
            requests = []
            class Dispatcher:
                def request(self, url, *, method, headers, body):
                    requests.append((method, url))
                    status, result = app.dispatch(method, url, headers, json.loads(body) if body else None,
                                                  principal=principal)
                    return HttpReply(status, json.dumps(result).encode())
            with patch("ai_service.server.version", return_value="2.0.13"), \
                    patch.object(runtime, "get", return_value={"id": "existing-task", "status": "completed"}), \
                    patch.object(runtime, "messages", return_value=[]), \
                    patch.object(runtime, "submit", side_effect=AssertionError("no task submission")):
                result = hook.observe(self.binding(), task_id="existing-task", transport=Dispatcher())
        return result, requests

    def test_actual_dispatcher_uses_root_rest_route_not_mcp_subpath(self):
        result, requests = self.dispatch_observation({"tasks:read", "tasks:work"})
        self.assertEqual(result["status"], "existing_task_observed")
        self.assertEqual(requests, [("POST", "https://service.example/mcp"),
                                    ("GET", "https://service.example/v1/tasks/existing-task")])

    def test_current_read_scope_cannot_initialize_and_is_not_promoted_to_worker(self):
        result, requests = self.dispatch_observation({"tasks:read"})
        self.assertEqual(result["status"], "observation_unavailable")
        self.assertFalse(result["service_identity_verified"])
        self.assertEqual(requests, [("POST", "https://service.example/mcp")])

    def test_seven_days_never_auto_activate_or_claim_protocol_acceptance(self):
        plan = hook.start_plan(NOW)
        self.assertEqual(len(plan["daily_checkpoints"]), 8)
        for elapsed, phase in ((timedelta(), "shadow_in_progress"),
                               (timedelta(days=7) - timedelta(microseconds=1), "shadow_in_progress"),
                               (timedelta(days=7), "shadow_elapsed_review_required")):
            with self.subTest(elapsed=elapsed):
                transport = Transport()
                result = hook.checkpoint(plan, ROOT / "config/ai-hook-shadow.json", environment(),
                                         now=NOW + elapsed, transport=transport)
                self.assertEqual(result["phase"], phase)
                self.assertFalse(result["formal_actions_enabled"])
                self.assertFalse(result["ai_submitted"])
                self.assertFalse(result["q04_protocol_accepted"])
                if elapsed >= timedelta(days=7):
                    self.assertEqual(transport.calls, [])
        with self.assertRaises(ValueError):
            hook.checkpoint(plan | {"formal_actions_enabled": True}, "unused", {}, now=NOW)

    def test_checkpoint_file_is_private_and_cannot_follow_symlink_or_restart_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "record.json"
            hook.write_record(path, hook.start_plan(NOW), exclusive=True)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                hook.write_record(path, hook.start_plan(NOW), exclusive=True)
            link = Path(tmp) / "link.json"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                hook.write_record(link, {})

    def test_static_imports_do_not_reach_mutating_task_clients_or_business_modules(self):
        tree = ast.parse(inspect.getsource(hook))
        imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertEqual(imported, {"__future__", "datetime", "pathlib", "urllib.parse", "ai_service.transport"})
        imports = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertEqual(imports, {"argparse", "json", "os", "re"})
        calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute)}
        self.assertTrue(calls.isdisjoint({"submit", "cancel", "message", "call_tool", "system", "popen", "exec"}))


if __name__ == "__main__":
    unittest.main()
