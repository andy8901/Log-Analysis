import os

from dotenv import load_dotenv

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Loads a .env file in the project root into os.environ, if one exists --
# regardless of which script starts the app (python wsgi.py, run_windows.bat,
# gunicorn/waitress directly, ...) or which terminal/session it's run from.
# override=False: a real environment variable already set in the shell
# still wins, .env only fills in what isn't already there.
load_dotenv(os.path.join(BASE_DIR, ".env"), override=False)


class Config:
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
    # Leave unset to disable entirely -- app/drive_sync.py no-ops when it's
    # missing rather than erroring.
    DRIVE_SYNC_ROOT = os.environ.get("DRIVE_SYNC_ROOT")
