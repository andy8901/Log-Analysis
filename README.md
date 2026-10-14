# Neosky Drone Diagnostic Platform

A centralized, multi-department web platform for uploading ArduPilot flight
logs (`.bin` / `.log` / `.tlog`) and getting back a health diagnostic:
pass/warning/critical status, root-cause alerts, time-series graphs, a
flight-path replay, and downloadable Word/Excel reports.

The log-parsing and diagnostic engine (`analyzer.py`, `rca.py`, `reports.py`,
`baselines/`) is unchanged from the original single-user tool. What's new is
everything around it: authentication, per-department data isolation, a
central drone registry keyed by MSN, department dashboards, and an admin
console with cross-department visibility.

## Roles

- **Department user** — logs in, uploads logs for their own department,
  sees that department's dashboard, drone registry, and every analysis
  result belonging to it.
- **Administrator** — sees every department, manages departments and user
  accounts, resolves MSN conflicts, and reviews the audit log. Admins don't
  upload logs themselves; they view a department's dashboard the same way a
  department user would, via `?department_id=`.

Access control is enforced server-side on every route — a department user
can never read, export, or browse another department's uploads, drones, or
reports by guessing an ID.

## Data model

```
Department -> Users
           -> Drones (unique MSN)
                   -> Uploads -> AnalysisResult (full process_log() report, as JSON)
```

Every upload is tied to a department, the user who submitted it, and (once
resolved) a drone. If an uploaded MSN already belongs to a *different*
department, the upload is rejected and an `MsnConflict` row is created for
an admin to reassign or dismiss — logs are never silently merged across
departments or MSNs.

## Running locally

```bash
python -m venv venv
source venv/bin/activate
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

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SECRET_KEY` | insecure dev value | Flask session signing key — set a real random value in production |
| `DATABASE_URL` | local `instance/app.db` SQLite file | SQLAlchemy connection string; point this at Postgres etc. for a real deployment |
| `ARCHIVE_ROOT` | `./archive` | Where original uploaded logs are permanently archived, per department/drone |
| `UPLOAD_TMP` | `./uploads` | Scratch space for files mid-upload |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | Only read once, to bootstrap the first admin account when the DB is empty |

## Deploying

The app ships with a production `Dockerfile` (Gunicorn, binds to `PORT`,
debug off) and a Render Blueprint (`render.yaml`).

**This platform's primary data is now the database** (departments, users,
drones, uploads, analysis results), not just the archived log files — so,
unlike the original tool, there is no usable "free tier with an ephemeral
filesystem" option. Use a host/plan with a persistent disk and point
`DATABASE_URL` + `ARCHIVE_ROOT` at it. `render.yaml` is pre-configured for
Render's "starter" plan with a 10GB disk mounted at `/app/data`.

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
