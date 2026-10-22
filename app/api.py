"""Read-only JSON API for other Neosky tools (the Live Twin), authenticated with an API key.

    GET /api/v1/drone-stats[?msn=064]     header  X-API-Key: <LIVE_TWIN_API_KEY>

Per drone MSN: logs uploaded, logs analysed, crashes, and root-cause conclusions (confirmed pilot/system error,
undetermined, other, not concluded yet) plus the investigator's suggested verdicts that nobody has confirmed.
Disabled (404) unless LIVE_TWIN_API_KEY is set.
"""
import hmac
import json
import os

from flask import Blueprint, abort, jsonify, request

from app.models import Drone, Upload

api_bp = Blueprint("api", __name__)


def _empty():
    return {"logs": 0, "analysed": 0, "failed": 0, "crashes": 0, "pilot_error": 0, "system_error": 0,
            "undetermined": 0, "other": 0, "not_concluded": 0, "suggested_pilot": 0, "suggested_system": 0,
            "last_upload": None}


@api_bp.route("/api/v1/drone-stats")
def drone_stats():
    key = os.environ.get("LIVE_TWIN_API_KEY", "")
    if not key:
        abort(404)
    if not hmac.compare_digest(request.headers.get("X-API-Key", ""), key):
        abort(401)
    msn = (request.args.get("msn") or "").strip()
    q = Upload.query.join(Drone, Upload.drone_id == Drone.id)
    if msn:
        q = q.filter(Drone.msn == msn)
    per = {}
    total = _empty()
    for u in q.all():
        m = u.drone.msn
        row = per.setdefault(m, _empty())
        for r in (row, total):
            r["logs"] += 1
            ts = u.uploaded_at.isoformat() if u.uploaded_at else None
            if ts and (r["last_upload"] is None or ts > r["last_upload"]):
                r["last_upload"] = ts
            if u.status == "failed":
                r["failed"] += 1
        if u.status != "completed" or not u.analysis:
            continue
        inv = {}
        try:
            inv = json.loads(u.analysis.report_json).get("investigation") or {}
        except (ValueError, TypeError, AttributeError):
            pass
        crash = inv.get("kind") == "crash"
        conc = u.conclusion if u.conclusion in ("pilot_error", "system_error", "undetermined", "other") else "not_concluded"
        blame = (inv.get("blame") or {}).get("primary") if inv.get("verdict") else None
        for r in (row, total):
            r["analysed"] += 1
            r["crashes"] += int(crash)
            r[conc] += 1
            if conc == "not_concluded" and blame in ("human", "system"):
                r["suggested_pilot" if blame == "human" else "suggested_system"] += 1
    return jsonify({"msn": msn or None, "total": total, "drones": per})
