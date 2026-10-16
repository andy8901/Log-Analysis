"""Mirrors the raw uploaded log and its generated reports into
<Department>/<MSN>/<Raw Logs|Reports>/<filename> under a local folder --
specifically, the folder that Google Drive for Desktop keeps synced to the
cloud. This deliberately does NOT talk to the Google Drive API: no service
account, no Cloud Console project, no JSON key. Install Google Drive for
Desktop, sign in with a normal Google account, and point
DRIVE_SYNC_ROOT at a folder inside its synced location (e.g.
"G:\\My Drive\\LogAnalysis" on Windows) -- Google's own app uploads
whatever lands there in the background, the same way it would for any
file you drag into that folder in Explorer.

Inert until DRIVE_SYNC_ROOT is set. Every public function is a safe no-op
(logs a warning, never raises) so a full disk or bad path never blocks an
upload -- the file is already safe in the local archive regardless."""

import os
import shutil

from flask import current_app


def is_configured():
    return bool(current_app.config.get("DRIVE_SYNC_ROOT"))


def upload_to_drive(local_path, department_name, msn, dest_filename, kind="Reports"):
    """Best-effort: copies local_path to
    <DRIVE_SYNC_ROOT>/<Department>/<MSN>/<kind>/dest_filename. `kind` is
    "Raw Logs" or "Reports" so the two never mix in the same folder.
    Returns the destination path, or None if sync isn't configured or the
    copy failed for any reason -- callers must never depend on this
    succeeding."""
    try:
        if not is_configured():
            return None

        dest_dir = os.path.join(
            current_app.config["DRIVE_SYNC_ROOT"], department_name, msn or "unassigned", kind
        )
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, dest_filename)
        shutil.copy2(local_path, dest_path)
        return dest_path

    except Exception:
        current_app.logger.warning(
            "Drive-folder sync failed for %s (department=%s, msn=%s) -- "
            "the file is still safe in the local archive.",
            dest_filename, department_name, msn, exc_info=True,
        )
        return None
