# Neosky Drone Diagnostic Platform

A centralized, multi-department web platform for uploading ArduPilot flight
logs (`.bin` / `.log` / `.tlog`) and getting back a health diagnostic:
pass/warning/critical status, root-cause alerts, time-series graphs, a
flight-path replay, and downloadable Word/Excel reports (now with chart
images embedded in the Word report, not just tables).

The log-parsing and diagnostic engine (`analyzer.py`, `rca.py`,
`multilog_analyzer.py`, `baselines/`) is unchanged from the original tools.
What's built around it: authentication, per-department *and per-person*
data isolation, a central drone registry keyed by MSN, department
dashboards, cross-department admin analytics, a bulk "Multilog Analysis"
mode, and optional Google Drive sync.

## Roles

- **Department user** — logs in, uploads logs for their own department.
  **Sees only their own uploads, reports, and multilog batches** — not a
  colleague's, even within the same department. Individual activity is
  private; the drone registry (MSN/model/status) is still shared
  department-wide, since that's shared hardware, not personal activity.
- **Administrator** — the only role that sees the full picture: every
  department, every person's individual activity, cross-department drone
  aggregates, and can bulk-export raw data to Excel. Admins don't upload
  logs themselves; they view a department's dashboard the same way a
  department user would, via `?department_id=`.

Access control is enforced server-side on every route — nobody can read,
export, or browse another person's (or department's) uploads, drones, or
reports by guessing an ID.

## Two analysis modes

- **Individual Log Analysis** (`/upload`) — one log in, one full
  diagnostic report out (status, RCA, charts, map replay, timeline),
  linked to a specific drone by MSN.
- **Multilog Analysis** (`/multilog`) — many logs in at once, one flat
  Excel report out (one row per log, ~150 extracted fields per row), using
  the same per-log field extraction as the standalone desktop Multilog
  Analyzer tool. Not tied to the drone registry — it's a bulk
  research/export action.

## Data model

```
Department -> Users
           -> Drones (unique MSN)
                   -> Uploads -> AnalysisResult (full process_log() report, as JSON)
           -> MultilogBatch (many logs -> one Excel report)
```

Every upload is tied to a department, the user who submitted it, and (once
resolved) a drone. If an uploaded MSN already belongs to a *different*
department, the upload is rejected and an `MsnConflict` row is created for
an admin to reassign or dismiss — logs are never silently merged across
departments or MSNs.

## Admin analytics

- **Overview** (`/admin/`) — org-wide totals, logs analyzed today/YTD, a
  14-day daily activity trend, and a per-department breakdown.
- **All Drones** (`/admin/drones`) — every drone across every department,
  with aggregates: total logs/flights, avg flight time, avg battery sag %
  (voltage drop across a flight), avg distance flown (summed GPS track,
  not just farthest point from home), critical/warning/pass counts.
- **People** (`/admin/people`) — per-person logs today/YTD/total and last
  upload time. This is the one place individual breakdowns are visible.
- **Export Raw Data** (`/admin/export/raw-data.xlsx`) — one Excel file,
  every completed upload across every department, one row each.

## Running locally

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
python wsgi.py
```

Open http://localhost:8080 — you'll land on the login page.

On first run, since there are no users yet, a default admin account is
bootstrapped automatically:

- username: `admin` (override with `ADMIN_USERNAME`)
- password: `admin123` (override with `ADMIN_PASSWORD` — **set this before
  first run in anything but a throwaway local environment**)

Log in as admin, create a department under **Departments**, then create a
department user account for it under **Users**. That department user can
then log in and start uploading logs.

**Note on upgrading from an older checkout:** this version added columns
to `AnalysisResult` and a new `MultilogBatch` table. There's no migration
tool (Alembic) wired up yet — delete `instance/app.db` and let it
re-bootstrap rather than running against an old schema.

## Running it for your whole organization on Windows

The Flask dev server (`python wsgi.py`) only handles one request at a
time — fine for testing alone, not for multiple departments hitting it at
once. Use **Waitress** instead (a pure-Python production server that,
unlike Gunicorn, actually runs on Windows):

```powershell
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
run_windows.bat
```

`run_windows.bat` starts Waitress bound to `0.0.0.0:8080` with 8 threads.
To make it reachable from other PCs in your office:

1. Find this PC's local IP: `ipconfig` → look for "IPv4 Address" (e.g.
   `192.168.1.50`).
2. Allow inbound traffic on port 8080 through Windows Firewall: Windows
   Defender Firewall → Advanced Settings → Inbound Rules → New Rule → Port
   → TCP → 8080 → Allow.
3. Everyone else on the same network/VPN opens `http://192.168.1.50:8080`.

This PC needs to stay on and `run_windows.bat` needs to keep running — for
unattended operation, wrap it as a Windows service with
[NSSM](https://nssm.cc/) so it starts automatically and survives logoffs.

If people need access from **outside** your office network, that needs
either a port-forward on your router (simple, but exposes your PC directly
to the internet — only do this with `SECRET_KEY`/`ADMIN_PASSWORD` set
properly and ideally a reverse proxy in front) or a tunnel tool like
Cloudflare Tunnel (free, gives a stable public hostname without opening
inbound ports on your router).

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | insecure dev value | Flask session signing key — set a real random value in production |
| `DATABASE_URL` | local `instance/app.db` SQLite file | SQLAlchemy connection string; point this at Postgres etc. for a real deployment |
| `ARCHIVE_ROOT` | `./archive` | Where original uploaded logs are permanently archived, per department/drone |
| `UPLOAD_TMP` | `./uploads` | Scratch space for files mid-upload |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | Only read once, to bootstrap the first admin account when the DB is empty |
| `GOOGLE_SERVICE_ACCOUNT_FILE` / `GOOGLE_DRIVE_ROOT_FOLDER_ID` | unset (Drive sync disabled) | See below |

## Optional: Google Drive sync (free-tier compatible)

Every upload mirrors both the raw log and its generated reports into a
Google Drive folder structure of
`<root>/<Department>/<MSN>/<Raw Logs|Reports>/<filename>` — raw logs and
reports land in separate subfolders. This needs **no paid Google
Workspace** — a regular free Gmail account's "My Drive" folder works, as
long as it's shared with a service account.

1. [console.cloud.google.com](https://console.cloud.google.com) → create/
   pick a project → enable the **Google Drive API**.
2. IAM & Admin → Service Accounts → **Create Service Account** (any name,
   no special project roles needed).
3. Open it → **Keys** tab → **Add Key** → **Create new key** → JSON.
   Download it, keep it private — this is a credential.
4. Open the JSON file, copy the `"client_email"` value (looks like
   `xxx@your-project.iam.gserviceaccount.com`).
5. In Google Drive, create (or pick) a folder to be the root, click
   **Share**, paste that service-account email, give it **Editor** access.
6. Copy the folder's id from its URL:
   `https://drive.google.com/drive/folders/<THIS_PART>`.
7. Set the two env vars:
   ```bash
   GOOGLE_SERVICE_ACCOUNT_FILE=/path/to/the/downloaded-key.json
   GOOGLE_DRIVE_ROOT_FOLDER_ID=<the folder id from step 6>
   ```

Drive sync is entirely best-effort: if it's not configured, or Drive is
unreachable, or the credentials break, uploads still succeed — only the
Drive mirror step is skipped (with a warning logged), never the upload
itself.

A personal Gmail account's quota caveat: files the service account
creates count against *its own* Drive storage, not the Gmail owner's. For
ArduPilot logs (a few MB to low hundreds of MB each) that's a long runway
at zero cost, but it's worth knowing if upload volume gets heavy.

## Deploying to the cloud

The app ships with a production `Dockerfile` (Gunicorn, binds to `PORT`,
debug off) and a Render Blueprint (`render.yaml`).

**This platform's primary data is now the database** (departments, users,
drones, uploads, analysis results), not just the archived log files — so
there's no usable "free tier with an ephemeral filesystem" option on
Render/Railway/Fly.io. If cost matters and you want a real public server
rather than hosting from an office PC, **Oracle Cloud's "Always Free"
tier** gives a genuinely free-forever persistent VM (not a trial) that can
run this with SQLite and the local archive, no monthly bill.

```bash
docker build -t log-analysis .
docker run -p 8080:8080 \
  -e SECRET_KEY=change-me \
  -e ADMIN_PASSWORD=change-me \
  -v $(pwd)/data:/app/data \
  -e DATABASE_URL=sqlite:////app/data/app.db \
  -e ARCHIVE_ROOT=/app/data/archive \
  log-analysis
```

## Known limitation: synchronous processing

Analysis still runs synchronously inside the upload request (as in the
original app) rather than on a background queue — an upload's HTTP request
doesn't return until `process_log()` finishes. This matches the original
tool's behavior and keeps the diagnostic engine untouched, but for very
large logs or many concurrent uploads, moving analysis to a background
worker (e.g. Celery or RQ + Redis) would be the natural next step; the
`Upload.status` field (`queued` / `processing` / `completed` / `failed`)
is already in place to support that without a schema change.
