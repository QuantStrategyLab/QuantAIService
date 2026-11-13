"""Offline adverse cases for production preparation and read-only smoke checks."""
import contextlib
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker_deploy", ROOT / "scripts/worker_deploy.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)
DIRECTORY = ROOT / "cloudflare/task-service"


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        base = json.loads((DIRECTORY / "wrangler.example.jsonc").read_text())
        (self.directory / "wrangler.example.jsonc").write_text(json.dumps(base))
        self.config = copy.deepcopy(base)
        for key in ("main", "compatibility_date", "compatibility_flags", "no_bundle", "observability"):
            self.config.pop(key, None)
        self.config["name"] = "synthetic-worker"
        self.config["workers_dev"] = False
        self.config["routes"] = []
        self.config["vars"].update(PUBLIC_ORIGIN="https://tasks.synthetic.test",
                                   PROJECT_ID="synthetic-project", TASK_STORE_NAME="synthetic-store",
                                   GITHUB_CLIENT_ID="synthetic-client", AGENT_PROVIDERS={})
        self.config["kv_namespaces"][0]["id"] = "synthetic-kv-id"
        self.config["services"][0]["service"] = "synthetic-worker"
        self.config["migrations"][0]["tag"] = "synthetic-existing-migration"
        self.environment = patch.dict(os.environ, WORKER_NAME="synthetic-worker", clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def configure(self):
        with patch.dict(os.environ, WORKER_CONFIG_JSON=json.dumps(self.config)):
            with contextlib.redirect_stdout(io.StringIO()):
                deploy.configure(self.directory)

    def test_preserves_committed_logs_and_resolves_entrypoint(self):
        self.configure()
        path = self.directory / ".wrangler/production.json"
        generated = json.loads(path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(generated["main"], str(self.directory / "dist/worker.mjs"))
        self.assertEqual(generated["observability"], {"enabled": True, "logs": {
            "enabled": True, "invocation_logs": False, "head_sampling_rate": 1}})
        self.assertFalse(generated["keep_vars"])

    def test_missing_config_sections_and_placeholders_fail_closed(self):
        original = copy.deepcopy(self.config)
        for key in ("routes", "workers_dev", "vars", "migrations", "services", "kv_namespaces"):
            with self.subTest(key=key):
                self.config = copy.deepcopy(original)
                del self.config[key]
                with self.assertRaises(ValueError):
                    self.configure()
        self.config = original
        self.config["vars"]["PROJECT_ID"] = "CONFIGURE_EXISTING_PROJECT"
        with self.assertRaises(ValueError):
            self.configure()

    def test_target_origin_binding_and_log_overrides_are_rejected(self):
        original = copy.deepcopy(self.config)
        for change in ({"name": "other-worker"}, {"observability": {"enabled": False}},
                       {"main": "unreviewed.mjs"}, {"account_id": "synthetic-account"},
                       {"services": []}, {"durable_objects": {}}, {"migrations": []}):
            with self.subTest(change=change):
                self.config = {**original, **change}
                with self.assertRaises(ValueError):
                    self.configure()
        self.config = copy.deepcopy(original)
        self.config["vars"]["PUBLIC_ORIGIN"] = "https://user:password@tasks.synthetic.test"
        with self.assertRaises(ValueError):
            self.configure()

    def test_mask_escapes_workflow_commands(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            deploy.mask({"id": "synthetic%value\n::notice::private"})
        self.assertEqual(output.getvalue(), "::add-mask::synthetic%25value%0A::notice::private\n")

    def test_smoke_get_only_optional_token_and_no_response_output(self):
        self.configure()
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(request)
                reply = io.BytesIO(json.dumps({"status": "ok"} if request.full_url.endswith("/healthz")
                                             else ({"tasks": [], "next_cursor": None, "private-body": "never-print-this"}
                                                   if "?" in request.full_url else {"counts": [], "stuck": {}})).encode())
                reply.status = 200
                return reply
        for token, expected in (("", 1), ("synthetic-bearer", 3)):
            requests.clear()
            output = io.StringIO()
            with patch.dict(os.environ, SMOKE_TASKS_TOKEN=token), patch.object(
                    deploy.urllib.request, "build_opener", return_value=Opener()), contextlib.redirect_stdout(output):
                deploy.smoke(self.directory)
            self.assertEqual(len(requests), expected)
            self.assertTrue(all(r.method == "GET" for r in requests))
            self.assertNotIn("never-print-this", output.getvalue())
            self.assertNotIn("Authorization", requests[0].headers)
            if token:
                self.assertEqual(requests[1].headers["Authorization"], "Bearer " + token)

    def test_cli_suppresses_raw_invalid_secret_and_exception(self):
        private = "synthetic-private-value-not-json"
        result = subprocess.run([sys.executable, str(ROOT / "scripts/worker_deploy.py"),
                                 "configure", str(self.directory)],
                                env={**os.environ, "WORKER_CONFIG_JSON": private},
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(private, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("private details suppressed", result.stderr)

    def test_redirect_handler_refuses_credential_forwarding(self):
        self.assertIsNone(deploy.NoRedirect().redirect_request(None, None, 302, "redirect", {},
                                                              "https://other.synthetic.test"))

    def test_smoke_failure_is_not_success(self):
        self.configure()
        class Opener:
            def open(self, request, timeout):
                response = io.BytesIO(b'{"status":"failed"}')
                response.status = 200
                return response
        with patch.object(deploy.urllib.request, "build_opener", return_value=Opener()), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            deploy.smoke(self.directory)


if __name__ == "__main__":
    unittest.main()
