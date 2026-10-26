"""Bridge regression tests — no network or production credentials."""
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.sync_from_r2 import InvalidSnapshot, sanitize, sync, ROOT, sanitize_overview, sync_overview


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


def overview_snapshot(period="weekly", gen=None):
    gen = gen or NOW - timedelta(minutes=10)
    since = None if period == "all_time" else gen - timedelta(days=7 if period == "weekly" else 30)
    metrics = {
        "users_total": 105, "new_accounts": 7, "active_accounts": 15,
        "active_today": 4, "active_pro": 12, "expiring_pro": 1,
        "bot_blocked": 2, "blocked_current": 3, "pending_current": 4,
        "paying_accounts": 5, "payments_by_status": {
            "pending": 2, "approved": 1, "rejected": 0,
        },
        "course_engagement": {
            "engaged_accounts": 10, "started_accounts": 8, "completed_accounts": 6,
        },
        "raw_private": {"telegram_id": 12345678, "api_key": "do-not-publish"},
    }
    return json.dumps({
        "version": 1, "expires_at": (gen + timedelta(days=1)).timestamp(),
        "data": {
            "platform": "overall", "period": period, "since": since.isoformat() if since else None,
            "generated_at": gen.isoformat(), "available": True,
            "source": "users + payments + canonical learning events",
            "scope": {"section": "overview"}, "metrics": metrics,
        },
    }).encode()


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


    def test_overview_only_exports_allowlisted_counts(self):
        result = sanitize_overview(overview_snapshot(), "weekly", NOW)
        self.assertEqual(result["dataset"], "hskai_admin_overview")
        self.assertEqual(result["metrics"]["users_total"], 105)
        self.assertEqual(result["metrics"]["paying_accounts"], 5)
        self.assertNotIn("do-not-publish", json.dumps(result))
        self.assertNotIn("telegram_id", json.dumps(result))

    def test_overview_rejects_home_scope_invalid_count_and_expired(self):
        broken=json.loads(overview_snapshot())
        broken["data"]["scope"]["section"]="home"
        with self.assertRaises(InvalidSnapshot):
            sanitize_overview(json.dumps(broken).encode(), "weekly", NOW)
        broken=json.loads(overview_snapshot())
        broken["data"]["metrics"]["users_total"]={"telegram_id":987}
        with self.assertRaises(InvalidSnapshot):
            sanitize_overview(json.dumps(broken).encode(), "weekly", NOW)
        with self.assertRaises(InvalidSnapshot):
            sanitize_overview(overview_snapshot(gen=NOW-timedelta(days=2)), "weekly", NOW)

    def test_overview_sync_creates_latest_and_removes_stale(self):
        ns = "a" * 24
        key=f"{ROOT}/{ns}/statistics/overall_weekly.json"
        client=FakeR2({key:overview_snapshot()})
        with TemporaryDirectory() as d:
            dest=Path(d)
            first=sync_overview(client, "hsk-ai-statistics", ns, dest, NOW)
            self.assertEqual(first["status"], "fresh")
            self.assertEqual(json.loads((dest/"data/latest.json").read_text())["metrics"]["users_total"],105)
            self.assertTrue((dest/"data/summary.md").exists())
            second=sync_overview(client, "hsk-ai-statistics", ns, dest, NOW+timedelta(days=2))
            self.assertEqual(second["status"],"unavailable")
            self.assertFalse((dest/"data/latest.json").exists())


if __name__ == "__main__":
    unittest.main()
