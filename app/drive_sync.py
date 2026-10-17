"""Mirrors the raw uploaded log and its generated reports into
<Department>/<MSN>/<Raw Logs|Reports>/<filename>, in Google Drive, via
either of two interchangeable mechanisms selected by which config var is
set:

- DRIVE_SYNC_ROOT (Windows/Mac office PC): a local folder that Google
  Drive for Desktop keeps synced to the cloud. A plain filesystem copy --
  no service account, no Cloud Console project, no JSON key. Install
  Google Drive for Desktop, sign in with a normal Google account, and
  point DRIVE_SYNC_ROOT at a folder inside its synced location (e.g.
  "G:\\My Drive\\LogAnalysis" on Windows).

- DRIVE_SYNC_RCLONE_REMOTE (a real server -- Google Drive for Desktop
  doesn't exist on Linux): an rclone remote:path, e.g.
  "gdrive:LogAnalysis", synced by shelling out to the `rclone` binary
  per file. Install rclone, run `rclone config` once to connect a Google
  account (same no-API-project, no-service-account story as Drive for
  Desktop), then set this to `<remote-name>:<folder>`.

Both mechanisms use the exact same <Department>/<MSN>/<kind>/<filename>
layout, so moving from one to the other doesn't change where anything
ends up in Drive. If both are set, DRIVE_SYNC_RCLONE_REMOTE wins.

Inert until one of these is set. Every public function is a safe no-op
(logs a warning, never raises) so a full disk, bad path, or unreachable
rclone remote never blocks an upload -- the file is already safe in the
local archive regardless."""

import os
import shutil
import subprocess
import tempfile
import time

from flask import current_app

_RCLONE_TIMEOUT_S = 120


def _rclone_remote():
    return current_app.config.get("DRIVE_SYNC_RCLONE_REMOTE")


def _folder_root():
    return current_app.config.get("DRIVE_SYNC_ROOT")


def is_configured():
    return bool(_rclone_remote()) or bool(_folder_root())


def diagnostics():
    """Everything the Admin Overview page needs to self-diagnose a sync
    problem without a shell on the server: which mechanism is active, its
    resolved destination, whether that destination looks reachable right
    now, and whether some other environment variable was overridden by
    .env to get here (see config.py) -- the most common cause of "I
    changed the drive but it's still using the old one"."""
    remote = _rclone_remote()
    if remote:
        return {
            "configured": True,
            "mode": "rclone",
            "path": remote,
            "abs_path": remote,
            "exists": shutil.which("rclone") is not None,
            "exists_label": "rclone found on PATH" if shutil.which("rclone") else "rclone binary not found on PATH",
            "env_overridden": current_app.config.get("DRIVE_SYNC_ROOT_ENV_OVERRIDDEN", False),
            "prior_env_value": current_app.config.get("DRIVE_SYNC_ROOT_PRIOR_ENV_VALUE"),
        }

    root = _folder_root()
    return {
        "configured": bool(root),
        "mode": "folder" if root else None,
        "path": root,
        "abs_path": os.path.abspath(root) if root else None,
        "exists": os.path.isdir(root) if root else False,
        "exists_label": None,
        "env_overridden": current_app.config.get("DRIVE_SYNC_ROOT_ENV_OVERRIDDEN", False),
        "prior_env_value": current_app.config.get("DRIVE_SYNC_ROOT_PRIOR_ENV_VALUE"),
    }


def test_write():
    """Writes, then immediately removes, a small marker file at the
    configured destination -- confirms the sync path actually works right
    now, not just that the config string looks plausible. Returns (ok,
    message) for a flash() on the Admin Overview page."""
    remote = _rclone_remote()
    if remote:
        return _rclone_test_write(remote)

    root = _folder_root()
    if not root:
        return False, "Drive sync is not configured (set DRIVE_SYNC_ROOT or DRIVE_SYNC_RCLONE_REMOTE in .env)."

    marker = os.path.join(root, f".neosky_sync_test_{int(time.time())}.tmp")
    try:
        os.makedirs(root, exist_ok=True)
        with open(marker, "w") as f:
            f.write("Neosky Drone Diagnostic Platform -- sync test file, safe to delete.\n")
        os.remove(marker)
        return True, f"Success -- wrote and removed a test file in {os.path.abspath(root)}."
    except Exception as exc:
        return False, f"Failed to write to {os.path.abspath(root)}: {exc}"


def _rclone_test_write(remote):
    marker_name = f".neosky_sync_test_{int(time.time())}.tmp"
    with tempfile.TemporaryDirectory() as tmp_dir:
        local_path = os.path.join(tmp_dir, marker_name)
        with open(local_path, "w") as f:
            f.write("Neosky Drone Diagnostic Platform -- sync test file, safe to delete.\n")

        dest = f"{remote.rstrip('/')}/{marker_name}"
        ok, message = _run_rclone(["copyto", local_path, dest])
        if not ok:
            return False, f"rclone copy to {remote} failed: {message}"

        # Best-effort cleanup -- a leftover test file in Drive isn't worth
        # failing the check over.
        _run_rclone(["deletefile", dest])
        return True, f"Success -- wrote and removed a test file at {dest} via rclone."


def _run_rclone(args):
    """Runs `rclone <args>`, returns (ok, stderr-or-exception-message).
    Never raises -- every caller here treats sync as best-effort."""
    try:
        result = subprocess.run(
            ["rclone", *args],
            capture_output=True, text=True, timeout=_RCLONE_TIMEOUT_S,
        )
        if result.returncode != 0:
            return False, (result.stderr or result.stdout or f"exit code {result.returncode}").strip()
        return True, ""
    except FileNotFoundError:
        return False, "rclone is not installed (or not on PATH) on this server"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {_RCLONE_TIMEOUT_S}s -- check network/auth (try `rclone config reconnect`)"
    except Exception as exc:
        return False, str(exc)


def upload_to_drive(local_path, department_name, msn, dest_filename, kind="Reports"):
    """Best-effort: copies local_path to
    <destination>/<Department>/<MSN>/<kind>/dest_filename, via whichever
    of DRIVE_SYNC_RCLONE_REMOTE/DRIVE_SYNC_ROOT is configured. `kind` is
    "Raw Logs" or "Reports" so the two never mix in the same folder.
    Returns the destination path, or None if sync isn't configured or the
    copy failed for any reason -- callers must never depend on this
    succeeding."""
    try:
        remote = _rclone_remote()
        if remote:
            dest_path = f"{remote.rstrip('/')}/{department_name}/{msn or 'unassigned'}/{kind}/{dest_filename}"
            ok, message = _run_rclone(["copyto", local_path, dest_path])
            if not ok:
                raise RuntimeError(message)
            return dest_path

        root = _folder_root()
        if not root:
            return None

        dest_dir = os.path.join(root, department_name, msn or "unassigned", kind)
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, dest_filename)
        shutil.copy2(local_path, dest_path)
        return dest_path

    except Exception:
        current_app.logger.warning(
            "Drive sync failed for %s (department=%s, msn=%s) -- "
            "the file is still safe in the local archive.",
            dest_filename, department_name, msn, exc_info=True,
        )
        return None
