"""Aggregate/derived metrics for dashboards -- all computed on top of data
analyzer.py already produces (gps_path, summary.initial/final_voltage).
Nothing here changes what the analysis engine itself detects or reports;
this only summarizes already-computed per-flight results across many
flights/drones/people."""

import math
from datetime import datetime, time, timezone


def gps_path_length_m(gps_path):
    """Total distance flown: sum of consecutive-point distances along the
    flight's own GPS track -- not the same as summary.max_range_m (farthest
    single point from home), since a flight can travel far in total while
    never getting far from its start point (e.g. a long survey pattern)."""
    if not gps_path or len(gps_path) < 2:
        return None
    total = 0.0
    for (lat1, lon1), (lat2, lon2) in zip(gps_path, gps_path[1:]):
        total += math.sqrt((lat2 - lat1) ** 2 + (lon2 - lon1) ** 2) * 111139
    return round(total, 1)


def battery_sag_pct(summary):
    """(initial - final) / initial, as a percentage. None if voltage data
    isn't available for this flight, rather than a misleading 0."""
    v0 = summary.get("initial_voltage")
    v1 = summary.get("final_voltage")
    if not v0 or v1 is None:
        return None
    try:
        return round((float(v0) - float(v1)) / float(v0) * 100, 2)
    except (TypeError, ZeroDivisionError):
        return None


def _series_mean(charts, key, field):
    series = (charts or {}).get(key) or {}
    values = series.get(field)
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def derive_result_metrics(report):
    """The denormalized AnalysisResult columns, computed once at upload
    time from a freshly-produced report dict -- all from data analyzer.py
    already computed (gps_path, report['charts'] time series), nothing new
    extracted from the raw log itself."""
    summary = report.get("summary", {}) or {}
    charts = report.get("charts", {}) or {}
    return {
        "flight_duration_min": summary.get("flight_duration"),
        "battery_sag_pct": battery_sag_pct(summary),
        "distance_travelled_m": gps_path_length_m(report.get("gps_path")),
        "avg_altitude_m": _series_mean(charts, "altitude_baro", "alt") or _series_mean(charts, "altitude_gps", "alt"),
        "avg_battery_voltage_v": _series_mean(charts, "battery", "volt"),
        "avg_current_a": _series_mean(charts, "current", "curr"),
    }


def _avg(values):
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 2) if values else None


def today_start():
    # Naive UTC, not aware: SQLite has no real datetime type, so
    # Upload.uploaded_at (stored as datetime.now(timezone.utc)) round-trips
    # back from the DB as a naive datetime with its tzinfo dropped --
    # comparing against an aware one here would raise TypeError, and
    # comparing an aware one at the SQL-query level would silently compare
    # wrong due to the mismatched ISO string formats.
    return datetime.combine(datetime.now(timezone.utc).date(), time.min)


def ytd_start():
    now = datetime.now(timezone.utc)
    return datetime(now.year, 1, 1)


def drone_aggregate(drone):
    completed = [u for u in drone.uploads if u.status == "completed" and u.analysis]
    results = [u.analysis for u in completed]
    return {
        "total_logs": len(drone.uploads),
        "total_flights": len(completed),
        "avg_flight_time_min": _avg([r.flight_duration_min for r in results]),
        "avg_battery_sag_pct": _avg([r.battery_sag_pct for r in results]),
        "avg_distance_m": _avg([r.distance_travelled_m for r in results]),
        "critical_count": sum(1 for r in results if r.report_status == "CRITICAL"),
        "warning_count": sum(1 for r in results if r.report_status == "WARNING"),
        "pass_count": sum(1 for r in results if r.report_status == "PASS"),
    }


def flight_summary_aggregate(uploads):
    """The 'graphical dashboard' panel: total logs reviewed, and org-wide
    (or department-wide, or per-person, depending on what `uploads` the
    caller already scoped) averages across every completed flight in that
    set, plus every flight's takeoff point for a map. Used identically by
    the admin overview, the admin per-department drill-in, and a
    department's own dashboard -- only the input list's scope changes."""
    completed = [u for u in uploads if u.status == "completed" and u.analysis]
    results = [u.analysis for u in completed]

    takeoffs = []
    for u in completed:
        report = u.analysis.get_report()
        home = (report.get("summary") or {})
        lat, lon = home.get("home_lat"), home.get("home_lon")
        if lat is not None and lon is not None:
            takeoffs.append({
                "lat": lat, "lon": lon,
                "drone": u.drone.msn if u.drone else u.msn_entered,
                "department": u.department.name,
                "date": u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else ""),
            })

    return {
        "total_logs": len(uploads),
        "total_flights": len(completed),
        "avg_flight_time_min": _avg([r.flight_duration_min for r in results]),
        "avg_distance_m": _avg([r.distance_travelled_m for r in results]),
        "avg_altitude_m": _avg([r.avg_altitude_m for r in results]),
        "avg_battery_voltage_v": _avg([r.avg_battery_voltage_v for r in results]),
        "avg_current_a": _avg([r.avg_current_a for r in results]),
        "takeoffs": takeoffs,
    }


def person_aggregate(user, uploads):
    """`uploads` is this user's full Upload list (caller already queried
    it) -- avoids an extra query per person on a page listing many people."""
    today = today_start()
    ytd = ytd_start()
    completed = [u for u in uploads if u.status == "completed"]
    return {
        "total_logs": len(uploads),
        "logs_today": sum(1 for u in completed if u.uploaded_at and u.uploaded_at >= today),
        "logs_ytd": sum(1 for u in completed if u.uploaded_at and u.uploaded_at >= ytd),
        "last_upload_at": max((u.uploaded_at for u in uploads), default=None),
    }


def _naive_now():
    # Matches today_start()/ytd_start(): naive UTC, since Upload.uploaded_at
    # round-trips from SQLite without tzinfo.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def period_window_counts(items, days=7, when=lambda x: x.uploaded_at):
    """(current, previous) counts of `items` falling in the last `days`
    days vs. the `days`-day window before that -- the raw input for a
    '+12% vs last 7 days' style trend badge."""
    from datetime import timedelta

    now = _naive_now()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)
    current = sum(1 for x in items if when(x) and cur_start <= when(x) <= now)
    previous = sum(1 for x in items if when(x) and prev_start <= when(x) < cur_start)
    return current, previous


def trend_pct(current, previous):
    """Percentage change current-vs-previous, or None when there's no prior
    period to compare against (so the template can show a neutral badge
    instead of a misleading +100%)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100, 1)


def active_drone_trend(uploads, days=7):
    """(current, previous) counts of *distinct* drones with at least one
    upload in each window -- an 'active fleet' trend rather than a raw
    upload-count trend."""
    from datetime import timedelta

    now = _naive_now()
    cur_start = now - timedelta(days=days)
    prev_start = now - timedelta(days=2 * days)
    cur_drones = {u.drone_id for u in uploads if u.drone_id and u.uploaded_at and cur_start <= u.uploaded_at <= now}
    prev_drones = {u.drone_id for u in uploads if u.drone_id and u.uploaded_at and prev_start <= u.uploaded_at < cur_start}
    return len(cur_drones), len(prev_drones)


def daily_flight_trend(uploads, days=14):
    """[{date, total, successful, anomalies}, ...] oldest first, zero-
    filled -- the 'Flights Trend' line chart. `anomalies` is flights whose
    report came back WARNING or CRITICAL; `successful` is the rest."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    buckets = {today - timedelta(days=i): {"total": 0, "successful": 0, "anomalies": 0} for i in range(days)}
    for u in uploads:
        if u.status != "completed" or not u.uploaded_at:
            continue
        d = u.uploaded_at.date()
        if d not in buckets:
            continue
        buckets[d]["total"] += 1
        status = u.analysis.report_status if u.analysis else None
        if status in ("WARNING", "CRITICAL"):
            buckets[d]["anomalies"] += 1
        else:
            buckets[d]["successful"] += 1
    ordered = sorted(buckets.items())
    return [{"date": d.strftime("%b %d"), **v} for d, v in ordered]


def issue_severity_breakdown(uploads):
    """Counts of completed flights by report_status -- the 'Issue
    Severity' donut. Only the two abnormal tiers are meaningful as a
    breakdown of *issues*; PASS is included for completeness."""
    counts = {"CRITICAL": 0, "WARNING": 0, "PASS": 0}
    for u in uploads:
        if u.status == "completed" and u.analysis and u.analysis.report_status in counts:
            counts[u.analysis.report_status] += 1
    return counts


_ANOMALY_CATEGORIES = (
    ("GPS Loss", ("gps", "satellite", "hdop", "ekf", "fence")),
    ("Motor Issue", ("motor", "esc", "vibration", "vibe")),
    ("Battery", ("battery", "voltage", "volt", "sag", "current", "failsafe")),
    ("Sensor", ("sensor", "compass", "mag", "baro", "imu", "gyro")),
)


def _categorize_alert(text):
    """Buckets an already-produced alert string into a display category,
    purely for the 'Anomaly Types' donut -- reads analyzer.py/rca.py's own
    output text, never re-derives or changes what they flagged."""
    low = (text or "").lower()
    for label, keywords in _ANOMALY_CATEGORIES:
        if any(k in low for k in keywords):
            return label
    return "Other"


def anomaly_type_breakdown(uploads):
    counts = {}
    for u in uploads:
        if u.status != "completed" or not u.analysis or u.analysis.report_status not in ("WARNING", "CRITICAL"):
            continue
        alerts = u.analysis.get_top_alerts()
        label = _categorize_alert(alerts[0]) if alerts else "Other"
        counts[label] = counts.get(label, 0) + 1
    return counts


def recent_anomalies(uploads, limit=6):
    """Most recent flights that came back WARNING/CRITICAL, for the
    'Recent Anomalies' list -- `uploads` is whatever scope the caller
    already filtered to (org-wide, one department, or one person)."""
    candidates = [
        u for u in uploads
        if u.status == "completed" and u.analysis and u.analysis.report_status in ("WARNING", "CRITICAL")
    ]
    candidates.sort(key=lambda u: u.uploaded_at or datetime.min, reverse=True)

    out = []
    for u in candidates[:limit]:
        alerts = u.analysis.get_top_alerts()
        out.append({
            "upload_id": u.id,
            "msn": u.drone.msn if u.drone else u.msn_entered,
            "department": u.department.name if u.department else None,
            "severity": u.analysis.report_status,
            "message": alerts[0] if alerts else "Anomaly detected",
            "date": u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else ""),
        })
    return out


def top_drones_from_uploads(uploads, limit=5, include_department=False):
    """Ranks the drones appearing in `uploads` by issue count -- grouped
    from the already-scoped upload list itself (never Drone.uploads), so a
    department account's own-uploads-only view never pulls in a
    colleague's flights against the same drone."""
    by_drone = {}
    for u in uploads:
        if not u.drone_id:
            continue
        by_drone.setdefault(u.drone_id, []).append(u)

    rows = []
    for drone_id, us in by_drone.items():
        drone = us[0].drone
        completed = [u for u in us if u.status == "completed" and u.analysis]
        issues = sum(1 for u in completed if u.analysis.report_status in ("CRITICAL", "WARNING"))
        last = max(us, key=lambda u: u.uploaded_at or datetime.min)
        row = {
            "drone_id": drone_id,
            "msn": drone.msn if drone else (us[0].msn_entered or "—"),
            "model": drone.model if drone else (us[0].drone_model or "—"),
            "total_flights": len(completed),
            "issues": issues,
            "last_flight": last.flight_date or (last.uploaded_at.strftime("%Y-%m-%d") if last.uploaded_at else "—"),
        }
        if include_department:
            row["department"] = us[0].department.name if us[0].department else "—"
        rows.append(row)

    rows.sort(key=lambda r: (r["issues"], r["total_flights"]), reverse=True)
    return rows[:limit]


def drone_flight_series(uploads):
    """Chronological per-flight series for one drone (duration, battery
    sag, distance) -- the 'drone wise' trend chart on a drone's detail
    page. `uploads` should already be scoped to the viewer's own uploads
    when the caller isn't an admin."""
    completed = sorted(
        (u for u in uploads if u.status == "completed" and u.analysis),
        key=lambda u: u.uploaded_at or datetime.min,
    )
    return {
        "labels": [u.flight_date or (u.uploaded_at.strftime("%Y-%m-%d") if u.uploaded_at else "") for u in completed],
        "duration": [u.analysis.flight_duration_min for u in completed],
        "battery_sag": [u.analysis.battery_sag_pct for u in completed],
        "distance": [u.analysis.distance_travelled_m for u in completed],
    }


def _ml_num(value):
    """A multilog_analyzer.py field is either a real number or the literal
    string multilog_analyzer.NA ('N/A (not in ArduPilot logs)') / 'N/A' for
    a field that log simply doesn't contain -- never a value to average or
    count as an issue. A value read back from an admin-imported Excel can
    also be NaN (float): pandas reads a blank cell, or any text in its own
    default NA-values list -- "None"/"N/A"/"null"/etc, case-insensitively
    -- as NaN, not the original string, so that has to be treated the same
    as "missing" here too."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value == value:  # excludes NaN (NaN != NaN)
        return value
    return None


def _ml_text(value):
    """Same deal as _ml_num but for a field read as text -- "" for
    anything that isn't a real value (None, NaN, or one of the "nothing
    here" placeholder strings), the stripped string otherwise."""
    if value is None:
        return ""
    if isinstance(value, float) and value != value:  # NaN
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("none", "nan", "n/a", "null") else text


def _investigation_texts(inv):
    """All title/text strings from one upload's investigation (findings +
    checks), lowercased and joined -- a cheap substring-matching surface for
    bucketing an already-produced investigation by category, the same
    'read the already-computed text, never re-derive it' pattern
    _categorize_alert() uses for analyzer.py's own alerts."""
    parts = []
    for item in (inv.get("findings") or []) + (inv.get("checks") or []):
        parts.append(str(item.get("title") or ""))
        parts.append(str(item.get("text") or ""))
    return " ".join(parts).lower()


def investigation_aggregate(uploads):
    """Org/department/person-wide rollup of investigator.py's per-flight
    investigation data (report['investigation']) -- read-only, same
    'summarize what's already computed' pattern as multilog_aggregate().
    Only uploads that already carry investigation data contribute (computed
    at upload time for a new .bin upload, or lazily backfilled on first view
    -- see routes_dept._ensure_investigation()); this never runs
    investigate() itself, so it stays cheap on a dashboard hit and simply
    reflects whatever subset currently has the data, same "-- when
    unavailable" pattern as the rest of this module.

    Two items this dashboard would otherwise want -- a PreArm-issue count
    and an average cold-start time -- have no source here: investigator.py
    doesn't surface either (PreArm messages are filtered out of its
    findings, and it computes no cold-start figure), and per this project's
    standing rule investigator.py's own detection logic is never extended
    to manufacture one."""
    today = today_start()
    scored = 0
    airtimes_min, max_dists, max_alts = [], [], []
    battery_fs_airtimes_min = []
    gcs_fs_dists = []
    rc_fs_dists = []
    thrust_issue = gps_issue = ekf_issue = motor_issue = 0
    human_error = system_error = 0
    crashes = crashes_today = 0

    for u in uploads:
        if u.status != "completed" or not u.analysis:
            continue
        report = u.analysis.get_report()
        inv = report.get("investigation") if report else None
        if not inv:
            continue
        scored += 1

        fig = inv.get("figures") or {}
        airtime_s = fig.get("airtime")
        airtime_min = airtime_s / 60.0 if airtime_s else None
        if airtime_min is not None:
            airtimes_min.append(airtime_min)
        if fig.get("maxDist") is not None:
            max_dists.append(fig["maxDist"])
        if fig.get("maxAlt") is not None:
            max_alts.append(fig["maxAlt"])

        text = _investigation_texts(inv)
        if "thrust" in text:
            thrust_issue += 1
        if "motor" in text:
            motor_issue += 1
        if "gps" in text:
            gps_issue += 1
        if "ekf" in text or "navigation" in text:
            ekf_issue += 1
        if "battery failsafe" in text and airtime_min is not None:
            battery_fs_airtimes_min.append(airtime_min)
        if "gcs" in text and "failsafe" in text and fig.get("maxDist") is not None:
            gcs_fs_dists.append(fig["maxDist"])
        if ("rc link lost" in text or "radio failsafe" in text) and fig.get("maxDist") is not None:
            rc_fs_dists.append(fig["maxDist"])

        primary = (inv.get("verdict") or {}).get("primary")
        if primary == "human":
            human_error += 1
        elif primary == "system":
            system_error += 1

        if inv.get("kind") == "crash":
            crashes += 1
            if u.uploaded_at and u.uploaded_at >= today:
                crashes_today += 1

    return {
        "scored_flights": scored,
        "avg_airtime_min": _avg(airtimes_min),
        "avg_max_distance_m": _avg(max_dists),
        "avg_max_altitude_m": _avg(max_alts),
        "avg_airtime_at_battery_fs_min": _avg(battery_fs_airtimes_min),
        "avg_distance_at_gcs_fs_m": _avg(gcs_fs_dists),
        "avg_distance_at_rc_fs_m": _avg(rc_fs_dists),
        "thrust_issue_flights": thrust_issue,
        "motor_issue_flights": motor_issue,
        "gps_issue_flights": gps_issue,
        "ekf_issue_flights": ekf_issue,
        "human_error_flights": human_error,
        "system_error_flights": system_error,
        "crash_count": crashes,
        "crashes_today": crashes_today,
    }


def investigation_category_breakdown(uploads):
    """Counts completed flights by investigation issue category -- the
    'Issue Breakdown' donut, built from the same investigator.py text as
    investigation_aggregate() above. A flight can land in more than one
    bucket (e.g. a GPS glitch that also tripped an EKF check), since each
    bucket answers its own yes/no question about that flight rather than
    assigning one exclusive category the way the severity donut does."""
    counts = {
        "Thrust Loss": 0, "Motor": 0, "GPS": 0, "EKF": 0,
        "Battery Failsafe": 0, "GCS/RC Failsafe": 0, "Crash": 0,
    }
    for u in uploads:
        if u.status != "completed" or not u.analysis:
            continue
        report = u.analysis.get_report()
        inv = report.get("investigation") if report else None
        if not inv:
            continue
        text = _investigation_texts(inv)
        if "thrust" in text:
            counts["Thrust Loss"] += 1
        if "motor" in text:
            counts["Motor"] += 1
        if "gps" in text:
            counts["GPS"] += 1
        if "ekf" in text or "navigation" in text:
            counts["EKF"] += 1
        if "battery failsafe" in text:
            counts["Battery Failsafe"] += 1
        if "gcs" in text and "failsafe" in text or "rc link lost" in text or "radio failsafe" in text:
            counts["GCS/RC Failsafe"] += 1
        if inv.get("kind") == "crash":
            counts["Crash"] += 1
    return {k: v for k, v in counts.items() if v > 0} or counts


def conclusion_breakdown(uploads):
    """Counts of completed, analyzed flights by their closing conclusion
    (see models.CONCLUSION_CHOICES) -- "yet to decide" is the undetermined
    bucket; a flight with no conclusion at all yet (conclusion is None,
    i.e. nobody has closed it out) is counted there too, since from a
    dashboard's point of view an unconcluded flight is exactly as
    undecided as one explicitly marked that way."""
    counts = {"pilot_error": 0, "system_error": 0, "undetermined": 0, "other": 0}
    for u in uploads:
        if u.status != "completed" or not u.analysis:
            continue
        key = u.conclusion if u.conclusion in counts else "undetermined"
        counts[key] += 1
    return counts


def todays_highlight(uploads):
    """The single most attention-worthy thing that happened today, for a
    one-line 'highlight of the day' callout -- a crash (investigator.py's
    kind == 'crash') if there was one, else the most recent CRITICAL
    flight, else None for both. Reads only already-computed report/
    investigation data."""
    today = today_start()
    todays = [u for u in uploads if u.status == "completed" and u.analysis and u.uploaded_at and u.uploaded_at >= today]
    todays.sort(key=lambda u: u.uploaded_at, reverse=True)

    crash = None
    critical = None
    for u in todays:
        report = u.analysis.get_report()
        inv = (report or {}).get("investigation") or {}
        msn = u.drone.msn if u.drone else u.msn_entered
        department = u.department.name if u.department else None
        if crash is None and inv.get("kind") == "crash":
            crash = {"msn": msn, "department": department}
        if critical is None and u.analysis.report_status == "CRITICAL":
            alerts = u.analysis.get_top_alerts()
            critical = {"msn": msn, "department": department, "message": alerts[0] if alerts else "Critical flight"}
    return {"crash": crash, "critical": critical}


def uploads_for_customer(uploads, customer_name):
    """Scopes an already-fetched upload list to one customer (by the
    customer_name recorded on each upload's drone -- see
    routes_dept.process_and_save_log()). customer_name=='' is treated as
    "no customer on file", matching uploads whose drone has none set."""
    if customer_name is None:
        return uploads
    if customer_name == "":
        return [u for u in uploads if not (u.drone and u.drone.customer_name)]
    return [u for u in uploads if u.drone and u.drone.customer_name == customer_name]


def customer_breakdown(uploads):
    """Counts of uploads per customer -- "Unassigned" for a drone with no
    customer_name on file yet. Backs the Organization Overview's customer
    dimension and its selector's option list."""
    counts = {}
    for u in uploads:
        name = (u.drone.customer_name if u.drone else None) or "Unassigned"
        counts[name] = counts.get(name, 0) + 1
    return counts


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return r * 2 * math.asin(math.sqrt(a))


# Default cap on brand-new (not-yet-cached) reverse-geocoding lookups a
# single call into the functions below will make -- keeps a page load
# bounded even when many flights have never been classified yet, since
# each live lookup is rate-limited to ~1/second (see app/geo.py). Callers
# that process the same scope more than once in one request (e.g. the
# admin dashboard's filtered-list-plus-breakdown-chart) should share one
# budget list across those calls instead of letting each default its own,
# so the total stays bounded request-wide, not per-call.
DEFAULT_GEO_LOOKUP_BUDGET = 8


def location_for_upload(upload, factory_lat, factory_lon, radius_km, geo_budget=None):
    """"Factory" if this flight's takeoff point is within radius_km of the
    given factory coordinates; otherwise a Karnataka district ("Karnataka:
    <district>") or another Indian state's name, from reverse-geocoding
    the takeoff point (see app/geo.py) -- "Unclassified" if that hasn't
    been resolved yet (not cached, and geo_budget's live-lookup allowance
    for this request is used up or geocoding simply failed). None if this
    flight has no GPS takeoff point at all, or the factory coordinates
    aren't configured (FACTORY_LAT/FACTORY_LON in .env)."""
    if factory_lat is None or factory_lon is None:
        return None
    if not upload.analysis:
        return None
    report = upload.analysis.get_report()
    summary = (report or {}).get("summary") or {}
    lat, lon = summary.get("home_lat"), summary.get("home_lon")
    if lat is None or lon is None:
        return None
    if haversine_km(lat, lon, factory_lat, factory_lon) <= radius_km:
        return "Factory"

    if geo_budget is None:
        geo_budget = [DEFAULT_GEO_LOOKUP_BUDGET]
    from app.geo import resolve as geo_resolve
    state, district = geo_resolve(lat, lon, geo_budget)
    if state and state.strip().lower() == "karnataka":
        return f"Karnataka: {district or 'district unresolved'}"
    if state:
        return state
    return "Unclassified"


def uploads_for_location(uploads, location, factory_lat, factory_lon, radius_km, geo_budget=None):
    """Scopes an already-fetched upload list to one location bucket --
    see location_for_upload(). "Karnataka" is a whole-state rollup: it
    matches a flight in *any* Karnataka district (location_for_upload()
    itself only ever returns a specific district, e.g. "Karnataka:
    Bengaluru Urban", never the bare state name), rather than requiring a
    second, separate filter per district."""
    if not location:
        return uploads
    if geo_budget is None:
        geo_budget = [DEFAULT_GEO_LOOKUP_BUDGET]

    def _matches(u):
        if u.status != "completed":
            return False
        loc = location_for_upload(u, factory_lat, factory_lon, radius_km, geo_budget)
        if location == "Karnataka":
            return bool(loc) and loc.startswith("Karnataka:")
        return loc == location

    return [u for u in uploads if _matches(u)]


def location_breakdown(uploads, factory_lat, factory_lon, radius_km, geo_budget=None):
    """Counts of completed flights by location bucket (Factory / a
    Karnataka district / another state), plus "Unclassified" for ones not
    yet resolved -- backs the Organization Overview's location dimension.
    Empty dict if the factory coordinates aren't configured."""
    if factory_lat is None or factory_lon is None:
        return {}
    if geo_budget is None:
        geo_budget = [DEFAULT_GEO_LOOKUP_BUDGET]
    counts = {}
    for u in uploads:
        if u.status != "completed":
            continue
        loc = location_for_upload(u, factory_lat, factory_lon, radius_km, geo_budget) or "Unclassified"
        counts[loc] = counts.get(loc, 0) + 1
    return counts


def uploads_for_drone(uploads, drone_id):
    """Scopes an already-fetched upload list to one drone -- the filter
    behind the org overview's drone selector (item #3): every existing
    aggregate function here already just takes a list of uploads, so
    scoping to one drone is a filter at the call site, not a new code path
    through each of them."""
    if not drone_id:
        return uploads
    return [u for u in uploads if u.drone_id == drone_id]


def uploads_for_drone_model(uploads, drone_model):
    """Scopes an already-fetched upload list to one drone model/type
    (Tavas, L15, DOPO, ...) across every department, drone and customer --
    distinct from uploads_for_drone() above, which scopes to a single
    physical unit by MSN. Reads the drone's current model when it has one
    (Upload.drone_model is just whatever was typed at upload time, which
    can drift from the drone's actual record)."""
    if not drone_model:
        return uploads
    return [u for u in uploads if (u.drone.model if u.drone else u.drone_model) == drone_model]


def multilog_aggregate(uploads):
    """Metrics that live only in the richer multilog_analyzer.py field set
    (see AnalysisResult.multilog_json) -- captured once at upload time for
    a real upload, or verbatim for an admin-imported historical row (both
    read the same way here, so imported data folds into these numbers
    exactly like live uploads). Flights without multilog_json (uploaded
    before this feature existed) are simply skipped -- these averages and
    counts reflect whatever subset of `uploads` has the data, the same
    "-- when unavailable" pattern as the rest of this module."""
    ranges = []
    failsafe_voltages = []
    thrust_issue_flights = 0
    gps_issue_flights = 0
    battery_failsafe_flights = 0
    scored_flights = 0

    for u in uploads:
        if u.status != "completed" or not u.analysis:
            continue
        ml = u.analysis.get_multilog()
        if not ml:
            continue
        scored_flights += 1

        max_range = _ml_num(ml.get("Max Home Distance (m)"))
        if max_range is not None:
            ranges.append(max_range)

        if _ml_text(ml.get("Battery Failsafe")).lower() == "yes":
            battery_failsafe_flights += 1
            min_volt = _ml_num(ml.get("Min Voltage (V)"))
            if min_volt is not None:
                failsafe_voltages.append(min_volt)

        if _ml_text(ml.get("Potential Thrust Loss")):
            thrust_issue_flights += 1

        glitch_count = _ml_num(ml.get("GPS Glitch Count")) or 0
        failsafe_type = _ml_text(ml.get("Failsafe Type"))
        if glitch_count > 0 or "GPS" in failsafe_type.upper():
            gps_issue_flights += 1

    return {
        "scored_flights": scored_flights,
        "avg_max_range_m": _avg(ranges),
        "avg_battery_voltage_at_failsafe_v": _avg(failsafe_voltages),
        "battery_failsafe_flights": battery_failsafe_flights,
        "thrust_issue_flights": thrust_issue_flights,
        "gps_issue_flights": gps_issue_flights,
    }
