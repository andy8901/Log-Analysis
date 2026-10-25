import os

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Loads a .env file in the project root into os.environ, if one exists --
# regardless of which script starts the app (python wsgi.py, run_windows.bat,
# gunicorn/waitress directly, ...) or which terminal/session it's run from.
#
# override=True: .env always wins, even over a real environment variable
# already set for the process (a leftover PowerShell `$env:`, or a
# persistent Windows System/User variable from an earlier setup attempt).
# This app is configured by editing .env, full stop -- a stray environment
# variable silently out-voting a freshly-edited .env file (e.g. Drive sync
# still writing to an old drive letter after .env was updated to a new one)
# is exactly the confusing failure mode override=False allowed.
_drive_sync_root_before_dotenv = os.environ.get("DRIVE_SYNC_ROOT")
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


class Config:
    # Bumped by hand on each notable release; shown in the sidebar footer so
    # a department reporting an issue (or an admin checking whether a
    # server has been updated) can say which version they're on.
    APP_VERSION = "1.4.0"

    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-secret-key-change-me")

    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'app.db')}"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Where original logs are archived, per department/drone, and where
    # generated reports are written before being streamed back to the user.
    ARCHIVE_ROOT = os.environ.get("ARCHIVE_ROOT", os.path.join(BASE_DIR, "archive"))
    UPLOAD_TMP = os.environ.get("UPLOAD_TMP", os.path.join(BASE_DIR, "uploads"))

    MAX_CONTENT_LENGTH = 200 * 1024 * 1024  # same cap as the original app

    ALLOWED_LOG_EXTENSIONS = {"bin", "log", "tlog"}

    # Bootstrap admin, created automatically on first run if no users exist.
    ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

    # Optional: mirror archived logs/reports into a local folder that's
    # synced to Google Drive by the Google Drive for Desktop app (not the
    # Drive API -- no service account/credentials needed). Point this at a
    # folder inside that app's synced location, e.g. on Windows:
    # "G:\My Drive\LogAnalysis" or "C:\Users\<you>\Google Drive\LogAnalysis".
    # Windows/Mac office-PC deployments only -- Google Drive for Desktop has
    # no Linux build, so this does nothing on a Linux server; use
    # DRIVE_SYNC_RCLONE_REMOTE there instead. Leave both unset to disable
    # Drive sync entirely -- app/drive_sync.py no-ops when neither is set
    # rather than erroring.
    DRIVE_SYNC_ROOT = os.environ.get("DRIVE_SYNC_ROOT")

    # Optional, and the one to use on a real server (Linux has no Google
    # Drive for Desktop): an rclone remote:path, e.g. "gdrive:LogAnalysis".
    # Install rclone and run `rclone config` once to connect a Google
    # account (same no-API-project story as Drive for Desktop), then set
    # this to "<the name you gave it>:<folder>". If both this and
    # DRIVE_SYNC_ROOT are set, this one wins.
    DRIVE_SYNC_RCLONE_REMOTE = os.environ.get("DRIVE_SYNC_RCLONE_REMOTE")

    # True if some other environment variable (persistent OS var, or a
    # leftover PowerShell $env: from an earlier terminal) had a *different*
    # DRIVE_SYNC_ROOT value before .env was applied -- surfaced on the
    # Admin Overview page so a "wrong drive letter" report is explainable
    # at a glance instead of requiring a shell on the server to diagnose.
    DRIVE_SYNC_ROOT_ENV_OVERRIDDEN = bool(
        _drive_sync_root_before_dotenv and _drive_sync_root_before_dotenv != DRIVE_SYNC_ROOT
    )
    DRIVE_SYNC_ROOT_PRIOR_ENV_VALUE = _drive_sync_root_before_dotenv

    # Optional: the factory/in-house testing site's coordinates, used to
    # tell "internal testing" flights apart from flights at a customer's
    # own site on the Organization Overview's location breakdown -- a
    # flight whose takeoff point (home_lat/home_lon) falls within
    # FACTORY_RADIUS_KM of this point is classified "Factory", everything
    # else "Field". Unset (either coordinate blank) disables the location
    # breakdown entirely rather than guessing a default location.
    FACTORY_LAT = float(os.environ["FACTORY_LAT"]) if os.environ.get("FACTORY_LAT") else None
    FACTORY_LON = float(os.environ["FACTORY_LON"]) if os.environ.get("FACTORY_LON") else None
    FACTORY_RADIUS_KM = float(os.environ.get("FACTORY_RADIUS_KM", "10"))
