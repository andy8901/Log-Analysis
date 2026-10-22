"""
Flight investigator: reads an ArduPilot dataflash (.bin) log and answers, in plain words,
"what happened on this flight, and was it a human error or a system error?".

    investigate(path) -> JSON-safe dict, stored with the upload's report as report["investigation"]

It works alongside analyzer.py / rca.py (which are untouched) and adds:
  - how the flight ended (normal, hard landing, crash, log ends in flight) from the flight's own evidence:
    take-off/touchdown from throttle, barometer drift correction, stuck-on-an-obstacle, free fall,
    crash check, IMU impact
  - a binary verdict, HUMAN ERROR or SYSTEM ERROR, built from tagged findings (pilot / setup / aircraft /
    conditions), each weighted by whether it caused, triggered or contributed to the outcome, plus the list of
    systems that were checked and found working (which is what rules a system error out)
  - the crash sequence, health checks with healthy ranges, flight phases, and a plain-language report
    (in simple words, what went wrong, possible causes, why exactly, corrective actions, data annexure)
  - a drift-corrected local track (east/north/height) for the 3D view, and chart series for the Word report

Only .bin dataflash logs are supported; for anything else investigate() returns None.
"""
import bisect
import math
import os
import re
import struct
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = 2
G = 9.80665

# ----------------------------------------------------------------------------------------------- dataflash reader
_FMT_CHARS = {
    "a": ("32h", None), "b": ("b", None), "B": ("B", None), "h": ("h", None), "H": ("H", None),
    "i": ("i", None), "I": ("I", None), "f": ("f", None), "d": ("d", None),
    "n": ("4s", "str"), "N": ("16s", "str"), "Z": ("64s", "str"),
    "c": ("h", 0.01), "C": ("H", 0.01), "e": ("i", 0.01), "E": ("I", 0.01), "L": ("i", 1e-7),
    "M": ("B", None), "q": ("q", None), "Q": ("Q", None),
}
WANT = {"PARM", "MSG", "MODE", "EV", "ERR", "ARM", "VER", "POS", "ATT", "CTUN", "RCIN", "RCOU", "RCI2", "RCO2", "BAT", "GPS",
        "VIBE", "IMU", "RFND", "XKF4", "XKF2", "POWR", "MOTB", "RAD", "CMD", "TERR", "FMTU", "UNIT", "MULT"}


class Msg:
    __slots__ = ("cols", "fmt", "rows", "idx")

    def __init__(self, cols, fmt):
        self.cols, self.fmt, self.rows = cols, fmt, []
        self.idx = {c: i for i, c in enumerate(cols)}

    def has(self, c):
        return c in self.idx


def read_dataflash(path):
    """Decode the messages the investigator needs. Returns ({name: Msg}, {name: type_id})."""
    with open(path, "rb") as fh:
        buf = fh.read()
    n = len(buf)
    fmts = {128: ("FMT", 89, None, None, None)}
    out, types = {}, {}
    p = 0
    unpack_from = struct.unpack_from
    while p + 3 <= n:
        if buf[p] != 0xA3 or buf[p + 1] != 0x95:
            nxt = buf.find(b"\xa3\x95", p + 1)
            if nxt < 0:
                break
            p = nxt
            continue
        mid = buf[p + 2]
        f = fmts.get(mid)
        if f is None:
            p += 1
            continue
        name, ln, st, conv, _ = f
        if p + ln > n:
            break
        if mid == 128:
            tid, tl = buf[p + 3], buf[p + 4]
            nm = buf[p + 5:p + 9].split(b"\0")[0].decode("ascii", "replace")
            fm = buf[p + 9:p + 25].split(b"\0")[0].decode("ascii", "replace")
            labels = buf[p + 25:p + 89].split(b"\0")[0].decode("ascii", "replace").split(",")
            types[nm] = tid
            if nm in WANT:
                try:
                    st = struct.Struct("<" + "".join(_FMT_CHARS[c][0] for c in fm))
                    conv = [_FMT_CHARS[c][1] for c in fm]
                    if st.size + 3 != tl:
                        raise ValueError
                    fmts[tid] = (nm, tl, st, conv, True)
                    if nm not in out:
                        out[nm] = Msg(labels[:len(fm)], fm)
                except (KeyError, ValueError, struct.error):
                    fmts[tid] = (nm, tl, None, None, False)
            else:
                fmts[tid] = (nm, tl, None, None, False)
        elif st is not None:
            vals = st.unpack_from(buf, p + 3)
            row, j = [], 0
            for c in conv:
                if c == "str":
                    row.append(vals[j].split(b"\0")[0].decode("ascii", "replace")); j += 1
                elif c is None and out[name].fmt[len(row)] == "a":
                    row.append(list(vals[j:j + 32])); j += 32
                elif c is None:
                    row.append(vals[j]); j += 1
                else:
                    row.append(vals[j] * c); j += 1
            out[name].rows.append(row)
        p += ln
    return out, types


# ----------------------------------------------------------------------------------------------- tables
MODES = {0: "STABILIZE", 1: "ACRO", 2: "ALT_HOLD", 3: "AUTO", 4: "GUIDED", 5: "LOITER", 6: "RTL", 7: "CIRCLE", 9: "LAND",
         11: "DRIFT", 13: "SPORT", 14: "FLIP", 15: "AUTOTUNE", 16: "POSHOLD", 17: "BRAKE", 18: "THROW", 19: "AVOID_ADSB",
         20: "GUIDED_NOGPS", 21: "SMART_RTL", 22: "FLOWHOLD", 23: "FOLLOW", 24: "ZIGZAG", 25: "SYSTEMID", 26: "AUTOROTATE",
         27: "AUTO_RTL", 28: "TURTLE"}
REASON = {0: "unknown", 1: "pilot (RC switch)", 2: "ground station command", 3: "radio failsafe", 4: "battery failsafe",
          5: "GCS (telemetry) failsafe", 6: "EKF failsafe", 7: "GPS glitch", 8: "mission end", 9: "throttle land escape",
          10: "fence breach", 11: "terrain failsafe", 12: "brake timeout", 13: "flip complete", 14: "avoidance",
          15: "avoidance recovery", 16: "throw complete", 17: "terminate", 18: "toy mode", 19: "crash failsafe", 25: "failsafe",
          26: "initialised", 31: "startup", 32: "scripting", 41: "mission command", 45: "auto RTL exit",
          48: "radio failsafe recovery", 50: "dead-reckoning failsafe", 53: "aux switch"}
PILOT_REASONS = {1, 2, 53}
ERR_SUB = {1: "Main", 2: "Radio", 3: "Compass", 5: "Radio failsafe", 6: "Battery failsafe", 7: "GPS failsafe", 8: "GCS failsafe",
           9: "Fence failsafe", 10: "Flight mode", 11: "GPS", 12: "Crash check", 13: "Flip", 15: "Parachute", 16: "EKF check",
           17: "EKF failsafe", 18: "Barometer", 19: "CPU", 20: "ADS-B failsafe", 21: "Terrain", 22: "Navigation",
           23: "Terrain failsafe", 24: "EKF primary", 25: "Thrust loss check", 26: "Sensor failsafe", 27: "Leak failsafe",
           28: "Pilot input", 29: "Vibration failsafe", 30: "Internal error", 31: "Dead-reckoning failsafe"}
FS_SUB = {5, 6, 7, 8, 9, 17, 20, 23, 26, 29, 31}
ARM_METHOD = {0: "rudder stick", 1: "ground station", 2: "aux switch", 3: "motor test", 4: "script", 5: "termination",
              6: "CPU failsafe", 7: "battery failsafe", 9: "AFS", 11: "parachute release", 12: "crash check",
              13: "landing detected", 14: "mission exit", 15: "fence breach", 16: "radio failsafe", 17: "disarm delay",
              18: "GCS failsafe", 19: "terrain failsafe", 20: "failsafe terminate", 21: "terrain failsafe",
              22: "motor detect done", 23: "bad flow of control", 24: "EKF failsafe", 27: "take-off timeout",
              28: "auto landed", 29: "pilot input failsafe", 32: "landing", 33: "dead-reckoning failsafe", 100: "unknown"}
PERSON_DISARM = {0, 1, 2}
EVN = {10: "Armed", 11: "Disarmed", 15: "Armed", 17: "Landing detected (maybe)", 18: "Landed", 28: "Took off (not landed)",
       19: "GPS lost", 21: "Flip start", 22: "Flip end", 25: "Home set", 54: "Motor emergency stop",
       55: "Emergency stop cleared", 56: "Motor interlock off", 57: "Motor interlock on", 60: "EKF altitude reset",
       61: "Landing cancelled by pilot", 62: "EKF yaw reset", 67: "GPS primary changed",
       73: "Pilot repositioning during landing", 51: "Parachute released"}
ORIENT = {0: "forward", 1: "forward-right", 2: "right", 3: "back-right", 4: "back", 5: "back-left", 6: "left",
          7: "forward-left", 24: "up", 25: "down"}
RETURN = {"RTL", "LAND", "SMART_RTL", "AUTO_RTL"}
AUTOM = RETURN | {"AUTO", "GUIDED", "BRAKE", "CIRCLE", "FOLLOW", "THROW", "AVOID_ADSB"}
MANUAL = {"STABILIZE", "ACRO", "ALT_HOLD", "SPORT", "DRIFT", "FLIP", "TURTLE", "GUIDED_NOGPS"}


# ----------------------------------------------------------------------------------------------- helpers
def _ok(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))


def med(a, d=None):
    v = sorted(x for x in a if _ok(x))
    return v[len(v) >> 1] if v else d


def max_of(a, d=0):
    v = [x for x in a if _ok(x)]
    return max(v) if v else d


def min_of(a, d=0):
    v = [x for x in a if _ok(x)]
    return min(v) if v else d


def dist(a, b):
    return math.hypot((b[0] - a[0]) * 111320, (b[1] - a[1]) * 111320 * math.cos(math.radians(a[0])))


def fmt_t(s):
    neg = s < 0
    s = abs(s)
    return ("-" if neg else "") + f"{int(s // 60):02d}:{int(s % 60):02d}"


def cap1(s):
    return s[:1].upper() + s[1:]


def compass(h, p):
    de = (p[1] - h[1]) * 111320 * math.cos(math.radians(h[0]))
    dn = (p[0] - h[0]) * 111320
    d = math.hypot(de, dn)
    if d < 3:
        return "over the take-off point"
    b = (math.degrees(math.atan2(de, dn)) + 360) % 360
    return f"{d:.0f} m {['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][int((b + 22.5) // 45) % 8]} of take-off"


def runs(times, gap=1.5):
    o = []
    for t in times:
        if o and t - o[-1][1] <= gap:
            o[-1][1] = t
        else:
            o.append([t, t])
    return o


def episodes(times, gap=3):
    o = []
    for t in times:
        if not o or t - o[-1] > gap:
            o.append(t)
    return o


def _bis(arr, t):
    """index of the last element <= t, -1 if none"""
    return bisect.bisect_right(arr, t) - 1


class Series:
    __slots__ = ("t", "v")

    def __init__(self, t, v):
        self.t, self.v = t, v

    def at(self, t):
        i = _bis(self.t, t)
        if i < 0:
            return self.v[0] if self.t[0] - t < 1 else None
        return self.v[i]


def series(M, msg, col, inst=0, inst_cols=("I", "Inst", "Instance", "C", "IMU")):
    m = M.get(msg)
    if m is None or not m.has(col):
        return None
    ti, ci = m.idx["TimeUS"], m.idx[col]
    ic = next((m.idx[c] for c in inst_cols if c in m.idx), None)
    t, v = [], []
    for r in m.rows:
        if ic is not None and inst is not None and r[ic] != inst:
            continue
        t.append(r[ti] / 1e6)
        v.append(r[ci])
    return Series(t, v) if t else None


def _r(x, n=1):
    return None if x is None else round(x, n)


# ----------------------------------------------------------------------------------------------- load
def load(M, types):
    P = {}
    if "PARM" in M:
        ix = M["PARM"].idx
        for r in M["PARM"].rows:
            P[r[ix["Name"]]] = r[ix["Value"]]
    texts = [{"t": r[0] / 1e6, "text": r[M["MSG"].idx["Message"]]} for r in M["MSG"].rows] if "MSG" in M else []
    fw = next((m for m in texts if re.match(r"^(ArduCopter|ArduPlane|ArduRover|ArduSub|Blimp|ArduHeli)\b", m["text"], re.I)), None)
    firmware = re.sub(r"\s*\([0-9a-f]+\)$", "", fw["text"]) if fw else (M["VER"].rows[0][M["VER"].idx["FWS"]] if "VER" in M and M["VER"].has("FWS") and M["VER"].rows else "")
    vehicle = "plane" if re.search("ArduPlane", firmware, re.I) else "rover" if re.search("Rover", firmware, re.I) else "sub" if re.search("Sub", firmware, re.I) else "copter"
    frame = next((m["text"].replace("Frame: ", "") for m in texts if m["text"].startswith("Frame:")), "")
    board = next((m["text"].split(" ")[0] for m in texts if re.match(r"^[A-Za-z][\w-]*\s+[0-9A-F]{8}\s+[0-9A-F]{8}", m["text"])), "")
    clock_ofs = None
    if "GPS" in M and M["GPS"].has("GWk"):
        ix, c = M["GPS"].idx, []
        for r in M["GPS"].rows:
            if r[ix["Status"]] >= 3 and r[ix["GWk"]] > 1000:
                c.append(315964800 + r[ix["GWk"]] * 604800 + r[ix["GMS"]] / 1000 - 18 - r[ix["TimeUS"]] / 1e6)
                if len(c) > 20:
                    break
        clock_ofs = med(c)
    modes = []
    if "MODE" in M:
        ix = M["MODE"].idx
        for r in M["MODE"].rows:
            n = r[ix["ModeNum"]] if "ModeNum" in ix else r[ix["Mode"]]
            modes.append({"t": r[ix["TimeUS"]] / 1e6, "name": MODES.get(n, f"MODE {n}") if vehicle == "copter" else f"MODE {n}",
                          "num": n, "rsn": r[ix["Rsn"]] if "Rsn" in ix else None})
    ev = [{"t": r[0] / 1e6, "id": r[M["EV"].idx["Id"]]} for r in M["EV"].rows] if "EV" in M else []
    err = [{"t": r[0] / 1e6, "sub": r[M["ERR"].idx["Subsys"]], "code": r[M["ERR"].idx["ECode"]]} for r in M["ERR"].rows] if "ERR" in M else []
    arm = []
    if "ARM" in M:
        ix = M["ARM"].idx
        arm = [{"t": r[0] / 1e6, "state": r[ix["ArmState"]], "method": r[ix["Method"]] if "Method" in ix else None} for r in M["ARM"].rows]

    pos = series(M, "POS", "Lat", None) or series(M, "GPS", "Lat", 0)
    t_start, t_end = (pos.t[0], pos.t[-1]) if pos else (0, 0)
    marks = [{"t": a["t"], "method": a["method"], "k": "a" if a["state"] else "d"} for a in arm]
    marks += [{"t": e["t"], "method": None, "k": "a"} for e in ev if e["id"] in (10, 15)]
    marks += [{"t": e["t"], "method": None, "k": "d"} for e in ev if e["id"] == 11]
    marks.sort(key=lambda m: m["t"])
    i = len(marks) - 1
    while i > 0:
        if marks[i]["k"] == marks[i - 1]["k"] and marks[i]["t"] - marks[i - 1]["t"] < 1.5:
            if marks[i]["method"] is not None:
                marks[i - 1]["method"] = marks[i]["method"]
            marks.pop(i)
        i -= 1
    cycles, cur = [], None
    for m in marks:
        if m["k"] == "a":
            if cur is None:
                cur = {"arm": m["t"], "disarm": None}
        else:
            if cur is None:
                cur = {"arm": t_start, "disarm": None, "armedAtStart": True}
            if cur["disarm"] is None:
                cur["disarm"] = m["t"]
                cur["disarmMethod"] = m["method"] if m["method"] is not None else cur.get("disarmMethod")
                cycles.append(cur)
                cur = None
    for c in cycles:
        a = next((x for x in arm if not x["state"] and abs(x["t"] - c["disarm"]) < 0.5), None)
        if a:
            c["disarmMethod"] = a["method"]
    if cur:
        cycles.append(cur)
    if not cycles and pos:
        cycles.append({"arm": t_start, "disarm": None, "armedAtStart": True})

    def mult_of(msg, col):
        try:
            tid, F, X = types.get(msg), M.get("FMTU"), M.get("MULT")
            if tid is None or F is None or X is None or msg not in M:
                return 1
            ci = M[msg].idx[col]
            f = M[msg].fmt[ci]
            if f in "cCeELfd":
                return 1
            row = next((r for r in F.rows if r[F.idx["FmtType"]] == tid), None)
            if row is None:
                return 1
            ch = row[F.idx["MultIds"]][ci]
            x = next((r for r in X.rows if chr(r[X.idx["Id"]]) == ch), None)
            return x[X.idx["Mult"]] if x and x[X.idx["Mult"]] else 1
        except Exception:
            return 1

    return {"M": M, "P": P, "mult_of": mult_of, "texts": texts, "firmware": firmware, "vehicle": vehicle, "frame": frame,
            "board": board, "clock_ofs": clock_ofs, "modes": modes, "ev": ev, "err": err, "arm": arm, "cycles": cycles,
            "t_start": t_start, "t_end": t_end}


# ----------------------------------------------------------------------------------------------- per-flight samples
def samples(L, c):
    M, P = L["M"], L["P"]
    a = max(L["t_start"], c["arm"] - 20)
    b = min(L["t_end"], (c["disarm"] if c["disarm"] is not None else L["t_end"]) + 20)
    base = "POS" if series(M, "POS", "Lat", None) else "GPS"
    bi = None if base == "POS" else 0
    S = {
        "lat": series(M, base, "Lat", bi), "lon": series(M, base, "Lng", bi), "amsl": series(M, base, "Alt", bi),
        "rel": series(M, "POS", "RelHomeAlt", None),
        "roll": series(M, "ATT", "Roll", None), "pitch": series(M, "ATT", "Pitch", None), "yaw": series(M, "ATT", "Yaw", None),
        "droll": series(M, "ATT", "DesRoll", None), "dpitch": series(M, "ATT", "DesPitch", None),
        "thO": series(M, "CTUN", "ThO", None), "dalt": series(M, "CTUN", "DAlt", None), "calt": series(M, "CTUN", "Alt", None),
        "crt": series(M, "CTUN", "CRt", None), "dcrt": series(M, "CTUN", "DCRt", None),
        "gs": series(M, "GPS", "Spd", 0), "gvz": series(M, "GPS", "VZ", 0), "sats": series(M, "GPS", "NSats", 0),
        "hdop": series(M, "GPS", "HDop", 0), "glat": series(M, "GPS", "Lat", 0), "glon": series(M, "GPS", "Lng", 0),
        "volt": series(M, "BAT", "Volt", 0), "curr": series(M, "BAT", "Curr", 0), "mah": series(M, "BAT", "CurrTot", 0),
        "pct": series(M, "BAT", "RemPct", 0),
        "vibeX": series(M, "VIBE", "VibeX", 0), "vibeY": series(M, "VIBE", "VibeY", 0), "vibeZ": series(M, "VIBE", "VibeZ", 0),
        "clip": series(M, "VIBE", "Clip", 0),
        "wn": series(M, "XKF2", "VWN", 0), "we": series(M, "XKF2", "VWE", 0),
        "sv": series(M, "XKF4", "SV", 0), "sp": series(M, "XKF4", "SP", 0), "sh": series(M, "XKF4", "SH", 0), "sm": series(M, "XKF4", "SM", 0),
        "rssi": series(M, "RAD", "RSSI", None), "vcc": series(M, "POWR", "Vcc", None),
        "thLim": series(M, "MOTB", "ThLimit", None), "terrH": series(M, "TERR", "CHeight", None),
    }
    for k, col in (("crt", "CRt"), ("dcrt", "DCRt")):
        sc = L["mult_of"]("CTUN", col)
        if S[k] is not None and sc != 1:
            S[k].v = [x * sc for x in S[k].v]
    mot = {}
    for n in range(1, 17):
        f = P.get(f"SERVO{n}_FUNCTION")
        if f is not None and 33 <= f <= 44:
            mot[int(f) - 33] = n
    mot_ch = [mot[k] for k in sorted(mot)] or [1, 2, 3, 4]
    motors = [series(M, "RCOU" if ch <= 14 else "RCO2", f"C{ch}", None) for ch in mot_ch]
    rcmap = {"roll": int(P.get("RCMAP_ROLL") or 1), "pitch": int(P.get("RCMAP_PITCH") or 2), "thr": int(P.get("RCMAP_THROTTLE") or 3),
             "yaw": int(P.get("RCMAP_YAW") or 4), "mode": int(P.get("FLTMODE_CH") or 5)}
    rc = {k: series(M, "RCIN" if ch <= 14 else "RCI2", f"C{ch}", None) for k, ch in rcmap.items()}
    mode_t = [m["t"] for m in L["modes"]]
    rows, last_t = [], -1
    lat = S["lat"]
    for i, t in enumerate(lat.t):
        if t < a or t > b or t - last_t < 0.095:
            continue
        last_t = t
        r = {"t": t}
        for k, s in S.items():
            v = s.at(t) if s is not None else None
            r[k] = None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v
        r["lat"], r["lon"] = lat.v[i], S["lon"].v[i]
        r["vibe"] = max(r["vibeX"] or 0, r["vibeY"] or 0, r["vibeZ"] or 0)
        r["m"] = [s.at(t) if s is not None else None for s in motors]
        r["rc"] = {k: (s.at(t) if s is not None else None) for k, s in rc.items()}
        mi = _bis(mode_t, t)
        r["mode"] = L["modes"][mi]["name"] if mi >= 0 else (L["modes"][0]["name"] if L["modes"] else "")
        r["armed"] = t >= c["arm"] and (c["disarm"] is None or t < c["disarm"])
        r["wind"] = math.hypot(r["wn"], r["we"]) if r["wn"] is not None and r["we"] is not None else None
        rows.append(r)
    return rows, mot_ch, rcmap


# ----------------------------------------------------------------------------------------------- analyse one flight
def analyse(L, k):
    c, P, M = L["cycles"][k], L["P"], L["M"]
    S, mot_ch, rcmap = samples(L, c)
    if len(S) < 5:
        return None
    arm_t, disarm_t, last = c["arm"], c["disarm"], S[-1]
    TS = [r["t"] for r in S]

    def tilt(r):
        return max(abs(r["roll"] or 0), abs(r["pitch"] or 0))

    def near(t):
        return S[max(0, min(len(S) - 1, _bis(TS, t)))]

    def rel_src(r):
        return r["rel"] if r["rel"] is not None else r["calt"] if r["calt"] is not None else 0

    at_arm = next((r for r in S if arm_t <= r["t"] <= arm_t + 1), None)
    pre_rel = [rel_src(r) for r in S if r["t"] <= arm_t + 0.5]
    base = 0
    if at_arm and abs(rel_src(at_arm)) < 1.5:
        base = rel_src(at_arm)
    elif pre_rel and abs(med(pre_rel)) < 1.5:
        base = med(pre_rel)
    home_row = next((r for r in S if r["t"] >= arm_t), S[0])
    home = [home_row["lat"], home_row["lon"], home_row["amsl"]]
    air = [r for r in S if r["armed"] and (rel_src(r) - base > 1.0 or (r["gs"] or 0) > 1.5)]
    takeoff = air[0]["t"] if air else None
    rest = [r for r in S if disarm_t and disarm_t + 2 <= r["t"] <= disarm_t + 14]
    rest_rel = med([rel_src(r) for r in rest]) if rest else None
    rest_tilt = med([tilt(r) for r in rest]) if rest else None
    touchdown = None
    if takeoff and disarm_t:
        ab = [r for r in S if takeoff + 5 <= r["t"] <= disarm_t]
        thr_hover = med([r["thO"] for r in ab if r["thO"]])
        cur_hover = med([r["curr"] for r in ab if r["curr"]])
        if thr_hover and thr_hover > 0.05:
            busy = [r for r in ab if (r["thO"] or 0) > 0.35 * thr_hover]
        elif cur_hover and cur_hover > 2:
            busy = [r for r in ab if (r["curr"] or 0) > 0.35 * cur_hover]
        else:
            ground = (rest_rel if rest_rel is not None else base) + 0.6
            busy = [r for r in ab if rel_src(r) > ground]
        touchdown = min(disarm_t, busy[-1]["t"] + 0.2 if busy else disarm_t)
    level_rest = rest_rel is not None and rest_tilt is not None and rest_tilt < 20
    drift = rest_rel - base if (level_rest and touchdown) else 0
    if abs(drift) > 15:
        drift = 0
    coslat = math.cos(math.radians(home[0]))
    for r in S:
        h = rel_src(r) - base
        if takeoff:
            kk = 0 if r["t"] <= takeoff else 1 if (not touchdown or r["t"] >= touchdown) else (r["t"] - takeoff) / (touchdown - takeoff)
            h -= drift * kk
        r["h"] = h
        r["e"] = (r["lon"] - home[1]) * 111320 * coslat
        r["n"] = (r["lat"] - home[0]) * 111320
    end_t = touchdown or last["t"]
    fl = [r for r in S if takeoff and takeoff <= r["t"] <= end_t]

    # IMU magnitude (first IMU only)
    imu_t, imu_g = [], []
    if "IMU" in M:
        ix = M["IMU"].idx
        ic = ix.get("I")
        for r in M["IMU"].rows:
            if ic is not None and r[ic] != 0:
                continue
            imu_t.append(r[ix["TimeUS"]] / 1e6)
            imu_g.append(math.sqrt(r[ix["AccX"]] ** 2 + r[ix["AccY"]] ** 2 + r[ix["AccZ"]] ** 2) / G)

    def imu_win(a, b):
        i = max(0, _bis(imu_t, a))
        o = []
        while i < len(imu_t) and imu_t[i] <= b:
            if imu_t[i] >= a:
                o.append((imu_t[i], imu_g[i]))
            i += 1
        return o

    RF = {}
    if "RFND" in M:
        ix = M["RFND"].idx
        for r in M["RFND"].rows:
            o = r[ix["Orient"]] if "Orient" in ix else int(P.get(f"RNGFND{(r[ix['Instance']] if 'Instance' in ix else 0) + 1}_ORIENT", 25))
            RF.setdefault(o, []).append((r[ix["TimeUS"]] / 1e6, r[ix["Dist"]], r[ix["Stat"]] if "Stat" in ix else 4))

    perched = None
    if touchdown and disarm_t and disarm_t - touchdown >= 3:
        held = [r for r in S if touchdown + 1 <= r["t"] <= disarm_t]
        if held and med([r["h"] for r in held]) > 2:
            perched = {"h": round(med([r["h"] for r in held]), 1), "t": touchdown, "dur": round(disarm_t - touchdown, 1),
                       "tilt": round(med([tilt(r) for r in held])), "e": med([r["e"] for r in held]), "n": med([r["n"] for r in held])}
    fall = None
    if disarm_t:
        after = [r for r in S if disarm_t <= r["t"] <= disarm_t + 5]
        if after:
            h0 = near(disarm_t)["h"]
            low = [t for t, g in imu_win(disarm_t - 0.5, disarm_t + 5) if g < 0.25]
            drop = h0 - min_of([r["h"] for r in after])
            if drop > 1.5 and (low or drop > 2.5):
                fall = {"drop": round(drop, 1), "t": low[0] if low else disarm_t,
                        "freefall": round(low[-1] - low[0], 1) if len(low) > 1 else None,
                        "midAirEnd": last["h"] > 0.3 and last["t"] - disarm_t < 5 and (S[-1]["h"] - S[-2]["h"]) < -0.1,
                        "endRoll": round(last["roll"] or 0)}
    ended_airborne = bool(takeoff) and not disarm_t and (last["h"] > 1.0 or (last["gs"] or 0) > 1.0)

    # ---- figures
    dist_flown = sum(dist([fl[i - 5]["lat"], fl[i - 5]["lon"]], [fl[i]["lat"], fl[i]["lon"]]) for i in range(5, len(fl), 5))
    far = max(fl, key=lambda r: dist(home, [r["lat"], r["lon"]])) if fl else S[0]
    volts = [r["volt"] for r in fl if r["volt"]]
    v_start = next((r["volt"] for r in S if r["t"] >= arm_t and r["volt"]), None)
    cells = P.get("BATT_CELLS") or P.get("BATT_SERIES_CELLS") or (max(1, round(v_start / 4.15)) if v_start and v_start > 5 else None)
    v_rest = med([r["volt"] for r in rest if r["volt"]]) if rest else None
    v_land = next((r["volt"] for r in reversed(fl) if r["volt"]), None)
    mah = max_of([r["mah"] or 0 for r in S]) or None
    cap = P.get("BATT_CAPACITY") or None
    winds = [r["wind"] for r in fl if r["wind"] is not None]
    if winds and max(winds) < 0.05:
        winds = []
    lean_rows = [r for r in fl if (r["gs"] or 0) < 0.6 and abs(r["crt"] or 0) < 0.3 and r["h"] > 3 and r["roll"] is not None and r["mode"] not in MANUAL]
    lean = None
    if len(lean_rows) >= 50:
        x = y = 0
        mags = []
        for r in lean_rows:
            bdir = math.atan2(r["roll"], -r["pitch"])
            mag = math.hypot(r["roll"], r["pitch"])
            bb = math.radians(r["yaw"] or 0) + bdir
            x += math.sin(bb) * mag
            y += math.cos(bb) * mag
            mags.append(mag)
        mv = math.hypot(x, y) / len(lean_rows)
        mags.sort()
        lean = {"deg": round(mv, 1), "p90": round(mags[int(len(mags) * 0.9)], 1),
                "from": round((math.degrees(math.atan2(x, y)) + 360) % 360),
                "label": "calm to light" if mv < 2.5 else "moderate" if mv < 6 else "strong" if mv < 10 else "very strong"}
    wind_max = max(winds) if winds else None
    wind_med = med(winds) if winds else None
    wind_row = next((r for r in fl if r["wind"] == wind_max), None) if wind_max is not None else None
    tilts_all = [(tilt(r), r["t"]) for r in fl]
    hover = med([r["thO"] for r in fl if (r["gs"] or 0) < 1 and abs(r["crt"] or 0) < 0.3]) if fl else None
    mot_max = max_of([max_of(r["m"]) for r in fl]) if fl else None
    pwm_max, pwm_min = P.get("MOT_PWM_MAX") or 2000, P.get("MOT_PWM_MIN") or 1000
    mot_range = max(1, pwm_max - pwm_min)
    rssis = [r["rssi"] for r in fl if r["rssi"] is not None and r["rssi"] > 0]
    terrs = [r["terrH"] for r in fl if r["t"] > (takeoff or 0) + 20 and r["t"] < end_t - 20 and r["terrH"] is not None]
    track_rows = [r for r in fl if r["t"] > (takeoff or 0) + 5 and r["t"] < (touchdown or end_t) - 2 and r["droll"] is not None]
    trk = [max(abs(r["roll"] - r["droll"]), abs(r["pitch"] - r["dpitch"])) for r in track_rows]
    track_rms = math.sqrt(sum(x * x for x in trk) / len(trk)) if trk else None
    vccs = [r["vcc"] for r in fl if r["vcc"]]
    fig = {
        "firmware": L["firmware"], "frame": L["frame"], "board": L["board"],
        "arm_t": arm_t, "disarm_t": disarm_t, "takeoff": takeoff, "touchdown": touchdown, "end_t": last["t"],
        "airtime": (end_t - takeoff) if takeoff else 0, "home": home, "drift": round(drift, 2), "distance": round(dist_flown),
        "maxAlt": round(max_of([r["h"] for r in fl]), 1), "maxSpeed": round(max_of([r["gs"] or 0 for r in fl]), 1),
        "maxDist": round(dist(home, [far["lat"], far["lon"]])), "farT": far["t"], "farWhere": compass(home, [far["lat"], far["lon"]]),
        "endWhere": compass(home, [last["lat"], last["lon"]]),
        "vStart": v_start, "vMin": min(volts) if volts else None, "vLand": v_land, "vRest": v_rest, "cells": cells, "mah": mah, "cap": cap,
        "pctEnd": next((r["pct"] for r in reversed(S) if r["pct"] is not None), None),
        "maxTilt": round(max_of([x[0] for x in tilts_all])) if tilts_all else 0, "tiltEps": episodes([t for v, t in tilts_all if v > 30]),
        "maxVibe": round(max_of([r["vibe"] or 0 for r in fl]), 1),
        "clips": (max_of([r["clip"] or 0 for r in fl]) - min_of([r["clip"] or 0 for r in fl])) if fl else 0,
        "minSats": min_of([r["sats"] for r in fl if r["sats"] is not None], None) if fl else None,
        "maxHdop": round(max_of([r["hdop"] or 0 for r in fl]), 2),
        "windMax": _r(wind_max), "windMed": _r(wind_med),
        "windFrom": round((math.degrees(math.atan2(-wind_row["we"], -wind_row["wn"])) + 360) % 360) if wind_row else None,
        "hover": round(hover, 2) if hover else None, "motMaxPct": round((mot_max - pwm_min) / mot_range * 100) if mot_max else None,
        "minRssi": min(rssis) if rssis else None, "terrMin": min(terrs) if terrs else None, "lean": lean,
        "trackRms": _r(track_rms), "trackMax": round(max(trk)) if trk else None,
        "perched": perched, "fall": fall, "endedAirborne": ended_airborne, "nMotors": len(mot_ch),
        "maxEkf": round(max_of([max(r["sv"] or 0, r["sp"] or 0, r["sh"] or 0, r["sm"] or 0) for r in fl]), 2),
        "minVcc": round(min(vccs), 2) if vccs else None,
    }

    # ---- events in plain words
    end_lim = (disarm_t if disarm_t is not None else L["t_end"]) + 30

    def in_f(t):
        return arm_t - 30 <= t <= end_lim

    E = []
    for m in L["modes"]:
        if in_f(m["t"]):
            E.append({"t": m["t"], "kind": "mode", "text": f"Mode {m['name']}" + (f" ({REASON.get(m['rsn'], 'reason %s' % m['rsn'])})" if m["rsn"] is not None else ""),
                      "mode": m["name"], "rsn": m["rsn"], "sev": "warn" if m["rsn"] in (3, 4, 5, 6, 7, 10, 11, 19, 25, 50) else ""})
    for e in L["err"]:
        if not in_f(e["t"]):
            continue
        name, fs, sub, code = ERR_SUB.get(e["sub"], f"Subsystem {e['sub']}"), e["sub"] in FS_SUB, e["sub"], e["code"]
        if fs:
            text = f"{name} triggered" if code else f"{name} cleared"
        elif sub == 12:
            text = "Crash check: loss of control" if code == 2 else "Crash check: crash detected"
        elif sub == 16:
            text = "EKF variance too high" if code else "EKF variance cleared"
        elif sub == 11:
            text = "GPS glitch" if code else "GPS glitch cleared"
        elif sub == 25:
            text = "Thrust loss detected" if code else "Thrust loss cleared"
        else:
            text = f"{name} error {code}" + (" (resolved)" if code == 0 else "")
        E.append({"t": e["t"], "kind": "err", "sub": sub, "code": code, "fs": fs, "text": text,
                  "sev": ("crit" if sub in (12, 25) else "warn") if code else ""})
    for e in L["ev"]:
        if in_f(e["t"]) and e["id"] in EVN:
            E.append({"t": e["t"], "kind": "ev", "id": e["id"], "text": EVN[e["id"]], "sev": "crit" if e["id"] in (19, 54, 51) else ""})
    skip = re.compile(r"^(PreArm|Param space|RCOut|ChibiOS|IMU\d: |GPS \d: |u-blox|New (mission|rally|fence)|RC Protocol|Frame:)")
    for m in L["texts"]:
        tx = m["text"]
        if in_f(m["t"]) and m["t"] > arm_t - 30 and not skip.match(tx) and not re.match(r"^\S+ [0-9A-F]{8} [0-9A-F]{8}", tx) and not tx.startswith("Ardu") \
                and not (re.search("failsafe|glitch", tx, re.I) and any(abs(e["t"] - m["t"]) < 0.6 for e in L["err"])):
            E.append({"t": m["t"], "kind": "msg", "text": tx,
                      "sev": "warn" if re.search(r"crash|fail|lost|critical|emergency|thrust|imbalance|error|glitch|battery \d+ is low", tx, re.I) else ""})
    if disarm_t:
        ar = next((x for x in L["arm"] if not x["state"] and abs(x["t"] - disarm_t) < 0.5), None)
        E.append({"t": disarm_t, "kind": "disarm", "text": "Disarmed" + (f" by {ARM_METHOD.get(ar['method'], 'method %s' % ar['method'])}" if ar and ar["method"] is not None else ""),
                  "method": ar["method"] if ar else None, "sev": "crit" if ar and ar["method"] == 12 else ""})
    E.sort(key=lambda e: e["t"])
    events = [e for e in E if not (e["kind"] == "ev" and e["id"] in (10, 11, 15) and any(x["kind"] == "disarm" and abs(x["t"] - e["t"]) < 0.5 for x in E))]

    # ---- failsafes and who responded
    failsafes = []
    for e in [x for x in events if x["kind"] == "err" and x["fs"]]:
        if e["code"] == 0:
            f = next((f for f in reversed(failsafes) if f["sub"] == e["sub"] and f["cleared"] is None), None)
            if f:
                f["cleared"] = round(e["t"] - f["t"], 1)
            continue
        prev = next((f for f in reversed(failsafes) if f["sub"] == e["sub"]), None)
        act = next((m for m in L["modes"] if e["t"] - 0.5 <= m["t"] <= e["t"] + 3 and m["rsn"] not in PILOT_REASONS), None)
        item = {"t": e["t"], "sub": e["sub"], "name": ERR_SUB[e["sub"]], "action": act["name"] if act else None, "cleared": None,
                "override": None, "repeat": bool(prev), "detail": None}
        if act:
            nx = next((m for m in L["modes"] if m["t"] > act["t"] and m["rsn"] in PILOT_REASONS), None)
            nx_any = next((m for m in L["modes"] if m["t"] > act["t"]), None)
            if nx and nx_any is nx and nx["t"] - act["t"] < 300:
                item["override"] = {"after": round(nx["t"] - act["t"], 1), "to": nx["name"], "by": REASON.get(nx["rsn"], "")}
        msg = next((m for m in L["texts"] if abs(m["t"] - e["t"]) < 0.5 and re.search(r"battery \d? ?is (low|critical)", m["text"], re.I)), None)
        if msg:
            item["detail"] = msg["text"]
        failsafes.append(item)

    def mode_at(t):
        m = None
        for x in L["modes"]:
            if x["t"] <= t:
                m = x
        return m

    # ---- detectors
    spikes = [(t, g) for t, g in imu_win(takeoff or arm_t, (disarm_t if disarm_t is not None else last["t"]) + 2) if g > 2.5]
    first_spike = next((s for s in spikes if not touchdown or s[0] >= touchdown - 15), None)
    contact = None
    if perched or fall:
        contact = min(first_spike[0], touchdown) if first_spike and first_spike[0] < (touchdown or float("inf")) + 1 else touchdown
    pre = [r for r in fl if (takeoff or 0) + 3 < r["t"] < (contact or end_t) - 0.5]

    bad = [r["t"] for r in pre if r["droll"] is not None and max(abs(r["roll"] - r["droll"]), abs(r["pitch"] - r["dpitch"])) > 25 and not (r["thO"] is not None and r["thO"] < 0.05)]
    rr = [x for x in runs(bad, 0.3) if x[1] - x[0] >= 0.8]
    att_loss = {"t": rr[0][0], "dur": rr[0][1] - rr[0][0]} if rr else None

    hits = []
    for r in pre:
        m = [x for x in r["m"] if x is not None]
        if len(m) < 3:
            continue
        mid = sorted(m)[len(m) >> 1]
        hi = max(m)
        if hi >= pwm_max - 0.03 * mot_range and hi - mid > 0.3 * mot_range:
            hits.append((r["t"], m.index(hi)))
    rr = [x for x in runs([h[0] for h in hits], 0.3) if x[1] - x[0] >= 1.0]
    motor_fault = None
    if rr:
        mm = [h[1] for h in hits if rr[0][0] <= h[0] <= rr[0][1]]
        motor_fault = {"t": rr[0][0], "dur": rr[0][1] - rr[0][0], "motor": max(set(mm), key=mm.count) + 1}

    bad = [r["t"] for r in pre if r["dcrt"] is not None and r["crt"] is not None and r["mode"] not in MANUAL and (r["thO"] or 0) > 0.9 and r["crt"] < r["dcrt"] - 1.5]
    rr = [x for x in runs(bad, 0.4) if x[1] - x[0] >= 2]
    thrust_short = {"t": rr[0][0], "dur": rr[0][1] - rr[0][0]} if rr else None

    vcc_low = next((r["t"] for r in fl if r["vcc"] is not None and r["vcc"] < 4.5), None) if fig["minVcc"] is not None and fig["minVcc"] < 4.5 else None
    sag = None
    if cells:
        for i in range(20, len(fl)):
            a, b = fl[i - 20], fl[i]
            if a["volt"] and b["volt"] and a["volt"] - b["volt"] > 0.25 * cells and (b["curr"] or 0) < (a["curr"] or 0) * 1.5 + 10:
                sag = {"t": b["t"], "from": a["volt"], "to": b["volt"]}
                break
    low_cell = fig["vMin"] / cells if fig["vMin"] and cells else None
    ekf_bad = next((e for e in events if e["kind"] == "err" and e["sub"] in (16, 17) and e["code"]), None)
    gps_gl = [e for e in events if e["kind"] == "err" and e["sub"] == 11 and e["code"]]
    pos_jump = None
    for i in range(1, len(fl)):
        d = dist([fl[i - 1]["lat"], fl[i - 1]["lon"]], [fl[i]["lat"], fl[i]["lon"]])
        dt = fl[i]["t"] - fl[i - 1]["t"]
        if dt > 0 and d / dt > max(25, (fl[i]["gs"] or 0) * 3 + 10):
            pos_jump = {"t": fl[i]["t"], "d": d}
            break
    div_m, div_t = 0, None
    for r in fl:
        if r["glat"] is not None:
            d = dist([r["lat"], r["lon"]], [r["glat"], r["glon"]])
            if d > div_m:
                div_m, div_t = d, r["t"]
    rc_fs = next((f for f in failsafes if f["sub"] == 5), None)
    gcs_fs = [f for f in failsafes if f["sub"] == 8]
    bat_fs = [f for f in failsafes if f["sub"] == 6]

    rc_min, rc_max = P.get(f"RC{rcmap['thr']}_MIN") or 1000, P.get(f"RC{rcmap['thr']}_MAX") or 2000

    def thr_stick(r):
        v = r["rc"]["thr"]
        return None if v is None else (v - rc_min) / max(1, rc_max - rc_min)

    def stick_dev(v):
        return 0 if v is None else abs(v - 1500) / 500

    auto_full = None
    hit_t = [r["t"] for r in S if r["armed"] and r["mode"] in AUTOM and (thr_stick(r) or 0) > 0.85]
    rr = runs(hit_t, 1.5)
    if rr:
        lg = max(rr, key=lambda x: x[1] - x[0])
        if lg[1] - lg[0] >= 3:
            sw = [r["rc"]["mode"] for r in S if r["armed"] and r["rc"]["mode"] is not None]
            auto_full = {"t": lg[0], "dur": lg[1] - lg[0], "mode": near(lg[0])["mode"], "switchStill": (max(sw) - min(sw) < 60) if sw else None}

    def pilot_active(a, b):
        return any(stick_dev(r["rc"]["roll"]) > 0.2 or stick_dev(r["rc"]["pitch"]) > 0.2 or stick_dev(r["rc"]["yaw"]) > 0.2 for r in S if a <= r["t"] <= b)

    disarm_ev = next((e for e in events if e["kind"] == "disarm"), None)
    person_disarm_air = disarm_ev if disarm_ev and disarm_ev.get("method") is not None and disarm_ev["method"] in PERSON_DISARM and near(disarm_t)["h"] > 2 else None
    ct = contact or touchdown
    contact_rows = [r for r in S if ct - 3 <= r["t"] <= ct + 0.3] if ct else []
    v_contact = max_of([-(r["crt"]) if r["crt"] is not None else (r["gvz"] or 0) for r in contact_rows]) if contact_rows else 0
    h_contact = max_of([r["gs"] or 0 for r in contact_rows]) if contact_rows else 0
    impact_g = max_of([g for t, g in imu_win(ct - 2, ct + 2)], 1) if ct else None
    m_ct = mode_at(ct - 0.2) if ct else None
    mode_contact = m_ct["name"] if m_ct else None

    def rf_ratio(series_, pred):
        up = [x for x in series_ if takeoff and takeoff + 10 <= x[0] <= end_t]
        return len(up) > 20 and sum(1 for x in up if pred(x[2])) / len(up) > 0.8

    rf_stuck = [ORIENT.get(o, f"orientation {o}") for o, s in RF.items() if o != 25 and rf_ratio(s, lambda st: st == 2)]
    rf_dead = [ORIENT.get(o, f"orientation {o}") for o, s in RF.items() if rf_ratio(s, lambda st: st <= 1)]
    if perched:
        def win(o):
            return [x for x in RF.get(o, []) if touchdown + 1 <= x[0] <= disarm_t]
        down = [x[1] for x in win(25) if x[2] == 4]
        if down:
            perched["airBelow"] = round(med(down), 1)
        close = [ORIENT.get(o, str(o)) for o in RF if o not in (24, 25) and ORIENT.get(o) not in rf_stuck and any(x[2] == 2 for x in win(o))]
        if close:
            perched["touching"] = close
        perched["atHome"] = math.hypot(perched["e"], perched["n"]) < 6
    sat_rows = [r for r in fl if (r["thLim"] is not None and r["thLim"] > 0) or any(x is not None and x >= pwm_max - 0.02 * mot_range for x in r["m"])]
    windy = fig["windMax"] is not None and fig["windMax"] > 9

    # ---- how it ended
    evid = []
    crash_msg = next((e for e in events if (e["kind"] == "err" and e["sub"] == 12 and e["code"]) or (e["kind"] == "msg" and re.match("crash", e["text"], re.I))), None)
    autop_crash = bool(crash_msg) or bool(disarm_ev and disarm_ev.get("method") == 12)
    if not takeoff:
        kind, title = "no_takeoff", "Armed but never took off"
    elif ended_airborne:
        kind = "log_end"
        title = f"The log stops while the drone is still flying: {max(0, last['h']):.0f} m up, {(last['gs'] or 0):.1f} m/s, {fig['endWhere']}"
        evid.append(["Last record", f"{fmt_t(last['t'] - arm_t)} after arming, {max(0, last['h']):.1f} m up", False])
        evid.append(["Disarm", "never logged", False])
    else:
        big = impact_g is not None and impact_g > 4
        dh = near(disarm_t)["h"]
        if perched:
            evid.append(["Where the descent stopped", f"{perched['h']:.1f} m above the take-off ground, throttle at idle for {perched['dur']:.0f} s", False])
            if perched.get("airBelow"):
                evid.append(["Down rangefinder while stopped", f"{perched['airBelow']} m of clear air below the drone", False])
            if perched.get("touching"):
                evid.append(["Side rangefinders while stopped", "something closer than their minimum range on the " + ", ".join(perched["touching"]), False])
        evid.append(["Descent speed at contact", f"{v_contact:.1f} m/s", v_contact < 3])
        evid.append(["Ground speed at contact", f"{h_contact:.1f} m/s", h_contact < 3])
        if impact_g is not None:
            evid.append(["Impact on the IMU", f"{impact_g:.1f} g", impact_g < 4])
        evid.append(["Tilt after stopping", f"{perched['tilt']}°" if perched else f"{rest_tilt:.0f}°" if rest_tilt is not None else "no data",
                     False if perched else (rest_tilt is None or rest_tilt < 30)])
        evid.append(["Height when disarmed", f"{max(0, dh):.1f} m", dh < 3])
        evid.append(["Crash messages", crash_msg["text"] if crash_msg else "none", not crash_msg])
        if fall:
            evid.append(["After the motors stopped", f"fell {'at least ' if fall['midAirEnd'] else ''}{fall['drop']} m" + (f", {fall['freefall']} s of free fall on the IMU" if fall["freefall"] else ""), False])
        if person_disarm_air:
            evid.append(["Disarmed in the air", f"by {ARM_METHOD.get(person_disarm_air['method'])} at {dh:.1f} m", False])
        bad_ev = [e for e in evid if e[2] is False]
        if autop_crash or perched or fall or person_disarm_air or (big and (v_contact > 3 or h_contact > 3)):
            kind = "crash"
            bits = []
            if perched:
                bits.append(f"came down on an obstacle about {perched['h']:.0f} m above the ground")
            if person_disarm_air:
                bits.append(f"the motors were stopped from the {ARM_METHOD.get(person_disarm_air['method'])} in the air")
            if autop_crash:
                bits.append("the crash check stopped the motors")
            if fall:
                bits.append(f"it fell from about {max(fall['drop'], perched['h'] if perched else 0):.0f} m" if fall["midAirEnd"] else f"it fell about {fall['drop']:.0f} m")
            if not bits:
                bits.append(f"hit the ground at {max(v_contact, h_contact):.1f} m/s ({impact_g:.1f} g)")
            how = (" during " + mode_contact) if mode_contact and mode_contact not in MANUAL else (" in " + mode_contact) if mode_contact else ""
            title = f"Crashed{how}: " + (", ".join(bits[:-1]) + " and " + bits[-1] if len(bits) > 1 else bits[0])
        elif bad_ev:
            kind, title = "hard_landing", "Hard or unusual landing: " + ", ".join(b[0].lower() for b in bad_ev)
        else:
            kind, title = "normal", f"Normal landing {fig['endWhere']}"

    # ---- findings: who or what was responsible
    F, ruled = [], []

    def add(cat, w, role, ttl, text, t=None, seq=False):
        F.append({"cat": cat, "w": w, "role": role, "title": ttl, "text": text, "t": t, "seq": seq})

    T = lambda t: fmt_t(t - arm_t)  # noqa: E731
    if att_loss:
        add("system", 3, "cause", "Lost attitude control before any contact",
            f"At {T(att_loss['t'])} the drone's actual roll/pitch was more than 25° away from what the autopilot asked for, for {att_loss['dur']:.1f} s, with power still on. That points to a motor, propeller, ESC or frame failure, or a badly tuned controller.", att_loss["t"], True)
    elif fig["trackMax"] is not None:
        ruled.append(["Attitude control", f"followed its target within {fig['trackMax']}° (RMS {fig['trackRms']}°) up to {'the contact' if contact else 'landing'}"])
    if motor_fault:
        add("system", 3, "cause", f"Motor {motor_fault['motor']} lost thrust",
            f"From {T(motor_fault['t'])} motor {motor_fault['motor']}'s output sat at maximum for {motor_fault['dur']:.1f} s while the others stayed well below: the autopilot was asking that arm for everything it had. Typical of a failing motor, ESC, loose or broken propeller.", motor_fault["t"], True)
    elif fl:
        ruled.append(["Motors and ESCs", "outputs stayed balanced" + (f", peak {fig['motMaxPct']}% of range" if fig["motMaxPct"] is not None else "")])
    if thrust_short:
        add("system", 2, "cause", "Couldn't hold height at full throttle",
            f"From {T(thrust_short['t'])} it kept sinking for {thrust_short['dur']:.0f} s with the throttle output above 90%. Not enough thrust: overweight, a weak motor/propeller, or a sagging battery.", thrust_short["t"], True)
    if vcc_low:
        add("system", 3, "cause", "Flight controller power dipped", f"The board supply fell to {fig['minVcc']} V (normal is about 5 V). A brown-out can reset or freeze the autopilot.", vcc_low, True)
    elif fig["minVcc"]:
        ruled.append(["Flight controller power", f"board supply stayed at {fig['minVcc']} V or above"])
    if sag and cells and fig["mah"] and fig["cap"] and fig["mah"] < 0.6 * fig["cap"]:
        add("system", 3, "cause", "Battery voltage collapsed with charge left",
            f"Voltage dropped from {sag['from']:.1f} V to {sag['to']:.1f} V in about 2 s with only {round(fig['mah'])} of {fig['cap']:.0f} mAh used. Likely a damaged cell or bad connector.", sag["t"], True)
    if ended_airborne and not att_loss and not motor_fault:
        add("system", 3, "cause", "Log stops in mid-air",
            f"The last record is {max(0, last['h']):.0f} m up and {(last['gs'] or 0):.1f} m/s with the motors still running, and no crash or disarm was logged. A sudden stop like this usually means the flight controller lost power or reset, or the SD card failed.", last["t"])
    if ekf_bad:
        add("system", 2, "cause" if contact and ekf_bad["t"] < contact else "contributing", "Navigation (EKF) became unreliable", f"{ekf_bad['text']} at {T(ekf_bad['t'])}.", ekf_bad["t"])
    if gps_gl:
        in_crash = bool(contact) and any(contact - 20 < g["t"] < contact for g in gps_gl)
        add("system" if in_crash else "environment", 2 if in_crash else 1, "contributing", ("GPS glitch " + (f"{len(gps_gl)} times" if len(gps_gl) > 1 else "")).strip(),
            f"ArduPilot flagged a GPS glitch or compass error at {', '.join(T(g['t']) for g in gps_gl)}. Usually poor sky view, multipath near buildings or interference.", gps_gl[0]["t"])
    elif fig["minSats"] is not None:
        ruled.append(["GPS", f"{fig['minSats']} satellites at worst, HDOP up to {fig['maxHdop']}, no glitches"])
    if pos_jump:
        add("system", 1, "contributing", "Position jumped", f"The position jumped {pos_jump['d']:.0f} m in one step at {T(pos_jump['t'])}.", pos_jump["t"], True)
    if not ekf_bad and fig["maxEkf"] <= 0.8:
        ruled.append(["Navigation (EKF)", f"variances stayed low (peak {fig['maxEkf']})"])
    if fig["maxVibe"] > 60 or fig["clips"] > 50:
        add("system", 2, "contributing", "Severe vibration", f"Vibration peaked at {fig['maxVibe']} m/s²" + (f" with {fig['clips']} accelerometer clips" if fig["clips"] else "") + ". That can corrupt height and position estimates.")
    elif fig["maxVibe"] > 0:
        ruled.append(["Vibration", f"peak {fig['maxVibe']} m/s²" + (f", {fig['clips']} clips" if fig["clips"] else ", no clipping")])
    if rc_fs:
        add("system", 2, "trigger", "RC link lost", f"Radio failsafe at {T(rc_fs['t'])}" + (f", autopilot switched to {rc_fs['action']}" if rc_fs["action"] else "") + (f"; the drone was up to {fig['maxDist']} m away" if fig["maxDist"] else "") + ".", rc_fs["t"])
    else:
        ruled.append(["RC link", "no radio failsafe"])
    for f in gcs_fs:
        add("system", 1, "trigger", "Telemetry link dropped",
            f"No ground-station heartbeat for {P.get('FS_GCS_TIMEOUT', 5):g} s at {T(f['t'])} triggered {f['action'] or 'the GCS failsafe'}" + (f"; the link came back {f['cleared']} s later" if f["cleared"] is not None else "") + ".", f["t"])
    if person_disarm_air:
        add("pilot", 3, "cause", "Motors stopped in the air by the pilot", f"The drone was disarmed from the {ARM_METHOD.get(person_disarm_air['method'])} at {near(disarm_t)['h']:.1f} m.", disarm_t)
    if kind in ("crash", "hard_landing"):
        if mode_contact and mode_contact in MANUAL and not att_loss and not motor_fault and not vcc_low:
            thr_low = any((thr_stick(r) if thr_stick(r) is not None else 1) < 0.3 for r in contact_rows)
            add("pilot", 3, "cause", f"Pilot was flying in {mode_contact} at contact",
                f"{mode_contact} gives the pilot direct control of {'position' if mode_contact == 'ALT_HOLD' else 'height and position'}. The aircraft followed its commands (no control or power fault found) and hit at {max(v_contact, h_contact):.1f} m/s" + (" with the throttle stick low." if thr_low else "."), ct)
        elif mode_contact in ("LOITER", "POSHOLD", "FLOWHOLD") and h_contact > 2 and pilot_active(ct - 5, ct) and not att_loss and not motor_fault:
            add("pilot", 3, "cause", f"Pilot flew into the obstacle in {mode_contact}", f"The pilot was steering (sticks deflected) and the drone was moving at {h_contact:.1f} m/s when it hit.", contact)
    if auto_full:
        add("pilot", 2, "cause" if kind == "crash" else "contributing", f"Throttle stick held full up during {auto_full['mode']}, no mode change",
            f"From {T(auto_full['t'])} the throttle stick was near full for {auto_full['dur']:.0f} s while {auto_full['mode']} was flying. {auto_full['mode']} ignores the throttle stick; switching the flight mode (Loiter or AltHold) gives control back." + (" The flight-mode switch never moved." if auto_full["switchStill"] else ""), auto_full["t"], True)
    for f in bat_fs:
        if f["override"]:
            add("pilot", 3 if kind == "crash" else 2, "contributing", "Battery failsafe overridden" + (" again" if f["repeat"] else ""),
                f"{f['detail'] or 'Battery failsafe'} at {T(f['t'])}. The autopilot switched to {f['action']}; {f['override']['after']} s later it was switched back to {f['override']['to']} from the {f['override']['by'].replace(' command', '')} and the flight continued.", f["t"])
    if perched:
        rtl_home = perched["atHome"] and mode_contact in RETURN
        add("setup", 3 if rtl_home else 2, "cause", "Obstacle above the take-off / home point" if perched["atHome"] else "Flight path into an obstacle",
            f"The descent stopped {perched['h']} m above the ground" + (" right over the take-off point" if perched["atHome"] else "") + " with something solid beside the drone" +
            (f" (down rangefinder: {perched['airBelow']} m of air below)" if perched.get("airBelow") else "") + "." +
            (" RTL comes straight down onto home, so the take-off spot needed clear sky above it (or a rally point somewhere clear)." if rtl_home
             else " The automatic path went through an obstacle; the mission or return altitude needed to clear it." if mode_contact in AUTOM else ""), touchdown)
    if gcs_fs and kind == "crash" and any(f["action"] and not f["override"] for f in gcs_fs):
        add("setup", 1, "contributing", "A telemetry dropout was set to end the flight in RTL",
            f"FS_GCS_ENABLE={P.get('FS_GCS_ENABLE', '?'):g} turns a ground-station link timeout into RTL even when the RC link is fine. FS_OPTIONS can keep pilot-controlled modes running instead." if isinstance(P.get("FS_GCS_ENABLE"), (int, float)) else
            "The GCS failsafe turns a ground-station link timeout into RTL even when the RC link is fine. FS_OPTIONS can keep pilot-controlled modes running instead.")
    if rf_stuck:
        add("setup", 1, "contributing", f"{cap1(', '.join(rf_stuck))} rangefinder faulty", 'It read "too close" for over 80% of the flight, even high in open air, so it couldn\'t warn of obstacles. Flown with a known-bad sensor.')
    if rf_dead:
        add("setup", 1, "contributing", f"{cap1(', '.join(rf_dead))} rangefinder gave no data", "No valid readings for most of the flight.")
    if P.get("ARMING_CHECK") == 0:
        add("setup", 1, "contributing", "Pre-arm safety checks were turned off", "ARMING_CHECK=0 lets the drone arm with failing sensors.")
    if low_cell is not None and low_cell < 3.3 and kind in ("crash", "log_end"):
        add("pilot", 2, "cause", "Flew the battery flat", f"The pack fell to {low_cell:.2f} V per cell under load.")
    if windy:
        add("environment", 2 if fig["windMax"] > 12 else 1, "cause" if kind == "crash" and len(sat_rows) > 10 else "contributing", "Strong wind",
            f"The EKF estimated wind up to {fig['windMax']} m/s (typical {fig['windMed']} m/s) from about {fig['windFrom']}°." + (" The motors hit their limits while fighting it." if len(sat_rows) > 10 else ""), wind_row["t"] if wind_row else None)
    elif fig["windMax"] is not None:
        ruled.append(["Wind", f"EKF estimate up to {fig['windMax']} m/s"])
    elif lean and lean["deg"] < 6:
        ruled.append(["Wind", f"{lean['label']}: it leaned {lean['deg']}° on average to hold position"])
    if not windy and lean and lean["deg"] >= 6:
        add("environment", 2 if lean["deg"] >= 10 else 1, "contributing", "Strong wind",
            f"To hold position it had to lean {lean['deg']}° on average (90% of the time under {lean['p90']}°) into wind from about {lean['from']}°.")
    if cells and low_cell and low_cell >= 3.3:
        ruled.append(["Battery", f"{low_cell:.2f} V per cell at its lowest under load"])

    # ---- binary verdict: human error or system error
    score = {"pilot": 0, "setup": 0, "system": 0, "environment": 0}
    for f in F:
        score[f["cat"]] += f["w"] * (1 if f["role"] == "cause" else 0.6 if f["role"] == "trigger" else 0.4)
    env_human = sum(f["w"] * (1 if f["role"] == "cause" else 0.4) for f in F if f["cat"] == "environment" and re.search("wind", f["title"], re.I))
    human = score["pilot"] + score["setup"] + env_human
    system = score["system"] + (score["environment"] - env_human)
    total = (human + system) or 1
    blame = None
    if F:
        is_h = human >= system
        share = (human if is_h else system) / total
        blame = {"primary": "human" if is_h else "system", "label": "Human error" if is_h else "System error",
                 "sub": ("piloting and setup" if score["pilot"] and score["setup"] else "piloting" if (score["pilot"] or env_human) else "setup and planning") if is_h else "aircraft",
                 "share": round(share * 100), "confidence": "high" if share > 0.7 else "medium" if share > 0.55 else "low",
                 "split": {"human": round(human / total * 100), "system": round(system / total * 100)}}
    verdict = None
    if kind in ("crash", "hard_landing", "log_end"):
        verdict = blame or {"primary": "unknown", "label": "Undetermined", "sub": "", "share": 0, "confidence": "low", "split": {"human": 0, "system": 0}}

    # ---- health checks
    checks = []

    def ck(sev, ttl, text, t=None, cat=None):
        checks.append({"sev": sev, "title": ttl, "text": text, "t": t, "cat": cat})

    if cells and fig["vLand"]:
        pl = fig["vLand"] / cells
        pr = fig["vRest"] / cells if fig["vRest"] else None
        if pl < 3.4 or (pr and pr < 3.55):
            ck("crit", "Landed on a nearly empty pack", f"{fig['vLand']:.2f} V under load at touchdown ({pl:.2f} V/cell)" + (f", {fig['vRest']:.2f} V at rest ({pr:.2f} V/cell)" if pr else "") + ". Aim to land at 3.5 V/cell or more under load.", touchdown, "pilot")
        elif pl < 3.55:
            ck("warn", "Battery used deep", f"{pl:.2f} V/cell under load at touchdown.", touchdown, "pilot")
        else:
            ck("ok", "Battery margin fine", f"{pl:.2f} V/cell under load at touchdown" + (f", {round(fig['mah'])} mAh used" if fig["mah"] else "") + (f" of {fig['cap']:.0f}" if fig["cap"] else "") + ".")
        if fig["pctEnd"] is not None and fig["pctEnd"] > 25 and pr and pr < 3.6:
            ck("warn", "Battery % doesn't match the voltage", f"The autopilot showed {round(fig['pctEnd'])}% left but the resting voltage ({pr:.2f} V/cell) says nearly empty. BATT_CAPACITY ({fig['cap']:.0f}) is probably larger than the pack's usable charge.", None, "setup")
    for f in bat_fs:
        ck("crit" if f["override"] else "warn", "Battery failsafe" + (" (again)" if f["repeat"] else ""),
           f"{f['detail'] or 'Battery failsafe'} at {T(f['t'])}" + (f", autopilot switched to {f['action']}" if f["action"] else "") +
           (f"; overridden after {f['override']['after']} s (back to {f['override']['to']} from the {f['override']['by'].replace(' command', '')})." if f["override"] else "."), f["t"], "pilot" if f["override"] else None)
    for f in gcs_fs:
        ck("warn", "Telemetry (GCS) failsafe", f"At {T(f['t'])}" + (f", autopilot switched to {f['action']}" if f["action"] else "") + (f"; link back after {f['cleared']} s" if f["cleared"] is not None else "") +
           (f"; pilot took over after {f['override']['after']} s" if f["override"] else (", and the drone stayed in " + f["action"]) if f["action"] else "") + ".", f["t"])
    if rc_fs:
        ck("crit", "RC link lost", f"Radio failsafe at {T(rc_fs['t'])}.", rc_fs["t"], "system")
    if gps_gl:
        ck("warn", f"GPS glitch ({len(gps_gl)})", ", ".join(T(g["t"]) for g in gps_gl) + ". Check the GPS mast, nearby metal/structures and interference from video transmitters or wiring.", gps_gl[0]["t"], "environment")
    elif fig["minSats"] is not None:
        weak = fig["minSats"] < 8 or fig["maxHdop"] > 1.5
        ck("warn" if weak else "ok", "Weak GPS" if weak else "GPS solid", f"{fig['minSats']} satellites at worst, HDOP up to {fig['maxHdop']}.")
    if div_m > 5:
        ck("warn", "GPS and EKF positions disagreed", f"Up to {div_m:.1f} m apart at {T(div_t)}.", div_t)
    elif div_t:
        ck("ok", "Path is consistent", f"The GPS and the autopilot's own position estimate agreed within {div_m:.1f} m all flight" + ("" if pos_jump else ", with no position jumps") + ".")
    if fig["maxVibe"] > 30:
        ck("crit" if fig["maxVibe"] > 60 else "warn", "High vibration", f"Peak {fig['maxVibe']} m/s² (guideline: under 30)" + (f", {fig['clips']} clips" if fig["clips"] else "") + ". Balance props, check motor bearings and the flight-controller mount.", None, "system")
    elif fig["maxVibe"] > 0:
        ck("ok", "Vibration healthy", f"Peak {fig['maxVibe']} m/s²" + (f", {fig['clips']} clips" if fig["clips"] else ", no clipping") + ".")
    if fig["trackRms"] is not None:
        loose = fig["trackRms"] > 5 or fig["trackMax"] > 30
        ck("warn" if loose else "ok", "Attitude tracking loose" if loose else "Tuning looks good", f"Actual roll/pitch followed the target within {fig['trackRms']}° RMS (worst {fig['trackMax']}°)." + (" Consider an AutoTune." if fig["trackRms"] > 5 else ""))
    if fig["hover"]:
        hv = fig["hover"]
        ck("warn" if hv > 0.6 else "info" if hv < 0.2 else "ok", "Little thrust headroom" if hv > 0.6 else "Hover throttle",
           f"Hovers at {round(hv * 100)}% throttle" + (": heavy for its motors, with little reserve for wind or manoeuvres." if hv > 0.6 else ": very overpowered, which can make it twitchy." if hv < 0.2 else "."))
    if fig["motMaxPct"] is not None and fig["motMaxPct"] > 95:
        ck("warn", "Motors reached their limit", f"At least one motor hit {fig['motMaxPct']}% of its range.")
    if fig["windMax"] is None and lean:
        ck("warn" if lean["deg"] >= 6 else "ok", "Wind (from the hover lean)",
           f"Leaned {lean['deg']}° on average to hold position ({lean['label']}), into wind from about {lean['from']}° ({['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW'][int((lean['from'] + 22.5) // 45) % 8]}). The log has no direct wind estimate.")
    if fig["windMax"] is not None:
        ck("warn" if windy else "ok", "Windy" if windy else "Wind", f"EKF wind estimate up to {fig['windMax']} m/s, typically {fig['windMed']} m/s" + (f", from about {fig['windFrom']}°." if fig["windFrom"] is not None else "."))
    if fig["tiltEps"]:
        ck("warn", f"Tilted past 30° {len(fig['tiltEps'])}×", f"Peak {fig['maxTilt']}°, usually hard braking or full-stick manoeuvres.", fig["tiltEps"][0], "pilot")
    if fig["minRssi"] is not None:
        ck("warn" if fig["minRssi"] < 40 else "ok", "Telemetry radio signal", f"Lowest local RSSI {fig['minRssi']}.")
    if fig["minVcc"] is not None:
        ck("crit" if fig["minVcc"] < 4.6 else "ok", "Flight-controller supply", f"Lowest {fig['minVcc']} V.")
    if rf_stuck:
        ck("warn", f'{cap1(", ".join(rf_stuck))} rangefinder always read "too close"', "Probably blocked, dirty or faulty, so it couldn't warn of obstacles.", None, "setup")
    if rf_dead:
        ck("warn", f"{cap1(', '.join(rf_dead))} rangefinder gave no data", "", None, "setup")
    if abs(drift) > 1.5:
        ck("info", "Barometer drift corrected", f"It read {drift:.1f} m on the ground after landing; heights are corrected for it.")
    if auto_full:
        ck("crit", f"Throttle stick held full in {auto_full['mode']}", f"For {auto_full['dur']:.0f} s from {T(auto_full['t'])}. Automatic modes ignore it; change the flight mode to take over.", auto_full["t"], "pilot")
    if fig["terrMin"] is not None:
        ck("info", "Height above terrain", f"Lowest {fig['terrMin']:.0f} m above the terrain database while flying.")

    # ---- phases
    st = []
    for i, r in enumerate(S):
        if perched and touchdown <= r["t"] < disarm_t:
            st.append("perched"); continue
        if fall and r["t"] >= disarm_t:
            st.append("fall"); continue
        if not takeoff or r["t"] < takeoff or (touchdown and r["t"] >= touchdown):
            st.append("ground"); continue
        lo = bisect.bisect_left(TS, r["t"] - 5)
        hi = max(lo, bisect.bisect_right(TS, r["t"] + 5) - 1)
        g = sum(S[j]["gs"] or 0 for j in range(lo, hi + 1)) / (hi - lo + 1)
        climb = (S[hi]["h"] - S[lo]["h"]) / max(1, S[hi]["t"] - S[lo]["t"])
        st.append("moving" if g > 1.5 else "climb" if climb > 0.5 else "descent" if climb < -0.5 else "hover")
    seg = []
    for r, s in zip(S, st):
        if seg and seg[-1][2] == s:
            seg[-1][1] = r["t"]
        else:
            seg.append([r["t"], r["t"], s])
    fixed, mg = {"ground", "perched", "fall"}, []
    for s in seg:
        p = mg[-1] if mg else None
        if p and p[2] not in fixed and s[2] not in fixed and (s[1] - s[0] < 10 or p[2] == s[2]):
            p[1] = s[1]
        elif p and p[2] == s[2]:
            p[1] = s[1]
        else:
            mg.append(s)
    phases = []
    for a, b, s in mg:
        rr_ = [r for r in S if a <= r["t"] <= b]
        if not rr_:
            continue
        hs = [r["h"] for r in rr_]
        if s == "perched":
            text = f"Stuck on an obstacle about {med(hs):.0f} m up, motors at idle, tilted {perched['tilt']}°"
        elif s == "fall":
            text = f"Falling after the motors stopped: {fall['drop']} m"
        elif s == "ground":
            text = "On the ground" + (" before take-off" if takeoff and a < takeoff else " after landing" if touchdown and a >= touchdown - 1 else "")
        elif s == "moving":
            d = sum(dist([rr_[j - 5]["lat"], rr_[j - 5]["lon"]], [rr_[j]["lat"], rr_[j]["lon"]]) for j in range(5, len(rr_), 5))
            fr = max(rr_, key=lambda r: dist(home, [r["lat"], r["lon"]]))
            en = rr_[-1]
            back = dist(home, [en["lat"], en["lon"]]) < dist(home, [rr_[0]["lat"], rr_[0]["lon"]]) - 10
            text = f"Moving {d:.0f} m at up to {max_of([r['gs'] or 0 for r in rr_]):.1f} m/s, " + (f"back to {compass(home, [en['lat'], en['lon']])}" if back else f"out to {compass(home, [fr['lat'], fr['lon']])}")
        elif s == "hover":
            mr = rr_[len(rr_) >> 1]
            text = f"Hover at about {med(hs):.0f} m, {compass(home, [mr['lat'], mr['lon']])}"
        elif s == "climb":
            text = f"Climb to {max(hs):.0f} m"
        else:
            text = f"Descent to {max(0, min(hs)):.0f} m"
        phases.append({"a": a, "b": b, "s": s, "text": text})

    # ---- crash sequence
    sequence = None
    if kind in ("crash", "log_end", "hard_landing"):
        first_issue = min([f["t"] for f in F if f["t"]] + [contact or end_t])
        frm = max(takeoff, first_issue - 15)
        lim = (disarm_t if disarm_t is not None else last["t"]) + 3
        crash_dis = disarm_ev and disarm_ev.get("method") == 12
        seq = []
        for e in events:
            if frm - 1 <= e["t"] <= lim and not (e["kind"] == "ev" and e["id"] in (57, 56, 28, 62)) and not (crash_dis and e["kind"] == "ev" and e["id"] in (17, 18) and abs(e["t"] - disarm_t) < 1.5):
                seq.append({"t": e["t"], "text": e["text"], "sev": e["sev"]})
        for ph in phases:
            if frm <= ph["a"] <= (contact or end_t) and ph["s"] not in fixed:
                seq.append({"t": ph["a"], "text": ph["text"], "sev": "", "phase": True})
        for f in F:
            if f["t"] and frm - 1 <= f["t"] <= lim and f["seq"]:
                seq.append({"t": f["t"], "text": f["title"], "sev": "crit" if f["cat"] == "system" else "warn"})
        if contact:
            seq.append({"t": contact, "text": f"Contact at about {near(contact)['h']:.1f} m: descent stops" if perched else "Contact with the ground", "sev": "crit"})
        if fall:
            seq.append({"t": fall["t"], "text": f"Free fall: drops {'at least ' if fall['midAirEnd'] else ''}{fall['drop']} m", "sev": "crit"})
        if spikes and contact:
            sp = [s for s in spikes if s[0] > contact - 2]
            if sp:
                m = max(sp, key=lambda s: s[1])
                seq.append({"t": m[0], "text": f"Peak impact {m[1]:.1f} g", "sev": "crit"})
        if (fall and fall["midAirEnd"]) or ended_airborne:
            seq.append({"t": last["t"], "text": "Log ends", "sev": ""})
        seq.sort(key=lambda s: s["t"])
        sequence = []
        for s in seq:
            if not any(abs(p["t"] - s["t"]) < 0.6 and p["text"] == s["text"] for p in sequence):
                sequence.append(s)

    return {"S": S, "fig": fig, "kind": kind, "title": title, "evidence": evid, "events": events, "failsafes": failsafes, "F": F,
            "ruled": ruled, "verdict": verdict, "blame": blame, "sequence": sequence, "checks": checks, "phases": phases,
            "contact": contact, "modeContact": mode_contact, "arm_t": arm_t, "pwmMin": pwm_min, "pwmMax": pwm_max,
            "thr_stick": thr_stick,
            "detect": {"att_loss": bool(att_loss), "motor_fault": bool(motor_fault), "thrust_short": bool(thrust_short),
                       "thrust_loss_err": any(e["kind"] == "err" and e["sub"] == 25 and e["code"] for e in events),
                       "ekf": bool(ekf_bad), "gps_glitches": len(gps_gl), "pos_jump": bool(pos_jump), "vcc_low": bool(vcc_low),
                       "sag": bool(sag), "vibration": fig["maxVibe"] > 60 or fig["clips"] > 50,
                       "prearm": sorted({m["text"] for m in L["texts"] if m["text"].startswith("PreArm")}),
                       "cold_start_s": round(arm_t, 1) if arm_t and arm_t > 0 else None}}


# ----------------------------------------------------------------------------------------------- plain-language report
FIX = [
    (r"obstacle above the take-off", "Take off from a spot with open sky above it, or set a rally point so RTL lands somewhere clear."),
    (r"flight path into an obstacle", "Raise the mission or return altitude (RTL_ALT) above nearby obstacles."),
    (r"throttle stick held full", "Pilot refresher: to take over from RTL or Auto, flip the flight-mode switch to Loiter or AltHold. The throttle stick alone does nothing."),
    (r"telemetry dropout was set", "Review FS_GCS_ENABLE and FS_OPTIONS so a telemetry-only dropout doesn't force RTL while the RC link is fine."),
    (r"rangefinder (faulty|gave no data)", "Clean, repair or replace the faulty rangefinder before the next flight."),
    (r"battery failsafe overridden", "Land when the battery failsafe triggers; don't switch back out of RTL."),
    (r"flew the battery flat", "Plan flights to land at 3.5 V per cell or more."),
    (r"motor \d+ lost thrust", "Inspect that motor, its ESC, propeller and wiring; replace damaged parts."),
    (r"lost attitude control", "Inspect motors, propellers, ESCs and frame, and check the tune before flying again."),
    (r"couldn't hold height", "Reduce weight or fit stronger motors/props; check the battery under load."),
    (r"power dipped|log stops in mid-air", "Check the flight-controller power supply (BEC, connectors) and the SD card."),
    (r"collapsed with charge left", "Retire the battery pack and check its connectors."),
    (r"navigation \(ekf\)|gps glitch|position jumped", "Check GPS and compass mounting and interference; fly with a clear sky view."),
    (r"vibration", "Balance propellers and check the flight-controller mounting."),
    (r"strong wind", "Respect the drone's wind limit; postpone flights in strong wind."),
    (r"pilot was flying in|pilot flew into", "Pilot training on obstacle clearance and landing approach."),
    (r"motors stopped in the air", "Never disarm in the air; protect the disarm switch."),
    (r"pre-arm safety checks", "Turn ARMING_CHECK back on."),
]
PLAIN = [
    (r"obstacle above the take-off", "an obstacle (such as a tree or structure) right above the take-off point, which the drone came down onto when it returned home"),
    (r"flight path into an obstacle", "a flight path that went through an obstacle"),
    (r"throttle stick held full", "the pilot not switching the flight mode to take back control (pushing the throttle stick does nothing while the drone is returning home)"),
    (r"telemetry link dropped", "the telemetry link to the ground station dropped, which made the drone return home automatically (RTL)"),
    (r"rc link lost", "the remote-control link was lost"),
    (r"battery failsafe overridden", "the low-battery safety return being cancelled so the flight could continue"),
    (r"flew the battery flat", "the battery being run flat"),
    (r"motor (\d+) lost thrust", "motor {0} losing power (motor, propeller or speed controller failure)"),
    (r"lost attitude control", "the drone losing control of its tilt before hitting anything (a mechanical or tuning failure)"),
    (r"log stops in mid-air", "the flight controller losing power or resetting in mid-air"),
    (r"strong wind", "flying in strong wind"),
]
CATN = {"pilot": "Human – pilot", "setup": "Human – setup", "system": "System – aircraft"}


def _lc1(t):
    return t if re.match(r"^[A-Z]{2}", t) else t[:1].lower() + t[1:]


def _list_and(a):
    a = list(a)
    return ", ".join(a[:-1]) + " and " + a[-1] if len(a) > 1 else (a[0] if a else "")


def _first(t, n=1):
    return " ".join(re.split(r"(?<=[.!?])\s+(?=[A-Z(])", str(t))[:n]).strip()


def _plain(f):
    for pat, txt in PLAIN:
        m = re.search(pat, f["title"], re.I)
        if m:
            return txt.format(*m.groups())
    return _lc1(f["title"])


def _is_human(f):
    return f["cat"] in ("pilot", "setup") or (f["cat"] == "environment" and re.search("wind", f["title"], re.I))


def build_report_text(A, L, tz):
    """Everything the page and the Word report say, in plain words."""
    f, v, bl = A["fig"], A["verdict"], A["blame"]
    arm = A["arm_t"]
    T = lambda t: ("T+" + fmt_t(t - arm)) if t is not None and t >= arm - 0.5 else ("T−" + fmt_t(arm - t) if t is not None else "")  # noqa: E731
    ofs = L["clock_ofs"]

    def clock(t):
        if ofs is None or t is None:
            return ""
        return datetime.fromtimestamp(t + ofs, tz).strftime("%H:%M:%S")

    def date(t):
        if ofs is None:
            return "date unknown (no GPS time)"
        return datetime.fromtimestamp(t + ofs, tz).strftime("%a %d %b %Y")

    K = {"normal": "Normal flight", "hard_landing": "Hard landing", "crash": "Crash", "log_end": "Log ends in flight", "no_takeoff": "No take-off"}.get(A["kind"], A["kind"])
    ordr = {"cause": 0, "trigger": 1, "contributing": 2}
    F = sorted(A["F"], key=lambda x: (ordr[x["role"]], -x["w"]))
    if bl:
        word = bl["label"].upper() if v else f"NO CRASH · ISSUES ARE {bl['label'].upper()}"
        sub = f"{bl['sub']} · {bl['confidence']} confidence (human {bl['split']['human']}%, system {bl['split']['system']}%)"
        side = bl["primary"]
    else:
        word, sub, side = "NO CRASH · NO ERROR FOUND", "The log shows no pilot mistakes and no aircraft faults.", "none"
    ok_list = [_lc1(r[0]) for r in A["ruled"]]
    human_f = [x for x in F if _is_human(x)]
    sys_f = [x for x in F if not _is_human(x)]
    trig = next((x for x in F if x["role"] == "trigger"), None)
    if v:
        what = (f"The drone crashed {'at the take-off point' if f['endWhere'] == 'over the take-off point' else f['endWhere']}" if A["kind"] == "crash"
                else "The log stops while the drone was still flying" if A["kind"] == "log_end" else "The drone landed hard")
        if bl and bl["primary"] == "human":
            simple = f"{what}. This was a human error, not a fault in the drone. "
            if ok_list:
                simple += f"The log shows the drone's own systems worked normally: {_list_and(ok_list[:6])}. "
            simple += f"It went wrong because of {_list_and(_plain(x) for x in human_f if x['role'] != 'contributing') or _list_and(_plain(x) for x in human_f)}."
            if trig:
                simple += f" It started when {_plain(trig)}; from then on the drone did exactly what its settings told it to do."
        elif bl:
            simple = f"{what}. This was a system error: part of the aircraft failed. The log shows {_list_and(_plain(x) for x in sys_f if x['role'] != 'contributing') or _list_and(_plain(x) for x in sys_f)}."
            simple += f" Human factors that made it worse: {_list_and(_lc1(x['title']) for x in human_f)}." if human_f else " Nothing the pilot did caused it."
        else:
            simple = f"{what}. The log does not show a clear human or system error. Check the data around the end of the flight."
    else:
        issues = [c for c in A["checks"] if c["sev"] in ("crit", "warn")]
        names = []
        for c in issues:
            n = re.sub(r" \((again|\d+)\)$", "", _lc1(c["title"]))
            if n not in names:
                names.append(n)
        simple = f"The flight ended with a normal landing {f['endWhere']}; there was no crash. "
        if issues:
            simple += (f"{len(issues)} issue{'s were' if len(issues) > 1 else ' was'} found that should be fixed before the next flight"
                       + (f", mainly {'human (how the drone was flown or set up)' if bl and bl['primary'] == 'human' else 'system (the aircraft itself)'}" if bl else "")
                       + f": {_list_and(names[:4])}.")
        else:
            simple += "No problems were found: no failsafes, glitches or hardware warnings."
    S = A["S"]
    end = None
    ct = A["contact"] or f["touchdown"]
    if ct:
        end = min(S, key=lambda r: abs(r["t"] - ct))
    else:
        end = S[-1]
    summary = [
        ["Outcome", A["title"]],
        ["Date and time", date(arm) + (f", armed {clock(arm)}" if ofs is not None else "") + (f", crash {clock(A['contact'])}" if A["contact"] else f", landed {clock(f['touchdown'])}" if f["touchdown"] else "")],
        ["Location", f"Take-off {f['home'][0]:.6f}, {f['home'][1]:.6f}" + (f" · {'crash' if A['contact'] else 'landing'} {end['lat']:.6f}, {end['lon']:.6f}" if A["contact"] or math.hypot(end["e"], end["n"]) > 3 else "")],
        ["Drone", ", ".join(x for x in (f["firmware"] or "ArduPilot", f["frame"], f["board"]) if x)],
        ["Flight", f"{_dur(f['airtime'])} in the air, up to {f['maxAlt']} m high, {f['maxDist']} m from take-off, {f['distance']} m flown"],
    ]
    if A["sequence"]:
        seen, wrong = set(), []
        for x in A["sequence"]:
            if x["sev"] in ("crit", "warn") and not re.search(r"^Crash: Disarming|^GCS Failsafe$|cleared", x["text"], re.I):
                k = re.sub(r"\d+(\.\d+)?", "#", x["text"])
                if k not in seen:
                    seen.add(k)
                    wrong.append({"t": T(x["t"]), "clock": clock(x["t"]), "text": x["text"]})
        wrong = wrong[:8]
    else:
        wrong = [{"t": T(c["t"]) if c["t"] else "", "clock": clock(c["t"]) if c["t"] else "", "text": f"{c['title']}: {_first(c['text'])}"}
                 for c in A["checks"] if c["sev"] in ("crit", "warn")][:6]
    causes = []
    for x in F[:6]:
        side_x = "Human" if _is_human(x) else "System"
        cat = ("Human – flying conditions" if side_x == "Human" else "System – GPS/sensors") if x["cat"] == "environment" else CATN.get(x["cat"], x["cat"])
        causes.append({"role": x["role"], "cat": cat, "side": side_x, "title": x["title"], "why": _first(x["text"], 2), "t": T(x["t"]) if x["t"] else ""})
    if F:
        why = " ".join(s for s in ([_first(trig["text"])] if trig else []) + [_first(x["text"], 2) for x in F if x["role"] == "cause"][:3] if s)
        if not any(x["role"] == "cause" for x in F):
            why = " ".join(_first(x["text"], 2) for x in F[:3])
        if A["kind"] == "crash" and f["fall"]:
            why += f" The crash check then stopped the motors and the drone fell about {max(f['fall']['drop'], f['perched']['h'] if f['perched'] else 0):.0f} m."
    else:
        why = "Nothing in the log indicates a problem: no failsafes, glitches or hardware warnings."
    ruled = ""
    if A["ruled"]:
        ruled = ("Why it is not a system error: " if bl and bl["primary"] == "human" else "Checked and working normally: ") + "; ".join(f"{_lc1(r[0])} ({r[1]})" for r in A["ruled"]) + "."
    actions = []
    for x in F:
        for pat, txt in FIX:
            if re.search(pat, x["title"], re.I) and txt not in actions:
                actions.append(txt)
                break
    P = L["P"]

    def st(good):
        return "" if good is None else ("ok" if good else "check")
    pc = lambda vv: f" ({vv / f['cells']:.2f} V per cell)" if f["cells"] and vv else ""  # noqa: E731
    am = P.get("ANGLE_MAX")
    data = [
        ["Time in the air", _dur(f["airtime"]), "", "From take-off to touchdown.", ""],
        ["Highest point", f"{f['maxAlt']} m", "", "Height above the take-off point.", ""],
        ["Farthest from take-off", f"{f['maxDist']} m, {f['farWhere']}", "", "Straight-line distance from the take-off point.", ""],
        ["Distance flown", f"{f['distance']} m", "", "Total length of the path.", ""],
        ["Top speed", f"{f['maxSpeed']} m/s ({f['maxSpeed'] * 3.6:.0f} km/h)", "", "Fastest ground speed from GPS.", ""],
        ["Maximum tilt", f"{f['maxTilt']}°", f"under {am / 100:g}°" if am else "under 35°", "How far it leaned. High values mean hard braking or sharp manoeuvres.", st(f["maxTilt"] <= (am / 100 if am else 35) + 2)],
        ["Battery at take-off", f"{f['vStart']:.2f} V{pc(f['vStart'])}" if f["vStart"] else "–", "about 4.2 V per cell", "A full pack reads about 4.2 V per cell.", st(f["vStart"] / f["cells"] > 4.0 if f["vStart"] and f["cells"] else None)],
        ["Battery at landing", f"{f['vLand']:.2f} V{pc(f['vLand'])}" if f["vLand"] else "–", "3.5 V per cell or more", "Voltage under load at touchdown. Lower means the pack was run too low, which damages it and risks a power loss.", st(f["vLand"] / f["cells"] >= 3.5 if f["vLand"] and f["cells"] else None)],
        ["Charge used", (f"{round(f['mah'])} mAh" + (f" of {f['cap']:.0f} mAh ({round(f['mah'] / f['cap'] * 100)}%)" if f["cap"] else "")) if f["mah"] else "–", "80% of capacity or less", "How much of the battery was used.", st(f["mah"] / f["cap"] <= 0.8 if f["mah"] and f["cap"] else None)],
        ["GPS satellites (worst)", f"{f['minSats']}" if f["minSats"] is not None else "–", "10 or more", "More satellites give a more accurate, stable position.", st(f["minSats"] >= 10 if f["minSats"] is not None else None)],
        ["GPS precision (worst HDOP)", f"{f['maxHdop']}" if f["maxHdop"] else "–", "below 1.0", "Lower is better. Above 1.5 the position can wander.", st(f["maxHdop"] < 1.0 if f["maxHdop"] else None)],
        ["Vibration (peak)", f"{f['maxVibe']} m/s²", "below 30 m/s²", "Strong vibration confuses the autopilot's height and position sensors.", st(f["maxVibe"] < 30)],
        ["Accelerometer clipping", f"{f['clips'] or 0:.0f}", "0", "Clipping means vibration overloaded the sensor.", st(not f["clips"])],
        ["Navigation health (EKF, peak)", f"{f['maxEkf']}", "below 0.8 (failsafe level)", "How confident the autopilot is in its own position and height; lower is better.", st(f["maxEkf"] < 0.8)],
        ["Attitude tracking", f"{f['trackRms']}° average, {f['trackMax']}° worst" if f["trackRms"] is not None else "–", "average under 3°", "How closely the drone followed the autopilot's commands. Large errors point to a motor, propeller or tuning problem.", st(f["trackRms"] < 3 if f["trackRms"] is not None else None)],
        ["Hover throttle", f"{round(f['hover'] * 100)}%" if f["hover"] else "–", "20–50%", "Power needed to hover. High values mean the drone is heavy for its motors.", st(0.2 <= f["hover"] <= 0.5 if f["hover"] else None)],
        ["Motor output (peak)", f"{f['motMaxPct']}%" if f["motMaxPct"] is not None else "–", "below 95%", "Motors at 100% have no reserve left for control.", st(f["motMaxPct"] < 95 if f["motMaxPct"] is not None else None)],
        ["Wind", f"{f['windMax']} m/s (estimate)" if f["windMax"] is not None else (f"{f['lean']['label']} ({f['lean']['deg']}° lean to hold position)" if f["lean"] else "–"), "lean under 6°", "The drone leans into the wind to hold its position; a bigger lean means stronger wind.",
         st(f["lean"]["deg"] < 6 if f["lean"] else (f["windMax"] < 9 if f["windMax"] is not None else None))],
        ["Flight-controller power", f"{f['minVcc']} V lowest" if f["minVcc"] else "–", "4.8–5.4 V", "A dip below 4.5 V can reset the autopilot.", st(f["minVcc"] >= 4.6 if f["minVcc"] else None)],
    ]
    if f["minRssi"] is not None:
        data.append(["Telemetry radio signal (lowest)", f"{f['minRssi']}", "above 40", "Signal strength of the telemetry link.", st(f["minRssi"] > 40)])
    if A["sequence"]:
        timeline_src = A["sequence"]
    else:
        timeline_src = [{"t": e["t"], "text": e["text"], "sev": e["sev"]} for e in A["events"] if e["t"] >= arm and (e["sev"] or e["kind"] in ("mode", "disarm"))]
        if f["takeoff"]:
            timeline_src.append({"t": f["takeoff"], "text": "Take-off", "sev": ""})
        if f["touchdown"]:
            timeline_src.append({"t": f["touchdown"], "text": f"Touchdown {f['endWhere']}", "sev": ""})
        timeline_src.sort(key=lambda x: x["t"])
    timeline = [{"t": T(x["t"]), "clock": clock(x["t"]), "text": x["text"], "sev": x["sev"]} for x in timeline_src]
    return {
        "title": "Flight Incident Report" if v else "Flight Report", "kind": A["kind"], "kind_label": K,
        "verdict_word": word, "verdict_sub": sub, "verdict_side": side, "simple": simple, "summary": summary,
        "wrong_title": "What went wrong" if v else "Issues found", "wrong": wrong, "causes": causes, "why": why, "ruled": ruled,
        "actions": actions[:5], "data": data, "timeline": timeline,
    }


def _dur(s):
    s = round(s or 0)
    h, m, x = s // 3600, (s % 3600) // 60, s % 60
    return f"{h} h {m} min" if h else f"{m} min {x} s" if m else f"{x} s"


# ----------------------------------------------------------------------------------------------- entry point
def _decimate(n, max_points):
    if n <= max_points:
        return list(range(n))
    step = n / max_points
    return sorted(set(int(i * step) for i in range(max_points)) | {n - 1})


def investigate(path, tz_name=None):
    """Analyse the longest flight in a .bin log. Returns a JSON-safe dict, or None for unsupported logs."""
    if not str(path).lower().endswith(".bin"):
        return None
    M, types = read_dataflash(path)
    L = load(M, types)
    if not L["cycles"]:
        return None
    k = max(range(len(L["cycles"])), key=lambda i: ((L["cycles"][i]["disarm"] if L["cycles"][i]["disarm"] is not None else L["t_end"]) - L["cycles"][i]["arm"]))
    A = analyse(L, k)
    if A is None:
        return None
    tz = timezone.utc
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name or os.environ.get("REPORT_TZ", "Asia/Kolkata"))
        except Exception:
            tz = timezone.utc
    rep = build_report_text(A, L, tz)
    f, S, arm = A["fig"], A["S"], A["arm_t"]
    # 3D track (local metres, x = east, y = height, z = north) and chart series, decimated
    idx = _decimate(len(S), 2000)
    span = A["pwmMax"] - A["pwmMin"] or 1000
    track = [{"t": round(S[i]["t"] - arm, 2), "x": round(S[i]["e"], 2), "y": round(S[i]["h"], 2), "z": round(S[i]["n"], 2),
              "roll": _r(S[i]["roll"]), "pitch": _r(S[i]["pitch"]), "yaw": _r(S[i]["yaw"]), "armed": S[i]["armed"]} for i in idx]

    def mk(label, t, color):
        if t is None:
            return None
        r = min(S, key=lambda x: abs(x["t"] - t))
        return {"label": label, "x": round(r["e"], 2), "y": round(r["h"], 2), "z": round(r["n"], 2), "t": round(t - arm, 2), "color": color}
    markers = [mk("Take-off", f["takeoff"], "accent")]
    if A["contact"]:
        markers.append(mk("Crash", A["contact"], "crit"))
    elif f["touchdown"]:
        markers.append(mk("Landing", f["touchdown"], "ok"))
    if f["endedAirborne"]:
        markers.append(mk("Log ends", S[-1]["t"], "crit"))
    for fs in A["failsafes"]:
        markers.append(mk(fs["name"], fs["t"], "warn"))
    if f["maxDist"] > 30:
        markers.append(mk(f"Farthest {f['maxDist']} m", f["farT"], "accent"))
    markers = [m for m in markers if m]
    cidx = _decimate(len(S), 1500)
    chart = {
        "t": [round(S[i]["t"] - arm, 2) for i in cidx],
        "height": [_r(S[i]["h"], 2) for i in cidx],
        "target": [_r(S[i]["dalt"] - S[i]["calt"] + S[i]["h"], 2) if S[i]["dalt"] is not None and S[i]["calt"] is not None else None for i in cidx],
        "speed": [_r(S[i]["gs"], 2) for i in cidx], "climb": [_r(S[i]["crt"], 2) for i in cidx],
        "volt": [_r(S[i]["volt"], 2) for i in cidx],
        "motors": [[_r((S[i]["m"][j] - A["pwmMin"]) / span * 100) if S[i]["m"][j] is not None else None for i in cidx] for j in range(len(S[0]["m"]))],
        "roll": [_r(S[i]["roll"]) for i in cidx], "droll": [_r(S[i]["droll"]) for i in cidx],
        "contact": round(A["contact"] - arm, 2) if A["contact"] else None,
        "failsafes": [round(fs["t"] - arm, 2) for fs in A["failsafes"]],
        "cell_floor": round(3.5 * f["cells"], 2) if f["cells"] else None,
    }
    locs = [["Take-off", f["home"][0], f["home"][1], f["home"][2], f["takeoff"]]]
    far = min(S, key=lambda r: abs(r["t"] - f["farT"]))
    locs.append(["Farthest point", far["lat"], far["lon"], far["amsl"], far["t"]])
    ct = A["contact"] or f["touchdown"]
    endr = min(S, key=lambda r: abs(r["t"] - ct)) if ct else S[-1]
    locs.append(["Crash (first contact)" if A["kind"] == "crash" else "Last position" if A["kind"] == "log_end" else "Landing", endr["lat"], endr["lon"], endr["amsl"], endr["t"]])
    if f["fall"]:
        locs.append(["Where it fell", S[-1]["lat"], S[-1]["lon"], S[-1]["amsl"], S[-1]["t"]])
    for fs in A["failsafes"]:
        r = min(S, key=lambda x: abs(x["t"] - fs["t"]))
        locs.append([fs["name"], r["lat"], r["lon"], r["amsl"], fs["t"]])
    T = lambda t: ("T+" + fmt_t(t - arm)) if t is not None else ""  # noqa: E731
    D = A["detect"]
    fs_events = []
    for fs in A["failsafes"]:
        r = min(S, key=lambda x: abs(x["t"] - fs["t"]))
        fs_events.append({"name": fs["name"], "sub": fs["sub"], "t_s": round(fs["t"] - arm, 1),
                          "airborne_s": round(fs["t"] - f["takeoff"], 1) if f["takeoff"] else None,
                          "dist_m": round(math.hypot(r["e"], r["n"]), 1), "height_m": _r(r["h"]),
                          "action": fs["action"], "overridden": bool(fs["override"])})
    flags = {
        "crash": A["kind"] == "crash", "log_end_in_air": A["kind"] == "log_end", "hard_landing": A["kind"] == "hard_landing",
        "thrust_loss": D["motor_fault"] or D["thrust_short"] or D["thrust_loss_err"],
        "motor_issue": D["motor_fault"] or D["att_loss"],
        "gps_issue": D["gps_glitches"] > 0 or D["pos_jump"],
        "ekf_issue": D["ekf"],
        "prearm_issues": len(D["prearm"]), "prearm_messages": D["prearm"][:10],
        "vibration_issue": D["vibration"], "power_issue": D["vcc_low"] or D["sag"],
        "battery_failsafe": any(e["sub"] == 6 for e in fs_events),
        "gcs_failsafe": any(e["sub"] == 8 for e in fs_events),
        "rc_failsafe": any(e["sub"] == 5 for e in fs_events),
        "cold_start_s": D["cold_start_s"],
    }
    return {
        "version": VERSION, "kind": A["kind"], "flags": flags, "failsafe_events": fs_events, "title": A["title"], "verdict": A["verdict"], "blame": A["blame"],
        "report": rep,
        "findings": [{**x, "t": T(x["t"]) if x["t"] else ""} for x in A["F"]],
        "ruled": A["ruled"], "evidence": A["evidence"],
        "checks": [{**c, "t": T(c["t"]) if c["t"] else ""} for c in sorted(A["checks"], key=lambda c: {"crit": 0, "warn": 1, "info": 2, "ok": 3}[c["sev"]])],
        "phases": [{"a": T(p["a"]), "b": T(p["b"]), "s": p["s"], "text": p["text"]} for p in A["phases"]],
        "locations": [{"name": n, "lat": round(la, 7), "lon": round(lo, 7), "alt": _r(al), "t": T(t)} for n, la, lo, al, t in locs],
        "figures": {k: f[k] for k in ("airtime", "maxAlt", "maxDist", "distance", "maxSpeed", "maxTilt", "vStart", "vLand", "cells", "mah", "cap",
                                       "minSats", "maxHdop", "maxVibe", "maxEkf", "trackRms", "hover", "motMaxPct", "drift", "firmware", "frame", "board")},
        "home": f["home"], "track": track, "markers": markers, "chart": chart,
        "flights_in_log": len(L["cycles"]),
    }


if __name__ == "__main__":  # quick manual check: python investigator.py some.bin
    import json
    import sys
    res = investigate(sys.argv[1])
    print(json.dumps({k: res[k] for k in ("kind", "title", "verdict", "blame")}, indent=1) if res else "unsupported")
    if res:
        print(json.dumps(res["report"], indent=1, ensure_ascii=False)[:6000])
