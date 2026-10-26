"""Bridge regression tests — no network or production credentials."""
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.sync_from_r2 import InvalidSnapshot, sanitize, sync, ROOT


NOW = datetime(2026, 10, 26, 4, 0, tzinfo=timezone.utc)


def snapshot(period="weekly", gen=None):
    gen = gen or NOW - timedelta(minutes=10)
    since = None if period == "all_time" else gen - timedelta(days=7 if period == "weekly" else 30)
    metrics = {
        "users_total": 105, "new_accounts": 7, "active_accounts": 15,
        "payments_by_status": {"pending": 2, "approved": 1, "rejected": 1},
        "ai_usage": {"recorded_requests": 8, "tokens": 123, "estimated_cost_usd": 0.56},
        "course_engagement": {
            "engaged_accounts": 4, "started_accounts": 4,
            "completed_accounts": 2, "unmatched_events": 0,
        },
        "registration_conversion": {
            "registered_accounts": 7, "converted_accounts": 1, "percent": 14.28,
        },
        "approved_amounts_by_currency": [{"currency": "USD", "amount": 12}],
    }
    payload = {
        "version": 1,
        "expires_at": (gen + timedelta(days=1)).timestamp(),
        "data": {
            "platform": "overall", "period": period, "available": True,
            "source": "AdminMiniAppService + pwa_metrics.overall_activity_metrics",
            "generated_at": gen.isoformat(), "since": since.isoformat() if since else None,
            "metrics": metrics,
            "raw_user_list": [{"email": "private@example.com", "token": "secret-canary"}],
        },
    }
    return json.dumps(payload).encode()


class FakeBody:
    def __init__(self, raw):
        self.raw = raw

    def read(self, n):
        return self.raw[:n]

    def close(self):
        pass


class FakeR2:
    def __init__(self, items, fail=False):
        self.items, self.fail = items, fail
        self.reads = []

    def get_object(self, *, Bucket, Key):
        self.reads.append((Bucket, Key))
        if self.fail:
            raise RuntimeError("network down")
        if Key not in self.items:
            e = ValueError("missing")
            e.response = {"Error": {"Code": "NoSuchKey"}}
            raise e
        raw = self.items[Key]
        return {"Body": FakeBody(raw), "ContentLength": len(raw)}


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.ns = "a" * 24
        self.key = f"{ROOT}/{self.ns}/statistics/overall_weekly.json"

    def test_allowlist_discards_private_source_fields(self):
        data = sanitize(snapshot(), "weekly", NOW)
        self.assertEqual(data["metrics"]["users_total"], 105)
        self.assertEqual(data["approved_amounts_by_currency"]["USD"], 12)
        self.assertNotIn("secret-canary", json.dumps(data))
        self.assertNotIn("private@example.com", json.dumps(data))

    def test_rejects_stale_wrong_period_and_wrong_source(self):
        for raw, period in (
            (snapshot(gen=NOW-timedelta(days=2)), "weekly"),
            (snapshot(), "monthly"),
        ):
            with self.assertRaises(InvalidSnapshot):
                sanitize(raw, period, NOW)
        data = json.loads(snapshot())
        data["data"]["source"] = "attacker"
        with self.assertRaises(InvalidSnapshot):
            sanitize(json.dumps(data).encode(), "weekly", NOW)

    def test_rejects_personal_data_in_aggregate_field(self):
        data = json.loads(snapshot())
        data["data"]["metrics"]["users_total"] = {"email": "user@example.com"}
        with self.assertRaises(InvalidSnapshot):
            sanitize(json.dumps(data).encode(), "weekly", NOW)

    def test_rejects_duplicate_json_keys(self):
        with self.assertRaises(InvalidSnapshot):
            sanitize(b'{"version":1,"version":1,"data":{},"expires_at":0}', "weekly", NOW)

    def test_fresh_output_then_stale_clear(self):
        client = FakeR2({self.key: snapshot()})
        with TemporaryDirectory() as d:
            dest = Path(d)
            status = sync(client, "hsk-ai-statistics", self.ns, dest, NOW)
            self.assertEqual(status["status"], "fresh")
            latest = (dest/"data/latest.json").read_text()
            self.assertNotIn("secret-canary", latest)
            self.assertTrue((dest/"data/summary.md").exists())
            self.assertEqual(len(client.reads), 3)
            status = sync(client, "hsk-ai-statistics", self.ns, dest, NOW+timedelta(days=2))
            self.assertEqual(status["status"], "unavailable")
            self.assertFalse((dest/"data/latest.json").exists())

    def test_remote_failure_does_not_mutate_data(self):
        client = FakeR2({self.key: snapshot()}, fail=True)
        with TemporaryDirectory() as d:
            dest = Path(d)
            (dest/"data").mkdir()
            (dest/"data/latest.json").write_text("prior")
            with self.assertRaises(RuntimeError):
                sync(client, "hsk-ai-statistics", self.ns, dest, NOW)
            self.assertEqual((dest/"data/latest.json").read_text(), "prior")

    def test_periods_are_isolated(self):
        client = FakeR2({
            self.key: snapshot(),
            f"{ROOT}/{self.ns}/statistics/overall_monthly.json": snapshot("monthly"),
        })
        with TemporaryDirectory() as d:
            status = sync(client, "hsk-ai-statistics", self.ns, Path(d), NOW)
            self.assertEqual(status["valid_periods"], ["monthly", "weekly"])
            self.assertTrue((Path(d)/"data/overall_monthly.json").exists())
            self.assertFalse((Path(d)/"data/overall_all_time.json").exists())


if __name__ == "__main__":
    unittest.main()
