import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


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
