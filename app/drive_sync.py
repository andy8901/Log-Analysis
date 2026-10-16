"""Mirrors both the raw uploaded log and its generated reports into a
Google Drive folder, organised as <Department>/<MSN>/<Raw Logs|Reports>/
<filename>, via a service account -- raw logs and reports land in their
own separate subfolders rather than mixed together. Works with either a
true Shared Drive or an ordinary folder that's been shared with the
service account's email as Editor -- GOOGLE_DRIVE_ROOT_FOLDER_ID is just
the destination folder's id either way (the trailing segment of its
"https://drive.google.com/drive/folders/<id>" URL). Inert until
GOOGLE_SERVICE_ACCOUNT_FILE and GOOGLE_DRIVE_ROOT_FOLDER_ID are both set --
every public function is a safe no-op (logs a warning, never raises) so a
Drive outage or missing credentials never blocks an upload."""

import threading

from flask import current_app

_SCOPES = ["https://www.googleapis.com/auth/drive"]

# One cached service client + folder-id lookup per process -- Drive API
# calls are network round-trips, and re-resolving the same department/MSN
# folder on every single upload would otherwise cost 2 extra API calls per
# upload forever.
_service = None
_folder_cache = {}
_lock = threading.Lock()


def is_configured():
    return bool(
        current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE")
        and current_app.config.get("GOOGLE_DRIVE_ROOT_FOLDER_ID")
    )


def _get_service():
    global _service
    if _service is not None:
        return _service

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds = service_account.Credentials.from_service_account_file(
        current_app.config["GOOGLE_SERVICE_ACCOUNT_FILE"], scopes=_SCOPES
    )
    _service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _service


def _find_or_create_folder(service, name, parent_id):
    cache_key = (parent_id, name)
    if cache_key in _folder_cache:
        return _folder_cache[cache_key]

    safe_name = name.replace("'", "\\'")
    query = (
        f"name = '{safe_name}' and '{parent_id}' in parents "
        "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
    )
    # includeItemsFromAllDrives/supportsAllDrives are harmless no-ops for a
    # plain shared folder and required for a true Shared Drive -- passing
    # them unconditionally means this works for either without needing to
    # know which kind GOOGLE_DRIVE_ROOT_FOLDER_ID actually points at.
    results = service.files().list(
        q=query,
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields="files(id, name)",
        pageSize=1,
    ).execute()

    files = results.get("files", [])
    if files:
        folder_id = files[0]["id"]
    else:
        created = service.files().create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            supportsAllDrives=True,
            fields="id",
        ).execute()
        folder_id = created["id"]

    _folder_cache[cache_key] = folder_id
    return folder_id


def upload_to_drive(local_path, department_name, msn, dest_filename, kind="Reports"):
    """Best-effort: mirrors local_path into
    <Department>/<MSN>/<kind>/dest_filename under the configured root
    folder. `kind` is "Raw Logs" or "Reports" so the two never mix in the
    same folder. Returns the Drive file id, or None if Drive isn't
    configured or the upload failed for any reason -- callers must never
    depend on this succeeding."""
    try:
        if not is_configured():
            return None

        from googleapiclient.http import MediaFileUpload

        with _lock:
            service = _get_service()
            root_id = current_app.config["GOOGLE_DRIVE_ROOT_FOLDER_ID"]
            dept_folder = _find_or_create_folder(service, department_name, root_id)
            msn_folder = _find_or_create_folder(service, msn or "unassigned", dept_folder)
            kind_folder = _find_or_create_folder(service, kind, msn_folder)

        media = MediaFileUpload(local_path, resumable=False)
        created = service.files().create(
            body={"name": dest_filename, "parents": [kind_folder]},
            media_body=media,
            supportsAllDrives=True,
            fields="id, webViewLink",
        ).execute()
        return created.get("id")

    except Exception:
        current_app.logger.warning(
            "Google Drive sync failed for %s (department=%s, msn=%s) -- "
            "the file is still safe in the local archive.",
            dest_filename, department_name, msn, exc_info=True,
        )
        return None
