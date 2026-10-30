"""Synthetic verification of independent source projections and privacy."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from scripts.section_bridge import (
    ROOT, InvalidSnapshot, _sanitize_section, sanitize_section, inventory, sync_sections,
    archive_sections, attach_index,
)
from scripts.sync_from_r2 import save_json
from scripts.build_archive import build_archive
from tests.test_archive import initialize, source as overview_source

NOW = datetime(2026, 10, 30, 9, 30, tzinfo=timezone.utc)
GEN = NOW - timedelta(minutes=10)


def raw_section(section="funnel", period="weekly", metrics=None):
    if metrics is None:
        metrics = {
            "attempt_funnel": {"attempts": 12, "collecting": False,
                               "steps": [{"key": k, "accounts": n} for k, n in [
                                   ("checkout_opened", 10),
                                   ("payment_instructions_viewed", 9),
                                   ("payment_receipt_selected", 7),
                                   ("payment_screenshot_submitted", 4),
                                   ("payment_approved", 2),
                               ]]},
            "registration_conversion": {"registered_accounts": 18, "converted_accounts": 2,
                                        "percent": 11.11},
        }
    since = None if period == "all_time" else GEN - timedelta(days=7 if period == "weekly" else 30)
    return json.dumps({"version": 1, "expires_at": (GEN + timedelta(days=1)).timestamp(),
        "data": {"platform": "overall", "period": period, "generated_at": GEN.isoformat(),
                 "since": since.isoformat() if since else None,
                 "available": True, "source": "first-party stored aggregate projections",
                 "scope": {"section": section}, "metrics": metrics}}).encode()


def feedback_raw(period="weekly"):
    return json.dumps({"version": 1, "expires_at": (GEN + timedelta(days=1)).timestamp(),
        "data": {"schema": "hskai_feedback_categories_v1", "period": period,
                 "generated_at": GEN.isoformat(), "since": (GEN - timedelta(days=7)).isoformat(),
                 "source": "all", "coverage": "most_recent_source_records_in_window",
                 "sample_limit_per_source": 500,
                 "historical_android_reports_complete": False,
                 "sources": [
                     {"source": "bot", "records_scanned": 12, "truncated": False,
                      "categories": [{"category": "limits", "count": 9},
                                     {"category": "price", "count": 3}]},
                     {"source": "release", "records_scanned": 2, "truncated": False,
                      "categories": [{"category": "low_rating", "count": 2}]},
                     {"source": "android", "records_scanned": 1, "truncated": False,
                      "categories": [{"category": "reported_issue", "count": 1}]},
                 ]}}).encode()


class Body:
    def __init__(self, raw): self.raw = raw
    def read(self, n): return self.raw[:n]
    def close(self): pass


class FakeR2:
    def __init__(self, items): self.items = items
    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": k} for k in self.items], "IsTruncated": False}
    def get_object(self, *, Bucket, Key):
        if Key not in self.items:
            err = RuntimeError("missing")
            err.response = {"Error": {"Code": "NoSuchKey"}}
            raise err
        return {"Body": Body(self.items[Key]), "ContentLength": len(self.items[Key])}


class SectionTests(unittest.TestCase):
    def test_source_funnel_is_not_an_overview_slice_and_strips_private_fields(self):
        m = json.loads(raw_section())["data"]["metrics"]
        m["private_user_list"] = [{"user_id": 882, "email": "hidden@example.com"}]
        raw = raw_section(metrics=m)
        doc = sanitize_section(raw, section="funnel", period="weekly", now=NOW, variant="overall_weekly")
        self.assertEqual(doc["metrics"]["steps"][0]["accounts"], 10)
        self.assertEqual(doc["metrics"]["steps"][-1]["accounts"], 2)
        self.assertEqual(doc["metrics"]["attempts"], 12)
        self.assertNotIn("private_user_list", json.dumps(doc))
        self.assertNotIn("hidden@example.com", json.dumps(doc))

    def test_rejects_wrong_scope_old_data_and_bad_funnel(self):
        with self.assertRaises(InvalidSnapshot):
            sanitize_section(raw_section("funnel"), section="course", period="weekly", now=NOW, variant="overall_weekly")
        with self.assertRaises(InvalidSnapshot):
            sanitize_section(raw_section(), section="funnel", period="weekly", now=NOW+timedelta(days=3), variant="overall_weekly")
        broken=json.loads(raw_section())
        broken["data"]["metrics"]["attempt_funnel"]["steps"][1]["accounts"]=15
        with self.assertRaises(InvalidSnapshot):
            sanitize_section(json.dumps(broken).encode(), section="funnel", period="weekly", now=NOW, variant="overall_weekly")

    def test_course_allows_uninstrumented_duration_not_fabricated(self):
        m = {"completed_user_lessons": 21, "detail_limited": True,
             "retention": {"d1": {"eligible": 17, "returned": 5, "percent": 29.4},
                           "d7": {"eligible": 12, "returned": 3, "percent": 25}},
             "activation": None}
        doc=sanitize_section(raw_section("course", metrics=m), section="course",
                             period="weekly", now=NOW, variant="overall_weekly")
        self.assertIsNone(doc["metrics"]["activation"]["eligible"])
        self.assertTrue(doc["metrics"]["detail_limited"])
        self.assertEqual(doc["metrics"]["retention"]["d7"]["returned"],3)

    def test_feedback_never_exports_small_categories(self):
        doc=sanitize_section(feedback_raw(), section="feedback", period="weekly", now=NOW, variant="all_weekly")
        self.assertEqual(doc["metrics"]["categories_at_least_5"], {"limits":9})
        self.assertTrue(doc["metrics"]["coverage"].startswith("latest_"))
        self.assertNotIn("low_rating", json.dumps(doc))

    def test_invalid_duplicate_json_is_rejected(self):
        with self.assertRaises(InvalidSnapshot):
            sanitize_section(b'{"version":1,"version":1}',section="funnel",period="weekly",
                             now=NOW,variant="overall_weekly")

    def test_inventory_sync_is_idempotent_and_legacy_index_kept(self):
        items={
            f"{ROOT}/{'a'*24}/statistics/overall_weekly.json":raw_section(),
            f"{ROOT}/{'b'*24}/feedback/all_weekly.json":feedback_raw(),
        }
        client=FakeR2(items)
        with TemporaryDirectory() as d:
            root=Path(d)
            self.assertEqual(len(inventory(client,"bucket")), 2)
            status=sync_sections(client,"bucket",root,NOW)
            self.assertEqual(len(status["valid"]),2)
            initialize(root,{"weekly":overview_source(generated=GEN)})
            build_archive(root,NOW)
            index_before=json.loads((root/"index.json").read_text())
            added=archive_sections(root,NOW)
            attach_index(root,added)
            result=json.loads((root/"index.json").read_text())
            self.assertEqual(result["sections"],index_before["sections"])
            self.assertEqual(result["independent_analytics"]["funnel"]["status"],"fresh")
            self.assertEqual(result["independent_analytics"]["feedback"]["status"],"fresh")
            self.assertEqual(len(list((root/"analytics/funnel/history").rglob("*.json"))),1)
            archive_sections(root,NOW)
            self.assertEqual(len(list((root/"analytics/funnel/history").rglob("*.json"))),1)
            self.assertNotIn("hidden@example.com",(root/"analytics/funnel/latest.json").read_text())

    def test_expiry_removes_live_alias_and_preserves_history(self):
        client=FakeR2({f"{ROOT}/{'a'*24}/statistics/overall_weekly.json":raw_section()})
        with TemporaryDirectory() as d:
            root=Path(d)
            sync_sections(client,"bucket",root,NOW)
            archive_sections(root,NOW)
            self.assertTrue((root/"analytics/funnel/latest.json").exists())
            sync_sections(client,"bucket",root,NOW+timedelta(days=2))
            meta=archive_sections(root,NOW+timedelta(days=2))
            self.assertEqual(meta["funnel"]["status"],"history_only")
            self.assertFalse((root/"analytics/funnel/latest.json").exists())
            self.assertEqual(len(list((root/"analytics/funnel/history").rglob("*.json"))),1)


if __name__ == "__main__":
    unittest.main()
