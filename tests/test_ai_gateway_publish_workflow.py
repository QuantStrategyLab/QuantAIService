"""Exercise the publish workflow guards with local fake tools only."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/publish-ai-gateway-dash.yml"
# Reviewed default-branch SHA: dashboard nav pin and 401 re-login fixes.
# Publish admits only this SHA.
PINNED_SOURCE_SHA = "0b992e1203c67307b4edcddef8e7d93150bcb368"
STALE_SOURCE_SHA = "7eb49dd9b237345511c74f5693b94539775f5976"
# Synthetic 40-hex value. It is not a repository object.
NON_PINNED_FIXTURE_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def step_script(name):
    text = WORKFLOW.read_text()
    step = text.split("      - name: " + name + "\n", 1)[1]
    body = step.split("        run: |\n", 1)[1]
    return textwrap.dedent(re.split(r"^      - |^  [a-z]", body, maxsplit=1, flags=re.M)[0])


class GatewayPublishWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        tools = self.root / "ai-gateway-tools/node_modules/.bin"
        tools.mkdir(parents=True)
        self.calls = self.root / "calls"
        wrangler = tools / "wrangler"
        wrangler.write_text('#!/bin/bash\necho "$*" >> "$CALLS"\necho "private-tool-diagnostic"\nexit "${TOOL_EXIT:-0}"\n')
        wrangler.chmod(0o755)
        git = self.root / "git"
        git.write_text('#!/bin/bash\nprintf "%s\\n" "$CHECKED_SHA"\n')
        git.chmod(0o755)
        self.sha = "a" * 40
        self.env = dict(os.environ, PATH=str(self.root) + os.pathsep + os.environ["PATH"],
                        RUNNER_TEMP=str(self.root), CALLS=str(self.calls),
                        SOURCE_SHA=self.sha, REVIEWED_SOURCE_SHA=self.sha, CHECKED_SHA=self.sha,
                        GITHUB_REF="refs/heads/main", DEFAULT_REF="refs/heads/main",
                        DRY_RUN="false", CLOUDFLARE_API_TOKEN="fake-token",
                        CLOUDFLARE_ACCOUNT_ID="<PROJECT_ID>")

    def run_step(self, name, **env):
        return subprocess.run(["bash", "-c", step_script(name)], cwd=self.root,
                              env=dict(self.env, **env), capture_output=True, text=True)

    def test_request_rejects_branch_short_sha_and_unreviewed_source(self):
        for env in ({"SOURCE_SHA": "main"}, {"SOURCE_SHA": "a" * 7},
                    {"SOURCE_SHA": "b" * 40}, {"GITHUB_REF": "refs/heads/feature"},
                    {"DRY_RUN": "unknown"}, {"SOURCE_SHA": "$(touch injected)"}):
            with self.subTest(env=env):
                self.assertNotEqual(self.run_step("Validate request", **env).returncode, 0)
        self.assertFalse((self.root / "injected").exists())
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.run_step("Validate request").returncode, 0)

    def test_publish_blocks_missing_credentials_and_wrong_checkout(self):
        for env in ({"CLOUDFLARE_API_TOKEN": ""}, {"CLOUDFLARE_ACCOUNT_ID": ""},
                    {"CHECKED_SHA": "b" * 40}, {"DRY_RUN": "true"},
                    {"SOURCE_SHA": "b" * 40}, {"GITHUB_REF": "refs/heads/feature"}):
            with self.subTest(env=env):
                result = self.run_step("Publish reviewed dashboard", **env)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())

    def test_tool_failure_blocks_and_diagnostics_are_suppressed(self):
        for name in ("Validate dashboard bundle", "Publish reviewed dashboard"):
            with self.subTest(name=name):
                result = self.run_step(name, TOOL_EXIT="17")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("private-tool-diagnostic", result.stdout + result.stderr)
                self.assertNotIn("fake-token", result.stdout + result.stderr)
        self.assertEqual(self.calls.read_text().splitlines(), [
            "deploy --dry-run --config wrangler.jsonc", "deploy --config wrangler.jsonc"])

    def test_dry_run_never_publishes_and_hides_success_diagnostics(self):
        result = self.run_step("Validate dashboard bundle", DRY_RUN="true")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), ["deploy --dry-run --config wrangler.jsonc"])
        self.assertNotIn("private-tool-diagnostic", result.stdout + result.stderr)

    def test_publish_success_invokes_only_local_fake_once(self):
        self.assertEqual(self.run_step("Publish reviewed dashboard").returncode, 0)
        self.assertEqual(self.calls.read_text().splitlines(), ["deploy --config wrangler.jsonc"])

    def test_manual_only_serialized_protected_publish(self):
        text = WORKFLOW.read_text()
        self.assertIn("  workflow_dispatch:", text)
        self.assertNotRegex(text, r"(?m)^  (push|pull_request|schedule|workflow_call):")
        self.assertIn("    default: true", text)
        self.assertIn("  cancel-in-progress: false", text)
        self.assertIn("    environment: production", text)
        self.assertIn("    needs: validate", text)
        self.assertIn("    if: ${{ !inputs.dry_run }}", text)
        self.assertEqual(text.count("secrets.CLOUDFLARE_API_TOKEN"), 1)
        self.assertNotIn("continue-on-error", text)

    def test_reviewed_source_pin_matches_fe_candidate_exactly(self):
        text = WORKFLOW.read_text()
        defaults = re.findall(r"(?m)^\s+default:\s+([0-9a-f]{40})\s*$", text)
        pins = re.findall(r"(?m)^  REVIEWED_SOURCE_SHA:\s+([0-9a-f]{40})\s*$", text)
        self.assertEqual(defaults, [PINNED_SOURCE_SHA])
        self.assertEqual(pins, [PINNED_SOURCE_SHA])
        self.assertEqual(text.count(PINNED_SOURCE_SHA), 2)
        self.assertNotIn(STALE_SOURCE_SHA, text)
        self.assertNotIn(NON_PINNED_FIXTURE_SHA, text)
        self.assertIn('[[ "$SOURCE_SHA" == "$REVIEWED_SOURCE_SHA" ]]', text)
        self.assertIn("permissions:\n  contents: read\n", text)
        self.assertEqual(text.count("environment: production"), 1)

    def test_validate_rejects_stale_historical_source_against_current_pin(self):
        for stale in (STALE_SOURCE_SHA, NON_PINNED_FIXTURE_SHA):
            with self.subTest(stale=stale):
                result = self.run_step(
                    "Validate request",
                    SOURCE_SHA=stale,
                    REVIEWED_SOURCE_SHA=PINNED_SOURCE_SHA,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())
        self.assertEqual(
            self.run_step(
                "Validate request",
                SOURCE_SHA=PINNED_SOURCE_SHA,
                REVIEWED_SOURCE_SHA=PINNED_SOURCE_SHA,
            ).returncode,
            0,
        )

    def test_publish_rejects_stale_source_even_with_credentials(self):
        for stale in (STALE_SOURCE_SHA, NON_PINNED_FIXTURE_SHA):
            with self.subTest(stale=stale):
                result = self.run_step(
                    "Publish reviewed dashboard",
                    SOURCE_SHA=stale,
                    REVIEWED_SOURCE_SHA=PINNED_SOURCE_SHA,
                    CHECKED_SHA=stale,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.calls.exists())


if __name__ == "__main__":
    unittest.main()
