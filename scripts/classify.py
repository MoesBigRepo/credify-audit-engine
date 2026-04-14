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


# ── Cross-bureau dedup (defense-in-depth for 3-bureau parsers) ──────────────
#
# MyFreeScoreNow has its own parser-level merge for continuation blocks
# (see parsers/myfreescorenow.py), but future 3-bureau parsers (SmartCredit,
# MyFICO, AnnualCreditReport) may emit separate rows for the same account
# under bureau-specific creditor names. This layer catches those cases
# provider-agnostically without disturbing single-bureau parsers.

CREDITOR_ALIASES = {
    # Navy Federal
    "navy federal": "navy federal",
    "navy fcu": "navy federal",
    "nfcu": "navy federal",
    "navy federal cr union": "navy federal",
    "navy federal credit union": "navy federal",
    # Capital One
    "capital one": "capital one",
    "cap one": "capital one",
    "cp1": "capital one",
    "capital one bank usa": "capital one",
    "capital one bank usa na": "capital one",
    "capital one na": "capital one",
    # Synchrony
    "synchrony": "synchrony",
    "syncb": "synchrony",
    "synchrony bank": "synchrony",
    # Chase / JPMorgan
    "chase": "chase",
    "jpmcb": "chase",
    "jpmcb card": "chase",
    "jpmcb card services": "chase",
    "jpmorgan chase": "chase",
    "jpmorgan chase bank": "chase",
    # Discover
    "discover": "discover",
    "discover fin": "discover",
    "discover financial": "discover",
    "discover bank": "discover",
    # American Express
    "amex": "amex",
    "american express": "amex",
    # Citi
    "citi": "citi",
    "citibank": "citi",
    "citicards": "citi",
    "citicards cbna": "citi",
    # Bank of America
    "bank of america": "bank of america",
    "boa": "bank of america",
    "bofa": "bank of america",
    "bk of amer": "bank of america",
    # Wells Fargo
    "wells fargo": "wells fargo",
    "wf": "wells fargo",
    "wells fargo bank": "wells fargo",
}

_CREDITOR_STRIP_SUFFIXES = (
    "credit union",
    "cr union",
    "national association",
    "bank usa na",
    "bank na",
    "bank",
    "llc",
    "inc",
    "corp",
    "corporation",
    "n.a.",
    "na",
    "fcu",
    "f.c.u.",
)

_RE_CREDITOR_WS = re.compile(r"\s+")
_RE_CREDITOR_NON_ALNUM = re.compile(r"[^a-z0-9\s/]")


def canonicalize_creditor(name):
    """Map a bureau-reported creditor string to a canonical form.

    Lookup order: exact alias match -> suffix-stripped alias match ->
    first-two-token fallback. Used as the creditor component of the
    cross-bureau dedup merge key.
    """
    if not name:
        return ""
    low = _RE_CREDITOR_NON_ALNUM.sub(" ", str(name).lower())
    low = _RE_CREDITOR_WS.sub(" ", low).strip()
    if low in CREDITOR_ALIASES:
        return CREDITOR_ALIASES[low]
    stripped = low
    # Repeatedly strip known trailing suffixes
    for _ in range(3):
        changed = False
        for suf in _CREDITOR_STRIP_SUFFIXES:
            if stripped.endswith(" " + suf):
                stripped = stripped[: -(len(suf) + 1)].strip()
                changed = True
                break
            if stripped == suf:
                stripped = ""
                changed = True
                break
        if not changed:
            break
    if stripped in CREDITOR_ALIASES:
        return CREDITOR_ALIASES[stripped]
    # Fallback: first two tokens of the stripped form
    tokens = stripped.split()
    if not tokens:
        return low
    return " ".join(tokens[:2])


_ALL_BUREAUS = frozenset({"TU", "EXP", "EQF"})


def _opened_match(a, b):
    """Opened-date match within ±1 month, year-only fallback."""
    oa = parse_opened(a.get("opened", ""))
    ob = parse_opened(b.get("opened", ""))
    if oa and ob:
        diff = abs((oa.year - ob.year) * 12 + (oa.month - ob.month))
        return diff <= 1
    # Year-only fallback
    ya = str(a.get("opened", "")).strip()[-4:]
    yb = str(b.get("opened", "")).strip()[-4:]
    if ya.isdigit() and yb.isdigit():
        return ya == yb
    return False


def _is_dedup_pair(a, b):
    """Provider-agnostic merge predicate."""
    # Canonical creditor match
    ca = canonicalize_creditor(a.get("creditor", ""))
    cb = canonicalize_creditor(b.get("creditor", ""))
    if not ca or not cb or ca != cb:
        return False

    # Responsibility compat — don't merge AU with primary
    if bool(a.get("is_au")) != bool(b.get("is_au")):
        return False

    # Balance match (within $1 when > 0; both-$0 requires same type+date)
    ba = parse_balance(a.get("balance", "0"))
    bb = parse_balance(b.get("balance", "0"))
    if ba > 0 or bb > 0:
        if abs(ba - bb) > 1:
            return False
    else:
        at = str(a.get("account_type_detail", a.get("type", ""))).strip().lower()
        bt = str(b.get("account_type_detail", b.get("type", ""))).strip().lower()
        if not at or at != bt:
            return False

    # Account type must match
    ta = str(a.get("type", "")).strip().lower()
    tb = str(b.get("type", "")).strip().lower()
    if not ta or ta != tb:
        return False

    # Opened-date match
    if not _opened_match(a, b):
        return False

    # Bureau coverage: disjoint (complementary) OR identical.
    # Reject ambiguous overlap like [TU] vs [TU, EXP].
    sa = set(a.get("bureaus", []))
    sb = set(b.get("bureaus", []))
    if sa and sb:
        if sa != sb and (sa & sb):
            return False

    return True


def _merge_dedup_pair(a, b):
    """Union-merge two records. Immutable: returns new dict."""
    _BLANK = ("", "--", "\u2014", None, [])
    _BUREAU_ORDER = ("TU", "EXP", "EQF")

    def _richness(r):
        return sum(1 for v in r.values() if v not in _BLANK and v != 0 and v != "0")

    base = a if _richness(a) >= _richness(b) else b
    other = b if base is a else a

    result = dict(base)
    for k, v in other.items():
        if result.get(k) in _BLANK and v not in _BLANK:
            result[k] = v

    # Bureaus: canonical-ordered union
    result["bureaus"] = sorted(
        set(a.get("bureaus", [])) | set(b.get("bureaus", [])),
        key=lambda x: _BUREAU_ORDER.index(x) if x in _BUREAU_ORDER else 99,
    )

    # Late days: max
    la = a.get("late_days", 0) or 0
    lb = b.get("late_days", 0) or 0
    result["late_days"] = max(la, lb)

    # Booleans: OR
    for flag in ("is_chargeoff", "is_collection", "is_repo",
                 "is_bankruptcy", "is_derogatory", "is_au"):
        result[flag] = bool(a.get(flag)) or bool(b.get(flag))

    # Payment status aligned with severity
    if lb > la:
        result["payment_status"] = b.get("payment_status") or a.get("payment_status", "")
    else:
        result["payment_status"] = a.get("payment_status") or b.get("payment_status", "")

    # Preserve original_creditor from whichever had one
    oc_a = str(a.get("original_creditor", "")).strip()
    oc_b = str(b.get("original_creditor", "")).strip()
    result["original_creditor"] = oc_a or oc_b

    # Account number: prefer longer string
    an_a = str(a.get("account_number", ""))
    an_b = str(b.get("account_number", ""))
    result["account_number"] = an_a if len(an_a) >= len(an_b) else an_b

    # Re-evaluate is_derogatory with merged fields
    result["is_derogatory"] = is_account_derogatory(result)

    return result


def dedup_accounts(raw_accounts):
    """Collapse same-tradeline records emitted under different bureau names.

    Returns a new list; never mutates input. Idempotent.
    """
    if not raw_accounts or len(raw_accounts) < 2:
        return list(raw_accounts or [])

    # Fast path: if every record already has full 3-bureau coverage, nothing to do
    if all(frozenset(a.get("bureaus", [])) == _ALL_BUREAUS for a in raw_accounts):
        return list(raw_accounts)

    out = []
    consumed = set()
    for i, a in enumerate(raw_accounts):
        if i in consumed:
            continue
        merged = a
        for j in range(i + 1, len(raw_accounts)):
            if j in consumed:
                continue
            if _is_dedup_pair(merged, raw_accounts[j]):
                merged = _merge_dedup_pair(merged, raw_accounts[j])
                consumed.add(j)
        out.append(merged)
    return out


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
