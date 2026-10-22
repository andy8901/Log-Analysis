"""Analysing one uploaded log and storing the results -- shared by the department upload page and the admin
folder (bulk) upload worker, so both paths produce exactly the same report, investigation, multilog fields,
dashboard record, archive copy and Drive sync."""
import json
import os
from datetime import datetime, timezone

from flask import current_app

from analyzer import process_log
from investigator import investigate

from app import db
from app.drive_sync import upload_to_drive
from app.fleet import fleet_metrics
from app.models import AnalysisResult, AuditLog
from app.stats import derive_result_metrics
from app.storage import archive_upload, remove_with_retry


def attach_investigation(report, log_path, upload_id):
    """Best-effort: adds report["investigation"] (investigator.py, .bin logs only). Never fails the upload."""
    try:
        inv = investigate(log_path)
        if inv:
            report["investigation"] = inv
    except Exception:
        current_app.logger.warning("flight investigation failed for upload %s", upload_id, exc_info=True)
    return report


def extract_multilog_json(archived_path, upload_id):
    """Best-effort ~150-field multilog extraction (see multilog_analyzer.py); None on any error."""
    from multilog_analyzer import analyze_single_log

    try:
        result = analyze_single_log(archived_path)
        if "_error" in result:
            current_app.logger.warning("multilog extraction skipped for upload %s: %s", upload_id, result["_error"])
            return None
        return json.dumps(result)
    except Exception:
        current_app.logger.warning("multilog extraction failed for upload %s", upload_id, exc_info=True)
        return None


def analyze_and_store(upload_row, tmp_path, department, drone, actor, sync_reports=None):
    """Runs every analysis on tmp_path and stores the result on upload_row. Returns the report dict.
    Raises on analysis failure after marking the upload failed (the caller decides how to tell the user)."""
    filename = upload_row.original_filename
    try:
        report = process_log(tmp_path, drone_model=upload_row.drone_model or drone.model)
        attach_investigation(report, tmp_path, upload_row.id)
    except Exception as exc:
        current_app.logger.exception("Log processing failed for upload %s", upload_row.id)
        upload_row.status = "failed"
        upload_row.error_message = str(exc)
        upload_row.pending_path = None
        db.session.commit()
        remove_with_retry(tmp_path)
        raise

    archived_path = archive_upload(tmp_path, filename, department.slug, drone.msn, upload_row.id)
    upload_row.archived_path = archived_path
    upload_row.pending_path = None
    upload_row.status = "completed"
    upload_row.analyzed_at = datetime.now(timezone.utc)

    multilog_json = extract_multilog_json(archived_path, upload_row.id)
    analysis = AnalysisResult(
        upload_id=upload_row.id,
        report_status=report.get("status"),
        alert_count=len(report.get("alerts") or []),
        top_alerts=json.dumps((report.get("alerts") or [])[:5]),
        report_json=json.dumps(report),
        multilog_json=multilog_json,
        fleet_json=json.dumps(fleet_metrics(report, json.loads(multilog_json) if multilog_json else None)),
        **derive_result_metrics(report),
    )
    db.session.add(analysis)
    AuditLog.record(actor, "upload_analyzed", f"upload={upload_row.id} drone={drone.msn}" + (f" batch={upload_row.batch_id}" if upload_row.batch_id else ""))
    db.session.commit()

    upload_to_drive(archived_path, department.name, drone.msn, os.path.basename(archived_path), kind="Raw Logs")
    if sync_reports:
        sync_reports(upload_row, report, department, drone)
    return report


def refresh_investigation(upload_row):
    """Re-runs the flight investigation on an already-archived .bin log (older uploads, or after an
    investigator update) and refreshes the cached dashboard record. Returns True if it now has one."""
    a = upload_row.analysis
    path = upload_row.archived_path
    if not a or not path or not path.lower().endswith(".bin") or not os.path.exists(path):
        return False
    report = a.get_report()
    report.pop("investigation", None)
    attach_investigation(report, path, upload_row.id)
    a.report_json = json.dumps(report)
    a.fleet_json = json.dumps(fleet_metrics(report, a.get_multilog()))
    db.session.commit()
    return bool(report.get("investigation"))
