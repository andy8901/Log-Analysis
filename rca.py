"""
Root-cause / crash-analysis engine.

Implements a timeline-based, multi-signal decision framework rather than
a single-message classifier -- a lone ERR message or EKF warning is
deliberately NOT enough on its own to conclude "crash", "system error", or
"pilot error". The design (crash-confidence tiers, primary/secondary/
contributing structure, pilot-command-vs-vehicle-response correlation,
category taxonomy) mirrors a detailed RCA specification the app's owner
provided; this module is the implementation of it.

This is advisory, not a verdict: it surfaces evidence and a best-effort
classification with an explicit confidence level, never a bare percentage
or an unqualified "pilot error". Physical inspection of the aircraft is
the only real confirmation of a crash and its cause -- everything here is
inferred from telemetry.
"""

import bisect


# ArduPilot ERR Subsys (see analyzer.ERROR_SUBSYSTEMS) -> RCA category.
#
# CRASH CHECK (12) is deliberately not a "cause" category -- it is the
# strongest possible *confirmation* that a crash occurred, but the thing
# that made the vehicle crash is whatever preceded it.
#
# GCS FAILSAFE (8) and FENCE FAILSAFE (9) are deliberately NOT mapped here,
# so they're never picked as a primary root cause: losing the ground
# station's telemetry LINK doesn't mechanically cause a fully-autonomous
# vehicle to crash the way losing RC control or motor thrust does, and a
# fence breach is typically a symptom of the vehicle already being
# off-course, not a mechanical cause of it. Both still appear in the
# ordinary alerts/timeline, just not as an RCA primary/secondary cause.
SUBSYS_TO_CATEGORY = {
    5: "RADIO_RC",              # RADIO FAILSAFE (actual loss of pilot RC control)
    6: "BATTERY_POWER",         # BATTERY FAILSAFE
    7: "GPS",                   # GPS FAILSAFE
    16: "EKF_ESTIMATION",        # EKF CHECK
    17: "EKF_ESTIMATION",        # EKF/INAV FAILSAFE
    18: "SENSOR",                 # BAROMETER
    25: "MOTOR_ESC",              # THRUST LOSS CHECK
    26: "SENSOR",                  # SENSORS FAILSAFE
    29: "VIBRATION_MECHANICAL",    # VIBRATION FAILSAFE
}

# Flight modes where the roll/pitch stick directly commands lean angle, so a
# pilot-input correlation is meaningful. In Auto/Guided/RTL/Circle/etc.
# desired attitude comes from the navigation controller, not the stick --
# attributing divergence there to "pilot error" would be wrong.
PILOT_ATTITUDE_MODES = {
    "STABILIZE", "ACRO", "ALT_HOLD", "LOITER", "POSHOLD",
    "SPORT", "DRIFT", "FLOWHOLD", "AUTOTUNE",
}

CATEGORY_LABELS = {
    "MOTOR_ESC": "Motor / ESC / Propulsion",
    "BATTERY_POWER": "Battery / Power",
    "EKF_ESTIMATION": "EKF / State Estimation",
    "SENSOR": "Sensor (barometer / other)",
    "RADIO_RC": "Radio / RC / Telemetry",
    "VIBRATION_MECHANICAL": "Vibration / Mechanical",
    "GPS": "GPS",
    "PILOT_INPUT": "Pilot Input",
    "UNKNOWN": "Unknown / Insufficient Evidence",
}

CRASH_CONFIDENCE_ORDER = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]


def find_attitude_divergence_events(att_time, roll_act, roll_des, pitch_act, pitch_des, threshold_deg=25):
    """Rising-edge events where actual attitude departs from desired by more
    than threshold_deg -- i.e. the vehicle failed to do what it was told,
    which is what actually matters for RCA (not just a large bank angle)."""
    events = []
    was_above = False
    for i in range(len(att_time)):
        err = max(abs(roll_act[i] - roll_des[i]), abs(pitch_act[i] - pitch_des[i]))
        if err > threshold_deg and not was_above:
            events.append({
                "t": att_time[i], "type": "ATTITUDE",
                "text": f"Attitude tracking error {round(err, 1)}° (actual vs desired)",
            })
            was_above = True
        elif err <= threshold_deg:
            was_above = False
    return events


def find_motor_output_anomaly_events(rcout_time, motor_series, threshold=400):
    """Rising-edge events where motor outputs spread apart abnormally --
    e.g. one motor pinned at minimum while another compensates at maximum
    is the classic signature of a lost propeller/motor/ESC, distinct from
    normal attitude-correction differences between motors."""
    events = []
    names = list(motor_series.keys())
    if not names:
        return events
    was_above = False
    for i in range(len(rcout_time)):
        vals = [motor_series[n][i] for n in names]
        spread = max(vals) - min(vals)
        if spread > threshold and not was_above:
            events.append({
                "t": rcout_time[i], "type": "MOTOR",
                "text": f"Motor output imbalance ({spread}µs spread across motors -- one likely at min/max compensating for the other)",
            })
            was_above = True
        elif spread <= threshold:
            was_above = False
    return events


def find_pilot_stick_events(rcin_time, rcin_roll, rcin_pitch, center=1500, threshold=300):
    """Rising-edge events where the roll/pitch stick is well off center --
    used to correlate pilot command against vehicle response, not as an
    anomaly in itself (a large stick input is often completely normal)."""
    events = []
    was_above = False
    for i in range(len(rcin_time)):
        dev = max(abs(rcin_roll[i] - center), abs(rcin_pitch[i] - center))
        if dev > threshold and not was_above:
            events.append({
                "t": rcin_time[i], "type": "PILOT_INPUT",
                "text": f"Large roll/pitch stick input ({dev}µs from center)",
            })
            was_above = True
        elif dev <= threshold:
            was_above = False
    return events


def mode_at(mode_changes, t):
    """Flight mode active at time t, from a chronological [(t, name), ...] list."""
    times = [m[0] for m in mode_changes]
    i = bisect.bisect_right(times, t)
    return mode_changes[i - 1][1] if i > 0 else None


def _nearest_pilot_input_before(pilot_events, t, window=1.5):
    best = None
    for e in pilot_events:
        if e["t"] <= t and (t - e["t"]) <= window:
            best = e
    return best


def classify_incident(
    alerts, timeline, possible_incidents, sudden_events,
    subsys_events, crash_text_detected,
    att_time, roll_act, roll_des, pitch_act, pitch_des,
    rcin_time, rcin_roll, rcin_pitch,
    rcout_time, motor_series,
    mode_changes,
    roll_error, pitch_error,
):
    """
    subsys_events: [{"t": float, "subsys": int, "text": str}] -- ERR
        messages with code != 0, already decoded elsewhere.
    crash_text_detected: [t, ...] timestamps where a "Crash: Disarming"
        (or similar) MSG was seen.
    sudden_events: the existing sample-to-sample jump events (VOLTAGE,
        CURRENT, ALTITUDE, RPM) from analyzer.detect_sudden_events, BEFORE
        correlate_incidents() is applied -- this function adds ATTITUDE
        events to that same pool and re-runs the correlation so a crash
        confirmed by multiple channels together (not just attitude alone)
        gets full credit.
    """

    attitude_events = find_attitude_divergence_events(att_time, roll_act, roll_des, pitch_act, pitch_des)
    pilot_events = find_pilot_stick_events(rcin_time, rcin_roll, rcin_pitch)
    motor_events = find_motor_output_anomaly_events(rcout_time, motor_series)

    # Re-run the cross-channel correlation with attitude and motor-output
    # events included, so e.g. "attitude divergence + altitude drop + motor
    # imbalance together" is recognised as the stronger, multi-signal crash
    # signature it is (this also catches motor/ESC failures on logs with no
    # ESC RPM telemetry, using the RCOU output fallback instead).
    from analyzer import correlate_incidents  # local import: avoids a cycle at module load
    combined_events = sudden_events + attitude_events + motor_events
    combined_incidents = correlate_incidents(combined_events)

    # ---------------- CRASH CONFIDENCE ----------------

    crash_check_events = [e for e in subsys_events if e["subsys"] == 12]

    reasons = []
    if crash_check_events or crash_text_detected:
        confidence = "CRITICAL"
        if crash_check_events:
            reasons.append("ArduPilot's own crash-check (ERR Subsys=CRASH_CHECK) fired.")
        if crash_text_detected:
            reasons.append("Log contains ArduPilot's own \"Crash: Disarming\" message.")
    elif combined_incidents:
        confidence = "HIGH"
        for ci in combined_incidents:
            reasons.append(ci["text"])
    elif roll_error > 30 or pitch_error > 30:
        confidence = "MEDIUM"
        reasons.append(f"Peak attitude tracking error ({max(roll_error, pitch_error)}°) exceeds a level consistent with controlled flight, but no corroborating channel confirms impact.")
    elif alerts:
        confidence = "LOW"
        reasons.append("Warning-level anomalies present, but none individually or collectively indicate loss of control.")
    else:
        confidence = "NONE"
        reasons.append("No anomalies detected.")

    incident_level = {
        "CRITICAL": "CONFIRMED_CRASH",
        "HIGH": "PROBABLE_CRASH",
        "MEDIUM": "POSSIBLE_LOSS_OF_CONTROL",
        "LOW": "WARNING",
        "NONE": "NORMAL",
    }[confidence]

    # ---------------- ROOT CAUSE (only if something happened) ----------------

    if confidence == "NONE":
        return {
            "incident_level": incident_level,
            "crash_confidence": confidence,
            "crash_confidence_reasons": reasons,
            "root_cause": None,
            "preceding_context": [],
            "secondary_events": [],
            "contributing_factors": [],
            "disclaimer": DISCLAIMER,
        }

    # Anchor time: earliest strong confirmation, else earliest correlated
    # cluster, else the point of peak attitude error, else the first alert
    # with a known timestamp.
    if crash_check_events or crash_text_detected:
        anchor_t = min([e["t"] for e in crash_check_events] + crash_text_detected)
    elif combined_incidents:
        anchor_t = min(ci["t"] for ci in combined_incidents)
    elif attitude_events:
        anchor_t = attitude_events[0]["t"]
    else:
        timed = [e["t"] for e in timeline if isinstance(e.get("t"), (int, float))]
        anchor_t = min(timed) if timed else 0

    # 30s lookback: an electrical/software glitch (voltage sag, EKF reset)
    # can precede a crash by a couple of seconds, but a developing mechanical
    # problem (a failing motor/ESC/prop) can show up as a sustained anomaly
    # for tens of seconds beforehand -- and since these detectors only flag
    # the *onset* of an anomaly (rising edge), not every sample while it
    # stays elevated, a too-narrow window would miss that onset entirely if
    # the problem had already begun and simply never resolved before impact.
    window_start = anchor_t - 30
    window_end = anchor_t + 1

    # Build the categorised, time-ordered event pool inside the window.
    pool = []
    for e in combined_events:
        if window_start <= e["t"] <= window_end:
            cat = {"VOLTAGE": "BATTERY_POWER", "CURRENT": "BATTERY_POWER",
                   "ALTITUDE": "ALTITUDE", "RPM": "MOTOR_ESC", "MOTOR": "MOTOR_ESC",
                   "ATTITUDE": "ATTITUDE"}[e["type"]]
            pool.append({"t": e["t"], "category": cat, "text": e["text"]})
    for e in subsys_events:
        if e["subsys"] == 12:
            continue  # confirmation marker, not a cause
        cat = SUBSYS_TO_CATEGORY.get(e["subsys"])
        if cat and window_start <= e["t"] <= window_end:
            pool.append({"t": e["t"], "category": cat, "text": e["text"]})

    pool.sort(key=lambda e: e["t"])

    # Resolve ATTITUDE/ALTITUDE placeholders into a real category: attitude
    # divergence needs the pilot-input + flight-mode check; a lone altitude
    # drop with nothing else in the window is too ambiguous to name a system.
    resolved = []
    for e in pool:
        if e["category"] == "ATTITUDE":
            mode = mode_at(mode_changes, e["t"])
            stick = _nearest_pilot_input_before(pilot_events, e["t"])
            if stick and mode in PILOT_ATTITUDE_MODES:
                resolved.append({**e, "category": "PILOT_INPUT",
                                  "text": e["text"] + f" -- coincides with a large stick input while in {mode} (pilot commands lean angle directly in this mode)"})
            elif mode and mode not in PILOT_ATTITUDE_MODES:
                resolved.append({**e, "category": "UNKNOWN",
                                  "text": e["text"] + f" -- occurred in {mode}, where desired attitude comes from the autopilot/mission, not the pilot's stick"})
            else:
                resolved.append({**e, "category": "UNKNOWN",
                                  "text": e["text"] + " -- no corroborating pilot input or system anomaly found to explain it"})
        elif e["category"] == "ALTITUDE":
            resolved.append({**e, "category": "UNKNOWN", "text": e["text"] + " (altitude change alone, cause not independently confirmed)"})
        else:
            resolved.append(e)

    if not resolved:
        root_cause = {
            "category": "UNKNOWN",
            "label": CATEGORY_LABELS["UNKNOWN"],
            "confidence": "LOW",
            "t": anchor_t,
            "text": "No specific precursor event found in the telemetry in the window leading up to this incident.",
        }
        preceding_context = []
        secondary = []
    else:
        # Prefer the first EXPLANATORY event (a resolved category) as the
        # primary cause over an earlier but ambiguous one (a lone altitude
        # drop with nothing to explain it, or attitude divergence with no
        # corroborating signal) -- chronological order matters, but an
        # earlier unexplained symptom shouldn't outrank a later event that
        # actually points at a specific system. The unresolved ones are
        # still surfaced, just as context rather than the named cause.
        primary_idx = next((i for i, e in enumerate(resolved) if e["category"] != "UNKNOWN"), None)

        if primary_idx is None:
            primary_idx = 0

        first = resolved[primary_idx]
        root_cause = {
            "category": first["category"],
            "label": CATEGORY_LABELS.get(first["category"], first["category"]),
            "confidence": "MEDIUM" if first["category"] == "UNKNOWN" else "MEDIUM-HIGH",
            "t": first["t"],
            "text": first["text"],
        }
        preceding_context = [
            {**e, "label": CATEGORY_LABELS.get(e["category"], e["category"])}
            for e in resolved[:primary_idx]
        ]
        secondary = [
            {**e, "label": CATEGORY_LABELS.get(e["category"], e["category"])}
            for e in resolved[primary_idx + 1:]
        ]

    # Contributing factors: notable but not the lead cause -- persistent
    # issues across the whole flight rather than the immediate trigger.
    contributing = []
    alert_text = " ".join(alerts)
    if "VIBRATION" in alert_text and root_cause["category"] != "VIBRATION_MECHANICAL":
        contributing.append("Elevated vibration levels recorded during the flight.")
    if ("GPS SATELLITES" in alert_text or "HDOP" in alert_text) and root_cause["category"] != "GPS":
        contributing.append("Degraded GPS quality (low satellite count and/or poor HDOP) recorded during the flight.")
    if "EKF INSTABILITY" in alert_text and root_cause["category"] != "EKF_ESTIMATION":
        contributing.append("EKF instability recorded elsewhere in the flight.")
    if "PARAMETER" in alert_text:
        contributing.append("One or more safety-relevant parameters differ from this airframe's baseline.")

    return {
        "incident_level": incident_level,
        "crash_confidence": confidence,
        "crash_confidence_reasons": reasons,
        "root_cause": root_cause,
        "preceding_context": preceding_context,
        "secondary_events": secondary,
        "contributing_factors": contributing,
        "disclaimer": DISCLAIMER,
    }


DISCLAIMER = (
    "This classification is inferred from flight-log telemetry using a rule-based "
    "correlation across multiple channels (attitude tracking, motor output, battery, "
    "vibration, EKF, RC input, and ArduPilot's own failsafe/crash-check events). It is "
    "advisory, not a verdict -- a single log signal is never treated as conclusive, and "
    "no automated system can replace a physical inspection of the aircraft as the final "
    "confirmation of a crash and its cause."
)
