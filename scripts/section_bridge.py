"""Read-only, privacy-bounded mirror of EXISTING Admin PWA section caches.

Never commit raw R2 objects, arbitrary fields, account IDs, text or small-cohort
breakdowns. Source-side cache generation is independent and remains on demand.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
import re

from scripts.sync_from_r2 import ROOT, PERIODS, _datetime, _parse, _get, _count, _amount, fetch, save_json, InvalidSnapshot

# Phase-one rollout: independent aggregate projections with known numeric
# contracts; other sections remain unavailable until their own privacy review.
SECTIONS = ("course", "funnel", "sources", "money", "feedback")
_SCOPED_SOURCE = "first-party stored aggregate projections"
_KEY = re.compile(r"^" + re.escape(ROOT) +
                  r"/([a-f0-9]{24})/(statistics|feedback)/((?:overall_(?:weekly|monthly|all_time))|(?:all_(?:weekly|monthly)))\.json$")
_COUNT_LIMIT = 2**63 - 1
_MAX_OBJECTS = 250
_MIN_CELL = 5


def _optional_count(obj, path):
    try:
        value = _get(obj, path)
    except InvalidSnapshot:
        return None
    return None if value is None else _count(value)


def _optional_ratio(obj, path):
    try:
        value = _get(obj, path)
    except InvalidSnapshot:
        return None
    return None if value is None else _amount(value, 100)


def _optional_amount(obj, path):
    try:
        value = _get(obj, path)
    except InvalidSnapshot:
        return None
    return None if value is None else _amount(value, 10**15)


def _optional_bool(obj, key):
    val = obj.get(key)
    if val is not None and type(val) is not bool:
        raise InvalidSnapshot("invalid boolean")
    return val


def _sanitize_section(section, metrics):
    if not isinstance(metrics, dict):
        raise InvalidSnapshot("invalid section metrics")
    if section == "course":
        keep = {
            "completed_user_lessons": ("completed_user_lessons",),
            "sessions": ("sessions",),
            "chat_messages": ("chat", "messages"),
            "chat_accounts": ("chat", "accounts"),
            "ai_requests": ("ai_usage", "recorded_requests"),
            "ai_tokens": ("ai_usage", "tokens"),
            "engaged_accounts": ("course_engagement", "engaged_accounts"),
            "started_accounts": ("course_engagement", "started_accounts"),
            "completed_accounts": ("course_engagement", "completed_accounts"),
        }
        result = {name: _optional_count(metrics, path) for name, path in keep.items()}
        result["detail_limited"] = _optional_bool(metrics, "detail_limited")
        result["retention"] = {}
        for day in ("d1", "d7"):
            result["retention"][day] = {
                "eligible": _optional_count(metrics, ("retention", day, "eligible")),
                "returned": _optional_count(metrics, ("retention", day, "returned")),
                "percent": _optional_ratio(metrics, ("retention", day, "percent")),
            }
        result["activation"] = {
            "eligible": _optional_count(metrics, ("activation", "eligible")),
            "started": _optional_count(metrics, ("activation", "started")),
            "percent": _optional_ratio(metrics, ("activation", "percent")),
        }
        return result
    if section == "funnel":
        f = metrics.get("attempt_funnel")
        result = {
            "registered_accounts": _optional_count(metrics, ("registration_conversion", "registered_accounts")),
            "converted_accounts": _optional_count(metrics, ("registration_conversion", "converted_accounts")),
            "conversion_percent": _optional_ratio(metrics, ("registration_conversion", "percent")),
            "attempts": None,
            "collecting": None,
            "steps": None,
        }
        if f is None:
            return result
        if not isinstance(f, dict) or not isinstance(f.get("steps"), list):
            raise InvalidSnapshot("invalid funnel")
        result["attempts"] = _count(f.get("attempts"))
        result["collecting"] = _optional_bool(f, "collecting")
        expected = ("checkout_opened", "payment_instructions_viewed", "payment_receipt_selected",
                    "payment_screenshot_submitted", "payment_approved")
        if [row.get("key") for row in f["steps"] if isinstance(row, dict)] not in ([], list(expected)):
            raise InvalidSnapshot("invalid funnel stages")
        if len(f["steps"]) not in (0, len(expected)):
            raise InvalidSnapshot("invalid funnel stage count")
        result["steps"] = [{"key": key, "accounts": _count(row.get("accounts"))}
                           for key, row in zip(expected, f["steps"])]
        if any(a["accounts"] < b["accounts"] for a, b in zip(result["steps"], result["steps"][1:])):
            raise InvalidSnapshot("inconsistent funnel")
        return result
    if section == "sources":
        entries = metrics.get("entries")
        attribution = metrics.get("attribution")
        traffic = metrics.get("traffic")
        for rows, field in ((entries, "visits"), (attribution, "payments"), (traffic, "visits")):
            if not isinstance(rows, list) or len(rows) > 100:
                raise InvalidSnapshot("invalid source aggregate")
            for row in rows:
                if not isinstance(row, dict):
                    raise InvalidSnapshot("invalid source row")
                _count(row.get(field))
        # Do not publish rare source categories or user counts; count only
        # source-entry EVENTS, approved payment records and anonymous visits.
        return {
            "subscription_entry_events": sum(row["visits"] for row in entries),
            "approved_payment_records_with_recorded_or_unknown_source": sum(row["payments"] for row in attribution),
            "anonymous_tagged_site_visits": sum(row["visits"] for row in traffic),
            "attribution": "descriptive_only_no_causal_claim",
            "anonymous_traffic_not_accounts": True,
        }
    if section == "money":
        names = ("payments", "canonical_payers", "new_paying_accounts", "renewal_payments",
                 "first_payments", "unpriced_payments", "ai_requests", "tokens")
        result = {key: _optional_count(metrics, (key,)) for key in names}
        result.update({
            "revenue_usd_estimate": _optional_amount(metrics, ("revenue_usd",)),
            "ai_cost_usd_estimate": _optional_amount(metrics, ("ai_usd",)),
            "revenue_is_estimate": True,
        })
        return result
    if section == "feedback":
        raise InvalidSnapshot("feedback requires dedicated validator")
    raise InvalidSnapshot("section not allowlisted")


def sanitize_section(raw, *, section, period, now, variant):
    if section not in SECTIONS or period not in PERIODS or now.tzinfo is None:
        raise InvalidSnapshot("invalid section request")
    if section == "feedback":
        if period == "all_time" or variant != "all_" + period:
            raise InvalidSnapshot("invalid feedback period")
    elif variant != "overall_" + period:
        raise InvalidSnapshot("invalid statistics variant")
    envelope = _parse(raw)
    if (not isinstance(envelope, dict) or set(envelope) != {"version", "expires_at", "data"}
            or type(envelope["version"]) is not int or envelope["version"] != 1):
        raise InvalidSnapshot("invalid cache envelope")
    data = envelope["data"]
    if not isinstance(data, dict):
        raise InvalidSnapshot("invalid section body")
    generated = _datetime(data.get("generated_at"))
    if generated > now.astimezone(timezone.utc) + timedelta(minutes=2):
        raise InvalidSnapshot("future generated timestamp")
    timestamp = envelope["expires_at"]
    if type(timestamp) not in (int, float) or not math.isfinite(timestamp):
        raise InvalidSnapshot("invalid cache expiry")
    try:
        expiry = datetime.fromtimestamp(timestamp, timezone.utc)
    except (ValueError, OverflowError):
        raise InvalidSnapshot("invalid cache expiry") from None
    if (abs((expiry - generated - timedelta(days=1)).total_seconds()) > 1
            or not now.astimezone(timezone.utc) < expiry <= now.astimezone(timezone.utc) + timedelta(days=1)):
        raise InvalidSnapshot("expired or inconsistent cache")
    since = _datetime(data.get("since")) if data.get("since") is not None else None
    if period == "all_time":
        if since is not None:
            raise InvalidSnapshot("invalid all-time since")
    elif since is None or abs((generated - since - timedelta(days=7 if period == "weekly" else 30)).total_seconds()) > 1:
        raise InvalidSnapshot("invalid reporting window")
    if data.get("period") != period:
        raise InvalidSnapshot("period mismatch")
    if section == "feedback":
        if (data.get("schema") != "hskai_feedback_categories_v1" or data.get("source") != "all"
                or data.get("coverage") != "most_recent_source_records_in_window"
                or data.get("sample_limit_per_source") != 500
                or data.get("historical_android_reports_complete") is not False):
            raise InvalidSnapshot("invalid feedback source")
        sources = data.get("sources")
        if not isinstance(sources, list) or len(sources) != 3 or [s.get("source") for s in sources] != ["bot", "release", "android"]:
            raise InvalidSnapshot("invalid feedback sources")
        counts = defaultdict(int)
        scanned = 0
        truncated = False
        allowed = {"price", "limits", "unclear", "pace", "other", "unclassified",
                   "low_rating", "neutral_rating", "positive_rating",
                   "unclassified_rating", "reported_issue"}
        for src in sources:
            n = _count(src.get("records_scanned"))
            if n > 500 or type(src.get("truncated")) is not bool:
                raise InvalidSnapshot("invalid feedback count")
            scanned += n
            truncated = truncated or src["truncated"]
            rows = src.get("categories")
            if not isinstance(rows, list) or len(rows) > 16:
                raise InvalidSnapshot("invalid feedback category list")
            for row in rows:
                if not isinstance(row, dict) or row.get("category") not in allowed:
                    raise InvalidSnapshot("invalid feedback category")
                count = _count(row.get("count"))
                if count > n:
                    raise InvalidSnapshot("invalid feedback category size")
                counts[row["category"]] += count
        # Suppress small categories; do not include total count of suppressed
        # values, so the client cannot subtract and reconstruct them.
        metrics = {"scanned_records": scanned, "truncated": truncated,
                   "categories_at_least_5": {k: v for k, v in sorted(counts.items()) if v >= _MIN_CELL},
                   "coverage": "latest_500_per_source"}
        source = "hskai_feedback_categories_v1"
        out_section = "feedback"
    else:
        if (data.get("platform") != "overall" or data.get("available") is not True
                or data.get("source") != _SCOPED_SOURCE
                or not isinstance(data.get("scope"), dict)
                or data["scope"].get("section") != section):
            raise InvalidSnapshot("wrong analytics source")
        metrics = _sanitize_section(section, data.get("metrics"))
        source = _SCOPED_SOURCE
        out_section = section
    return {
        "schema_version": 1, "dataset": "hskai_verified_r2_sections",
        "section": out_section, "platform": "overall", "period": period,
        "window": {"weekly": "rolling_7_days", "monthly": "rolling_30_days",
                   "all_time": "mixed_lifetime_and_recent_detail"}[period],
        "since": since.isoformat() if since else None,
        "generated_at": generated.isoformat(),
        "observation_date_utc": generated.date().isoformat(),
        "expires_at": expiry.isoformat(), "source": source,
        "available": True, "metrics": metrics,
    }


def inventory(client, bucket):
    """List only aggregate cache keys; never display or persist namespaces."""
    paths = []
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": ROOT + "/", "MaxKeys": 200}
        if token:
            kw["ContinuationToken"] = token
        page = client.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            key = obj.get("Key")
            if not isinstance(key, str):
                raise InvalidSnapshot("invalid object key")
            match = _KEY.fullmatch(key)
            if match:
                paths.append((key, match.group(2), match.group(3)))
            if len(paths) > _MAX_OBJECTS:
                raise InvalidSnapshot("too many aggregate objects")
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
        if not token:
            raise InvalidSnapshot("incomplete object listing")
    return paths


def sync_sections(client, bucket, base, now):
    """Collect and validate all candidates before writing any sanitized files."""
    if now.tzinfo is None:
        raise ValueError("Now must be timezone-aware")
    base = Path(base)
    candidates = {}
    invalid = set()
    for key, kind, variant in inventory(client, bucket):
        raw = fetch(client, bucket, key)
        if raw is None:
            continue
        parsed = _parse(raw)
        data = parsed.get("data") if isinstance(parsed, dict) else None
        if not isinstance(data, dict):
            continue
        section = (data.get("scope") or {}).get("section") if isinstance(data.get("scope"), dict) else None
        if kind == "feedback":
            section = "feedback" if data.get("schema") == "hskai_feedback_categories_v1" else None
        if section not in SECTIONS:
            continue
        period = variant.split("_", 1)[1] if variant.startswith(("overall_", "all_")) else ""
        if period not in PERIODS or (section == "feedback") != (kind == "feedback"):
            continue
        pair = (section, period)
        try:
            doc = sanitize_section(raw, section=section, period=period, now=now, variant=variant)
            if pair in candidates:
                # Never combine multiple deployments with ambiguous namespaces.
                raise InvalidSnapshot("ambiguous aggregate source")
            candidates[pair] = doc
        except InvalidSnapshot:
            invalid.add(pair)
    if any(pair in candidates for pair in invalid):
        raise InvalidSnapshot("ambiguous or invalid duplicate source")
    for section in SECTIONS:
        for period in PERIODS:
            file = base / "data" / "sections" / f"{section}_{period}.json"
            doc = candidates.get((section, period))
            if doc:
                save_json(file, doc)
            else:
                file.unlink(missing_ok=True)
    status = {
        "schema_version": 1, "dataset": "hskai_verified_r2_sections",
        "valid": [{"section": s, "period": p} for s, p in sorted(candidates)],
        "invalid": [{"section": s, "period": p} for s, p in sorted(invalid)],
        "note": "Existing on-demand R2 caches only; no source SQL refresh performed.",
    }
    save_json(base / "data" / "sections" / "status.json", status)
    return status


def _immutable(path, doc):
    if path.exists():
        from scripts.build_archive import _read_json
        if _read_json(path) != doc:
            raise InvalidSnapshot("timestamp collision in section history")
    else:
        save_json(path, doc)


def archive_sections(base, now):
    """Append independent section histories WITHOUT changing legacy overview slices."""
    base = Path(base)
    status_path = base / "data" / "sections" / "status.json"
    if not status_path.exists():
        return {}
    from scripts.build_archive import _read_json, _dt
    status = _read_json(status_path)
    if status.get("dataset") != "hskai_verified_r2_sections":
        raise InvalidSnapshot("invalid section sync status")
    by_section = {}
    for section in SECTIONS:
        for period in PERIODS:
            path = base / "data" / "sections" / f"{section}_{period}.json"
            dest = base / "analytics" / section / "periods" / f"{period}.json"
            if path.exists():
                doc = _read_json(path)
                if (doc.get("section") != section or doc.get("period") != period
                        or doc.get("dataset") != "hskai_verified_r2_sections"
                        or _dt(doc.get("expires_at")) <= now):
                    raise InvalidSnapshot("invalid section archive input")
                stamp = _dt(doc["generated_at"])
                hist = (base / "analytics" / section / "history" /
                        stamp.strftime("%Y") / stamp.strftime("%m") /
                        (stamp.strftime("%Y-%m-%dT%H%M%S%fZ") + "_" + period + ".json"))
                _immutable(hist, doc)
                save_json(dest, doc)
            else:
                dest.unlink(missing_ok=True)
        latest = base / "analytics" / section / "latest.json"
        weekly = base / "analytics" / section / "periods" / "weekly.json"
        if weekly.exists():
            save_json(latest, _read_json(weekly))
        else:
            latest.unlink(missing_ok=True)
        history = sorted((base / "analytics" / section / "history").rglob("*.json")) if (base / "analytics" / section / "history").exists() else []
        by_section[section] = {
            "status": "fresh" if weekly.exists() else "history_only" if history else "not_cached",
            "latest": f"analytics/{section}/latest.json" if weekly.exists() else None,
            "periods": {p: f"analytics/{section}/periods/{p}.json" for p in PERIODS
                        if (base / "analytics" / section / "periods" / f"{p}.json").exists()},
            "historical_snapshots": len(history),
            "most_recent_history": history[-1].relative_to(base).as_posix() if history else None,
            "source": "independent_r2_section",
        }
    return by_section


def attach_index(base, sections):
    """Preserve root legacy schema, attach independent datasets discoverably."""
    base = Path(base)
    from scripts.build_archive import _read_json
    path = base / "index.json"
    current = _read_json(path)
    if current.get("dataset") != "hskai_private_analytics_index":
        raise InvalidSnapshot("unexpected root index")
    current["independent_analytics"] = sections
    save_json(path, current)



def main():
    """GitHub Actions-only entry point: sync reads R2; archive reads local safe JSON."""
    import os
    import sys
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    now = datetime.now(timezone.utc)
    if mode == "archive":
        sections = archive_sections(Path("."), now)
        attach_index(Path("."), sections)
        print("Verified independent analytics archive:", len(sections), "section statuses")
        return 0
    if mode != "sync":
        print("Usage: python -m scripts.section_bridge sync|archive", file=sys.stderr)
        return 2
    account = os.environ.get("R2_ACCOUNT_ID", "")
    bucket = os.environ.get("R2_BUCKET", "")
    access = os.environ.get("R2_ACCESS_KEY_ID", "")
    secret = os.environ.get("R2_SECRET_ACCESS_KEY", "")
    if (not re.fullmatch(r"[0-9a-f]{32}", account) or not access or not secret
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)):
        print("R2 aggregate config unavailable", file=sys.stderr)
        return 2
    import boto3
    from botocore.config import Config
    client = boto3.client("s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        region_name="auto", aws_access_key_id=access, aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=10,
                      retries={"max_attempts": 2}))
    try:
        status = sync_sections(client, bucket, Path("."), now)
    except Exception as exc:
        # No R2 object keys, source content, credentials or network URLs in logs.
        print("Independent R2 sync failed: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("Independent R2 sync:", len(status["valid"]), "fresh section-periods;",
          len(status["invalid"]), "invalid/expired section-periods")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
