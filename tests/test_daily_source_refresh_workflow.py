"""Guard the owner-only daily aggregate refresh contract without calling production."""
from pathlib import Path
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "daily-r2-source-refresh.yml"


class DailySourceRefreshWorkflowTests(unittest.TestCase):
    def test_owner_only_scheduled_once_before_r2_mirror(self):
        script = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", script)
        self.assertIn("cron: '35 6 * * *'", script)
        self.assertIn("github.actor == 'akbarholturaev11-lang'", script)
        self.assertIn("github.event_name == 'schedule'", script)
        self.assertIn("contents: read", script)
        self.assertIn("cancel-in-progress: false", script)
        self.assertNotIn("pull_request_target", script)
        self.assertNotIn("issues:", script)
        self.assertNotIn("actions/checkout", script)

    def test_existing_server_source_only_and_private_output(self):
        script = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("secrets.HSKAI_MANUAL_AGGREGATE_REFRESH_TOKEN", script)
        self.assertIn("https://hskai.app/api/internal/admin-pwa/analytics/refresh", script)
        self.assertIn("for section in course funnel money sources", script)
        self.assertIn("set(d.get(\"periods\",{}))=={\"weekly\"}", script)
        self.assertIn("set(d.get(\"periods\",{}))=={\"weekly\",\"monthly\",\"all_time\"}", script)
        self.assertIn("curl --silent --show-error --fail --max-time", script)
        self.assertNotIn("python -m scripts", script)
        self.assertNotIn("git push", script)
        self.assertNotIn("gh workflow run", script)
        self.assertNotIn('echo "$REFRESH_TOKEN"', script)
        self.assertNotIn("curl --verbose", script)


if __name__ == "__main__":
    unittest.main()
