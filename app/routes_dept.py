import json
import os
import tempfile
import threading
from datetime import datetime, timezone
from uuid import uuid4

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from analyzer import process_log
from incident_report import generate_incident_report
from investigator import investigate
from reports import generate_excel, generate_word_report

from app import db
from app.decorators import department_required
from app.drive_sync import upload_to_drive
from app.flight3d import build_track
from app.models import AnalysisResult, AuditLog, CONCLUSION_CHOICES, Department, Drone, Upload
from app.stats import (
    anomaly_type_breakdown, daily_flight_trend, derive_result_metrics,
    drone_flight_series, flight_summary_aggregate, multilog_aggregate,
    period_window_counts, recent_anomalies, top_drones_from_uploads, trend_pct,
)
from app.storage import allowed_log_file, archive_upload, remove_with_retry
from app.warranty import status_for_drone as warranty_status_for_drone

dept_bp = Blueprint("dept", __name__)

DRONE_MODEL_CHOICES = ["DOPO", "Tavas", "SWAT", "L15", "NS01", "Nimble", "Customized"]


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

    # Confidentiality rule: a department account only ever sees its own
    # uploads here, never a colleague's -- only an admin gets the
    # department-wide picture (via ?department_id= on this same route).
    own_only = not current_user.is_admin
    base_filter = {"department_id": department.id}
    if own_only:
        base_filter["user_id"] = current_user.id

    query = Upload.query.filter_by(**base_filter)

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

    all_uploads = Upload.query.filter_by(**base_filter)
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
    scoped_uploads = all_uploads.all()
    scoped_completed = [u for u in scoped_uploads if u.status == "completed" and u.analysis]

    flight_summary = flight_summary_aggregate(scoped_completed)
    daily_trend = daily_flight_trend(scoped_completed, days=14)
    anomaly_types = anomaly_type_breakdown(scoped_completed)
    anomalies = recent_anomalies(scoped_completed, limit=6)
    top_drones = top_drones_from_uploads(scoped_uploads, limit=5)
    ml_stats = multilog_aggregate(scoped_completed)

    uploads_cur, uploads_prev = period_window_counts(scoped_uploads, days=7)
    flights_cur, flights_prev = period_window_counts(scoped_completed, days=7)
    trends = {
        "uploads": trend_pct(uploads_cur, uploads_prev),
        "flights": trend_pct(flights_cur, flights_prev),
    }

    return render_template(
        "dept/dashboard.html",
        department=department,
        uploads=uploads,
        summary=summary,
        flight_summary=flight_summary,
        daily_trend=daily_trend,
        anomaly_types=anomaly_types,
        anomalies=anomalies,
        top_drones=top_drones,
        ml_stats=ml_stats,
        trends=trends,
        model_choices=DRONE_MODEL_CHOICES,
        filters={"msn": msn, "model": model, "status": status},
        own_only=own_only,
    )


@dept_bp.route("/upload", methods=["GET", "POST"])
@login_required
@department_required
def upload():
    if current_user.is_admin:
        abort(403, "Admin accounts don't upload logs on a department's behalf")

    department = current_user.department

    if request.method == "GET":
        return render_template(
            "dept/upload.html", model_choices=DRONE_MODEL_CHOICES, known_customers=known_customers(),
        )

    drone_model = request.form.get("drone", "").strip()
    msn = request.form.get("msn", "").strip()
    flight_date = request.form.get("date", "").strip()
    reported_by = request.form.get("reported_by", "").strip()
    customer_name = request.form.get("customer", "").strip()

    file = request.files.get("log_file")
    try:
        upload_row = process_and_save_log(
            file, department, drone_model, msn, flight_date, reported_by, current_user,
            customer_name=customer_name,
        )
    except ValueError as exc:
        flash(str(exc), "error")
        return render_template(
            "dept/upload.html", model_choices=DRONE_MODEL_CHOICES, known_customers=known_customers(),
        ), 400

    if upload_row.status == "failed":
        flash(f"Processing failed: {upload_row.error_message}", "error")
        return redirect(url_for("dept.dashboard"))

    return redirect(url_for("dept.view_report", upload_id=upload_row.id))


def known_customers():
    """Distinct customer names already on file -- backs the "Customer"
    field's suggestion list on the upload forms (see process_and_save_log()'s
    customer_name handling below) and the Organization Overview's Customer
    filter. Union of Drone.customer_name (set by an actual upload) and
    Warranty.customer_name (bulk-imported from the external tracker --
    admin.import_warranty()/import_warranty_sheet()): the tracker covers
    plenty of customers whose drones have never had a log uploaded through
    this app yet, so a name that only exists there still needs to show up
    here, not just once a drone happens to be tagged with it."""
    from app.models import Warranty

    drone_names = {r[0] for r in db.session.query(Drone.customer_name).filter(Drone.customer_name.isnot(None), Drone.customer_name != "").distinct()}
    warranty_names = {r[0] for r in db.session.query(Warranty.customer_name).distinct()}
    return sorted(drone_names | warranty_names)


def process_and_save_log(file, department, drone_model, msn, flight_date, reported_by, user, customer_name=None):
    """The single-log processing pipeline -- validate, save to a tmp path,
    resolve/create the drone, run analyzer.py + investigator.py +
    multilog_analyzer.py (all untouched analysis logic, only ever read
    from), archive the raw log, persist Upload + AnalysisResult, and sync
    to Drive. Shared by a department's own upload() above and an admin's
    batch Multilog folder upload (routes_admin.import_logs_batch), so this
    pipeline only has one copy to keep correct.

    Raises ValueError for a validation problem (bad/missing file, no MSN)
    -- nothing is written to the DB in that case. Otherwise always returns
    the Upload row: its status is "completed" on success or "failed" if
    processing itself raised (still saved, so it shows up as a visible
    failure instead of silently vanishing)."""
    if not file or not file.filename:
        raise ValueError("No file selected")
    if not allowed_log_file(file.filename):
        raise ValueError("Invalid file type -- only .bin, .log, .tlog are accepted")
    if not msn:
        raise ValueError("MSN is required so this log can be attached to a drone record")

    filename = secure_filename(file.filename)
    # uuid4, not pid: a retried/resubmitted upload of the same filename must
    # never collide on the same temp path -- on Windows in particular, a
    # collision here can leave the previous attempt's file locked (by AV/
    # indexer scanning) right when the next attempt tries to write it.
    tmp_path = os.path.join(current_app.config["UPLOAD_TMP"], f"{uuid4().hex}_{filename}")
    file.save(tmp_path)

    # Resolve (or register) the drone this MSN belongs to, scoped to this
    # department -- MSNs are only unique per department, so two
    # departments are free to each have their own drone registered under
    # the same MSN string.
    drone = Drone.query.filter_by(msn=msn, department_id=department.id).first()

    if drone is None:
        drone = Drone(msn=msn, model=drone_model or "UNKNOWN", department_id=department.id, customer_name=customer_name or None)
        db.session.add(drone)
        db.session.flush()
    elif customer_name:
        # Only ever fills in/corrects it -- a blank Customer field on a
        # later upload (e.g. someone who doesn't know it) must never wipe
        # out a value a previous upload already recorded.
        drone.customer_name = customer_name

    upload_row = Upload(
        department_id=department.id,
        user_id=user.id,
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
        _attach_investigation(report, tmp_path, upload_row.id)
    except Exception as exc:
        current_app.logger.exception("Log processing failed for upload %s", upload_row.id)
        upload_row.status = "failed"
        upload_row.error_message = str(exc)
        db.session.commit()
        remove_with_retry(tmp_path)
        return upload_row

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
        multilog_json=_extract_multilog_json(archived_path, upload_row.id),
        **derive_result_metrics(report),
    )
    db.session.add(analysis)
    AuditLog.record(user, "upload_analyzed", f"upload={upload_row.id} drone={drone.msn}")
    db.session.commit()

    # Plain values captured now, before handing off to a background thread
    # -- upload_row/department/drone are SQLAlchemy objects bound to this
    # request's session, and db.session.commit() just expired their
    # attributes, so touching them from another thread (after this request's
    # session may already be torn down) is unsafe. Drive sync is already
    # best-effort (both calls below swallow their own errors), so running
    # it off the request's critical path -- rather than blocking the
    # response, and for batch upload every file still waiting behind this
    # one -- on a slow or unresponsive Drive folder costs nothing.
    mission = {
        "drone": upload_row.drone_model,
        "msn": upload_row.msn_entered,
        "date": upload_row.flight_date,
        "reported_by": upload_row.reported_by,
    }
    _dispatch_drive_sync(
        current_app._get_current_object(), archived_path, department.name, drone.msn,
        upload_row.id, filename, report, mission,
    )

    return upload_row


def _dispatch_drive_sync(app, archived_path, department_name, drone_msn, upload_id, original_filename, report, mission):
    def _run():
        with app.app_context():
            upload_to_drive(archived_path, department_name, drone_msn, os.path.basename(archived_path), kind="Raw Logs")
            _sync_reports_to_drive(report, department_name, drone_msn, upload_id, original_filename, mission)

    threading.Thread(target=_run, daemon=True, name=f"drive-sync-{upload_id}").start()


def _attach_investigation(report, log_path, upload_id):
    """Best-effort: adds report["investigation"] -- the plain-language "what happened, and was it a human error or a
    system error" analysis from investigator.py (.bin logs only). Never blocks or fails the upload."""
    try:
        inv = investigate(log_path)
        if inv:
            report["investigation"] = inv
    except Exception:
        current_app.logger.warning("flight investigation failed for upload %s", upload_id, exc_info=True)
    return report


def _ensure_investigation(upload_row, report):
    """Reports analysed before the investigator existed get it computed once, from the archived log, on first view."""
    if report.get("investigation") or not upload_row.archived_path:
        return report
    if not upload_row.archived_path.lower().endswith(".bin") or not os.path.exists(upload_row.archived_path):
        return report
    _attach_investigation(report, upload_row.archived_path, upload_row.id)
    if report.get("investigation"):
        upload_row.analysis.report_json = json.dumps(report)
        db.session.commit()
    return report


def _extract_multilog_json(archived_path, upload_id):
    """Best-effort: runs the same ~150-field extraction the admin Multilog
    Analysis export uses (multilog_analyzer.analyze_single_log(), untouched
    analysis logic) once at upload time, so the richer dashboard metrics
    that live only in that field set (range from home, battery voltage at
    a failsafe trigger, thrust/GPS issue counts) are available without
    re-parsing the raw log later. Never blocks or fails the upload -- on
    any error this just leaves multilog_json unset, same as Drive sync."""
    from multilog_analyzer import analyze_single_log

    try:
        result = analyze_single_log(archived_path)
        if "_error" in result:
            current_app.logger.warning(
                "multilog extraction skipped for upload %s: %s", upload_id, result["_error"]
            )
            return None
        return json.dumps(result)
    except Exception:
        current_app.logger.warning(
            "multilog extraction failed for upload %s", upload_id, exc_info=True
        )
        return None


def _sync_reports_to_drive(report, department_name, drone_msn, upload_id, original_filename, mission):
    """Generates the Word/Excel reports fresh (just to get a file to hand
    to Drive) and mirrors them into that drone's "Reports" subfolder,
    alongside the raw log's own "Raw Logs" subfolder synced by the caller.
    Best-effort: failures are logged and swallowed inside upload_to_drive()
    itself. Takes plain values rather than the Upload/Department/Drone
    objects themselves -- see _dispatch_drive_sync(), which runs this in a
    background thread where those ORM objects aren't safe to touch."""
    jobs = [
        ("word", "docx", "Report", lambda p: generate_word_report(report, mission, p)),
        ("xlsx", "xlsx", "Report", lambda p: generate_excel(report, p)),
    ]
    if report.get("investigation"):
        jobs.append(("incident", "docx", "Incident_Report", lambda p: generate_incident_report(report, mission, p)))
    for fmt, ext, suffix, generate in jobs:
        with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            generate(tmp_path)
            upload_to_drive(
                tmp_path, department_name, drone_msn,
                f"{upload_id}_{os.path.splitext(original_filename)[0]}_{suffix}.{ext}",
            )
        except Exception:
            current_app.logger.warning(
                "Report generation for Drive sync failed (upload=%s, fmt=%s)",
                upload_id, fmt, exc_info=True,
            )
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _get_upload_scoped(upload_id):
    upload_row = Upload.query.get_or_404(upload_id)

    if current_user.is_admin:
        return upload_row
    # Confidentiality rule: a department account can only ever open its own
    # upload, not a colleague's, even within the same department.
    if upload_row.user_id != current_user.id:
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

    if upload_row.source == "imported":
        flash(
            "This is backfilled historical data with no raw log behind it -- "
            "it feeds the dashboard aggregates, but there's no full report to view here.",
            "error",
        )
        return redirect(url_for("dept.dashboard", department_id=upload_row.department_id))

    report = _ensure_investigation(upload_row, upload_row.analysis.get_report())
    track, track_meta = build_track(report)
    warranty = warranty_status_for_drone(upload_row.drone) if upload_row.drone else None
    return render_template(
        "dept/report_view.html", upload=upload_row, report=report,
        track=track, track_meta=track_meta, conclusion_choices=CONCLUSION_CHOICES,
        warranty=warranty,
    )


@dept_bp.route("/reports/<int:upload_id>/conclusion", methods=["POST"])
@login_required
@department_required
def set_conclusion(upload_id):
    upload_row = _get_upload_scoped(upload_id)

    if not upload_row.analysis:
        abort(404)

    value = request.form.get("conclusion", "").strip()
    note = request.form.get("conclusion_note", "").strip()
    valid_values = {v for v, _ in CONCLUSION_CHOICES}

    if value not in valid_values:
        flash("Choose a conclusion before continuing.", "error")
        return redirect(url_for("dept.view_report", upload_id=upload_id))

    upload_row.conclusion = value
    upload_row.conclusion_note = note or None
    upload_row.concluded_by_id = current_user.id
    upload_row.concluded_at = datetime.now(timezone.utc)
    AuditLog.record(current_user, "set_conclusion", f"upload={upload_id} conclusion={value}")
    db.session.commit()

    flash("Conclusion recorded -- downloads are now available.", "success")
    return redirect(url_for("dept.view_report", upload_id=upload_id))


@dept_bp.route("/export/<int:upload_id>/<fmt>")
@login_required
@department_required
def export(upload_id, fmt):
    upload_row = _get_upload_scoped(upload_id)

    if not upload_row.analysis or upload_row.source == "imported":
        abort(404)

    if not upload_row.has_conclusion:
        flash("Record a conclusion (Pilot error / System error / Yet to decide) before downloading.", "error")
        return redirect(url_for("dept.view_report", upload_id=upload_id))

    if fmt not in ("word", "xlsx", "incident"):
        abort(404)
    report = _ensure_investigation(upload_row, upload_row.analysis.get_report())
    if fmt == "incident" and not report.get("investigation"):
        flash("The incident report needs a .bin dataflash log; this upload doesn't have one.", "error")
        return redirect(url_for("dept.view_report", upload_id=upload_id))
    ext = "xlsx" if fmt == "xlsx" else "docx"

    with tempfile.NamedTemporaryFile(suffix=f".{ext}", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        mission = {
            "drone": upload_row.drone_model,
            "msn": upload_row.msn_entered,
            "date": upload_row.flight_date,
            "reported_by": upload_row.reported_by,
        }
        if fmt == "incident":
            generate_incident_report(report, mission, tmp_path)
        elif fmt == "word":
            generate_word_report(report, mission, tmp_path)
        else:
            generate_excel(report, tmp_path)

        download_name = (f"Neosky_Incident_Report_{upload_row.id}.docx" if fmt == "incident"
                         else f"Neosky_Report_{upload_row.id}.{ext}")
        return send_file(tmp_path, as_attachment=True, download_name=download_name)
    finally:
        # send_file streams the file; it's safe to drop our reference once
        # the response has been built -- Flask has already read it into the
        # response by the time this generator-less view returns.
        try:
            os.remove(tmp_path)
        except OSError:
            pass


@dept_bp.route("/export/<int:upload_id>/video")
@login_required
@department_required
def export_video(upload_id):
    from app.flight_video import render_flight_video

    upload_row = _get_upload_scoped(upload_id)

    if not upload_row.analysis or upload_row.source == "imported":
        abort(404)

    if not upload_row.has_conclusion:
        flash("Record a conclusion (Pilot error / System error / Yet to decide) before downloading.", "error")
        return redirect(url_for("dept.view_report", upload_id=upload_id))

    report = upload_row.analysis.get_report()

    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        ok = render_flight_video(report, tmp_path)
        if not ok:
            flash("No GPS track in this log -- nothing to render.", "error")
            return redirect(url_for("dept.view_report", upload_id=upload_row.id))

        download_name = f"Neosky_Flight3D_{upload_row.id}.mp4"
        return send_file(tmp_path, as_attachment=True, download_name=download_name)
    finally:
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

    query = Upload.query.filter_by(drone_id=drone.id)
    own_only = not current_user.is_admin
    if own_only:
        # Confidentiality rule: a department account sees this drone's
        # history only for logs *it* uploaded -- a colleague's uploads
        # against the same drone stay private to that colleague and admin.
        query = query.filter_by(user_id=current_user.id)

    uploads = query.order_by(Upload.uploaded_at.desc()).all()
    series = drone_flight_series(uploads)
    return render_template(
        "dept/drone_detail.html", drone=drone, uploads=uploads,
        own_only=own_only, series=series,
    )
