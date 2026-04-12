"""Credify classification engine — single source of truth for all account helpers."""
import re
from datetime import datetime

MONTHS_SHORT = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
_RE_NON_NUMERIC = re.compile(r'[^\d.]')
_RE_UNSAFE_FILENAME = re.compile(r'[^\w\s-]')


def _clean_name_part(s):
    """Strip trailing dashes and collapse multi-spaces."""
    s = re.sub(r'[-]+$', '', s.strip())
    return re.sub(r'\s+', ' ', s).strip()


def resolve_client_name(client):
    first = _clean_name_part(str(client.get("first_name", "")))
    last = _clean_name_part(str(client.get("last_name", "")))
    if first and last: return f"{first} {last}".title()
    if first: return first.title()
    if last: return last.title()
    name = _clean_name_part(str(client.get("name", "")))
    return name.title() if name else "Client"


def resolve_client_last(client):
    last = _clean_name_part(str(client.get("last_name", "")))
    if last: return last.title()
    name = _clean_name_part(str(client.get("name", "")))
    return name.split()[-1].title() if name else "Client"


def parse_balance(s):
    try: return float(_RE_NON_NUMERIC.sub('', str(s)))
    except (ValueError, TypeError): return 0.0


def parse_opened(s):
    if not s: return None
    s = str(s).strip()
    for fmt in ("%m/%Y", "%m/%d/%Y", "%Y"):
        try: return datetime.strptime(s, fmt)
        except ValueError: continue
    return None


def months_between(dt, now):
    return (now.year - dt.year) * 12 + (now.month - dt.month)


def age_display(months):
    if months <= 0: return "0yr 0mo"
    return f"{months // 12}yr {months % 12}mo"


def format_currency(n):
    if n == int(n): return f"${int(n):,}"
    return f"${n:,.2f}"


def format_compact(n):
    """Format large numbers compactly: $45K, $1.5K, $100K."""
    if n >= 100000: return f"${n/1000:.0f}K"
    if n >= 1000:
        if n % 1000 == 0: return f"${int(n/1000)}K"
        return f"${n/1000:.1f}".rstrip('0').rstrip('.') + "K"
    return format_currency(n)


def safe_filename(name):
    sanitized = _RE_UNSAFE_FILENAME.sub('', str(name)).strip()
    return sanitized if sanitized else "Client"


def format_opened(dt):
    if not dt: return "\u2014"
    return f"{MONTHS_SHORT[dt.month - 1]} {dt.year}"


def is_account_derogatory(a):
    if a.get("is_derogatory"): return True
    if a.get("is_chargeoff") or a.get("is_collection") or a.get("is_repo") or a.get("is_bankruptcy"): return True
    if (a.get("late_days") or 0) >= 30: return True
    status = str(a.get("status", "")).lower()
    for kw in ["charge-off", "charge off", "chargeoff", "collection", "past due", "delinquent", "repossession", "foreclosure"]:
        if kw in status: return True
    pstatus = str(a.get("payment_status", "")).lower()
    for kw in ["charge", "collection", "past due", "delinquent"]:
        if kw in pstatus: return True
    return False


def parse_open_closed(a):
    status = str(a.get("status", "")).lower()
    acct_status = str(a.get("account_status", "")).lower()
    for s in [status, acct_status]:
        if "open" in s: return "Open"
        if "closed" in s or "paid" in s or "transfer" in s: return "Closed"
    return "\u2014"


def partition_accounts(raw_accounts):
    """Single-pass partition into (derog, clean). Replaces 8 redundant filter passes."""
    derog, clean = [], []
    for a in raw_accounts:
        (derog if is_account_derogatory(a) else clean).append(a)
    return derog, clean


def score_tier(score):
    """Data classification tier. Used by parsers and internal logic."""
    if score >= 800: return "Exceptional"
    if score >= 740: return "Very Good"
    if score >= 670: return "Good"
    if score >= 580: return "Fair"
    return "Poor"


def friendly_tier(score):
    """User-facing display tier with range labels. Used in strategy HTML projections."""
    if score >= 800: return "Exceptional"
    if score >= 740: return "Very Good"
    if score >= 670: return "Good \u2014 Very Good"
    if score >= 620: return "Fair \u2014 Good"
    if score >= 580: return "Fair"
    return "Below Average"
