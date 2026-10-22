"""Fleet-wide numbers for the dashboards.

Every completed upload gets a small per-flight record (AnalysisResult.fleet_json) combining the three sources
the platform already has, best one first:
  1. report["investigation"]  (investigator.py: crash/verdict, failsafe timing and distance, thrust/GPS/EKF/
     motor/pre-arm flags, cold-start time)
  2. multilog_json            (multilog_analyzer.py: voltages, current, range, failsafe and issue fields; the only
                               source for admin-imported historical rows)
  3. the analyzer.py report    (summary block)
It's computed once and cached; older rows get it on the first dashboard load. Aggregation then works on these
small records only, so filtering by department/drone/period stays fast.
"""
import json
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from app.stats import _ml_num, _ml_text

FLEET_VERSION = 3

ISSUES = OrderedDict([
    ("crash", "Crashes"),
    ("battery_fs", "Battery failsafe"),
    ("gcs_fs", "GCS (telemetry) failsafe"),
    ("rc_fs", "RC failsafe"),
    ("thrust_loss", "Potential thrust loss"),
    ("motor_issue", "Motor issues"),
    ("gps_issue", "GPS issues"),
    ("ekf_issue", "EKF issues"),
    ("prearm_issue", "Pre-arm issues"),
    ("vibration_issue", "High vibration"),
])
CONCLUSIONS = OrderedDict([("system_error", "System error"), ("pilot_error", "Pilot error"),
                           ("undetermined", "Yet to decide"), ("other", "Other"), (None, "Not concluded")])


def _first(*vals):
    for v in vals:
        if v is not None:
            return v
    return None


def _yes(v):
    t = _ml_text(v).lower()
    return t in ("yes", "true", "1", "y")


def _nonempty_flag(v):
    t = _ml_text(v).lower()
    return bool(t) and t not in ("0", "no", "false", "none", "ok", "normal")


def fleet_metrics(report, ml):
    report = report or {}
    ml = ml or {}
    inv = report.get("investigation") or {}
    fig = inv.get("figures") or {}
    flags = inv.get("flags") or {}
    events = inv.get("failsafe_events") or []
    summ = report.get("summary") or {}
    rca = report.get("rca") or {}

    def ev(sub):
        return [e for e in events if e.get("sub") == sub]

    bat, gcs, rc = ev(6), ev(8), ev(5)
    failsafe_type = _ml_text(ml.get("Failsafe Type")).upper()
    warn_text = (_ml_text(ml.get("Warning Messages")) + " " + _ml_text(ml.get("Error Messages"))).lower()
    m = {
        "v": FLEET_VERSION,
        "investigated": bool(inv),
        "inv_version": inv.get("version"),
        "flight_time_min": _first(summ.get("flight_duration") if isinstance(summ.get("flight_duration"), (int, float)) else None,
                                  round(fig["airtime"] / 60, 2) if fig.get("airtime") else None, _ml_num(ml.get("Flight Time (min)"))),
        "distance_m": _first(fig.get("distance"), _ml_num(ml.get("Distance Travelled (m)"))),
        "range_m": _first(fig.get("maxDist"), _ml_num(ml.get("Max Home Distance (m)")), summ.get("max_range_m")),
        "max_alt_m": _first(fig.get("maxAlt"), _ml_num(ml.get("Max Relative Altitude (m)")), summ.get("max_baro_altitude")),
        "max_voltage": _first(_ml_num(ml.get("Max Voltage (V)")), fig.get("vStart"), summ.get("initial_voltage")),
        "min_voltage": _first(_ml_num(ml.get("Min Voltage (V)")), summ.get("min_voltage")),
        "avg_current": _first(_ml_num(ml.get("Avg Current (A)")), summ.get("avg_current")),
        "cold_start_s": flags.get("cold_start_s"),
        "kind": inv.get("kind"),
        "headline": inv.get("title"),
        "crash": bool(flags.get("crash")) if inv else rca.get("incident_level") == "CONFIRMED_CRASH",
        "verdict": (inv.get("blame") or {}).get("primary") if inv.get("verdict") else None,
        "battery_fs_airborne_min": round(bat[0]["airborne_s"] / 60, 2) if bat and bat[0].get("airborne_s") is not None else None,
        "gcs_fs_dist_m": gcs[0]["dist_m"] if gcs else None,
        "rc_fs_dist_m": rc[0]["dist_m"] if rc else None,
        "prearm_count": flags.get("prearm_issues") or 0,
        "vibration_issue": bool(flags.get("vibration_issue")),
        "home": [summ.get("home_lat"), summ.get("home_lon")] if summ.get("home_lat") is not None else (
            [_ml_num(ml.get("Takeoff Latitude")), _ml_num(ml.get("Takeoff Longitude"))] if _ml_num(ml.get("Takeoff Latitude")) else None),
    }
    if flags:
        # The flight investigation read the raw log; its flags are the authority.
        m.update({
            "battery_fs": bool(flags.get("battery_failsafe")) or bool(bat),
            "gcs_fs": bool(flags.get("gcs_failsafe")) or bool(gcs),
            "rc_fs": bool(flags.get("rc_failsafe")) or bool(rc),
            "thrust_loss": bool(flags.get("thrust_loss")),
            "motor_issue": bool(flags.get("motor_issue")),
            "gps_issue": bool(flags.get("gps_issue")),
            "ekf_issue": bool(flags.get("ekf_issue")),
            "prearm_issue": bool(flags.get("prearm_issues")),
        })
    else:
        # Older records without an investigation: fall back to the multilog fields.
        m.update({
            "battery_fs": _yes(ml.get("Battery Failsafe")),
            "gcs_fs": "GCS" in failsafe_type,
            "rc_fs": _yes(ml.get("RC Failsafe")) or "RADIO" in failsafe_type,
            "thrust_loss": _yes(ml.get("Potential Thrust Loss")),
            "motor_issue": _nonempty_flag(ml.get("Motor/ESC Error Flags")),
            "gps_issue": (_ml_num(ml.get("GPS Glitch Count")) or 0) > 0,
            "ekf_issue": (_ml_num(ml.get("EKF Error Count")) or 0) > 0,
            "prearm_issue": "prearm" in warn_text,
        })
    m["battery_fs_voltage"] = _ml_num(ml.get("Min Voltage (V)")) if m["battery_fs"] else None
    return m


def get_fleet(upload, commit_list=None):
    """The cached per-flight record, computed (and queued for saving) if missing or stale."""
    a = upload.analysis
    if a is None:
        return None
    if a.fleet_json:
        try:
            m = json.loads(a.fleet_json)
            if m.get("v") == FLEET_VERSION:
                return m
        except ValueError:
            pass
    try:
        m = fleet_metrics(a.get_report(), a.get_multilog())
    except Exception:
        return None
    a.fleet_json = json.dumps(m)
    if commit_list is not None:
        commit_list.append(a)
    return m


# ----------------------------------------------------------------------------------------------- time windows
def local_tz():
    import os
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(os.environ.get("REPORT_TZ", "Asia/Kolkata"))
    except Exception:
        return timezone.utc


def local_today_start_utc():
    """Midnight today in the organisation's time zone, as the naive-UTC datetime the DB stores."""
    tz = local_tz()
    now = datetime.now(tz)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc).replace(tzinfo=None)


PERIODS = OrderedDict([("all", "All time"), ("today", "Today"), ("7d", "Last 7 days"), ("30d", "Last 30 days"), ("ytd", "This year")])


def period_start(period):
    today = local_today_start_utc()
    if period == "today":
        return today
    if period == "7d":
        return today - timedelta(days=6)
    if period == "30d":
        return today - timedelta(days=29)
    if period == "ytd":
        tz = local_tz()
        return datetime(datetime.now(tz).year, 1, 1, tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return None


# ----------------------------------------------------------------------------------------------- aggregation
def _avg(vals, nd=1):
    v = [x for x in vals if isinstance(x, (int, float))]
    return (round(sum(v) / len(v), nd), len(v)) if v else (None, 0)


def overview(uploads, today=None):
    """All dashboard numbers for a set of uploads (already filtered by department/drone/period/person)."""
    from app import db
    today = today or local_today_start_utc()
    pending_save = []
    completed = [u for u in uploads if u.status == "completed" and u.analysis]
    recs = []
    for u in completed:
        m = get_fleet(u, pending_save)
        if m:
            recs.append((u, m))
    if pending_save:
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()

    def avg(key, nd=1):
        return _avg([m.get(key) for _, m in recs], nd)

    issues = OrderedDict((k, sum(1 for _, m in recs if m.get(k))) for k in ISSUES)
    conclusions = OrderedDict((k, sum(1 for u, _ in recs if (u.conclusion or None) == k)) for k in CONCLUSIONS)
    suggested = Counter(m.get("verdict") for u, m in recs if m.get("verdict") and not u.conclusion)
    today_recs = [(u, m) for u, m in recs if u.uploaded_at and u.uploaded_at >= today]
    active_drones = {u.drone_id for u, _ in recs if u.drone_id}
    kpi = {
        "uploads": len(uploads),
        "flights": len(recs),
        "active_drones": len(active_drones),
        "crashes": issues["crash"],
        "logs_today": len(today_recs),
        "crashes_today": sum(1 for _, m in today_recs if m.get("crash")),
        "failed": sum(1 for u in uploads if u.status == "failed"),
        "pending": sum(1 for u in uploads if u.status in ("queued", "processing")),
        "investigated": sum(1 for _, m in recs if m.get("investigated")),
    }
    averages = OrderedDict([
        ("flight_time_min", ("Avg flight time", "min", *avg("flight_time_min", 1))),
        ("distance_m", ("Avg distance per flight", "m", *avg("distance_m", 0))),
        ("range_m", ("Avg range per flight", "m", *avg("range_m", 0))),
        ("max_alt_m", ("Avg max altitude", "m", *avg("max_alt_m", 1))),
        ("max_voltage", ("Avg max voltage", "V", *avg("max_voltage", 2))),
        ("min_voltage", ("Avg min voltage", "V", *avg("min_voltage", 2))),
        ("avg_current", ("Avg current", "A", *avg("avg_current", 1))),
        ("cold_start_s", ("Avg cold-start time", "s", *avg("cold_start_s", 0))),
    ])
    failsafe = OrderedDict([
        ("battery_fs_airborne_min", ("Avg flight time at battery failsafe", "min", *avg("battery_fs_airborne_min", 1), issues["battery_fs"])),
        ("gcs_fs_dist_m", ("Avg distance at GCS failsafe", "m", *avg("gcs_fs_dist_m", 0), issues["gcs_fs"])),
        ("rc_fs_dist_m", ("Avg distance at RC failsafe", "m", *avg("rc_fs_dist_m", 0), issues["rc_fs"])),
        ("battery_fs_voltage", ("Avg voltage at battery failsafe", "V", *avg("battery_fs_voltage", 2), issues["battery_fs"])),
    ])

    # today's highlights
    def severity(um):
        u, m = um
        st = u.analysis.report_status if u.analysis else ""
        return (0 if m.get("crash") else 1 if st == "CRITICAL" else 2 if st == "WARNING" else 3, -(u.uploaded_at.timestamp() if u.uploaded_at else 0))
    top_today = sorted(today_recs, key=severity)[:6]
    issue_today = Counter(k for _, m in today_recs for k in ISSUES if m.get(k))
    highlights = {
        "logs": len(today_recs),
        "crashes": sum(1 for _, m in today_recs if m.get("crash")),
        "departments": len({u.department_id for u, _ in today_recs}),
        "drones": len({u.drone_id for u, _ in today_recs if u.drone_id}),
        "top_issue": ISSUES[issue_today.most_common(1)[0][0]] if issue_today else None,
        "items": [{"upload_id": u.id, "msn": u.drone.msn if u.drone else u.msn_entered, "department": u.department.name,
                   "headline": m.get("headline") or (u.analysis.report_status if u.analysis else ""),
                   "status": u.analysis.report_status if u.analysis else "", "crash": m.get("crash"),
                   "verdict": m.get("verdict"), "time": u.uploaded_at} for u, m in top_today],
    }
    takeoffs = [{"lat": m["home"][0], "lon": m["home"][1], "drone": u.drone.msn if u.drone else u.msn_entered,
                 "department": u.department.name, "crash": m.get("crash"), "upload_id": u.id}
                for u, m in recs if m.get("home") and m["home"][0] is not None][-400:]
    return {"kpi": kpi, "averages": averages, "failsafe": failsafe, "issues": issues, "issue_labels": ISSUES,
            "conclusions": conclusions, "conclusion_labels": CONCLUSIONS, "suggested": dict(suggested),
            "highlights": highlights, "takeoffs": takeoffs, "records": recs}


def department_rows(departments, uploads, users, drones, today=None):
    today = today or local_today_start_utc()
    by_dept = {}
    for u in uploads:
        by_dept.setdefault(u.department_id, []).append(u)
    rows = []
    for d in departments:
        ups = by_dept.get(d.id, [])
        recs = [(u, m) for u in ups if u.status == "completed" and u.analysis for m in [get_fleet(u)] if m]
        rows.append({
            "department": d,
            "users": sum(1 for x in users if x.department_id == d.id),
            "drones": sum(1 for x in drones if x.department_id == d.id),
            "uploads": len(ups),
            "flights": len(recs),
            "today": sum(1 for u, _ in recs if u.uploaded_at and u.uploaded_at >= today),
            "crashes": sum(1 for _, m in recs if m.get("crash")),
            "issues": sum(1 for _, m in recs if any(m.get(k) for k in ISSUES if k != "crash")),
            "pilot": sum(1 for u, _ in recs if u.conclusion == "pilot_error"),
            "system": sum(1 for u, _ in recs if u.conclusion == "system_error"),
            "undecided": sum(1 for u, _ in recs if u.conclusion == "undetermined"),
            "failed": sum(1 for u in ups if u.status == "failed"),
        })
    return rows


def drone_series(records):
    """Per-flight trend for one drone, oldest first."""
    recs = sorted(records, key=lambda um: um[0].uploaded_at or datetime.min)
    lab = lambda u: u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else str(u.id))  # noqa: E731
    return {
        "labels": [lab(u) for u, _ in recs],
        "flight_time": [m.get("flight_time_min") for _, m in recs],
        "range": [m.get("range_m") for _, m in recs],
        "min_voltage": [m.get("min_voltage") for _, m in recs],
        "max_alt": [m.get("max_alt_m") for _, m in recs],
        "crash": [bool(m.get("crash")) for _, m in recs],
        "ids": [u.id for u, _ in recs],
    }
