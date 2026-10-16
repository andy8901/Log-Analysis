# Neosky Drone Diagnostic Platform

A centralized, multi-department web platform for uploading ArduPilot flight
logs (`.bin` / `.log` / `.tlog`) and getting back a health diagnostic:
pass/warning/critical status, root-cause alerts, time-series graphs, an
interactive 3D flight replay (plus a downloadable MP4 of it), and
downloadable Word/Excel reports (now with chart images embedded in the
Word report, not just tables).

The log-parsing and diagnostic engine (`analyzer.py`, `rca.py`,
`multilog_analyzer.py`, `baselines/`) is unchanged from the original tools.
What's built around it: authentication, per-department *and per-person*
data isolation, a central drone registry keyed by MSN, department
dashboards, cross-department admin analytics, an admin-only Multilog
Analysis export, a 3D flight replay, and optional Google Drive sync.

## Roles

- **Department user** — logs in, uploads logs for their own department via
  Individual Log Analysis. **Sees only their own uploads and reports** —
  not a colleague's, even within the same department. Individual activity
  is private; the drone registry (MSN/model/status) is still shared
  department-wide, since that's shared hardware, not personal activity.
  Cannot run Multilog Analysis — that's admin-only (see below).
- **Administrator** — the only role that sees the full picture: every
  department, every person's individual activity, cross-department drone
  aggregates, and Multilog Analysis across everyone's data. Admins don't
  upload logs themselves; they view a department's dashboard the same way
  a department user would, via `?department_id=`.

Access control is enforced server-side on every route — nobody can read,
export, or browse another person's (or department's) uploads, drones, or
reports by guessing an ID.

## Analysis

- **Individual Log Analysis** (`/upload`) — one log in, one full
  diagnostic report out (status, RCA, charts, a 3D flight replay, a
  downloadable MP4 of that replay, timeline), linked to a specific drone
  by MSN. This is the only way a log enters the system — departments and
  individuals upload here, never through a separate bulk-upload path.
- **Multilog Analysis** (`/admin/export/raw-data.xlsx`, admin-only) — one
  flat Excel report, one row per log, ~150 extracted fields per row, using
  the same per-log field extraction as the standalone desktop Multilog
  Analyzer tool. Deliberately **not** something a department or individual
  account can trigger, and never a separate file upload of its own — it
  always runs over logs departments have *already* uploaded via Individual
  Log Analysis, across every department at once.

## Data model

```
Department -> Users
           -> Drones (MSN unique *within* a department)
                   -> Uploads -> AnalysisResult (full process_log() report, as JSON)
```

Every upload is tied to a department, the user who submitted it, and (once
resolved) a drone. MSN only has to be unique within a department, not
organization-wide — two different departments are free to each register
their own drone under the same MSN string; they're entirely separate
records and never merged.

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

Set these either as real environment variables, or (recommended, especially
on Windows) in a `.env` file in the project root — it's loaded
automatically on startup regardless of how you launch the app
(`python wsgi.py`, `run_windows.bat`, gunicorn/waitress directly). A real
environment variable already set in your shell always wins over `.env`.
`.env` is gitignored — it's config, never commit it.

```
SECRET_KEY=something-random
ADMIN_PASSWORD=a-real-password
DRIVE_SYNC_ROOT=I:\My Drive\LogAnalysis
```

A `.env` file beats setting `$env:VAR=...` by hand in PowerShell: that only
applies to the one terminal session it was typed in, so restarting the
server from a new window — or forgetting you typed it — silently reverts
to the old/default value. This is the #1 cause of "I changed the setting
but it's not taking effect."

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | insecure dev value | Flask session signing key — set a real random value in production |
| `DATABASE_URL` | local `instance/app.db` SQLite file | SQLAlchemy connection string; point this at Postgres etc. for a real deployment |
| `ARCHIVE_ROOT` | `./archive` | Where original uploaded logs are permanently archived, per department/drone |
| `UPLOAD_TMP` | `./uploads` | Scratch space for files mid-upload |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | Only read once, to bootstrap the first admin account when the DB is empty |
| `DRIVE_SYNC_ROOT` | unset (Drive sync disabled) | See below |

## Optional: Google Drive sync (the easy way — no Cloud Console)

Every upload mirrors both the raw log and its generated reports into
`<DRIVE_SYNC_ROOT>/<Department>/<MSN>/<Raw Logs|Reports>/<filename>` — raw
logs and reports land in separate subfolders. This is done with a plain
filesystem copy, not the Drive API — no service account, no Cloud Console
project, no JSON key.

1. Install **[Google Drive for Desktop](https://www.google.com/drive/download/)**
   on the PC running this app, and sign in with a normal Google account.
2. It creates a synced folder on disk (Windows default: it shows up as a
   drive letter, e.g. `G:\My Drive`, or under
   `C:\Users\<you>\Google Drive` depending on how you set it up during
   install).
3. Create a subfolder in there for this app, e.g. `G:\My Drive\LogAnalysis`.
4. Set the env var to that path:
   ```powershell
   $env:DRIVE_SYNC_ROOT="G:\My Drive\LogAnalysis"
   ```

That's it — whatever this app writes into that folder, Google Drive for
Desktop uploads in the background automatically, exactly as if you'd
dragged the file into that folder yourself in File Explorer.

Sync is entirely best-effort: if `DRIVE_SYNC_ROOT` isn't set, or the path
is unreachable, or the disk is full, uploads still succeed — only the
Drive-folder copy is skipped (with a warning logged), never the upload
itself. Nothing here touches your Drive storage quota differently than
dragging the same file in manually would.

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
