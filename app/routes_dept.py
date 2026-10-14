import json
import os
import tempfile
from datetime import datetime, timezone
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from analyzer import process_log
from reports import generate_excel, generate_word_report

from app import db
from app.decorators import department_required
from app.models import AnalysisResult, AuditLog, Department, Drone, MsnConflict, Upload
from app.storage import allowed_log_file, archive_upload, remove_with_retry

dept_bp = Blueprint("dept", __name__)

DRONE_MODEL_CHOICES = ["DOPO", "Tavas", "SWAT", "L15", "NS01", "Nimble"]


def resolve_department():
    """The department whose data this request should act on: always the
    caller's own department, except an admin may pass ?department_id=
    to read (never upload into) another department's view."""
    if current_user.is_admin:
        dept_id = request.args.get("department_id", type=int)
        if not dept_id:
            abort(400, "department_id is required for admin preview")
        return Department.query.get_or_404(dept_id)

    if current_user.department is None:
        abort(403)
    return current_user.department


@dept_bp.route("/")
@login_required
def home():
    if current_user.is_admin:
        return redirect(url_for("admin.dashboard"))
    return redirect(url_for("dept.dashboard"))


@dept_bp.route("/dashboard")
@login_required
@department_required
def dashboard():
    department = resolve_department()

    query = Upload.query.filter_by(department_id=department.id)

    msn = request.args.get("msn", "").strip()
    model = request.args.get("model", "").strip()
    status = request.args.get("status", "").strip()

    if msn:
        query = query.join(Drone, isouter=True).filter(
            db.or_(Drone.msn.ilike(f"%{msn}%"), Upload.msn_entered.ilike(f"%{msn}%"))
        )
    if model:
        query = query.filter(Upload.drone_model == model)
    if status:
        query = query.filter(Upload.status == status)

    uploads = query.order_by(Upload.uploaded_at.desc()).limit(500).all()

    all_uploads = Upload.query.filter_by(department_id=department.id)
    summary = {
        "total_drones": Drone.query.filter_by(department_id=department.id).count(),
        "total_logs": all_uploads.count(),
        "completed": all_uploads.filter_by(status="completed").count(),
        "pending": all_uploads.filter(Upload.status.in_(["queued", "processing"])).count(),
        "failed": all_uploads.filter_by(status="failed").count(),
        "critical": all_uploads.join(AnalysisResult).filter(
            AnalysisResult.report_status == "CRITICAL"
        ).count(),
    }

    return render_template(
        "dept/dashboard.html",
        department=department,
        uploads=uploads,
        summary=summary,
        model_choices=DRONE_MODEL_CHOICES,
        filters={"msn": msn, "model": model, "status": status},
    )


@dept_bp.route("/upload", methods=["GET", "POST"])
@login_required
@department_required
def upload():
    if current_user.is_admin:
        abort(403, "Admin accounts don't upload logs on a department's behalf")

    department = current_user.department

    if request.method == "GET":
        return render_template("dept/upload.html", model_choices=DRONE_MODEL_CHOICES)

    drone_model = request.form.get("drone", "").strip()
    msn = request.form.get("msn", "").strip()
    flight_date = request.form.get("date", "").strip()
    reported_by = request.form.get("reported_by", "").strip()

    file = request.files.get("log_file")
    if not file or not file.filename:
        flash("No file selected", "error")
        return render_template("dept/upload.html", model_choices=DRONE_MODEL_CHOICES), 400

    if not allowed_log_file(file.filename):
        flash("Invalid file type -- only .bin, .log, .tlog are accepted", "error")
        return render_template("dept/upload.html", model_choices=DRONE_MODEL_CHOICES), 400

    if not msn:
        flash("MSN is required so this log can be attached to a drone record", "error")
        return render_template("dept/upload.html", model_choices=DRONE_MODEL_CHOICES), 400

    filename = secure_filename(file.filename)
    # uuid4, not pid: a retried/resubmitted upload of the same filename must
    # never collide on the same temp path -- on Windows in particular, a
    # collision here can leave the previous attempt's file locked (by AV/
    # indexer scanning) right when the next attempt tries to write it.
    tmp_path = os.path.join(current_app.config["UPLOAD_TMP"], f"{uuid4().hex}_{filename}")
    file.save(tmp_path)

    # Resolve (or register) the drone this MSN belongs to, before touching
    # the analysis engine at all.
    drone = Drone.query.filter_by(msn=msn).first()

    if drone and drone.department_id != department.id:
        conflict = MsnConflict(
            msn=msn,
            requesting_department_id=department.id,
            existing_drone_id=drone.id,
            requested_by_id=current_user.id,
        )
        db.session.add(conflict)
        AuditLog.record(
            current_user, "msn_conflict",
            f"MSN {msn} already belongs to department {drone.department_id}",
        )
        db.session.commit()
        remove_with_retry(tmp_path)
        flash(
            f"MSN '{msn}' is already registered to another department. "
            "This upload was not processed -- an administrator has been "
            "flagged to review the conflict.",
            "error",
        )
        return render_template("dept/upload.html", model_choices=DRONE_MODEL_CHOICES), 409

    if drone is None:
        drone = Drone(msn=msn, model=drone_model or "UNKNOWN", department_id=department.id)
        db.session.add(drone)
        db.session.flush()

    upload_row = Upload(
        department_id=department.id,
        user_id=current_user.id,
        drone_id=drone.id,
        original_filename=filename,
        drone_model=drone_model,
        msn_entered=msn,
        flight_date=flight_date,
        reported_by=reported_by,
        status="processing",
    )
    db.session.add(upload_row)
    db.session.flush()  # assign upload_row.id without committing yet

    try:
        report = process_log(tmp_path, drone_model=drone_model or drone.model)
    except Exception as exc:
        current_app.logger.exception("Log processing failed for upload %s", upload_row.id)
        upload_row.status = "failed"
        upload_row.error_message = str(exc)
        db.session.commit()
        remove_with_retry(tmp_path)
        flash(f"Processing failed: {exc}", "error")
        return redirect(url_for("dept.dashboard"))

    archived_path = archive_upload(tmp_path, filename, department.slug, drone.msn, upload_row.id)

    upload_row.archived_path = archived_path
    upload_row.status = "completed"
    upload_row.analyzed_at = datetime.now(timezone.utc)

    analysis = AnalysisResult(
        upload_id=upload_row.id,
        report_status=report.get("status"),
        alert_count=len(report.get("alerts") or []),
        top_alerts=json.dumps((report.get("alerts") or [])[:5]),
        report_json=json.dumps(report),
    )
    db.session.add(analysis)
    AuditLog.record(current_user, "upload_analyzed", f"upload={upload_row.id} drone={drone.msn}")
    db.session.commit()

    return redirect(url_for("dept.view_report", upload_id=upload_row.id))


def _get_upload_scoped(upload_id):
    upload_row = Upload.query.get_or_404(upload_id)

    if current_user.is_admin:
        return upload_row
    if upload_row.department_id != current_user.department_id:
        abort(403)
    return upload_row


@dept_bp.route("/reports/<int:upload_id>")
@login_required
@department_required
def view_report(upload_id):
    upload_row = _get_upload_scoped(upload_id)

    if not upload_row.analysis:
        flash("This upload has no completed analysis yet.", "error")
        return redirect(url_for("dept.dashboard", department_id=upload_row.department_id))

    report = upload_row.analysis.get_report()
    return render_template("dept/report_view.html", upload=upload_row, report=report)


@dept_bp.route("/export/<int:upload_id>/<fmt>")
@login_required
@department_required
def export(upload_id, fmt):
    upload_row = _get_upload_scoped(upload_id)

    if not upload_row.analysis:
        abort(404)

    report = upload_row.analysis.get_report()
    ext = "docx" if fmt == "word" else "xlsx"

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        if fmt == "word":
            mission = {
                "drone": upload_row.drone_model,
                "msn": upload_row.msn_entered,
                "date": upload_row.flight_date,
                "reported_by": upload_row.reported_by,
            }
            generate_word_report(report, mission, tmp_path)
        else:
            generate_excel(report, tmp_path)

        download_name = f"Neosky_Report_{upload_row.id}.{ext}"
        return send_file(tmp_path, as_attachment=True, download_name=download_name)
    finally:
        # send_file streams the file; it's safe to drop our reference once
        # the response has been built -- Flask has already read it into the
        # response by the time this generator-less view returns.
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@dept_bp.route("/drones")
@login_required
@department_required
def drone_list():
    department = resolve_department()
    drones = Drone.query.filter_by(department_id=department.id).order_by(Drone.msn).all()
    return render_template("dept/drone_list.html", department=department, drones=drones)


@dept_bp.route("/drones/<int:drone_id>")
@login_required
@department_required
def drone_detail(drone_id):
    drone = Drone.query.get_or_404(drone_id)

    if not current_user.is_admin and drone.department_id != current_user.department_id:
        abort(403)

    uploads = Upload.query.filter_by(drone_id=drone.id).order_by(Upload.uploaded_at.desc()).all()
    return render_template("dept/drone_detail.html", drone=drone, uploads=uploads)
