import os
import tempfile

import pandas as pd
from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required

from app import db
from app.decorators import admin_required
from app.models import AnalysisResult, AuditLog, Department, Drone, MsnConflict, MultilogBatch, Upload, User, slugify
from app.stats import daily_counts, drone_aggregate, person_aggregate, today_start, ytd_start

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

    rows = []
    for dept in departments:
        uploads = Upload.query.filter_by(department_id=dept.id)
        completed = uploads.filter_by(status="completed")
        rows.append({
            "department": dept,
            "drones": Drone.query.filter_by(department_id=dept.id).count(),
            "uploads": uploads.count(),
            "completed": completed.count(),
            "failed": uploads.filter_by(status="failed").count(),
            "critical": uploads.join(AnalysisResult).filter(
                AnalysisResult.report_status == "CRITICAL"
            ).count(),
            "logs_today": completed.filter(Upload.uploaded_at >= today).count(),
            "logs_ytd": completed.filter(Upload.uploaded_at >= ytd).count(),
        })

    all_completed = Upload.query.filter_by(status="completed")
    totals = {
        "departments": len(departments),
        "users": User.query.count(),
        "drones": Drone.query.count(),
        "uploads": Upload.query.count(),
        "logs_today": all_completed.filter(Upload.uploaded_at >= today).count(),
        "logs_ytd": all_completed.filter(Upload.uploaded_at >= ytd).count(),
        "pending_conflicts": MsnConflict.query.filter_by(status="pending").count(),
    }

    trend = daily_counts(Upload.query.filter_by(status="completed").all(), days=14)

    return render_template("admin/dashboard.html", rows=rows, totals=totals, trend=trend)


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
    """The one bulk, cross-department raw-data export -- every completed
    upload's key extracted fields, one row each. Admin-only: this is
    exactly the data a department account is restricted from seeing in
    bulk for anyone but itself."""
    uploads = (
        Upload.query.filter_by(status="completed")
        .order_by(Upload.uploaded_at.desc())
        .all()
    )

    rows = []
    for u in uploads:
        a = u.analysis
        rows.append({
            "Upload ID": u.id,
            "Department": u.department.name,
            "Drone MSN": u.drone.msn if u.drone else u.msn_entered,
            "Drone Model": u.drone_model,
            "Uploaded By": u.user.full_name,
            "Uploaded At": u.uploaded_at.strftime("%Y-%m-%d %H:%M:%S") if u.uploaded_at else "",
            "Flight Date": u.flight_date,
            "Original Filename": u.original_filename,
            "Result Status": a.report_status if a else "",
            "Alert Count": a.alert_count if a else "",
            "Flight Duration (min)": a.flight_duration_min if a else "",
            "Battery Sag (%)": a.battery_sag_pct if a else "",
            "Distance Travelled (m)": a.distance_travelled_m if a else "",
            "Top Alerts": " | ".join(a.get_top_alerts()) if a else "",
        })

    df = pd.DataFrame(rows)
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        df.to_excel(tmp_path, index=False, engine="openpyxl")
        AuditLog.record(current_user, "export_raw_data", f"{len(rows)} rows")
        db.session.commit()
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


# ---- MSN conflicts -------------------------------------------------------

@admin_bp.route("/conflicts")
def conflicts():
    pending = (
        MsnConflict.query.filter_by(status="pending")
        .order_by(MsnConflict.created_at.desc())
        .all()
    )
    resolved = (
        MsnConflict.query.filter(MsnConflict.status != "pending")
        .order_by(MsnConflict.created_at.desc())
        .limit(50)
        .all()
    )
    return render_template("admin/conflicts.html", pending=pending, resolved=resolved)


@admin_bp.route("/conflicts/<int:conflict_id>/reassign", methods=["POST"])
def reassign_conflict(conflict_id):
    from datetime import datetime, timezone

    conflict = MsnConflict.query.get_or_404(conflict_id)
    drone = conflict.existing_drone

    drone.department_id = conflict.requesting_department_id
    conflict.status = "reassigned"
    conflict.resolved_at = datetime.now(timezone.utc)
    AuditLog.record(
        current_user, "reassign_drone",
        f"drone={drone.msn} -> department={conflict.requesting_department_id}",
    )
    db.session.commit()
    flash(f"Drone '{drone.msn}' reassigned to the requesting department", "success")
    return redirect(url_for("admin.conflicts"))


@admin_bp.route("/conflicts/<int:conflict_id>/dismiss", methods=["POST"])
def dismiss_conflict(conflict_id):
    from datetime import datetime, timezone

    conflict = MsnConflict.query.get_or_404(conflict_id)
    conflict.status = "dismissed"
    conflict.resolved_at = datetime.now(timezone.utc)
    AuditLog.record(current_user, "dismiss_conflict", f"msn={conflict.msn}")
    db.session.commit()
    flash("Conflict dismissed", "success")
    return redirect(url_for("admin.conflicts"))


# ---- Multilog batches (admin: every department's) -----------------------

@admin_bp.route("/multilog")
def all_multilog_batches():
    batches = MultilogBatch.query.order_by(MultilogBatch.created_at.desc()).all()
    return render_template("admin/multilog.html", batches=batches)


# ---- Audit log -----------------------------------------------------------

@admin_bp.route("/audit")
def audit():
    entries = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(300).all()
    return render_template("admin/audit.html", entries=entries)
