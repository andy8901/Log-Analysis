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


def _series_mean(charts, key, field):
    series = (charts or {}).get(key) or {}
    values = series.get(field)
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def derive_result_metrics(report):
    """The denormalized AnalysisResult columns, computed once at upload
    time from a freshly-produced report dict -- all from data analyzer.py
    already computed (gps_path, report['charts'] time series), nothing new
    extracted from the raw log itself."""
    summary = report.get("summary", {}) or {}
    charts = report.get("charts", {}) or {}
    return {
        "flight_duration_min": summary.get("flight_duration"),
        "battery_sag_pct": battery_sag_pct(summary),
        "distance_travelled_m": gps_path_length_m(report.get("gps_path")),
        "avg_altitude_m": _series_mean(charts, "altitude_baro", "alt") or _series_mean(charts, "altitude_gps", "alt"),
        "avg_battery_voltage_v": _series_mean(charts, "battery", "volt"),
        "avg_current_a": _series_mean(charts, "current", "curr"),
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


def flight_summary_aggregate(uploads):
    """The 'graphical dashboard' panel: total logs reviewed, and org-wide
    (or department-wide, or per-person, depending on what `uploads` the
    caller already scoped) averages across every completed flight in that
    set, plus every flight's takeoff point for a map. Used identically by
    the admin overview, the admin per-department drill-in, and a
    department's own dashboard -- only the input list's scope changes."""
    completed = [u for u in uploads if u.status == "completed" and u.analysis]
    results = [u.analysis for u in completed]

    takeoffs = []
    for u in completed:
        report = u.analysis.get_report()
        home = (report.get("summary") or {})
        lat, lon = home.get("home_lat"), home.get("home_lon")
        if lat is not None and lon is not None:
            takeoffs.append({
                "lat": lat, "lon": lon,
                "drone": u.drone.msn if u.drone else u.msn_entered,
                "department": u.department.name,
                "date": u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else ""),
            })

    return {
        "total_logs": len(uploads),
        "total_flights": len(completed),
        "avg_flight_time_min": _avg([r.flight_duration_min for r in results]),
        "avg_distance_m": _avg([r.distance_travelled_m for r in results]),
        "avg_altitude_m": _avg([r.avg_altitude_m for r in results]),
        "avg_battery_voltage_v": _avg([r.avg_battery_voltage_v for r in results]),
        "avg_current_a": _avg([r.avg_current_a for r in results]),
        "takeoffs": takeoffs,
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


def _naive_now():
    # Matches today_start()/ytd_start(): naive UTC, since Upload.uploaded_at
    # round-trips from SQLite without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def period_window_counts(items, days=7, when=lambda x: x.uploaded_at):
    """(current, previous) counts of `items` falling in the last `days`
    days vs. the `days`-day window before that -- the raw input for a
    '+12% vs last 7 days' style trend badge."""
    from datetime import timedelta

    now = _naive_now()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)
    current = sum(1 for x in items if when(x) and cur_start <= when(x) <= now)
    previous = sum(1 for x in items if when(x) and prev_start <= when(x) < cur_start)
    return current, previous


def trend_pct(current, previous):
    """Percentage change current-vs-previous, or None when there's no prior
    period to compare against (so the template can show a neutral badge
    instead of a misleading +100%)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def active_drone_trend(uploads, days=7):
    """(current, previous) counts of *distinct* drones with at least one
    upload in each window -- an 'active fleet' trend rather than a raw
    upload-count trend."""
    from datetime import timedelta

    now = _naive_now()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)
    cur_drones = {u.drone_id for u in uploads if u.drone_id and u.uploaded_at and cur_start <= u.uploaded_at <= now}
    prev_drones = {u.drone_id for u in uploads if u.drone_id and u.uploaded_at and prev_start <= u.uploaded_at < cur_start}
    return len(cur_drones), len(prev_drones)


def daily_flight_trend(uploads, days=14):
    """[{date, total, successful, anomalies}, ...] oldest first, zero-
    filled -- the 'Flights Trend' line chart. `anomalies` is flights whose
    report came back WARNING or CRITICAL; `successful` is the rest."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    buckets = {today - timedelta(days=i): {"total": 0, "successful": 0, "anomalies": 0} for i in range(days)}
    for u in uploads:
        if u.status != "completed" or not u.uploaded_at:
            continue
        d = u.uploaded_at.date()
        if d not in buckets:
            continue
        buckets[d]["total"] += 1
        status = u.analysis.report_status if u.analysis else None
        if status in ("WARNING", "CRITICAL"):
            buckets[d]["anomalies"] += 1
        else:
            buckets[d]["successful"] += 1
    ordered = sorted(buckets.items())
    return [{"date": d.strftime("%b %d"), **v} for d, v in ordered]


def issue_severity_breakdown(uploads):
    """Counts of completed flights by report_status -- the 'Issue
    Severity' donut. Only the two abnormal tiers are meaningful as a
    breakdown of *issues*; PASS is included for completeness."""
    counts = {"CRITICAL": 0, "WARNING": 0, "PASS": 0}
    for u in uploads:
        if u.status == "completed" and u.analysis and u.analysis.report_status in counts:
            counts[u.analysis.report_status] += 1
    return counts


_ANOMALY_CATEGORIES = (
    ("GPS Loss", ("gps", "satellite", "hdop", "ekf", "fence")),
    ("Motor Issue", ("motor", "esc", "vibration", "vibe")),
    ("Battery", ("battery", "voltage", "volt", "sag", "current", "failsafe")),
    ("Sensor", ("sensor", "compass", "mag", "baro", "imu", "gyro")),
)


def _categorize_alert(text):
    """Buckets an already-produced alert string into a display category,
    purely for the 'Anomaly Types' donut -- reads analyzer.py/rca.py's own
    output text, never re-derives or changes what they flagged."""
    low = (text or "").lower()
    for label, keywords in _ANOMALY_CATEGORIES:
        if any(k in low for k in keywords):
            return label
    return "Other"


def anomaly_type_breakdown(uploads):
    counts = {}
    for u in uploads:
        if u.status != "completed" or not u.analysis or u.analysis.report_status not in ("WARNING", "CRITICAL"):
            continue
        alerts = u.analysis.get_top_alerts()
        label = _categorize_alert(alerts[0]) if alerts else "Other"
        counts[label] = counts.get(label, 0) + 1
    return counts


def recent_anomalies(uploads, limit=6):
    """Most recent flights that came back WARNING/CRITICAL, for the
    'Recent Anomalies' list -- `uploads` is whatever scope the caller
    already filtered to (org-wide, one department, or one person)."""
    candidates = [
        u for u in uploads
        if u.status == "completed" and u.analysis and u.analysis.report_status in ("WARNING", "CRITICAL")
    ]
    candidates.sort(key=lambda u: u.uploaded_at or datetime.min, reverse=True)

    out = []
    for u in candidates[:limit]:
        alerts = u.analysis.get_top_alerts()
        out.append({
            "upload_id": u.id,
            "msn": u.drone.msn if u.drone else u.msn_entered,
            "department": u.department.name if u.department else None,
            "severity": u.analysis.report_status,
            "message": alerts[0] if alerts else "Anomaly detected",
            "date": u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else ""),
        })
    return out


def top_drones_from_uploads(uploads, limit=5, include_department=False):
    """Ranks the drones appearing in `uploads` by issue count -- grouped
    from the already-scoped upload list itself (never Drone.uploads), so a
    department account's own-uploads-only view never pulls in a
    colleague's flights against the same drone."""
    by_drone = {}
    for u in uploads:
        if not u.drone_id:
            continue
        by_drone.setdefault(u.drone_id, []).append(u)

    rows = []
    for drone_id, us in by_drone.items():
        drone = us[0].drone
        completed = [u for u in us if u.status == "completed" and u.analysis]
        issues = sum(1 for u in completed if u.analysis.report_status in ("CRITICAL", "WARNING"))
        last = max(us, key=lambda u: u.uploaded_at or datetime.min)
        row = {
            "drone_id": drone_id,
            "msn": drone.msn if drone else (us[0].msn_entered or "—"),
            "model": drone.model if drone else (us[0].drone_model or "—"),
            "total_flights": len(completed),
            "issues": issues,
            "last_flight": last.flight_date or (last.uploaded_at.strftime("%Y-%m-%d") if last.uploaded_at else "—"),
        }
        if include_department:
            row["department"] = us[0].department.name if us[0].department else "—"
        rows.append(row)

    rows.sort(key=lambda r: (r["issues"], r["total_flights"]), reverse=True)
    return rows[:limit]


def drone_flight_series(uploads):
    """Chronological per-flight series for one drone (duration, battery
    sag, distance) -- the 'drone wise' trend chart on a drone's detail
    page. `uploads` should already be scoped to the viewer's own uploads
    when the caller isn't an admin."""
    completed = sorted(
        (u for u in uploads if u.status == "completed" and u.analysis),
        key=lambda u: u.uploaded_at or datetime.min,
    )
    return {
        "labels": [u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else "") for u in completed],
        "duration": [u.analysis.flight_duration_min for u in completed],
        "battery_sag": [u.analysis.battery_sag_pct for u in completed],
        "distance": [u.analysis.distance_travelled_m for u in completed],
    }
