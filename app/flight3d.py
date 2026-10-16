"""Builds one aligned flight track -- {t, x, y(altitude), z, roll, pitch, yaw}
per GPS sample, in local meter coordinates -- shared by the interactive 3D
replay (client-side Three.js) and the server-side MP4 export. Purely a
post-processing view over data analyzer.py already computed and put in the
report dict (gps_path, gps_path_time, replay_headings, charts.altitude_baro,
charts.attitude_roll/pitch); analyzer.py itself is untouched.

replay_headings is already real ATT.Yaw aligned to gps_path_time (see
analyzer.py); roll/pitch aren't pre-aligned the same way, so this re-does
the same nearest-time-sample lookup analyzer.py itself uses internally."""

import bisect
import math

METERS_PER_DEGREE = 111139  # same flat-earth approximation used throughout


def nearest_value(times, values, query_t):
    """Value at the sample time closest to query_t. None if times is empty."""
    if not times:
        return None
    i = bisect.bisect_left(times, query_t)
    if i == 0:
        return values[0]
    if i >= len(times):
        return values[-1]
    before, after = times[i - 1], times[i]
    return values[i - 1] if (query_t - before) <= (after - query_t) else values[i]


def build_track(report):
    """Returns (track, meta). track is a list of dicts, one per gps_path
    point, in chronological order; meta carries the path's bounding size
    (for camera framing). Empty list if the log has no GPS fix."""
    path = report.get("gps_path") or []
    times = report.get("gps_path_time") or []
    headings = report.get("replay_headings") or []

    if not path or not times or len(path) != len(times):
        return [], {}

    charts = report.get("charts") or {}
    alt_series = charts.get("altitude_baro") or charts.get("altitude_gps") or {}
    alt_t, alt_v = alt_series.get("t") or [], alt_series.get("alt") or []
    roll_series = charts.get("attitude_roll") or {}
    roll_t, roll_v = roll_series.get("t") or [], roll_series.get("actual") or []
    pitch_series = charts.get("attitude_pitch") or {}
    pitch_t, pitch_v = pitch_series.get("t") or [], pitch_series.get("actual") or []

    lat0, lon0 = path[0]
    alt0 = nearest_value(alt_t, alt_v, times[0]) or 0.0

    track = []
    for i, (lat, lon) in enumerate(path):
        t = times[i]
        x = (lon - lon0) * METERS_PER_DEGREE
        z = (lat - lat0) * METERS_PER_DEGREE
        alt = nearest_value(alt_t, alt_v, t)
        y = (alt - alt0) if alt is not None else 0.0
        roll = nearest_value(roll_t, roll_v, t) or 0.0
        pitch = nearest_value(pitch_t, pitch_v, t) or 0.0
        yaw = headings[i] if i < len(headings) and headings[i] is not None else 0.0
        track.append({
            "t": t, "x": round(x, 2), "y": round(y, 2), "z": round(z, 2),
            "roll": roll, "pitch": pitch, "yaw": yaw,
        })

    xs = [p["x"] for p in track]
    ys = [p["y"] for p in track]
    zs = [p["z"] for p in track]
    meta = {
        "x_range": [min(xs), max(xs)],
        "y_range": [min(ys), max(ys)],
        "z_range": [min(zs), max(zs)],
    }
    return track, meta


def decimate_track(track, max_frames=240):
    """Even-stride sample down to at most max_frames points -- for video
    rendering, where each frame costs real render time, unlike the
    interactive view which can happily animate over every point."""
    if len(track) <= max_frames:
        return track
    stride = math.ceil(len(track) / max_frames)
    return track[::stride]
