#!/usr/bin/env python3
"""Read only validated HSK AI overall R2 aggregates; never upload raw R2 objects."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys

PERIODS = ("weekly", "monthly", "all_time")
ROOT = "admin-pwa/aggregates/v1"
MAX_BYTES = 128 * 1024
SOURCE = "AdminMiniAppService + pwa_metrics.overall_activity_metrics"
COUNT_PATHS = {
    "users_total": ("users_total",),
    "new_accounts": ("new_accounts",),
    "active_accounts": ("active_accounts",),
    "payments_pending": ("payments_by_status", "pending"),
    "payments_approved": ("payments_by_status", "approved"),
    "payments_rejected": ("payments_by_status", "rejected"),
    "ai_recorded_requests": ("ai_usage", "recorded_requests"),
    "ai_tokens": ("ai_usage", "tokens"),
    "course_engaged_accounts": ("course_engagement", "engaged_accounts"),
    "course_started_accounts": ("course_engagement", "started_accounts"),
    "course_completed_accounts": ("course_engagement", "completed_accounts"),
    "course_unmatched_events": ("course_engagement", "unmatched_events"),
    "registered_accounts": ("registration_conversion", "registered_accounts"),
    "converted_accounts": ("registration_conversion", "converted_accounts"),
}


class InvalidSnapshot(ValueError):
    pass


def _datetime(value):
    if not isinstance(value, str) or len(value) > 64:
        raise InvalidSnapshot("invalid timestamp")
    try:
        d = datetime.fromisoformat(value)
        if d.tzinfo is None:
            raise ValueError("naive")
        return d.astimezone(timezone.utc)
    except ValueError as exc:
        raise InvalidSnapshot("invalid timestamp") from exc


def _get(obj, keys):
    for key in keys:
        if not isinstance(obj, dict) or key not in obj:
            raise InvalidSnapshot("required field missing")
        obj = obj[key]
    return obj


def _count(value):
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        raise InvalidSnapshot("invalid count")
    return value


def _amount(value, ceiling=2**63 - 1):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= ceiling:
        raise InvalidSnapshot("invalid number")
    return value


def _parse(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise InvalidSnapshot("invalid payload size")

    def no_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise InvalidSnapshot("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys,
                          parse_constant=lambda _: (_ for _ in ()).throw(InvalidSnapshot("nonfinite")))
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise InvalidSnapshot("invalid payload JSON") from exc


def sanitize(raw, period, now):
    if period not in PERIODS or now.tzinfo is None:
        raise InvalidSnapshot("invalid request")
    payload = _parse(raw)
    if not isinstance(payload, dict) or set(payload) != {"version", "expires_at", "data"} or type(payload["version"]) is not int or payload["version"] != 1:
        raise InvalidSnapshot("unsupported envelope")
    data = payload["data"]
    if not isinstance(data, dict) or data.get("platform") != "overall" or data.get("period") != period:
        raise InvalidSnapshot("wrong platform or period")
    if data.get("available") is not True or data.get("source") != SOURCE:
        raise InvalidSnapshot("unsupported source")
    gen = _datetime(data.get("generated_at"))
    exp_epoch = payload["expires_at"]
    if type(exp_epoch) not in (int, float) or not math.isfinite(exp_epoch):
        raise InvalidSnapshot("invalid expiry")
    try:
        expiry = datetime.fromtimestamp(exp_epoch, tz=timezone.utc)
    except (OverflowError, ValueError):
        raise InvalidSnapshot("invalid expiry")
    now = now.astimezone(timezone.utc)
    if abs((expiry - gen - timedelta(days=1)).total_seconds()) > 1 or not now < expiry <= now + timedelta(days=1):
        raise InvalidSnapshot("expired, early or inconsistent expiry")
    if gen > now + timedelta(minutes=2):
        raise InvalidSnapshot("future timestamp")
    if period == "all_time":
        if data.get("since") is not None:
            raise InvalidSnapshot("invalid all-time start")
        since = None
    else:
        since = _datetime(data.get("since"))
        if abs((gen - since - timedelta(days=7 if period == "weekly" else 30)).total_seconds()) > 1:
            raise InvalidSnapshot("invalid period boundaries")
    raw_metrics = data.get("metrics")
    if not isinstance(raw_metrics, dict):
        raise InvalidSnapshot("missing metrics")
    metrics = {name: _count(_get(raw_metrics, path)) for name, path in COUNT_PATHS.items()}
    metrics["ai_estimated_cost_usd"] = _amount(_get(raw_metrics, ("ai_usage", "estimated_cost_usd")))
    conversion = _get(raw_metrics, ("registration_conversion", "percent"))
    metrics["registration_conversion_percent"] = None if conversion is None else _amount(conversion, 100)
    rows = _get(raw_metrics, ("approved_amounts_by_currency",))
    if not isinstance(rows, list) or len(rows) > 32:
        raise InvalidSnapshot("bad currency list")
    currencies = {}
    for row in rows:
        if not isinstance(row, dict):
            raise InvalidSnapshot("bad currency row")
        c = row.get("currency")
        if not isinstance(c, str) or not re.fullmatch(r"[A-Z]{3}", c) or c in currencies:
            raise InvalidSnapshot("bad currency")
        currencies[c] = _amount(row.get("amount"))
    # The output is freshly constructed, NEVER copied from arbitrary R2 fields.
    return {
        "schema_version": 1, "dataset": "hskai_statistics", "platform": "overall",
        "period": period, "generated_at": gen.isoformat(), "expires_at": expiry.isoformat(),
        "since": since.isoformat() if since else None, "available": True, "timezone": "UTC",
        "source": "HSK AI verified aggregate statistics",
        "interpretation": {
            "users_total": "All canonical accounts, including admins",
            "new_accounts": "Registrations within selected period",
            "active_accounts": "Recent activity; all-time uses rolling 30 days",
            "payments": "Payment records, not distinct purchasers",
            "conversion": "Approved Pro conversion of registration cohort; null if empty",
            "ai_cost": "Stored estimate, not provider invoice",
            "currencies": "Original currencies must NOT be summed together",
        },
        "metrics": metrics, "approved_amounts_by_currency": dict(sorted(currencies.items())),
    }


def fetch(client, bucket, key):
    try:
        result = client.get_object(Bucket=bucket, Key=key)
    except Exception as exc:
        response = getattr(exc, "response", {})
        code = str(response.get("Error", {}).get("Code", "")) if isinstance(response, dict) else ""
        if code in ("NoSuchKey", "NotFound", "404"):
            return None
        raise
    body = result["Body"]
    try:
        if int(result.get("ContentLength", MAX_BYTES + 1)) > MAX_BYTES:
            raise InvalidSnapshot("oversized object")
        raw = body.read(MAX_BYTES + 1)
    finally:
        body.close()
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise InvalidSnapshot("oversized read")
    return raw


def save_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def summarize(data):
    m = data["metrics"]
    currency = data["approved_amounts_by_currency"]
    money = ", ".join(f"{code}: {v}" for code, v in currency.items()) or "none recorded"
    conversion = m["registration_conversion_percent"]
    return (
        "# HSK AI — latest weekly aggregated statistics\n\n"
        f"Generated (UTC): **{data['generated_at']}**  \n"
        f"Expires (UTC): **{data['expires_at']}**\n\n"
        f"- Total accounts: **{m['users_total']}**\n"
        f"- New registrations (7d): **{m['new_accounts']}**\n"
        f"- Active accounts (7d): **{m['active_accounts']}**\n"
        f"- Approved payment records (7d): **{m['payments_approved']}**\n"
        f"- Pro registration-cohort conversion: **{conversion if conversion is not None else 'unavailable'}**%\n"
        f"- AI usage requests: **{m['ai_recorded_requests']}**\n"
        f"- Estimated AI cost: **USD {m['ai_estimated_cost_usd']}** (not an invoice)\n"
        f"- Approved payment amounts by currency: **{money}**\n\n"
        "This is a cached snapshot, not live SQL. Counts are not unique buyers; "
        "payments in different currencies must not be summed.\n"
    )


def sync(client, bucket, namespace, base, now):
    if not re.fullmatch(r"[0-9a-f]{24}", namespace):
        raise ValueError("Invalid R2 namespace")
    valid = {}
    validation_errors = []
    # Fetch all first: network failures must not modify previously published files.
    for period in PERIODS:
        key = f"{ROOT}/{namespace}/statistics/overall_{period}.json"
        raw = fetch(client, bucket, key)
        if raw is None:
            continue
        try:
            valid[period] = sanitize(raw, period, now)
        except InvalidSnapshot:
            validation_errors.append(period)
    out = base / "data"
    out.mkdir(parents=True, exist_ok=True)
    for period in PERIODS:
        file = out / f"overall_{period}.json"
        if period in valid:
            save_json(file, valid[period])
        else:
            file.unlink(missing_ok=True)
    if "weekly" in valid:
        save_json(out / "latest.json", valid["weekly"])
        (out / "summary.md").write_text(summarize(valid["weekly"]), encoding="utf-8")
    else:
        (out / "latest.json").unlink(missing_ok=True)
        (out / "summary.md").unlink(missing_ok=True)
    status = {
        "schema_version": 1, "dataset": "hskai_statistics",
        "status": "fresh" if "weekly" in valid else "unavailable",
        "valid_periods": sorted(valid),
        "generation_times": {p: valid[p]["generated_at"] for p in valid},
        "expires_at": {p: valid[p]["expires_at"] for p in valid},
        "invalid_periods": validation_errors,
        "note": "R2 cache updates on first-party Admin PWA demand, not on GitHub schedule.",
    }
    save_json(out / "status.json", status)
    return status


def main():
    account = os.getenv("R2_ACCOUNT_ID", "")
    bucket = os.getenv("R2_BUCKET", "")
    namespace = os.getenv("R2_BASIC_STATS_NAMESPACE", "")
    access = os.getenv("R2_ACCESS_KEY_ID", "")
    secret = os.getenv("R2_SECRET_ACCESS_KEY", "")
    if (not re.fullmatch(r"[0-9a-f]{32}", account)
        or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket)
        or not re.fullmatch(r"[0-9a-f]{24}", namespace)
        or not access or not secret):
        print("R2 configuration missing/invalid; no secrets displayed", file=sys.stderr)
        return 2
    import boto3
    from botocore.config import Config
    client = boto3.client(
        "s3", endpoint_url=f"https://{account}.r2.cloudflarestorage.com",
        region_name="auto", aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=Config(signature_version="s3v4", connect_timeout=5, read_timeout=10,
                      retries={"max_attempts": 2}),
    )
    try:
        status = sync(client, bucket, namespace, Path("."), datetime.now(timezone.utc))
    except Exception as exc:
        # Do not leak request URLs or key names from SDK exceptions.
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error", {})
            meta = response.get("ResponseMetadata", {})
            code = error.get("Code", "") if isinstance(error, dict) else ""
            http = meta.get("HTTPStatusCode") if isinstance(meta, dict) else None
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,48}", code):
                print("R2 sync unavailable: S3 error " + code +
                      (f" (HTTP {http})" if type(http) is int and 100 <= http <= 599 else ""),
                      file=sys.stderr)
            else:
                print("R2 sync unavailable: S3 request rejected", file=sys.stderr)
        else:
            print("R2 sync unavailable: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("R2 sync: " + status["status"] + "; valid variants: " + ",".join(status["valid_periods"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
