"""Aggregate/derived metrics for dashboards -- all computed on top of data
analyzer.py already produces (gps_path, summary.initial/final_voltage).
Nothing here changes what the analysis engine itself detects or reports;
this only summarizes already-computed per-flight results across many
flights/drones/people."""

import math
from datetime import datetime, time, timezone


def gps_path_length_m(gps_path):
    """Total distance flown: sum of consecutive-point distances along the
    flight's own GPS track -- not the same as summary.max_range_m (farthest
    single point from home), since a flight can travel far in total while
    never getting far from its start point (e.g. a long survey pattern)."""
    if not gps_path or len(gps_path) < 2:
        return None
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(gps_path, gps_path[1:]):
        total += math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2) * 111139
    return round(total, 1)


def battery_sag_pct(summary):
    """(initial - final) / initial, as a percentage. None if voltage data
    isn't available for this flight, rather than a misleading 0."""
    v0 = summary.get("initial_voltage")
    v1 = summary.get("final_voltage")
    if not v0 or v1 is None:
        return None
    try:
        return round((float(v0) - float(v1)) / float(v0) * 100, 2)
    except (TypeError, ZeroDivisionError):
        return None


def derive_result_metrics(report):
    """The three denormalized AnalysisResult columns, computed once at
    upload time from a freshly-produced report dict."""
    summary = report.get("summary", {}) or {}
    return {
        "flight_duration_min": summary.get("flight_duration"),
        "battery_sag_pct": battery_sag_pct(summary),
        "distance_travelled_m": gps_path_length_m(report.get("gps_path")),
    }


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def today_start():
    # Naive UTC, not aware: SQLite has no real datetime type, so
    # Upload.uploaded_at (stored as datetime.now(timezone.utc)) round-trips
    # back from the DB as a naive datetime with its tzinfo dropped --
    # comparing against an aware one here would raise TypeError, and
    # comparing an aware one at the SQL-query level would silently compare
    # wrong due to the mismatched ISO string formats.
    return datetime.combine(datetime.now(timezone.utc).date(), time.min)


def ytd_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, 1, 1)


def drone_aggregate(drone):
    completed = [u for u in drone.uploads if u.status == "completed" and u.analysis]
    results = [u.analysis for u in completed]
    return {
        "total_logs": len(drone.uploads),
        "total_flights": len(completed),
        "avg_flight_time_min": _avg([r.flight_duration_min for r in results]),
        "avg_battery_sag_pct": _avg([r.battery_sag_pct for r in results]),
        "avg_distance_m": _avg([r.distance_travelled_m for r in results]),
        "critical_count": sum(1 for r in results if r.report_status == "CRITICAL"),
        "warning_count": sum(1 for r in results if r.report_status == "WARNING"),
        "pass_count": sum(1 for r in results if r.report_status == "PASS"),
    }


def person_aggregate(user, uploads):
    """`uploads` is this user's full Upload list (caller already queried
    it) -- avoids an extra query per person on a page listing many people."""
    today = today_start()
    ytd = ytd_start()
    completed = [u for u in uploads if u.status == "completed"]
    return {
        "total_logs": len(uploads),
        "logs_today": sum(1 for u in completed if u.uploaded_at and u.uploaded_at >= today),
        "logs_ytd": sum(1 for u in completed if u.uploaded_at and u.uploaded_at >= ytd),
        "last_upload_at": max((u.uploaded_at for u in uploads), default=None),
    }


def daily_counts(uploads, days=14):
    """[(date, count), ...] for the last `days` days, oldest first, zero-
    filled -- for a simple 'logs analyzed per day' admin view."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    buckets = {today - timedelta(days=i): 0 for i in range(days)}
    for u in uploads:
        if u.status != "completed" or not u.uploaded_at:
            continue
        d = u.uploaded_at.date()
        if d in buckets:
            buckets[d] += 1
    return sorted(buckets.items())
