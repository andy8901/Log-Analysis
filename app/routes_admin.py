import json
import os
import re
import tempfile
from datetime import datetime, timezone

import pandas as pd
from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.decorators import admin_required
from app.drive_sync import diagnostics as drive_sync_diagnostics
from app.drive_sync import test_write as drive_sync_test_write
from app.models import AnalysisResult, AuditLog, Department, Drone, Upload, User, Warranty, slugify
from app.routes_dept import DRONE_MODEL_CHOICES, known_customers, process_and_save_log
from app.stats import (
    active_drone_trend, conclusion_breakdown, customer_breakdown, drone_aggregate,
    factory_vs_field_comparison, flight_summary_aggregate, daily_flight_trend,
    haversine_km, investigation_aggregate, investigation_category_breakdown,
    investigation_queue, issue_severity_breakdown, location_breakdown,
    location_for_upload, multilog_aggregate, person_aggregate, recent_anomalies,
    reliability_summary, today_start, todays_highlight, top_drones_from_uploads,
    trend_pct, period_window_counts, uploads_for_customer, uploads_for_drone,
    uploads_for_drone_model, uploads_for_location, ytd_start,
)

admin_bp = Blueprint("admin", __name__)


@admin_bp.before_request
@login_required
@admin_required
def _guard():
    # before_request runs before every view in this blueprint; the two
    # decorators above enforce "logged in" + "role == admin" uniformly
    # instead of repeating them on each route.
    pass


@admin_bp.route("/")
def dashboard():
    departments = Department.query.order_by(Department.name).all()
    today = today_start()
    ytd = ytd_start()

    all_uploads = Upload.query.all()
    all_drones_list = Drone.query.order_by(Drone.msn).all()
    all_completed_uploads = [u for u in all_uploads if u.status == "completed" and u.analysis]

    selected_drone_id = request.args.get("drone_id", type=int)
    selected_drone = next((d for d in all_drones_list if d.id == selected_drone_id), None) if selected_drone_id else None
    selected_drone_model = request.args.get("drone_model") or None
    selected_department_id = request.args.get("filter_department_id", type=int)
    selected_department = next((d for d in departments if d.id == selected_department_id), None) if selected_department_id else None
    selected_customer = request.args.get("customer") or None
    selected_location = request.args.get("location") or None

    factory_lat = current_app.config.get("FACTORY_LAT")
    factory_lon = current_app.config.get("FACTORY_LON")
    factory_radius_km = current_app.config.get("FACTORY_RADIUS_KM")
    # A normal page load NEVER makes a live geocoding call -- budget 0
    # means every location lookup this request makes (the scoping below,
    # plus the location_breakdown() chart further down) is cache-only,
    # falling back to "Unclassified" for anything not yet resolved. A live
    # Nominatim call is rate-limited to ~1/second with its own timeout on
    # top (see app/geo.py), so even a handful of uncached points on a
    # dashboard load would have made the whole page wait several seconds
    # to tens of seconds -- exactly the "takes 10 sec to open" regression
    # this fixed. New locations are only ever resolved by the explicit
    # "Resolve Pending Locations" admin action below (resolve_locations()),
    # which the admin runs on demand and which is allowed to take a while.
    geo_budget = [0]

    # Everything below the top totals row (the "interactive analytics"
    # panel: trend charts, donuts, top drones, anomalies, averages) is
    # computed from this scoped list -- the org-wide totals row above it
    # intentionally stays unscoped, since "how many drones/departments
    # exist" shouldn't change just because one dimension is filtered.
    # The five dimensions (drone, drone model, department, customer,
    # location) compose: each is just a filter over whatever the previous
    # one already narrowed down to.
    def _scope(uploads):
        uploads = uploads_for_drone(uploads, selected_drone_id)
        uploads = uploads_for_drone_model(uploads, selected_drone_model)
        if selected_department_id:
            uploads = [u for u in uploads if u.department_id == selected_department_id]
        uploads = uploads_for_customer(uploads, selected_customer)
        uploads = uploads_for_location(uploads, selected_location, factory_lat, factory_lon, factory_radius_km, geo_budget)
        return uploads

    scoped_completed_uploads = _scope(all_completed_uploads)
    scoped_uploads = _scope(all_uploads)

    uploads_by_dept = {}
    drones_by_dept = {}
    for u in all_uploads:
        uploads_by_dept.setdefault(u.department_id, []).append(u)
    for d in all_drones_list:
        drones_by_dept.setdefault(d.department_id, []).append(d)

    rows = []
    for dept in departments:
        dept_uploads = uploads_by_dept.get(dept.id, [])
        completed = [u for u in dept_uploads if u.status == "completed"]
        critical = [u for u in completed if u.analysis and u.analysis.report_status == "CRITICAL"]
        warning = [u for u in completed if u.analysis and u.analysis.report_status == "WARNING"]
        rows.append({
            "department": dept,
            "drones": len(drones_by_dept.get(dept.id, [])),
            "uploads": len(dept_uploads),
            "completed": len(completed),
            "failed": len([u for u in dept_uploads if u.status == "failed"]),
            "critical": len(critical),
            "warning": len(warning),
            "logs_today": len([u for u in completed if u.uploaded_at and u.uploaded_at >= today]),
            "logs_ytd": len([u for u in completed if u.uploaded_at and u.uploaded_at >= ytd]),
        })

    org_investigation = investigation_aggregate(all_completed_uploads)
    totals = {
        "departments": len(departments),
        "users": User.query.count(),
        "drones": len(all_drones_list),
        "uploads": len(all_uploads),
        "logs_today": len([u for u in all_completed_uploads if u.uploaded_at and u.uploaded_at >= today]),
        "logs_ytd": len([u for u in all_completed_uploads if u.uploaded_at and u.uploaded_at >= ytd]),
        "crashes_total": org_investigation["crash_count"],
        "crashes_today": org_investigation["crashes_today"],
    }

    uploads_cur, uploads_prev = period_window_counts(all_uploads, days=7)
    flights_cur, flights_prev = period_window_counts(all_completed_uploads, days=7)
    drones_cur, drones_prev = active_drone_trend(all_uploads, days=7)
    trends = {
        "uploads": trend_pct(uploads_cur, uploads_prev),
        "flights": trend_pct(flights_cur, flights_prev),
        "drones": trend_pct(drones_cur, drones_prev),
    }

    flight_summary = flight_summary_aggregate(scoped_completed_uploads)
    daily_trend = daily_flight_trend(scoped_completed_uploads, days=14)
    severity = issue_severity_breakdown(scoped_completed_uploads)
    anomalies = recent_anomalies(scoped_completed_uploads, limit=6)
    top_drones = top_drones_from_uploads(scoped_uploads, limit=5, include_department=True)
    ml_stats = multilog_aggregate(scoped_completed_uploads)
    inv_stats = investigation_aggregate(scoped_completed_uploads)
    issue_breakdown = investigation_category_breakdown(scoped_completed_uploads)
    conclusion_counts = conclusion_breakdown(scoped_completed_uploads)
    today_highlight = todays_highlight(all_completed_uploads)
    reliability = reliability_summary(scoped_completed_uploads)
    factory_vs_field = factory_vs_field_comparison(
        scoped_completed_uploads, factory_lat, factory_lon, factory_radius_km, geo_budget
    )
    rca_queue = investigation_queue(scoped_completed_uploads, limit=15)

    any_filter_active = bool(
        selected_drone_id or selected_drone_model or selected_department_id
        or selected_customer or selected_location
    )
    dept_chart = [
        {"name": row["department"].name, "flights": row["completed"], "anomalies": row["critical"] + row["warning"]}
        for row in rows
    ] if not any_filter_active else None

    all_customers = known_customers()

    from app.geo import INDIAN_STATES_AND_UTS, KARNATAKA_DISTRICTS

    return render_template(
        "admin/dashboard.html", rows=rows, totals=totals, trends=trends,
        flight_summary=flight_summary, daily_trend=daily_trend,
        severity=severity, anomalies=anomalies, top_drones=top_drones,
        ml_stats=ml_stats, inv_stats=inv_stats, issue_breakdown=issue_breakdown,
        conclusion_counts=conclusion_counts, today_highlight=today_highlight,
        reliability=reliability, factory_vs_field=factory_vs_field, rca_queue=rca_queue,
        dept_chart=dept_chart, drive_sync=drive_sync_diagnostics(),
        all_drones=all_drones_list, selected_drone=selected_drone,
        all_drone_models=DRONE_MODEL_CHOICES, selected_drone_model=selected_drone_model,
        departments=departments, selected_department=selected_department,
        all_customers=all_customers, selected_customer=selected_customer,
        selected_location=selected_location,
        karnataka_districts=KARNATAKA_DISTRICTS, indian_states=INDIAN_STATES_AND_UTS,
        factory_configured=bool(factory_lat is not None and factory_lon is not None),
        customer_chart=customer_breakdown(scoped_completed_uploads),
        location_chart=location_breakdown(scoped_completed_uploads, factory_lat, factory_lon, factory_radius_km, geo_budget),
    )


@admin_bp.route("/drive-sync-test", methods=["POST"])
def drive_sync_test():
    ok, message = drive_sync_test_write()
    AuditLog.record(current_user, "drive_sync_test", f"ok={ok}: {message}")
    db.session.commit()
    flash(message, "success" if ok else "error")
    return redirect(url_for("admin.dashboard"))


@admin_bp.route("/resolve-locations", methods=["POST"])
def resolve_locations():
    """Explicitly geocodes a batch of not-yet-classified flight locations
    (see models.GeoCache / app/geo.py) -- the only place a live,
    rate-limited Nominatim call ever happens; a normal Organization
    Overview page load is cache-only (geo_budget=[0] in dashboard()
    above) specifically so it never blocks on this. Safe to click
    repeatedly: each run picks up wherever the cache currently stands and
    only spends its budget on genuinely new points."""
    factory_lat = current_app.config.get("FACTORY_LAT")
    factory_lon = current_app.config.get("FACTORY_LON")
    factory_radius_km = current_app.config.get("FACTORY_RADIUS_KM")
    if factory_lat is None or factory_lon is None:
        flash("Set FACTORY_LAT/FACTORY_LON in .env first -- location resolving needs them.", "error")
        return redirect(url_for("admin.dashboard"))

    from app.geo import is_cached

    uploads = [u for u in Upload.query.all() if u.status == "completed" and u.analysis]
    budget = [30]
    resolved = 0
    for u in uploads:
        if budget[0] <= 0:
            break
        before = budget[0]
        location_for_upload(u, factory_lat, factory_lon, factory_radius_km, budget)
        if budget[0] < before:
            resolved += 1

    # A point that's already cached is done -- resolved (even if the
    # lookup itself came back empty and it ends up "Unclassified" for
    # good) or factory/no-GPS and never needed geocoding in the first
    # place. Only count ones that still have no GeoCache row at all as
    # genuinely waiting for a turn, so this number actually hits zero
    # once a few runs have cleared the backlog, instead of permanently
    # re-counting every point Nominatim simply couldn't resolve.
    still_pending = 0
    for u in uploads:
        report = u.analysis.get_report()
        summary = (report or {}).get("summary") or {}
        lat, lon = summary.get("home_lat"), summary.get("home_lon")
        if lat is None or lon is None:
            continue
        if haversine_km(lat, lon, factory_lat, factory_lon) <= factory_radius_km:
            continue
        if not is_cached(lat, lon):
            still_pending += 1

    AuditLog.record(current_user, "resolve_locations", f"{resolved} resolved this run, {still_pending} still pending")
    db.session.commit()
    flash(
        f"Resolved {resolved} location(s)."
        + (f" {still_pending} more still pending -- click again to continue." if still_pending else " All caught up."),
        "success",
    )
    return redirect(url_for("admin.dashboard"))


# ---- Global drones / people views --------------------------------------

@admin_bp.route("/drones")
def all_drones():
    department_id = request.args.get("department_id", type=int)
    query = Drone.query
    if department_id:
        query = query.filter_by(department_id=department_id)

    drones = query.order_by(Drone.msn).all()
    rows = [{"drone": d, **drone_aggregate(d)} for d in drones]

    return render_template(
        "admin/drones.html", rows=rows,
        departments=Department.query.order_by(Department.name).all(),
        selected_department_id=department_id,
    )


@admin_bp.route("/people")
def all_people():
    users = User.query.filter_by(role="department").order_by(User.full_name).all()
    all_uploads = Upload.query.all()
    by_user = {}
    for u in all_uploads:
        by_user.setdefault(u.user_id, []).append(u)

    rows = [
        {"user": u, **person_aggregate(u, by_user.get(u.id, []))}
        for u in users
    ]
    return render_template("admin/people.html", rows=rows)


@admin_bp.route("/export/raw-data.xlsx")
def export_raw_data():
    """Multilog Analysis, admin-only: one flat Excel, one row per log
    across every department (real uploads and admin-imported historical
    rows alike), using the full ~150-field extraction (the same per-log
    analysis as the standalone desktop Multilog tool), with Department Name
    prepended. Deliberately not something a department or individual
    account can trigger -- it always runs over data departments have
    *already* uploaded via Individual Log Analysis (or an admin imported),
    never a fresh file upload of its own.

    Prefers each upload's stored AnalysisResult.multilog_json (captured
    once at upload time, or verbatim for an imported row) over re-parsing
    the raw archived log -- faster, and still correct for a row whose raw
    log was since moved/deleted. Only falls back to a live re-parse for a
    pre-existing upload from before multilog_json was captured."""
    from multilog_analyzer import analyze_single_log

    rows = []
    skipped = []

    uploads = (
        Upload.query.filter_by(status="completed")
        .order_by(Upload.uploaded_at.desc())
        .all()
    )
    for u in uploads:
        if not u.analysis:
            skipped.append(f"upload {u.id}: no analysis")
            continue

        result = u.analysis.get_multilog()
        if result is None:
            if not u.archived_path or not os.path.exists(u.archived_path):
                skipped.append(f"upload {u.id}: raw log no longer on disk")
                continue
            result = analyze_single_log(u.archived_path)
            if "_error" in result:
                skipped.append(f"upload {u.id} ({u.original_filename}): {result['_error']}")
                continue

        rows.append({"Department Name": u.department.name, **result})

    df = pd.DataFrame(rows)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df.to_excel(tmp_path, index=False, engine="openpyxl")
        AuditLog.record(
            current_user, "export_raw_data",
            f"{len(rows)} rows, {len(skipped)} skipped",
        )
        db.session.commit()
        if skipped:
            current_app.logger.warning("export_raw_data skipped %d entries: %s", len(skipped), skipped)
        return send_file(tmp_path, as_attachment=True, download_name="all_departments_raw_data.xlsx")
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


_IMPORT_COLUMN_ALIASES = {
    "msn": ("MSN", "Drone MSN", "MSN No", "MSN Number", "Serial Number", "Serial No", "Serial"),
    "model": ("Drone Model", "Model"),
    "department": ("Department Name", "Department"),
    "reported_by": ("Reported By", "Reporter"),
    "flight_date": ("Flight Date", "Date"),
    "filename": ("Log Name", "Filename", "File Name"),
}

# The exact field set multilog_analyzer.analyze_single_log() produces --
# nothing extracted from the log itself ever includes which physical drone
# (MSN) it came from, since a flight log has no idea how this app's fleet
# registry is organized. Used to tell a genuinely new column the admin
# prepended (MSN/Model/Department) apart from the log's own extracted
# fields, positionally, when it isn't given a name this importer already
# recognizes.
_KNOWN_MULTILOG_COLUMNS = {
    "Log Name", "Date", "Log Start Time", "Log End Time",
    "Overall Endurance / Log Duration (sec)", "Arm Start Time", "Disarm End Time",
    "Armed Flight Duration / Flight Endurance (sec)", "Flight Time (min)",
    "Arm Events Count", "Disarm Events Count", "Flight Mode", "Mode Change Count",
    "Takeoff Time", "Landing Time", "Takeoff Latitude", "Takeoff Longitude",
    "Landing Latitude", "Landing Longitude", "Latitude", "Longitude", "GPS Fix Type",
    "Avg Sat Count", "Min Sat Count", "Max Sat Count", "Max HDOP", "Avg HDOP",
    "GPS Accuracy Horizontal", "GPS Accuracy Vertical", "GPS Ground Speed Avg",
    "GPS Ground Speed Max", "GPS Course", "GPS Glitch Count", "GPS Glitch Duration (sec)",
    "Distance Travelled (m)", "Max Home Distance (m)", "Max Baro Alt (m)",
    "Avg Baro Alt (m)", "Max GPS Alt (m)", "Avg GPS Alt (m)", "Max Relative Altitude (m)",
    "Avg Relative Altitude (m)", "Max Climb Rate (m/s)", "Max Descent Rate (m/s)",
    "Avg Rangefinder (m)", "Min Rangefinder (m)", "Max Rangefinder (m)",
    "Lidar / 6-Side Status", "Lidar (6-Side Status)", "Min Voltage (V)", "Max Voltage (V)",
    "Avg Voltage (V)", "Avg Current (A)", "Max Current (A)", "Battery SOC (%)",
    "Consumed Battery (mAh)", "Battery Temperature", "Min Cell Voltage", "Max Cell Voltage",
    "Cell Voltage Difference", "Battery Resistance", "Max Power (W)", "Battery Failsafe",
    "ax_avg", "ay_avg", "az_avg", "ax_max", "ay_max", "az_max",
    "gx_avg", "gy_avg", "gz_avg", "gx_max", "gy_max", "gz_max",
    "Vibration X Avg", "Vibration Y Avg", "Vibration Z Avg", "vib_avg", "vib_max", "vib_rms",
    "IMU Clipping X", "IMU Clipping Y", "IMU Clipping Z",
    "Roll Max", "Roll Min", "Pitch Max", "Pitch Min", "Yaw Max", "Yaw Min",
    "roll_max", "pitch_max", "yaw_max",
    "Desired Roll Max", "Desired Pitch Max", "Desired Yaw Max", "Desired Altitude Max",
    "Roll Tracking Error", "Pitch Tracking Error", "Yaw Tracking Error",
    "Altitude Tracking Error", "Actual Climb Rate", "Desired Climb Rate",
    "Desired Ground Speed", "Actual Ground Speed",
    "RC Roll Input", "RC Pitch Input", "RC Throttle Input", "RC Yaw Input", "RC RSSI",
    "RC Link Quality", "RC Failsafe", "RC Signal Loss Count", "RC Signal Loss Duration (sec)",
    "RC Override Events", "Motor Output Imbalance", "Motor/ESC Error Flags",
    "ESC Voltage", "ESC Current", "ESC Temperature", "ESC RPM Error",
    "Thrust Saturation Events", "Potential Thrust Loss",
    "EKF Error Count", "EKF Reset Count", "EKF Lane Switches", "Active EKF Lane",
    "EKF Source", "EKF Velocity Variance", "EKF Position Variance", "EKF Height Variance",
    "EKF Compass Variance", "EKF Yaw Variance", "Yaw Reset Events", "Compass Error Count",
    "Compass Variance", "Compass Health", "Failsafe Event Count", "Failsafe Type",
    "Failsafe Duration (sec)", "RTL Events", "Auto Land Events",
    "Emergency Stop / Disarm Events", "Telemetry RSSI", "Telemetry Link Quality",
    "Telemetry Packet Loss", "Telemetry Link Loss Count", "Telemetry Link Loss Duration",
    "GCS Connection Status", "GCS Latency", "Gimbal Pitch", "Gimbal Yaw",
    "Camera Recording Status", "Payload Communication Status", "Payload Health",
    "Sensor Health", "Critical Messages", "Error Messages", "Warning Messages",
    "Event Messages", "Parameter Change Events", "Reboot Events", "Firmware Version",
    "Flight Controller Version", "GPS Module / Firmware Information", "Total Error Count",
    "Total Warning Count", "Critical Event Timestamp(s)", "Flight Duration (sec)",
} | {
    f"RFND_{i}_{stat} (m)" for i in range(6) for stat in ("Avg", "Max", "Min")
} | {
    f"Motor {n} RPM {stat}" for n in range(1, 5) for stat in ("Avg", "Min", "Max")
} | {
    f"Motor {n} Output {stat}" for n in range(1, 5) for stat in ("Avg", "Max")
}


def _find_import_column(columns, field):
    lower_map = {str(c).strip().lower(): c for c in columns}
    for alias in _IMPORT_COLUMN_ALIASES[field]:
        if alias.lower() in lower_map:
            return lower_map[alias.lower()]
    return None


_MSN_IN_FILENAME_RE = re.compile(r"MSN[_\s-]?(\d+)", re.I)
# A second, organization-specific convention seen in practice: the MSN as
# a bare number directly in front of "_log" (no "MSN" word at all), e.g.
# "NIMBLE CRPF 009_log_10_2025-6-26-11-01-22.bin" -- MSN 009. Tried only
# after the "MSN_<number>" pattern above finds nothing.
_MSN_BEFORE_LOG_RE = re.compile(r"(\d+)_log(?![a-z])", re.I)


def _extract_msn_from_filename(name):
    """Some organizations' own log-naming convention embeds the drone's
    MSN right in the filename -- either "MSN_064_log_23_2026-2-21...bin"
    or "<Model> <Customer> 064_log_23_...bin" (MSN directly before
    "_log", no "MSN" word) -- a per-row fallback tried when there's no MSN
    column at all, named or positional. Returns None (not a guess) for a
    filename that doesn't match either, e.g. ones filed under an incident
    folder name instead ("Crash_log_...", "Testing Logs (...)_log_...")."""
    if not name:
        return None
    m = _MSN_IN_FILENAME_RE.search(name)
    if m:
        return m.group(1)
    m = _MSN_BEFORE_LOG_RE.search(name)
    return m.group(1) if m else None


def _positional_fallback_columns(columns):
    """When MSN/Model/Department aren't found by name, check whether the
    admin instead prepended them as extra columns ahead of the standard
    Multilog extraction fields (the admin's own stated convention: "first
    2-3 columns will have msn no") -- any column among the first three that
    isn't part of _KNOWN_MULTILOG_COLUMNS is assumed, in that order, to be
    MSN, then Drone Model, then Department. Only ever fills in whichever of
    these name-based matching didn't already find."""
    extras = [c for c in columns[:3] if str(c).strip() not in _KNOWN_MULTILOG_COLUMNS]
    return dict(zip(("msn", "model", "department"), extras))


def _import_num(value):
    try:
        if value is None:
            return None
        f = float(value)
        return None if f != f else f  # NaN != NaN
    except (TypeError, ValueError):
        return None


def _import_text(row, *keys):
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip().lower() not in ("", "nan", "none"):
            return str(v).strip()
    return None


def _build_imported_report(row):
    """Reshapes one Multilog-format Excel row into the same minimal shape
    the rest of the app reads off a report dict (summary/alerts/status) --
    there's no raw log behind an imported row, so no charts/gps_path/
    timeline, only what the row itself carries."""
    summary = {
        "flight_duration": _import_num(row.get("Flight Time (min)")),
        "max_range_m": _import_num(row.get("Max Home Distance (m)")),
        "home_lat": _import_num(row.get("Takeoff Latitude")),
        "home_lon": _import_num(row.get("Takeoff Longitude")),
        "initial_voltage": _import_num(row.get("Max Voltage (V)")),
        "final_voltage": _import_num(row.get("Min Voltage (V)")),
    }

    alerts = []
    for col in ("Critical Messages", "Error Messages", "Warning Messages"):
        text = _import_text(row, col)
        if text:
            alerts.append(f"{col.upper()}: {text}")
    thrust = _import_text(row, "Potential Thrust Loss")
    if thrust:
        alerts.append(f"POTENTIAL THRUST LOSS: {thrust}")

    battery_failsafe = str(row.get("Battery Failsafe", "No")).strip().lower() == "yes"
    failsafe_count = _import_num(row.get("Failsafe Event Count")) or 0
    if battery_failsafe or failsafe_count > 0:
        status = "CRITICAL"
    elif alerts:
        status = "WARNING"
    else:
        status = "PASS"

    report = {
        "status": status, "alerts": alerts, "summary": summary,
        "charts": {}, "gps_path": [], "gps_path_time": [], "timeline": [],
    }

    max_v, min_v = _import_num(row.get("Max Voltage (V)")), _import_num(row.get("Min Voltage (V)"))
    battery_sag_pct = round((max_v - min_v) / max_v * 100, 2) if max_v else None

    metrics = {
        "flight_duration_min": _import_num(row.get("Flight Time (min)")),
        "battery_sag_pct": battery_sag_pct,
        "distance_travelled_m": _import_num(row.get("Distance Travelled (m)")),
        "avg_altitude_m": _import_num(row.get("Avg Baro Alt (m)")) or _import_num(row.get("Avg GPS Alt (m)")),
        "avg_battery_voltage_v": _import_num(row.get("Avg Voltage (V)")),
        "avg_current_a": _import_num(row.get("Avg Current (A)")),
    }
    return report, metrics


@admin_bp.route("/import-raw-data", methods=["GET", "POST"])
def import_raw_data():
    """Lets an admin backfill historical flights that only exist as an
    already-extracted Excel (the same Multilog-format export this app's
    own Export Raw Data produces) -- e.g. logs from before this platform
    existed, or whose raw .bin is gone. Each row becomes an Upload +
    AnalysisResult with source="imported": no raw log, no 3D replay, but
    it counts in every dashboard aggregate exactly like a real upload,
    since those all read AnalysisResult the same way regardless of origin.

    Column matching is best-effort and case-insensitive (see
    _IMPORT_COLUMN_ALIASES): MSN is required per row (the file's own
    column, if present) -- a row with no resolvable MSN is skipped, never
    guessed. Department comes from the file's own column when present,
    otherwise every row uses the department selected on this form."""
    departments = Department.query.order_by(Department.name).all()

    if request.method == "GET":
        return render_template("admin/import_raw_data.html", departments=departments)

    file = request.files.get("data_file")
    fallback_dept_id = request.form.get("department_id", type=int)

    if not file or not file.filename:
        flash("No file selected", "error")
        return redirect(url_for("admin.import_raw_data"))

    try:
        df = pd.read_excel(file, engine="openpyxl")
    except Exception as exc:
        flash(f"Could not read that file as an Excel sheet: {exc}", "error")
        return redirect(url_for("admin.import_raw_data"))

    columns = list(df.columns)
    msn_col = _find_import_column(columns, "msn")
    model_col = _find_import_column(columns, "model")
    dept_col = _find_import_column(columns, "department")
    reported_col = _find_import_column(columns, "reported_by")
    date_col = _find_import_column(columns, "flight_date")
    filename_col = _find_import_column(columns, "filename")

    # Fall back to position for whichever of MSN/Model/Department wasn't
    # found by name: a standard Multilog export never names these (the
    # flight log itself has no idea which drone it came from), so an admin
    # who prepended them as extra columns may not have used a name this
    # importer recognizes.
    if msn_col is None or model_col is None or dept_col is None:
        positional = _positional_fallback_columns(columns)
        msn_col = msn_col or positional.get("msn")
        model_col = model_col or positional.get("model")
        dept_col = dept_col or positional.get("department")

    # Last resort for MSN specifically: some organizations' own log-naming
    # convention already embeds it in the filename (see
    # _extract_msn_from_filename) -- tried per row below, so this only
    # blocks the import outright when there's truly nothing to go on for
    # *any* row.
    if msn_col is None and filename_col is None:
        flash(
            "No MSN column found, and no Log Name/Filename column to extract one from "
            "(e.g. \"MSN_064_log_23...\"). Add an MSN column (named MSN, Drone MSN, "
            "Serial Number, etc., or as one of the first 3 columns), or a filename "
            "column whose values embed MSN_<number>.",
            "error",
        )
        return redirect(url_for("admin.import_raw_data"))

    dept_by_name = {d.name.strip().lower(): d for d in departments}
    fallback_dept = next((d for d in departments if d.id == fallback_dept_id), None)

    imported = 0
    skipped = []

    for i, row in df.iterrows():
        row = row.to_dict()
        msn = _import_text(row, msn_col) if msn_col else None
        if not msn and filename_col:
            msn = _extract_msn_from_filename(_import_text(row, filename_col))
        if not msn:
            skipped.append(f"row {i + 2}: no MSN (not in a column, and not found as MSN_<number> in the filename)")
            continue

        department = fallback_dept
        if dept_col:
            name = _import_text(row, dept_col)
            if name:
                department = dept_by_name.get(name.strip().lower())
                if department is None:
                    skipped.append(f"row {i + 2}: unknown department '{name}'")
                    continue
        if department is None:
            skipped.append(f"row {i + 2}: no department (select one on the form, or add a Department column)")
            continue

        drone = Drone.query.filter_by(msn=msn, department_id=department.id).first()
        if drone is None:
            model = _import_text(row, model_col) or "UNKNOWN"
            drone = Drone(msn=msn, model=model, department_id=department.id)
            db.session.add(drone)
            db.session.flush()

        flight_date = _import_text(row, date_col)
        filename = _import_text(row, filename_col) or f"imported_row_{i + 2}"

        upload_row = Upload(
            department_id=department.id,
            user_id=current_user.id,
            drone_id=drone.id,
            original_filename=filename,
            drone_model=drone.model,
            msn_entered=msn,
            flight_date=flight_date,
            reported_by=_import_text(row, reported_col),
            status="completed",
            source="imported",
            analyzed_at=datetime.now(timezone.utc),
        )
        db.session.add(upload_row)
        db.session.flush()

        report, metrics = _build_imported_report(row)
        analysis = AnalysisResult(
            upload_id=upload_row.id,
            report_status=report["status"],
            alert_count=len(report["alerts"]),
            top_alerts=json.dumps(report["alerts"][:5]),
            report_json=json.dumps(report),
            multilog_json=json.dumps(row, default=str),
            **metrics,
        )
        db.session.add(analysis)
        imported += 1

    AuditLog.record(
        current_user, "import_raw_data",
        f"{imported} row(s) imported, {len(skipped)} skipped",
    )
    db.session.commit()

    if imported:
        flash(f"Imported {imported} historical flight(s).", "success")
    if skipped:
        preview = "; ".join(skipped[:10]) + (f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else "")
        flash(f"Skipped {len(skipped)} row(s): {preview}", "error")
    if not imported and not skipped:
        flash("The file had no rows to import.", "error")

    return redirect(url_for("admin.import_raw_data"))


@admin_bp.route("/batch-upload")
def batch_upload():
    """Lets an admin run a whole folder of raw logs through the same
    processing pipeline a department's own Upload Log page uses
    (process_and_save_log() in routes_dept.py -- analyzer.py,
    investigator.py and multilog_analyzer.py, all untouched), instead of
    one-at-a-time per department. Each file becomes its own Upload +
    AnalysisResult, exactly like a real department upload, so it shows up
    in every dashboard aggregate the same way.

    The page itself (batch_upload.html) drives this with JavaScript: the
    browser's folder picker (webkitdirectory) submits every file inside as
    a flat multi-file list, and the page POSTs them one at a time to
    batch_upload_file() below, updating a live per-file status table and
    an overall % complete as each one finishes -- a single synchronous
    all-files-at-once POST would leave the browser showing nothing but a
    spinner for however long the whole folder takes."""
    departments = Department.query.order_by(Department.name).all()
    return render_template(
        "admin/batch_upload.html", departments=departments, model_choices=DRONE_MODEL_CHOICES,
        known_customers=known_customers(),
    )


@admin_bp.route("/batch-upload/file", methods=["POST"])
def batch_upload_file():
    """Processes exactly one file from the batch-upload page's live
    uploader -- returns JSON instead of a redirect so the page can show
    per-file progress instead of blocking on the whole folder. Reuses
    process_and_save_log(), the exact same pipeline a real department
    upload runs.

    MSN is resolved per file, not required on the form: an organization's
    own filename convention (MSN_<number>_...) is tried first (the same
    fallback already used by Import Historical Data), falling back to
    whatever MSN the admin typed on the form (useful when every file in
    the folder really is the same one drone). A file that resolves to
    neither is reported back as skipped, not a hard failure for the whole
    batch."""
    department_id = request.form.get("department_id", type=int)
    department = Department.query.get(department_id) if department_id else None
    drone_model = request.form.get("drone", "").strip()
    manual_msn = request.form.get("msn", "").strip()
    flight_date = request.form.get("date", "").strip()
    reported_by = request.form.get("reported_by", "").strip() or current_user.full_name
    customer_name = request.form.get("customer", "").strip()

    file = request.files.get("log_file")
    filename = file.filename if file and file.filename else "(no file)"

    if department is None:
        return jsonify(ok=False, filename=filename, error="Invalid department")

    resolved_msn = _extract_msn_from_filename(filename) or manual_msn
    if not resolved_msn:
        return jsonify(
            ok=False, filename=filename,
            error="No MSN found in filename (MSN_<number>), and none entered on the form",
        )

    try:
        upload_row = process_and_save_log(
            file, department, drone_model, resolved_msn, flight_date, reported_by, current_user,
            customer_name=customer_name,
        )
    except ValueError as exc:
        return jsonify(ok=False, filename=filename, msn=resolved_msn, error=str(exc))

    db.session.commit()

    if upload_row.status == "failed":
        return jsonify(ok=False, filename=filename, msn=resolved_msn, error=upload_row.error_message)

    AuditLog.record(current_user, "batch_upload", f"department={department.name} msn={resolved_msn} upload={upload_row.id}")
    db.session.commit()

    return jsonify(
        ok=True, filename=filename, msn=resolved_msn, upload_id=upload_row.id,
        report_status=upload_row.analysis.report_status if upload_row.analysis else None,
        archived_name=os.path.basename(upload_row.archived_path) if upload_row.archived_path else None,
        view_url=url_for("dept.view_report", upload_id=upload_row.id),
    )


_WARRANTY_COLUMN_ALIASES = {
    "customer": ("Customer Name", "Customer"),
    "product": ("Product Name", "Product", "Model"),
    "msn": ("MSN No", "MSN", "MSN Number", "Serial Number"),
    "purchase_date": ("Purchase Date",),
    "warranty_months": ("Warranty (Months)", "Warranty Months", "Warranty"),
}


def _find_warranty_column(columns, field):
    lower_map = {str(c).strip().lower(): c for c in columns}
    for alias in _WARRANTY_COLUMN_ALIASES[field]:
        if alias.lower() in lower_map:
            return lower_map[alias.lower()]
    return None


def _import_warranty_dataframe(df):
    """Upserts every row of an already-parsed warranty tracker DataFrame
    into the Warranty table -- the common path behind both the file-upload
    and Google Sheet sync forms below. Returns (imported, updated, skipped
    reasons) rather than touching flash()/redirect() itself, so each
    caller can report the result in its own way."""
    columns = list(df.columns)
    customer_col = _find_warranty_column(columns, "customer")
    product_col = _find_warranty_column(columns, "product")
    msn_col = _find_warranty_column(columns, "msn")
    purchase_col = _find_warranty_column(columns, "purchase_date")
    months_col = _find_warranty_column(columns, "warranty_months")

    if customer_col is None or msn_col is None:
        raise ValueError(
            "Need at least a Customer Name column and an MSN No column "
            "(case-insensitive; also accepts \"Customer\"/\"MSN\")."
        )

    imported = updated = 0
    skipped = []

    for i, row in df.iterrows():
        row = row.to_dict()
        customer = _import_text(row, customer_col)
        msn = _import_text(row, msn_col)
        if not customer or not msn:
            skipped.append(f"row {i + 2}: missing customer and/or MSN")
            continue

        purchase_date = pd.to_datetime(row.get(purchase_col), errors="coerce") if purchase_col else None
        purchase_date = purchase_date.date() if purchase_date is not None and not pd.isna(purchase_date) else None
        months = _import_num(row.get(months_col)) if months_col else None

        existing = Warranty.query.filter_by(msn=msn, customer_name=customer).first()
        if existing is None:
            existing = Warranty(msn=msn, customer_name=customer)
            db.session.add(existing)
            imported += 1
        else:
            updated += 1

        existing.product_name = _import_text(row, product_col) if product_col else existing.product_name
        if purchase_date is not None:
            existing.purchase_date = purchase_date
        if months is not None:
            existing.warranty_months = int(months)

    return imported, updated, skipped


def _flash_warranty_import_result(imported, updated, skipped):
    if imported or updated:
        flash(f"Imported {imported} new warranty record(s), updated {updated} existing.", "success")
    if skipped:
        preview = "; ".join(skipped[:10]) + (f" (+{len(skipped) - 10} more)" if len(skipped) > 10 else "")
        flash(f"Skipped {len(skipped)} row(s): {preview}", "error")
    if not imported and not updated and not skipped:
        flash("No rows to import.", "error")


@admin_bp.route("/import-warranty", methods=["GET", "POST"])
def import_warranty():
    """Bulk-loads the external warranty tracker spreadsheet into the
    Warranty table (see models.Warranty / app/warranty.py) -- one row per
    (customer, MSN), used to show live warranty status on a flight's
    report page. Only Customer Name/Product Name/MSN No/Purchase Date/
    Warranty (Months) are read; any Expiry/Days Left/Status/Drone Age
    columns in the sheet are ignored and recomputed live instead, since
    those go stale the moment they're calculated relative to "today".

    Upserts by (msn, customer_name) -- re-importing an updated tracker
    (corrected purchase date, renewed warranty, ...) just overwrites the
    matching row instead of creating a duplicate. See import_warranty_sheet()
    below for syncing straight from a shared Google Sheet instead of a
    manually-exported file."""
    if request.method == "GET":
        return render_template("admin/import_warranty.html", default_sheet_url=DEFAULT_WARRANTY_SHEET_URL)

    file = request.files.get("data_file")
    if not file or not file.filename:
        flash("No file selected", "error")
        return redirect(url_for("admin.import_warranty"))

    try:
        df = pd.read_excel(file, engine="openpyxl")
    except Exception as exc:
        flash(f"Could not read that file as an Excel sheet: {exc}", "error")
        return redirect(url_for("admin.import_warranty"))

    try:
        imported, updated, skipped = _import_warranty_dataframe(df)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("admin.import_warranty"))

    AuditLog.record(current_user, "import_warranty", f"file: {imported} new, {updated} updated, {len(skipped)} skipped")
    db.session.commit()
    _flash_warranty_import_result(imported, updated, skipped)
    return redirect(url_for("admin.import_warranty"))


# The link shared for this org's live warranty tracker -- pre-fills the
# "Sync from Google Sheet" field on the Import Warranty Data page so an
# admin doesn't have to paste it in every time; still overridable there
# for a different sheet.
DEFAULT_WARRANTY_SHEET_URL = "https://docs.google.com/spreadsheets/d/1cDK_aAGt52zYKHy5MW_2IF5ySX-p24PzfXhHlK1rgSk/edit?gid=1581997623#gid=1581997623"

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")
_SHEET_GID_RE = re.compile(r"[#&?]gid=(\d+)")


def _sheet_csv_url(share_url):
    """A normal Google Sheets share/edit URL -> the CSV export URL for the
    one tab it points at (its gid, defaulting to the first tab, 0, if the
    URL doesn't name one) -- requires the sheet be shared as "Anyone with
    the link can view" (or published to the web); there's no OAuth wired
    up here for a private sheet."""
    id_match = _SHEET_ID_RE.search(share_url)
    if not id_match:
        raise ValueError("Doesn't look like a Google Sheets URL (expected .../spreadsheets/d/<id>/...)")
    gid_match = _SHEET_GID_RE.search(share_url)
    gid = gid_match.group(1) if gid_match else "0"
    return f"https://docs.google.com/spreadsheets/d/{id_match.group(1)}/export?format=csv&gid={gid}"


def _fetch_sheet_csv(share_url):
    import urllib.error
    import urllib.request

    csv_url = _sheet_csv_url(share_url)
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Neosky-WarrantySync/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError(
                "Google Sheets refused the request (not shared) -- set sharing to "
                "\"Anyone with the link can view\", or use the file upload instead."
            ) from exc
        raise ValueError(f"Google Sheets returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Couldn't reach Google Sheets from this server: {exc.reason}") from exc

    text = raw.decode("utf-8-sig", errors="replace")
    if "<html" in text[:200].lower():
        raise ValueError(
            "Got an HTML page instead of CSV data -- the sheet probably isn't shared as "
            "\"Anyone with the link can view\". Share it that way, or use the file upload instead."
        )
    return text


@admin_bp.route("/import-warranty/sync-sheet", methods=["POST"])
def import_warranty_sheet():
    """Same upsert as import_warranty() above, but pulls the data straight
    from a shared Google Sheet instead of a manually-exported/uploaded
    file -- lets the org keep editing its live warranty tracker in Sheets
    and just re-sync here when it changes, instead of re-exporting and
    re-uploading an Excel file every time."""
    import io

    sheet_url = request.form.get("sheet_url", "").strip() or DEFAULT_WARRANTY_SHEET_URL

    try:
        csv_text = _fetch_sheet_csv(sheet_url)
        df = pd.read_csv(io.StringIO(csv_text))
        imported, updated, skipped = _import_warranty_dataframe(df)
    except ValueError as exc:
        flash(f"Sheet sync failed: {exc}", "error")
        return redirect(url_for("admin.import_warranty"))
    except Exception as exc:
        flash(f"Sheet sync failed: could not parse the sheet as CSV ({exc})", "error")
        return redirect(url_for("admin.import_warranty"))

    AuditLog.record(current_user, "import_warranty", f"sheet sync: {imported} new, {updated} updated, {len(skipped)} skipped")
    db.session.commit()
    _flash_warranty_import_result(imported, updated, skipped)
    return redirect(url_for("admin.import_warranty"))


# ---- Departments -----------------------------------------------------

@admin_bp.route("/departments", methods=["GET", "POST"])
def departments():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Department name is required", "error")
        elif Department.query.filter_by(name=name).first():
            flash("A department with that name already exists", "error")
        else:
            dept = Department(name=name, slug=slugify(name))
            db.session.add(dept)
            AuditLog.record(current_user, "create_department", name)
            db.session.commit()
            flash(f"Department '{name}' created", "success")
        return redirect(url_for("admin.departments"))

    all_departments = Department.query.order_by(Department.name).all()
    return render_template("admin/departments.html", departments=all_departments)


@admin_bp.route("/departments/<int:dept_id>/delete", methods=["POST"])
def delete_department(dept_id):
    dept = Department.query.get_or_404(dept_id)

    if dept.users or dept.drones:
        flash("Cannot delete a department that still has users or drones", "error")
        return redirect(url_for("admin.departments"))

    AuditLog.record(current_user, "delete_department", dept.name)
    db.session.delete(dept)
    db.session.commit()
    flash(f"Department '{dept.name}' deleted", "success")
    return redirect(url_for("admin.departments"))


# ---- Users -------------------------------------------------------------

@admin_bp.route("/users", methods=["GET", "POST"])
def users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "department")
        department_id = request.form.get("department_id", type=int)

        errors = []
        if not username or not password or not full_name:
            errors.append("Username, full name, and password are all required")
        if role not in ("admin", "department"):
            errors.append("Invalid role")
        if role == "department" and not department_id:
            errors.append("A department account must be assigned a department")
        if User.query.filter_by(username=username).first():
            errors.append("That username is already taken")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            user = User(
                username=username,
                full_name=full_name,
                role=role,
                department_id=department_id if role == "department" else None,
            )
            user.set_password(password)
            db.session.add(user)
            AuditLog.record(current_user, "create_user", username)
            db.session.commit()
            flash(f"User '{username}' created", "success")

        return redirect(url_for("admin.users"))

    all_users = User.query.order_by(User.username).all()
    all_departments = Department.query.order_by(Department.name).all()
    return render_template("admin/users.html", users=all_users, departments=all_departments)


@admin_bp.route("/users/<int:user_id>/toggle-active", methods=["POST"])
def toggle_user_active(user_id):
    user = User.query.get_or_404(user_id)

    if user.id == current_user.id:
        flash("You can't deactivate your own account", "error")
        return redirect(url_for("admin.users"))

    user.is_active_flag = not user.is_active_flag
    AuditLog.record(current_user, "toggle_user_active", f"{user.username} -> {user.is_active_flag}")
    db.session.commit()
    flash(f"User '{user.username}' is now {'active' if user.is_active_flag else 'deactivated'}", "success")
    return redirect(url_for("admin.users"))


@admin_bp.route("/users/<int:user_id>/reset-password", methods=["POST"])
def reset_password(user_id):
    user = User.query.get_or_404(user_id)
    new_password = request.form.get("password", "")

    if len(new_password) < 6:
        flash("Password must be at least 6 characters", "error")
        return redirect(url_for("admin.users"))

    user.set_password(new_password)
    AuditLog.record(current_user, "reset_password", user.username)
    db.session.commit()
    flash(f"Password reset for '{user.username}'", "success")
    return redirect(url_for("admin.users"))


# ---- Audit log -----------------------------------------------------------

@admin_bp.route("/audit")
def audit():
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("admin/audit.html", entries=entries)
