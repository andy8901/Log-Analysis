"""Warranty status for a drone, computed fresh from the imported Warranty
row (see routes_admin.import_warranty()) rather than stored -- "days left"
and "status" are relative to today, so a stored value would go stale the
moment it was written. Expiry/age math only; nothing here touches the
analysis engines."""

from datetime import date, datetime

EXPIRING_SOON_DAYS = 30


def _add_months(d, months):
    """d + `months` calendar months, clamped to the shorter month's last
    day when the start day doesn't exist there (e.g. Jan 31 + 1 month ->
    Feb 28/29, not Mar 3) -- plain day-arithmetic would silently drift."""
    month_index = d.month - 1 + months
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = d.day
    while True:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1


def _as_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def compute_status(purchase_date, warranty_months, today=None):
    """(expiry_date, days_left, status) -- status is "Active", "Expiring
    Soon" (within EXPIRING_SOON_DAYS), or "Expired". None/None/None if
    there's not enough data (no purchase date or warranty term on file)."""
    purchase_date = _as_date(purchase_date)
    if purchase_date is None or not warranty_months:
        return None, None, None

    today = today or date.today()
    expiry = _add_months(purchase_date, warranty_months)
    days_left = (expiry - today).days

    if days_left < 0:
        status = "Expired"
    elif days_left <= EXPIRING_SOON_DAYS:
        status = "Expiring Soon"
    else:
        status = "Active"
    return expiry, days_left, status


def drone_age_days(purchase_date, today=None):
    purchase_date = _as_date(purchase_date)
    if purchase_date is None:
        return None
    today = today or date.today()
    return (today - purchase_date).days


def _normalize_msn(msn):
    """Loses a purely-cosmetic leading-zero difference ("077" vs "77") --
    Excel silently turns a text MSN column into numbers if it isn't
    explicitly formatted as text, dropping any leading zero on import,
    while this app's own MSN fields (typed on the upload form, or
    extracted from a filename like "MSN_077_...") keep it. Comparing on
    this normalized form instead of the raw string is what lets the two
    sides still match. Non-numeric MSNs pass through unchanged."""
    text = str(msn).strip()
    return text.lstrip("0") or "0" if text.isdigit() else text


def lookup(warranty_model, msn, customer_name=None):
    """The Warranty row for this drone, best-effort: an exact (msn,
    customer_name) match when a customer is on file for the drone, else
    the only Warranty row for that msn if there's exactly one (unambiguous
    even without a customer match), else None if there's no row or the
    msn alone is ambiguous across multiple customers. MSN matching is
    leading-zero-insensitive (see _normalize_msn)."""
    if not msn:
        return None
    target = _normalize_msn(msn)
    all_rows = warranty_model.query.all()
    matches = [r for r in all_rows if _normalize_msn(r.msn) == target]
    if customer_name:
        by_customer = [r for r in matches if r.customer_name == customer_name]
        if by_customer:
            return by_customer[0]
    return matches[0] if len(matches) == 1 else None


def status_for_drone(drone):
    """Full warranty summary dict for a Drone row, or None if there's no
    matching Warranty data on file. `drone` must have .msn and
    .customer_name (a plain Drone model instance)."""
    from app.models import Warranty

    row = lookup(Warranty, drone.msn, drone.customer_name)
    if row is None:
        return None

    expiry, days_left, status = compute_status(row.purchase_date, row.warranty_months)
    return {
        "customer_name": row.customer_name,
        "product_name": row.product_name,
        "purchase_date": row.purchase_date,
        "warranty_months": row.warranty_months,
        "expiry_date": expiry,
        "days_left": days_left,
        "status": status,
        "drone_age_days": drone_age_days(row.purchase_date),
    }
