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
import time

from flask import current_app


def is_configured():
    return bool(current_app.config.get("DRIVE_SYNC_ROOT"))


def diagnostics():
    """Everything the Admin Overview page needs to self-diagnose a sync
    problem without a shell on the server: the configured path, its
    resolved absolute form (so a relative path isn't silently ambiguous),
    whether that folder currently exists, and whether some other
    environment variable was overridden by .env to get here (see
    config.py) -- the most common cause of "I changed the drive but it's
    still using the old one"."""
    root = current_app.config.get("DRIVE_SYNC_ROOT")
    return {
        "configured": bool(root),
        "path": root,
        "abs_path": os.path.abspath(root) if root else None,
        "exists": os.path.isdir(root) if root else False,
        "env_overridden": current_app.config.get("DRIVE_SYNC_ROOT_ENV_OVERRIDDEN", False),
        "prior_env_value": current_app.config.get("DRIVE_SYNC_ROOT_PRIOR_ENV_VALUE"),
    }


def test_write():
    """Writes, then immediately removes, a small marker file at the
    configured root -- confirms the running server process can actually
    write there right now, not just that the path string looks plausible.
    Returns (ok, message) for a flash() on the Admin Overview page."""
    root = current_app.config.get("DRIVE_SYNC_ROOT")
    if not root:
        return False, "Drive sync is not configured (DRIVE_SYNC_ROOT is unset in .env)."

    marker = os.path.join(root, f".neosky_sync_test_{int(time.time())}.tmp")
    try:
        os.makedirs(root, exist_ok=True)
        with open(marker, "w") as f:
            f.write("Neosky Drone Diagnostic Platform -- sync test file, safe to delete.\n")
        os.remove(marker)
        return True, f"Success -- wrote and removed a test file in {os.path.abspath(root)}."
    except Exception as exc:
        return False, f"Failed to write to {os.path.abspath(root)}: {exc}"


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
