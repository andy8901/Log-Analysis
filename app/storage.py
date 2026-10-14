import os
import shutil

from flask import current_app


def allowed_log_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in current_app.config["ALLOWED_LOG_EXTENSIONS"]
    )


def department_archive_dir(department_slug, msn):
    safe_msn = msn or "unassigned"
    path = os.path.join(current_app.config["ARCHIVE_ROOT"], department_slug, safe_msn)
    os.makedirs(path, exist_ok=True)
    return path


def archive_upload(tmp_path, filename, department_slug, msn, upload_id):
    """Moves a just-uploaded log out of the shared tmp folder into its
    permanent, department+drone-scoped home. Mirrors the original app's
    'archive every log permanently' behaviour, just organised per tenant
    instead of in one flat folder."""
    dest_dir = department_archive_dir(department_slug, msn)
    dest_name = f"{upload_id}_{filename}"
    dest_path = os.path.join(dest_dir, dest_name)
    shutil.move(tmp_path, dest_path)
    return dest_path
