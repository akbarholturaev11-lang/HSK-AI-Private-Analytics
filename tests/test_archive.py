"""Regression checks for date-based archives and source-grounded category splits."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from scripts.build_archive import build_archive, SECTION_METRICS
from scripts.sync_from_r2 import OVERVIEW_PATHS, save_json

GEN = datetime(2026, 10, 26, 1, 1, tzinfo=timezone.utc)
NOW = GEN + timedelta(minutes=10)


def source(period="weekly", generated=GEN):
    since = None if period == "all_time" else generated - timedelta(days=7 if period == "weekly" else 30)
    return {
        "schema_version": 1,
        "dataset": "hskai_admin_overview",
        "platform": "overall",
        "period": period,
        "since": since.isoformat() if since else None,
        "generated_at": generated.isoformat(),
        "expires_at": (generated + timedelta(days=1)).isoformat(),
        "available": True,
        "source": "HSK AI Admin PWA overview aggregate projection",
        "metrics": {name: n + 1 for n, name in enumerate(OVERVIEW_PATHS)},
    }


def initialize(base, snapshots, status="fresh"):
    (base / "data").mkdir(parents=True, exist_ok=True)
    for period, snapshot in snapshots.items():
        save_json(base / "data" / f"overview_{period}.json", snapshot)
    save_json(base / "data" / "status.json", {
        "dataset": "hskai_admin_overview",
        "source_section": "overview",
        "status": status,
        "valid_periods": list(snapshots),
    })


class ArchiveTests(unittest.TestCase):
    def test_all_sections_and_month_index_are_grounded(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source()})
            idx = build_archive(root, NOW)
            self.assertEqual(idx["total_historical_snapshots"], 5)
            self.assertEqual(idx["months"][0]["month_utc"], "2026-10")
            self.assertEqual(idx["months"][0]["snapshots"], 5)
            self.assertEqual(idx["sections"]["marketing"]["status"], "not_instrumented")
            self.assertIsNone(idx["sections"]["marketing"]["latest"])
            self.assertFalse((root / "marketing" / "latest.json").exists())
            self.assertTrue((root / "index.json").exists())
            self.assertEqual(json.loads((root / "indexes/2026-10.json").read_text())["snapshot_count"], 5)
            for section, names in SECTION_METRICS.items():
                latest = json.loads((root / section / "latest.json").read_text())
                self.assertEqual(set(latest["metrics"]), set(names))
                self.assertEqual(latest["observation_date_utc"], "2026-10-26")
                self.assertEqual(latest["period"], "weekly")
                self.assertEqual(latest["window"], "rolling_7_days")
                self.assertEqual(len(list((root / section / "history").rglob("*.json"))), 1)
                self.assertIn("history/", idx["sections"][section]["most_recent_history"])
                self.assertNotIn("raw_user", (root / section / "latest.json").read_text())

    def test_repeat_same_snapshot_is_idempotent(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source()})
            first = build_archive(root, NOW)
            root_data = (root / "index.json").read_bytes()
            monthly_data = (root / "indexes/2026-10.json").read_bytes()
            second = build_archive(root, NOW + timedelta(minutes=1))
            self.assertEqual(first, second)
            self.assertEqual(root_data, (root / "index.json").read_bytes())
            self.assertEqual(monthly_data, (root / "indexes/2026-10.json").read_bytes())
            self.assertEqual(len(list(root.rglob("*_weekly.json"))), 5)

    def test_same_day_different_snapshots_and_periods_do_not_overwrite(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source(), "monthly": source("monthly")})
            build_archive(root, NOW)
            updated = source(generated=GEN + timedelta(minutes=20))
            initialize(root, {"weekly": updated, "monthly": source("monthly")})
            idx = build_archive(root, NOW + timedelta(minutes=21))
            self.assertEqual(idx["total_historical_snapshots"], 15)
            self.assertEqual(idx["sections"]["courses"]["historical_snapshots"], 3)
            self.assertEqual({p["period"] for p in idx["recent_snapshots"]}, {"weekly", "monthly"})
            self.assertTrue((root / "users/periods/monthly.json").exists())
            self.assertEqual(len(list((root / "users/history").rglob("*.json"))), 3)

    def test_expired_cache_removes_latest_but_preserves_history(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source()})
            build_archive(root, NOW)
            for file in (root / "data").glob("overview_*.json"):
                file.unlink()
            initialize(root, {}, status="unavailable")
            idx = build_archive(root, GEN + timedelta(days=2))
            self.assertEqual(idx["status"], "history_only")
            self.assertEqual(idx["total_historical_snapshots"], 5)
            self.assertFalse((root / "overview/latest.json").exists())
            self.assertFalse((root / "payments/periods/weekly.json").exists())
            self.assertEqual(len(list((root / "overview/history").rglob("*.json"))), 1)

    def test_cross_month_snapshot_date_and_index(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source()})
            build_archive(root, NOW)
            nov = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
            initialize(root, {"weekly": source(generated=nov)})
            idx = build_archive(root, nov + timedelta(minutes=5))
            self.assertEqual([m["month_utc"] for m in idx["months"]], ["2026-11", "2026-10"])
            self.assertEqual(idx["total_historical_snapshots"], 10)
            self.assertTrue((root / "indexes/2026-11.json").exists())
            self.assertTrue((root / "indexes/2026-10.json").exists())

    def test_reject_extra_metric_without_publishing(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            poisoned = source()
            poisoned["metrics"]["private_user_list"] = [{"id": 1}]
            initialize(root, {"weekly": poisoned})
            with self.assertRaisesRegex(ValueError, "Non-allowlisted"):
                build_archive(root, NOW)
            self.assertFalse((root / "index.json").exists())
            self.assertFalse((root / "users/latest.json").exists())

    def test_immutable_timestamp_collision_fails_closed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source()})
            build_archive(root, NOW)
            replacement = source()
            replacement["metrics"]["users_total"] = 600
            initialize(root, {"weekly": replacement})
            with self.assertRaisesRegex(ValueError, "Historical snapshot differs"):
                build_archive(root, NOW)

    def test_only_period_start_is_compared_not_summed(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            initialize(root, {"weekly": source(), "monthly": source("monthly")})
            idx = build_archive(root, NOW)
            weekly = next(e for e in idx["recent_snapshots"] if e["period"] == "weekly")
            monthly = next(e for e in idx["recent_snapshots"] if e["period"] == "monthly")
            self.assertEqual(weekly["window"], "rolling_7_days")
            self.assertEqual(monthly["window"], "rolling_30_days")
            self.assertEqual(weekly["generated_at"], monthly["generated_at"])
            self.assertNotEqual(weekly["since"], monthly["since"])


if __name__ == "__main__":
    unittest.main()
