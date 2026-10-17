import os
import tempfile

import pandas as pd
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.decorators import admin_required
from app.drive_sync import diagnostics as drive_sync_diagnostics
from app.drive_sync import test_write as drive_sync_test_write
from app.models import AnalysisResult, AuditLog, Department, Drone, Upload, User, slugify
from app.stats import (
    active_drone_trend, drone_aggregate, flight_summary_aggregate,
    daily_flight_trend, issue_severity_breakdown, person_aggregate,
    recent_anomalies, today_start, top_drones_from_uploads, trend_pct,
    period_window_counts, ytd_start,
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

    dept_chart = [
        {"name": row["department"].name, "flights": row["completed"], "anomalies": row["critical"] + row["warning"]}
        for row in rows
    ]

    return render_template(
        "admin/dashboard.html", rows=rows, totals=totals, trends=trends,
        flight_summary=flight_summary, daily_trend=daily_trend,
        severity=severity, anomalies=anomalies, top_drones=top_drones,
        dept_chart=dept_chart, drive_sync=drive_sync_diagnostics(),
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
    across every department, using the full ~150-field extraction (the
    same per-log analysis as the standalone desktop Multilog tool), with
    Department Name prepended. Deliberately not something a department or
    individual account can trigger -- it always runs over data departments
    have *already* uploaded via Individual Log Analysis, never a fresh
    file upload of its own. The raw archived log for each completed
    upload is re-parsed with multilog_analyzer.analyze_single_log() to
    get this field set (not stored at upload time -- only derived there
    for the three dashboard metrics -- so it's computed fresh here)."""
    from multilog_analyzer import analyze_single_log

    rows = []
    skipped = []

    uploads = (
        Upload.query.filter_by(status="completed")
        .order_by(Upload.uploaded_at.desc())
        .all()
    )
    for u in uploads:
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
