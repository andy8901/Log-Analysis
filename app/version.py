"""App version, shown in the sidebar, on the login page and in generated reports.

Bump __version__ (and add a line to CHANGELOG.md) with every release. The build id is the git commit the
server is running, when it can be read (a git checkout), or APP_BUILD from the environment (e.g. set by CI or
the Docker build); it's informational only.
"""
import os
import subprocess

__version__ = "2.1.0"
RELEASE_DATE = "2026-10-22"
ANALYSIS_ENGINES = {"investigator": None}  # filled lazily from investigator.VERSION


def _git_build():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(["git", "-C", root, "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=3)
        return out.stdout.strip() or None
    except Exception:
        return None


BUILD = os.environ.get("APP_BUILD") or _git_build()


def version_info():
    try:
        import investigator
        inv = investigator.VERSION
    except Exception:
        inv = None
    return {"version": __version__, "release_date": RELEASE_DATE, "build": BUILD, "investigator": inv,
            "label": f"v{__version__}" + (f" · {BUILD}" if BUILD else "")}
