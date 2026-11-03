"""Keep monitor authentication bound to explicit runtime configuration."""
from pathlib import Path
import unittest


class MonitorProjectConfigTests(unittest.TestCase):
    def test_authentication_requires_project_variable_and_preflight(self):
        root = Path(__file__).resolve().parents[1]
        for name, count in (("quant-monitor-inputs.yml", 2),
                            ("quant-monitor-operational-diagnosis.yml", 1)):
            with self.subTest(workflow=name):
                text = (root / ".github/workflows" / name).read_text()
                self.assertEqual(text.count("project_id: ${{ vars.QUANT_MONITOR_GCP_PROJECT_ID }}"), count)
                self.assertEqual(text.count("- name: Validate cloud project configuration"), count)
                self.assertEqual(text.count("GCP_PROJECT_ID: ${{ vars.QUANT_MONITOR_GCP_PROJECT_ID }}"), count)
                self.assertEqual(text.count('[[ "$GCP_PROJECT_ID" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]'), count)


    def test_private_bucket_secret_is_checked_before_cloud_authentication(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github/workflows/quant-monitor-operational-diagnosis.yml").read_text()
        self.assertIn("QUANT_MONITOR_GCS_BUCKET: ${{ secrets.QUANT_MONITOR_GCS_BUCKET }}", text)
        self.assertLess(text.index("- name: Validate private storage configuration"),
                        text.index("- name: Authenticate to the bounded cloud state"))
        self.assertIn("configured_bucket(os.environ)", text)
