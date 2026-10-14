import os
import shutil
import time

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


def _retry_on_windows_file_lock(func, *args):
    """On Windows, a file just closed by pymavlink/antivirus/the search
    indexer can stay briefly locked ("being used by another process") even
    though nothing in this app still holds it open -- retry past that
    transient window rather than failing the whole upload over it. No-op
    retry cost on platforms where this never happens (succeeds first try)."""
    last_error = None
    for attempt in range(5):
        try:
            return func(*args)
        except OSError as exc:
            last_error = exc
            time.sleep(0.3 * (attempt + 1))
    raise last_error


def archive_upload(tmp_path, filename, department_slug, msn, upload_id):
    """Moves a just-uploaded log out of the shared tmp folder into its
    permanent, department+drone-scoped home. Mirrors the original app's
    'archive every log permanently' behaviour, just organised per tenant
    instead of in one flat folder."""
    dest_dir = department_archive_dir(department_slug, msn)
    dest_name = f"{upload_id}_{filename}"
    dest_path = os.path.join(dest_dir, dest_name)
    _retry_on_windows_file_lock(shutil.move, tmp_path, dest_path)
    return dest_path


def remove_with_retry(path):
    if os.path.exists(path):
        _retry_on_windows_file_lock(os.remove, path)
