"""Best-effort reverse geocoding (takeoff lat/lon -> Indian state /
Karnataka district), used only to bucket a flight for the Organization
Overview's Location breakdown -- see stats.location_for_upload(). Calls
OpenStreetMap's Nominatim (public, free, no API key, usage-policy capped
at roughly 1 request/second), cached permanently in the database
(models.GeoCache) since the same handful of real-world sites get flown
from repeatedly. Never raises; every caller gets (None, None) back on any
failure (network down, timeout, unparseable response, sandbox/firewall
blocking outbound access) and treats that as "not yet classified" rather
than blocking the page."""

import json
import time
import urllib.error
import urllib.request

# Karnataka's districts -- the Organization Overview's Location filter
# lists these individually (rather than lumping the whole state together)
# since the factory and most of this org's own testing/operations are
# there. Every other Indian state/UT is listed as a whole; Karnataka is
# deliberately excluded from that list below since it's represented here
# instead.
KARNATAKA_DISTRICTS = [
    "Bagalkot", "Ballari", "Belagavi", "Bengaluru Rural", "Bengaluru Urban",
    "Bidar", "Chamarajanagar", "Chikballapur", "Chikkamagaluru", "Chitradurga",
    "Dakshina Kannada", "Davanagere", "Dharwad", "Gadag", "Hassan", "Haveri",
    "Kalaburagi", "Kodagu", "Kolar", "Koppal", "Mandya", "Mysuru", "Raichur",
    "Ramanagara", "Shivamogga", "Tumakuru", "Udupi", "Uttara Kannada",
    "Vijayapura", "Vijayanagara", "Yadgir",
]

INDIAN_STATES_AND_UTS = [
    "Andhra Pradesh", "Arunachal Pradesh", "Assam", "Bihar", "Chhattisgarh",
    "Goa", "Gujarat", "Haryana", "Himachal Pradesh", "Jharkhand", "Kerala",
    "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Andaman and Nicobar Islands", "Chandigarh",
    "Dadra and Nagar Haveli and Daman and Diu", "Delhi",
    "Jammu and Kashmir", "Ladakh", "Lakshadweep", "Puducherry",
]

_ROUND_TO = 2  # ~1.1km grid -- flights from the same real-world site cluster into one cache entry
_TIMEOUT_S = 4
_MIN_CALL_GAP_S = 1.1  # Nominatim's usage policy: no more than ~1 request/second
_USER_AGENT = "Neosky-LogAnalysis/1.0 (internal drone flight-log diagnostic tool)"

_last_call_at = 0.0


def _round(v):
    return round(v, _ROUND_TO)


def _fetch(lat, lon):
    """One live Nominatim reverse-geocode call, rate-limited to roughly
    1/second across calls in this process. Returns (state, district) or
    (None, None) on any failure -- never raises."""
    global _last_call_at
    wait = _MIN_CALL_GAP_S - (time.monotonic() - _last_call_at)
    if wait > 0:
        time.sleep(wait)
    _last_call_at = time.monotonic()

    url = (
        "https://nominatim.openstreetmap.org/reverse"
        f"?lat={lat}&lon={lon}&format=jsonv2&addressdetails=1&zoom=8"
    )
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None, None

    address = data.get("address") or {}
    state = address.get("state")
    district = address.get("state_district") or address.get("county") or address.get("district")
    return state, district


def resolve(lat, lon, budget):
    """(state, district) for this coordinate -- the DB cache first, a live
    Nominatim lookup only if that misses and `budget` allows it.

    `budget` is a single-element list used as a shared mutable counter
    across every point resolved within one request (e.g. one Organization
    Overview page load): a live call is only made while budget[0] > 0,
    decremented on each one. This bounds how many brand-new lookups a
    single page load will make -- a page with many never-before-seen
    flight locations degrades to "not yet classified" for the rest
    instead of making the request wait on a long chain of rate-limited
    network calls. A later page load picks up where this one left off,
    since every resolved point (including a failed lookup, to avoid
    retrying it every single time) is cached permanently."""
    from app import db
    from app.models import GeoCache

    rlat, rlon = _round(lat), _round(lon)
    cached = GeoCache.query.filter_by(lat_r=rlat, lon_r=rlon).first()
    if cached:
        return cached.state, cached.district

    if budget[0] <= 0:
        return None, None
    budget[0] -= 1

    state, district = _fetch(lat, lon)
    db.session.add(GeoCache(lat_r=rlat, lon_r=rlon, state=state, district=district))
    db.session.commit()
    return state, district


def is_cached(lat, lon):
    """Whether this point already has a GeoCache row -- true even if that
    lookup came back empty (state/district both None), since that's still
    a permanent, already-spent answer, not something still waiting for a
    turn. Used to count genuinely-pending points (never attempted) apart
    from ones that were tried and simply couldn't be resolved -- resolve()
    itself can't tell the two apart from its return value alone, since
    both read as (None, None)."""
    from app.models import GeoCache

    rlat, rlon = _round(lat), _round(lon)
    return GeoCache.query.filter_by(lat_r=rlat, lon_r=rlon).first() is not None
