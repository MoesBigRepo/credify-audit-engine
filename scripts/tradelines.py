#!/usr/bin/env python3
"""
tradeline_engine.py — Fetch live tradeline inventory and find optimal 2-AU combos.

Public API:
    fetch_tradelines(min_limit=10000, timeout=10) -> list[dict]
    find_best_combo(tradelines, existing_accounts, target_months=120, min_months=84) -> dict | None
"""

import concurrent.futures
import itertools
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime

MAX_RESPONSE_SIZE = 5 * 1024 * 1024  # 5 MB per vendor response
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "credify", "tradelines")
CACHE_TTL = 300  # seconds — 5 min; prevents re-fetch during multi-file audit sessions

API_BASE = "https://tradeline-api.moe-marketing93.workers.dev"
ENDPOINTS = {
    "supply": f"{API_BASE}/api/supply",
    "genie": f"{API_BASE}/api/genie",
    "boost": f"{API_BASE}/api/boost",
    "gfs": f"{API_BASE}/api/gfs",
}
XLSX_FALLBACK = os.path.expanduser("~/Documents/Tradeline Marketplace LIVE.xlsx")
MONTHS_LIST = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]

BANK_MAP = {
    "chase": "Chase", "barclays": "Barclays", "barclay": "Barclays",
    "discover": "Discover", "citi": "Citi", "citibank": "Citi",
    "capital one": "Capital One", "cap one": "Capital One", "cp1": "Capital One",
    "american express": "American Express", "amex": "American Express",
    "wells fargo": "Wells Fargo", "wf": "Wells Fargo",
    "bank of america": "Bank of America", "bank of america bofa": "Bank of America",
    "boa": "Bank of America", "bofa": "Bank of America",
    "td": "TD Bank", "td bank": "TD Bank",
    "nfcu": "Navy FCU", "navy fcu": "Navy FCU", "nasa fcu": "NASA FCU",
    "elan": "Elan", "elan financial": "Elan",
    "usaa": "USAA", "usbank": "US Bank", "us bank": "US Bank",
    "pnc": "PNC", "truist": "Truist", "fnbo": "FNBO", "sofi": "SoFi",
    "goldman": "Goldman Sachs", "alliant cu": "Alliant CU",
    "synchrony": "Synchrony", "fidelity": "Fidelity",
    "macy's amex": "Macy's Amex", "citi best buy": "Citi Best Buy",
}


# ── Helpers ──────────────────────────────────────────────────────────────────

def _standardize_bank(name):
    """Normalize bank name via lookup table, title-case fallback."""
    if not name:
        return "Unknown"
    low = str(name).strip().lower()
    if low in BANK_MAP:
        return BANK_MAP[low]
    return str(name).strip().title()


def _is_amex(bank):
    """Check if bank is American Express (AU age not reported)."""
    low = str(bank).lower()
    return low == "american express" or low == "amex" or "amex" in low


def _parse_price(s):
    """Parse price string like '$750' to float."""
    try:
        return float(re.sub(r'[^\d.]', '', str(s)))
    except (ValueError, TypeError):
        return 0.0


def _parse_limit(s):
    """Parse limit string like '$10,000' or '4.5K' to float."""
    try:
        s = str(s).strip().replace('$', '').replace(',', '')
        if s.upper().endswith('K'):
            return float(s[:-1]) * 1000
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _age_display(months):
    """Format months as 'Xyr Ymo' string."""
    if months <= 0:
        return "0yr 0mo"
    return f"{months // 12}yr {months % 12}mo"


def _compute_age_supply(date_str, now):
    """Compute age in months from Supply format 'YYYY Mon' (e.g., '2019 Apr'). Returns (age_months, opened_year)."""
    try:
        parts = str(date_str).strip().split()
        yr = int(parts[0])
        mo = MONTHS_LIST.index(parts[1].lower()[:3])
        return (now.year - yr) * 12 + (now.month - 1 - mo), yr
    except (ValueError, IndexError):
        return 0, 0


def _compute_age_genie(year_str, now):
    """Compute age in months from Genie year-opened (e.g., '2021'). Returns (age_months, opened_year)."""
    try:
        yr = int(year_str)
        if yr < 1950 or yr > now.year:
            return 0, 0
        return (now.year - yr) * 12 + (now.month - 1), yr
    except (ValueError, TypeError):
        return 0, 0


def _compute_age_boost(age_float_str):
    """Compute age in months from Boost pre-computed float (e.g., '21.2' = 21yr 2mo)."""
    try:
        val = float(age_float_str)
        years = int(val)
        months_part = int(round((val - years) * 10))
        if months_part > 11:
            months_part = 11
        return years * 12 + months_part
    except (ValueError, TypeError):
        return 0


def _compute_age_gfs(age_str):
    """Compute age in months from GFS 'N years' string."""
    match = re.search(r'(\d+)\s*year', str(age_str), re.I)
    if match:
        return int(match.group(1)) * 12
    return 0


# ── Normalizers ──────────────────────────────────────────────────────────────

def _normalize_supply(item, now):
    """Normalize a Supply API row: [bank, cardId, limit, dateOpened, purchaseBy, reportRange, stock, price]."""
    try:
        bank = _standardize_bank(item[0])
        limit = _parse_limit(item[2])
        age_months, opened_year = (0, 0) if _is_amex(bank) else _compute_age_supply(item[3], now)
        price = _parse_price(item[7])
        spots = int(item[6]) if item[6] else 0
        return {
            "bank": bank, "limit": limit, "age_months": age_months,
            "age_years": age_months // 12, "opened_year": opened_year,
            "age_display": _age_display(age_months), "price": price,
            "spots": spots, "vendor": "Supply",
            "statement_date": str(item[5]) if len(item) > 5 else "",
        }
    except (IndexError, TypeError):
        return None


def _normalize_genie(item, now):
    """Normalize a Genie API row: [spots, yearOpened, bank, limit, stmtDay, price, cardId]."""
    try:
        bank = _standardize_bank(item[2])
        limit = _parse_limit(item[3])
        age_months, opened_year = (0, 0) if _is_amex(bank) else _compute_age_genie(item[1], now)
        price = _parse_price(item[5])
        spots = int(item[0]) if item[0] else 0
        return {
            "bank": bank, "limit": limit, "age_months": age_months,
            "age_years": age_months // 12, "opened_year": opened_year,
            "age_display": _age_display(age_months), "price": price,
            "spots": spots, "vendor": "Genie",
            "statement_date": str(item[4]) if len(item) > 4 else "",
        }
    except (IndexError, TypeError):
        return None


def _normalize_boost(item, now):
    """Normalize a Boost API row: [lender, limit, age, spots, stmtDay, postDate, price]."""
    try:
        bank = _standardize_bank(item[0])
        limit = _parse_limit(item[1])
        age_months = 0 if _is_amex(bank) else _compute_age_boost(item[2])
        opened_year = now.year - (age_months // 12) if age_months > 0 else 0
        price = _parse_price(item[6])
        spots = int(item[3]) if item[3] else 0
        return {
            "bank": bank, "limit": limit, "age_months": age_months,
            "age_years": age_months // 12, "opened_year": opened_year,
            "age_display": _age_display(age_months), "price": price,
            "spots": spots, "vendor": "Boost",
            "statement_date": str(item[4]) if len(item) > 4 else "",
        }
    except (IndexError, TypeError):
        return None


def _normalize_gfs(item, now):
    """Normalize a GFS API row: [lender, limit, age, price, postDates, purchaseBy, stmtDate, tradelineId]."""
    try:
        bank = _standardize_bank(item[0])
        limit = _parse_limit(item[1])
        age_months = 0 if _is_amex(bank) else _compute_age_gfs(item[2])
        opened_year = now.year - (age_months // 12) if age_months > 0 else 0
        price = _parse_price(item[3])
        return {
            "bank": bank, "limit": limit, "age_months": age_months,
            "age_years": age_months // 12, "opened_year": opened_year,
            "age_display": _age_display(age_months), "price": price,
            "spots": None, "vendor": "GFS",
            "statement_date": str(item[6]) if len(item) > 6 else "",
        }
    except (IndexError, TypeError):
        return None


NORMALIZERS = {
    "supply": _normalize_supply,
    "genie": _normalize_genie,
    "boost": _normalize_boost,
    "gfs": _normalize_gfs,
}


# ── Fetching ─────────────────────────────────────────────────────────────────

def _cache_path(vendor):
    return os.path.join(CACHE_DIR, f"{vendor}.json")


def _cache_read(vendor):
    """Return cached JSON bytes if fresh, else None."""
    p = _cache_path(vendor)
    try:
        if os.path.exists(p) and (datetime.now().timestamp() - os.path.getmtime(p)) < CACHE_TTL:
            with open(p, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


def _cache_write(vendor, raw_bytes):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        path = _cache_path(vendor)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(raw_bytes)
    except Exception:
        pass


def _fetch_vendor(vendor, timeout):
    """Fetch and normalize one vendor's tradeline data. Uses 60s disk cache."""
    normalize = NORMALIZERS[vendor]
    results = []
    now = datetime.now()

    # Try cache first
    cached = _cache_read(vendor)
    if cached:
        raw = cached
    else:
        url = ENDPOINTS[vendor]
        ssl_ctx = ssl.create_default_context()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "CreditStrategy/1.0"})
            with urllib.request.urlopen(req, timeout=timeout, context=ssl_ctx) as resp:
                raw = resp.read(MAX_RESPONSE_SIZE + 1)
                if len(raw) > MAX_RESPONSE_SIZE:
                    print(f"  [tradeline_engine] {vendor} response too large, skipping", file=sys.stderr)
                    return results
            _cache_write(vendor, raw)
        except ssl.SSLError as e:
            print(f"  [tradelines] {vendor} SSL ERROR: {e}", file=sys.stderr)
            return results
        except urllib.error.URLError as e:
            print(f"  [tradelines] {vendor} network error: {e}", file=sys.stderr)
            return results
        except Exception as e:
            print(f"  [tradelines] {vendor} unexpected: {type(e).__name__}: {e}", file=sys.stderr)
            return results

    try:
        data = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except Exception:
        return results
    if not isinstance(data, dict):
        return results
    accounts = data.get("accounts", [])
    if not isinstance(accounts, list):
        return results
    for item in accounts:
        try:
            row = normalize(item, now)
            if row:
                results.append(row)
        except Exception:
            continue
    return results


def fetch_tradelines(min_limit=10000, timeout=5):
    """
    Fetch tradelines from all 4 vendor APIs in parallel.
    Deduplicates keeping cheapest. Filters by min_limit and availability.
    Falls back to local xlsx if all APIs fail.
    """
    all_rows = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(_fetch_vendor, v, timeout): v for v in ENDPOINTS}
        for future in concurrent.futures.as_completed(futures, timeout=timeout + 5):
            vendor = futures[future]
            try:
                rows = future.result()
                all_rows.extend(rows)
            except Exception:
                print(f"  [tradeline_engine] {vendor} error", file=sys.stderr)

    if not all_rows:
        print("  [tradeline_engine] All APIs failed, trying xlsx fallback...", file=sys.stderr)
        all_rows = _try_xlsx_fallback()

    # Dedup: group by (bank, limit, age_months), keep cheapest
    seen = {}
    for row in all_rows:
        key = (row["bank"], int(row["limit"]), row["age_months"])
        if key not in seen or row["price"] < seen[key]["price"]:
            seen[key] = row
    deduped = list(seen.values())

    # Filter: min limit, has spots, positive age
    filtered = [
        r for r in deduped
        if r["limit"] >= min_limit
        and (r["spots"] is None or r["spots"] > 0)
        and r["age_months"] > 0
        and r["price"] > 0
    ]

    filtered.sort(key=lambda r: r["price"])
    return filtered


def _try_xlsx_fallback():
    """Try loading tradelines from local xlsx file."""
    if not os.path.exists(XLSX_FALLBACK):
        return []
    try:
        import openpyxl
        wb = openpyxl.load_workbook(XLSX_FALLBACK, read_only=True)
        ws = wb.active
        results = []
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not row or len(row) < 4:
                continue
            try:
                bank = _standardize_bank(str(row[0]))
                limit = _parse_limit(str(row[1]))
                age_months = _compute_age_gfs(str(row[2]))
                if _is_amex(bank):
                    age_months = 0
                price = _parse_price(str(row[3]))
                results.append({
                    "bank": bank, "limit": limit, "age_months": age_months,
                    "age_display": _age_display(age_months), "price": price,
                    "spots": None, "vendor": "XLSX",
                    "statement_date": "",
                })
            except Exception:
                continue
        wb.close()
        return results
    except ImportError:
        print("  [tradeline_engine] openpyxl not installed, xlsx fallback unavailable", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  [tradeline_engine] xlsx fallback failed: {e}", file=sys.stderr)
        return []


# ── Combo Calculator ─────────────────────────────────────────────────────────

def _parse_opened_date(opened_str):
    """Parse opened date string to datetime. Handles MM/YYYY, MM/DD/YYYY, YYYY."""
    if not opened_str:
        return None
    s = str(opened_str).strip()
    for fmt in ("%m/%Y", "%m/%d/%Y", "%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def find_best_combo(tradelines, existing_accounts, target_months=120, min_months=84):
    """
    Find the cheapest 2-AU tradeline combo that brings post-deletion AAOA
    closest to target (120mo = 10yr), with minimum acceptable (84mo = 7yr).

    Returns dict with pick1, pick2, combo_price, new_aaoa_months, etc.
    Returns None if fewer than 2 tradelines available.
    """
    if len(tradelines) < 2:
        return None

    now = datetime.now()

    # Filter existing accounts: remove derogatory
    clean = [a for a in existing_accounts if not a.get("is_derogatory", False)]

    # Compute ages
    account_ages = []
    for acct in clean:
        dt = _parse_opened_date(acct.get("opened"))
        if dt:
            months = (now.year - dt.year) * 12 + (now.month - dt.month)
            if months > 0:
                account_ages.append(months)

    post_del_total = sum(account_ages)
    post_del_count = len(account_ages)
    post_del_aaoa = post_del_total / post_del_count if post_del_count > 0 else 0

    # Single-pass: find cheapest combo meeting minimum, O(1) memory
    best_key = None
    best_data = None
    for a, b in itertools.combinations(tradelines, 2):
        new_total = post_del_total + a["age_months"] + b["age_months"]
        new_count = post_del_count + 2
        new_avg = new_total / new_count
        combo_price = a["price"] + b["price"]
        meets_min = new_avg >= min_months
        key = (not meets_min, combo_price, -new_avg)
        if best_key is None or key < best_key:
            best_key = key
            best_data = (a, b, new_avg, new_count, combo_price)

    if best_data is None:
        return None

    pick1, pick2, new_avg, new_count, combo_price = best_data

    return {
        "pick1": pick1,
        "pick2": pick2,
        "combo_price": combo_price,
        "new_aaoa_months": int(new_avg),
        "new_aaoa_display": _age_display(int(new_avg)),
        "target_met": new_avg >= min_months,
        "shortfall_months": max(0, int(min_months - new_avg)),
        "post_deletion_aaoa_months": int(post_del_aaoa),
        "post_deletion_aaoa_display": _age_display(int(post_del_aaoa)),
        "existing_count": post_del_count,
        "new_count": new_count,
        "new_total_limit": pick1["limit"] + pick2["limit"],
    }
