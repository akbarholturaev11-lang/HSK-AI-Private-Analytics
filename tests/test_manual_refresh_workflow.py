"""Fail-closed checks for the public-repository manual refresh workflow."""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "manual-refresh-request.yml"


class ManualRefreshWorkflowTests(unittest.TestCase):
    def test_requires_owner_for_issue_and_workflow_dispatch(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("github.actor == 'akbarholturaev11-lang'", workflow)
        self.assertIn("github.event.issue.user.login == 'akbarholturaev11-lang'", workflow)
        self.assertIn("[HSK AI] Refresh analytics", workflow)
        self.assertIn("types: [opened]", workflow)
        self.assertIn("workflow_dispatch:", workflow)

    def test_secrets_are_not_printed_and_sync_is_dedicated(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("secrets.HSKAI_MANUAL_AGGREGATE_REFRESH_TOKEN", workflow)
        self.assertIn("https://hskai.app/api/internal/admin-pwa/analytics/refresh", workflow)
        self.assertIn("--fail --max-time 90", workflow)
        self.assertIn("gh workflow run sync-r2.yml --ref main", workflow)
        self.assertNotIn("pull_request_target", workflow)
        self.assertNotIn("actions/checkout", workflow)
        self.assertNotIn('echo "$REFRESH_TOKEN"', workflow)
        self.assertNotIn('curl --verbose', workflow)


if __name__ == "__main__":
    unittest.main()
