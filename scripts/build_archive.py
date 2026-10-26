"""Archive verified HSK AI overview snapshots by section, observation time and period.

The R2 sync produces a sanitized 16-count dataset. This module ONLY slices those
allowlisted counts. It never reads R2, queries PostgreSQL or invents missing data.
History is immutable; expiry removes only convenient "latest" aliases.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re

from scripts.sync_from_r2 import OVERVIEW_PATHS, PERIODS, save_json

SECTION_METRICS = {
    "overview": tuple(OVERVIEW_PATHS),
    "users": (
        "users_total", "new_accounts", "active_accounts", "active_today",
        "bot_blocked", "blocked_current",
    ),
    "sales": ("active_pro", "expiring_pro", "paying_accounts"),
    "payments": (
        "payments_pending", "payments_approved", "payments_rejected", "pending_current",
    ),
    "courses": (
        "course_engaged_accounts", "course_started_accounts", "course_completed_accounts",
    ),
}
SECTIONS = (*SECTION_METRICS, "marketing")
SOURCE = "HSK AI Admin PWA overview aggregate projection"
WINDOWS = {
    "weekly": "rolling_7_days",
    "monthly": "rolling_30_days",
    "all_time": "since_inception_with_some_rolling_current_metrics",
}
MAX_HISTORY_FILES = 20000
_FILENAME = re.compile(r"(\d{4}-\d{2}-\d{2})T(\d{6})(\d{6})Z_(weekly|monthly|all_time)\.json")


def _dt(value):
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid archive timestamp")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("Archive timestamp must have timezone")
    return dt.astimezone(timezone.utc)


def _read_json(path, max_bytes=64 * 1024):
    if path.stat().st_size > max_bytes:
        raise ValueError("Oversized archive source")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_source(data, period, now):
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("Unexpected data schema")
    if (data.get("dataset") != "hskai_admin_overview"
            or data.get("platform") != "overall"
            or data.get("source") != SOURCE
            or data.get("available") is not True
            or data.get("period") != period):
        raise ValueError("Unexpected overview source")
    generated, expires = _dt(data.get("generated_at")), _dt(data.get("expires_at"))
    if not generated <= now < expires or abs((expires - generated - timedelta(days=1)).total_seconds()) > 1:
        raise ValueError("Source no longer fresh")
    since = data.get("since")
    if period == "all_time":
        if since is not None:
            raise ValueError("Invalid all-time window")
    else:
        start = _dt(since)
        if abs((generated - start - timedelta(days=7 if period == "weekly" else 30)).total_seconds()) > 1:
            raise ValueError("Wrong source window")
    metrics = data.get("metrics")
    if not isinstance(metrics, dict) or set(metrics) != set(OVERVIEW_PATHS):
        raise ValueError("Non-allowlisted overview metrics")
    for value in metrics.values():
        if type(value) is not int or not 0 <= value <= 2**63 - 1:
            raise ValueError("Invalid overview metric")
    return generated


def _slice(data, section):
    generated = _dt(data["generated_at"])
    period = data["period"]
    return {
        "schema_version": 1,
        "dataset": "hskai_admin_overview",
        "section": section,
        "platform": "overall",
        "period": period,
        "window": WINDOWS[period],
        "since": data["since"],
        "generated_at": data["generated_at"],
        "observation_date_utc": generated.date().isoformat(),
        "expires_at": data["expires_at"],
        "available": True,
        "source": SOURCE,
        "metrics": {name: data["metrics"][name] for name in SECTION_METRICS[section]},
        "interpretation": {
            "measurements": "mixed current-state and selected-period aggregates",
            "daily_snapshot": "Observation captured this day; NOT counts for this one day",
            "payments": "record counts and distinct paying accounts are different grains",
            "overlapping_windows": "Overlapping 7d/30d snapshots must not be summed",
        },
    }


def _history_path(section, doc):
    generated = _dt(doc["generated_at"])
    stamp = generated.strftime("%Y-%m-%dT%H%M%S%fZ")
    return Path(section) / "history" / generated.strftime("%Y") / generated.strftime("%m") / (stamp + "_" + doc["period"] + ".json")


def _write_immutable(path, doc):
    if path.exists():
        existing = _read_json(path)
        if existing != doc:
            raise ValueError("Historical snapshot differs at the same timestamp")
        return
    save_json(path, doc)


def _entry(section, path, doc):
    return {
        "section": section,
        "path": path.as_posix(),
        "period": doc["period"],
        "window": doc["window"],
        "observation_date_utc": doc["observation_date_utc"],
        "generated_at": doc["generated_at"],
        "since": doc["since"],
        "expires_at": doc["expires_at"],
    }


def _existing_history(base):
    by_month = defaultdict(list)
    by_section = defaultdict(list)
    count = 0
    for section in SECTION_METRICS:
        hist = base / section / "history"
        if not hist.exists():
            continue
        for path in hist.rglob("*.json"):
            count += 1
            if count > MAX_HISTORY_FILES:
                raise ValueError("Too many archived snapshots; add index pagination")
            rel = path.relative_to(base)
            if len(rel.parts) != 5 or rel.parts[:2] != (section, "history"):
                raise ValueError("Unexpected archive folder")
            _, _, year, month, filename = rel.parts
            match = _FILENAME.fullmatch(filename)
            if (not match or match.group(1)[:4] != year
                    or match.group(1)[5:7] != month):
                raise ValueError("Unexpected archive filename")
            doc = _read_json(path)
            if (not isinstance(doc, dict) or doc.get("section") != section
                    or doc.get("period") != match.group(4)
                    or doc.get("available") is not True
                    or set(doc.get("metrics", {})) != set(SECTION_METRICS[section])):
                raise ValueError("Invalid historical section data")
            dt = _dt(doc.get("generated_at"))
            if _history_path(section, doc) != rel:
                raise ValueError("Archive timestamp/path mismatch")
            entry = _entry(section, rel, doc)
            by_month[year + "-" + month].append(entry)
            by_section[section].append(entry)
    return by_month, by_section


def build_archive(base, now=None):
    """Write section latest/period aliases, immutable history, and navigable indexes."""
    base = Path(base)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    status_path = base / "data" / "status.json"
    status = _read_json(status_path)
    if status.get("dataset") != "hskai_admin_overview" or status.get("source_section") != "overview":
        raise ValueError("Unexpected sync status source")
    announced = status.get("valid_periods")
    if not isinstance(announced, list) or any(p not in PERIODS for p in announced) or len(set(announced)) != len(announced):
        raise ValueError("Invalid status periods")
    incoming = {}
    # Validate ALL fresh candidate files before changing output.
    for period in announced:
        path = base / "data" / f"overview_{period}.json"
        doc = _read_json(path)
        _validate_source(doc, period, now)
        incoming[period] = doc

    # Only verified, allowlisted metrics are persisted.
    for section in SECTION_METRICS:
        for period in PERIODS:
            alias = base / section / "periods" / (period + ".json")
            if period in incoming:
                section_doc = _slice(incoming[period], section)
                history_rel = _history_path(section, section_doc)
                _write_immutable(base / history_rel, section_doc)
                save_json(alias, section_doc)
            else:
                alias.unlink(missing_ok=True)
        latest = base / section / "latest.json"
        if "weekly" in incoming:
            save_json(latest, _slice(incoming["weekly"], section))
        else:
            latest.unlink(missing_ok=True)

    by_month, by_section = _existing_history(base)
    all_entries = []
    for month in sorted(by_month):
        entries = sorted(by_month[month], key=lambda r: (r["generated_at"], r["section"], r["period"], r["path"]), reverse=True)
        save_json(base / "indexes" / (month + ".json"), {
            "schema_version": 1, "dataset": "hskai_private_analytics_month_index",
            "month_utc": month, "snapshot_count": len(entries),
            "snapshots": entries,
        })
        all_entries.extend(entries)
    sorted_all = sorted(all_entries, key=lambda r: (r["generated_at"], r["path"]), reverse=True)
    sections = {}
    for section in SECTIONS:
        past = sorted(by_section.get(section, []), key=lambda r: (r["generated_at"], r["path"]), reverse=True)
        sections[section] = {
            "status": ("not_instrumented" if section == "marketing" else
                       "fresh" if "weekly" in incoming else
                       "history_only" if past else "no_data"),
            "latest": (section + "/latest.json") if section != "marketing" and "weekly" in incoming else None,
            "periods": {p: section + "/periods/" + p + ".json" for p in PERIODS if p in incoming} if section != "marketing" else {},
            "historical_snapshots": len(past),
            "most_recent_history": past[0]["path"] if past else None,
            "oldest_history": past[-1]["path"] if past else None,
        }
    root = {
        "schema_version": 1,
        "dataset": "hskai_private_analytics_index",
        "status": "fresh" if "weekly" in incoming else "history_only" if sorted_all else "no_data",
        "source": "validated Admin PWA overview R2 cache",
        "sections": sections,
        "months": [{
            "month_utc": month,
            "index": "indexes/" + month + ".json",
            "snapshots": len(by_month[month]),
        } for month in sorted(by_month, reverse=True)],
        "recent_snapshots": sorted_all[:30],
        "total_historical_snapshots": len(sorted_all),
        "notes": [
            "The date in a filename is the UTC observation/generation date, not a single-day reporting window.",
            "Weekly and monthly figures are rolling windows and MUST NOT be added across snapshots.",
            "Only validated source measurements are published; missing months, days or marketing metrics cannot be inferred.",
            "Archived observations remain after the 24-hour R2 expiry, but historical values must not be presented as live.",
            "Current paid Pro stock and period-specific payment flows use different denominators.",
        ],
    }
    save_json(base / "index.json", root)
    return root


if __name__ == "__main__":
    out = build_archive(Path("."))
    print("Archive index: " + out["status"] + " entries=" + str(out["total_historical_snapshots"]))
