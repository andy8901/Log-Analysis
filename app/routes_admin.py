import json
import os
import re
import tempfile
from datetime import datetime, timezone

import pandas as pd
from flask import Blueprint, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.decorators import admin_required
from app.drive_sync import diagnostics as drive_sync_diagnostics
from app.drive_sync import test_write as drive_sync_test_write
from app.models import AnalysisResult, AuditLog, Department, Drone, Upload, User, slugify
from app.stats import (
    active_drone_trend, drone_aggregate, flight_summary_aggregate,
    daily_flight_trend, issue_severity_breakdown, multilog_aggregate,
    person_aggregate, recent_anomalies, today_start, top_drones_from_uploads,
    trend_pct, period_window_counts, ytd_start,
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
    all_drones_list = Drone.query.all()
    all_completed_uploads = [u for u in all_uploads if u.status == "completed" and u.analysis]

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

    totals = {
        "departments": len(departments),
        "users": User.query.count(),
        "drones": len(all_drones_list),
        "uploads": len(all_uploads),
        "logs_today": len([u for u in all_completed_uploads if u.uploaded_at and u.uploaded_at >= today]),
        "logs_ytd": len([u for u in all_completed_uploads if u.uploaded_at and u.uploaded_at >= ytd]),
    }

    uploads_cur, uploads_prev = period_window_counts(all_uploads, days=7)
    flights_cur, flights_prev = period_window_counts(all_completed_uploads, days=7)
    drones_cur, drones_prev = active_drone_trend(all_uploads, days=7)
    trends = {
        "uploads": trend_pct(uploads_cur, uploads_prev),
        "flights": trend_pct(flights_cur, flights_prev),
        "drones": trend_pct(drones_cur, drones_prev),
    }

    flight_summary = flight_summary_aggregate(all_completed_uploads)
    daily_trend = daily_flight_trend(all_completed_uploads, days=14)
    severity = issue_severity_breakdown(all_completed_uploads)
    anomalies = recent_anomalies(all_completed_uploads, limit=6)
    top_drones = top_drones_from_uploads(all_uploads, limit=5, include_department=True)
    ml_stats = multilog_aggregate(all_completed_uploads)

    dept_chart = [
        {"name": row["department"].name, "flights": row["completed"], "anomalies": row["critical"] + row["warning"]}
        for row in rows
    ]

    return render_template(
        "admin/dashboard.html", rows=rows, totals=totals, trends=trends,
        flight_summary=flight_summary, daily_trend=daily_trend,
        severity=severity, anomalies=anomalies, top_drones=top_drones,
        ml_stats=ml_stats, dept_chart=dept_chart, drive_sync=drive_sync_diagnostics(),
    )


@admin_bp.route("/drive-sync-test", methods=["POST"])
def drive_sync_test():
    ok, message = drive_sync_test_write()
    AuditLog.record(current_user, "drive_sync_test", f"ok={ok}: {message}")
    db.session.commit()
    flash(message, "success" if ok else "error")
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


def _extract_msn_from_filename(name):
    """Some organizations' own log-naming convention embeds the drone's
    MSN right in the filename (e.g. "MSN_064_log_23_2026-2-21...bin") --
    a per-row fallback tried when there's no MSN column at all, named or
    positional. Returns None (not a guess) for a filename that doesn't
    match, e.g. ones filed under an incident folder name instead
    ("Crash_log_...", "Testing Logs (...)_log_...")."""
    if not name:
        return None
    m = _MSN_IN_FILENAME_RE.search(name)
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
