"""Background worker for the admin folder (bulk) upload and for re-running the flight investigation on older logs.

Files arrive one HTTP request each (so a folder of hundreds of logs never hits a request size or timeout limit)
and are queued here; a single daemon thread per server process analyses them one at a time, exactly like a
normal upload (app/processing.analyze_and_store). Each job claims its upload with an atomic
"queued -> processing" update, so with several server processes (gunicorn workers) a file is never analysed twice.
Files still waiting when the server stopped are picked up again on the next start.
"""
import os
import queue
import threading
from datetime import datetime, timedelta, timezone

_q = queue.Queue()
_lock = threading.Lock()
_thread = None
_app = None


def init_worker(app):
    """Called from create_app(): remembers the app and resumes anything left waiting by a previous run."""
    global _app
    _app = app
    if app.config.get("TESTING_NO_WORKER"):
        return
    try:
        with app.app_context():
            resume_pending()
    except Exception:
        app.logger.warning("bulk upload: could not resume pending files", exc_info=True)


def _ensure_thread():
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_run, name="bulk-upload-worker", daemon=True)
            _thread.start()


def enqueue(kind, upload_id):
    _q.put((kind, upload_id))
    _ensure_thread()


def queue_size():
    return _q.qsize()


def _run():
    from app import db
    while True:
        kind, uid = _q.get()
        try:
            with _app.app_context():
                try:
                    if kind == "analyze":
                        _analyze(uid)
                    elif kind == "reinvestigate":
                        from app.models import Upload
                        from app.processing import refresh_investigation
                        u = Upload.query.get(uid)
                        if u:
                            refresh_investigation(u)
                except Exception:
                    _app.logger.warning("bulk worker: %s %s failed", kind, uid, exc_info=True)
                    db.session.rollback()
                finally:
                    db.session.remove()
        finally:
            _q.task_done()


def _analyze(uid):
    from app import db
    from app.models import Upload
    from app.processing import analyze_and_store
    from app.routes_dept import _sync_reports_to_drive

    claimed = Upload.query.filter_by(id=uid, status="queued").update({"status": "processing"})
    db.session.commit()
    if not claimed:
        return
    u = Upload.query.get(uid)
    if not u.pending_path or not os.path.exists(u.pending_path):
        u.status, u.error_message = "failed", "The uploaded file is no longer on disk (server restarted before it was analysed?)."
        db.session.commit()
        return
    try:
        analyze_and_store(u, u.pending_path, u.department, u.drone, u.user, sync_reports=_sync_reports_to_drive)
    except Exception:
        pass  # already marked failed with the error message by analyze_and_store


def resume_pending():
    """Re-queue files waiting from a previous run, and files left 'processing' by a server that stopped mid-way."""
    from app import db
    from app.models import Upload
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)
    stuck = Upload.query.filter(Upload.status == "processing", Upload.pending_path.isnot(None), Upload.uploaded_at < stale).all()
    for u in stuck:
        u.status = "queued"
    if stuck:
        db.session.commit()
    waiting = Upload.query.filter(Upload.status == "queued", Upload.pending_path.isnot(None)).order_by(Upload.id).all()
    for u in waiting:
        enqueue("analyze", u.id)
    return len(waiting)
