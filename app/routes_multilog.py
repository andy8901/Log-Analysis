import os
from datetime import datetime, timezone
from uuid import uuid4

import pandas as pd
from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template,
    request, send_file, url_for,
)
from flask_login import current_user, login_required
from werkzeug.utils import secure_filename

from multilog_analyzer import analyze_single_log

from app import db
from app.decorators import department_required
from app.drive_sync import upload_to_drive
from app.models import AuditLog, MultilogBatch
from app.storage import allowed_log_file, department_archive_dir, remove_with_retry

multilog_bp = Blueprint("multilog", __name__)


@multilog_bp.route("/multilog", methods=["GET", "POST"])
@login_required
@department_required
def multilog():
    if current_user.is_admin:
        abort(403, "Admin accounts don't run analyses on a department's behalf")

    department = current_user.department

    if request.method == "GET":
        batches = (
            MultilogBatch.query.filter_by(user_id=current_user.id)
            .order_by(MultilogBatch.created_at.desc())
            .all()
        )
        return render_template("dept/multilog.html", batches=batches)

    files = [f for f in request.files.getlist("log_files") if f and f.filename]
    if not files:
        flash("Select at least one log file", "error")
        return redirect(url_for("multilog.multilog"))

    bad = [f.filename for f in files if not allowed_log_file(f.filename)]
    if bad:
        flash(f"Invalid file type(s): {', '.join(bad)} -- only .bin, .log, .tlog are accepted", "error")
        return redirect(url_for("multilog.multilog"))

    batch = MultilogBatch(
        department_id=department.id,
        user_id=current_user.id,
        file_count=len(files),
        status="processing",
    )
    db.session.add(batch)
    db.session.flush()

    rows = []
    errors = []

    for f in files:
        filename = secure_filename(f.filename)
        tmp_path = os.path.join(current_app.config["UPLOAD_TMP"], f"{uuid4().hex}_{filename}")
        f.save(tmp_path)
        try:
            result = analyze_single_log(tmp_path)
            if "_error" in result:
                errors.append(f"{filename}: {result['_error']}")
            else:
                rows.append(result)
        except Exception as exc:
            current_app.logger.exception("Multilog: failed to analyze %s", filename)
            errors.append(f"{filename}: {exc}")
        finally:
            remove_with_retry(tmp_path)

    batch.success_count = len(rows)
    batch.error_count = len(errors)
    if errors:
        import json
        batch.error_detail = json.dumps(errors)

    if not rows:
        batch.status = "failed"
        db.session.commit()
        flash("No logs could be analyzed -- see the batch's error details.", "error")
        return redirect(url_for("multilog.multilog"))

    dest_dir = department_archive_dir(department.slug, "multilog")
    result_filename = f"batch_{batch.id}_Neosky_Report.xlsx"
    result_path = os.path.join(dest_dir, result_filename)
    pd.DataFrame(rows).to_excel(result_path, index=False, engine="openpyxl")

    batch.result_path = result_path
    batch.status = "completed"
    batch.completed_at = datetime.now(timezone.utc)
    AuditLog.record(
        current_user, "multilog_batch",
        f"batch={batch.id} files={len(files)} ok={len(rows)} errors={len(errors)}",
    )
    db.session.commit()

    upload_to_drive(result_path, department.name, "multilog", result_filename)

    flash(f"Analyzed {len(rows)}/{len(files)} logs successfully.", "success")
    return redirect(url_for("multilog.multilog"))


def _get_batch_scoped(batch_id):
    batch = MultilogBatch.query.get_or_404(batch_id)
    if current_user.is_admin:
        return batch
    if batch.user_id != current_user.id:
        abort(403)
    return batch


@multilog_bp.route("/multilog/<int:batch_id>/download")
@login_required
def download_batch(batch_id):
    batch = _get_batch_scoped(batch_id)
    if not batch.result_path or not os.path.exists(batch.result_path):
        abort(404)
    return send_file(
        batch.result_path, as_attachment=True,
        download_name=f"Neosky_Multilog_Report_{batch.id}.xlsx",
    )
