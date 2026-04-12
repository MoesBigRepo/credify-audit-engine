"""Credify strategy builder — score projections, plan steps, comparisons, context assembly.
All functions import from classify.py — zero private copies."""
from datetime import datetime
from markupsafe import Markup
from classify import (
    parse_balance, parse_opened, months_between, age_display,
    format_currency, format_compact, format_opened, is_account_derogatory,
    parse_open_closed, resolve_client_name, resolve_client_last, friendly_tier,
)


def _pick_primary_per_bureau(raw_scores):
    """Select one primary score per bureau. Prefer FICO Score 8, then first available."""
    by_bureau = {}
    for s in raw_scores:
        bureau = s.get("bureau", "Unknown")
        model = s.get("model", "")
        if bureau not in by_bureau:
            by_bureau[bureau] = s
        elif "score 8" in model.lower() and "score 8" not in by_bureau[bureau].get("model", "").lower():
            by_bureau[bureau] = s
    return list(by_bureau.values())


def build_scores(data):
    """Build score display list — one primary score per bureau, with color coding."""
    primary = _pick_primary_per_bureau(data.get("scores", []))
    scores, min_score = [], 999
    for s in primary:
        val = s.get("value", "N/A")
        tier = s.get("tier", "")
        color = "accent-red"
        if isinstance(val, (int, float)):
            if val < 580: color = "accent-red"
            elif val < 670: color = "accent-amber"
            else: color = "accent-green"
            if val < min_score: min_score = val
        scores.append({"bureau": s.get("bureau", ""), "value": val, "tier": tier, "color": color})
    return scores, min_score if min_score < 999 else 0


def build_negative_groups(derog):
    """Takes pre-partitioned derog list. Do NOT re-filter raw_accounts here."""
    groups = {"Charge-Offs": [], "Collections": [], "Severely Delinquent": [], "Late Payments": [], "Other": []}
    total_debt = 0.0
    for a in derog:
        balance = parse_balance(a.get("balance", "0"))
        total_debt += balance
        badges = []
        if a.get("is_chargeoff"): badges.append({"color": "red", "label": "Charge-Off"})
        if a.get("is_collection"): badges.append({"color": "red", "label": "Collection"})
        ld = a.get("late_days", 0) or 0
        if ld >= 120 and not a.get("is_chargeoff"): badges.append({"color": "red", "label": f"{ld}+ Days Late"})
        elif ld >= 60 and not a.get("is_chargeoff"): badges.append({"color": "amber", "label": f"{ld} Days Late"})
        elif ld >= 30 and not a.get("is_chargeoff"): badges.append({"color": "amber", "label": "30 Days Late"})
        cred = str(a.get("creditor", "")).lower()
        atype = str(a.get("type", "")).lower()
        if any(k in cred or k in atype for k in ["affirm", "bnpl", "self ", "kikoff"]):
            badges.append({"color": "purple", "label": "BNPL"})
        details = a.get("details", "")
        reason = details.split(".")[0] if details else a.get("status", "")
        row = {"name": a.get("creditor", "Unknown"), "badges": badges, "reason": reason, "balance": format_currency(balance)}
        if a.get("is_chargeoff"): groups["Charge-Offs"].append(row)
        elif a.get("is_collection"): groups["Collections"].append(row)
        elif ld >= 120: groups["Severely Delinquent"].append(row)
        elif ld > 0: groups["Late Payments"].append(row)
        else: groups["Other"].append(row)
    result = [{"label": k, "accounts": v} for k, v in groups.items() if v]
    return {"negative_groups": result, "negative_count": len(derog), "total_negative_debt": format_currency(total_debt), "total_negative_debt_raw": total_debt}


def _is_closed_revolving_clean(a):
    """Closed revolving accounts with perfect history carry no FICO weight.
    They don't provide age or utilization. Skip them entirely unless derogatory."""
    open_closed = parse_open_closed(a)
    if open_closed != "Closed":
        return False
    acct_type = str(a.get("type", "")).lower()
    is_revolving = any(k in acct_type for k in ["revolving", "credit card", "store card", "charge card"])
    if not is_revolving:
        return False
    # If it has late payments or negative marks, it's NOT clean — show it for deletion
    if (a.get("late_days") or 0) > 0:
        return False
    return True


def build_keeper_accounts(clean, now):
    """Takes pre-partitioned clean list. Do NOT re-filter raw_accounts here.
    Excludes closed revolving accounts with clean history — they carry no FICO weight."""
    keepers = []
    for a in clean:
        if _is_closed_revolving_clean(a):
            continue
        name = a.get("creditor", "Unknown")
        dt = parse_opened(a.get("opened"))
        mo = months_between(dt, now) if dt else 0
        balance = parse_balance(a.get("balance", "0"))
        limit = parse_balance(a.get("credit_limit", "0"))
        high_credit = parse_balance(a.get("high_credit", "0"))
        open_closed = parse_open_closed(a)
        bureaus = a.get("bureaus", [])
        bureaus_str = ", ".join(bureaus) if isinstance(bureaus, list) and bureaus else "\u2014"
        is_au = a.get("is_au", False)
        responsibility = "Authorized User" if is_au else a.get("responsibility", "Individual")
        acct_type = a.get("type", "")
        is_revolving = any(k in acct_type.lower() for k in ["revolving", "credit card", "store card", "charge card"])
        amount_label = "Credit Limit" if is_revolving else "Original Loan"
        amount_value = format_currency(limit) if is_revolving and limit > 0 else (format_currency(high_credit) if high_credit > 0 else "\u2014")
        keepers.append({
            "name": name, "type": acct_type, "opened": format_opened(dt),
            "date_opened": a.get("opened", "\u2014"), "age": age_display(mo),
            "age_months": mo, "age_color": "green" if mo >= 60 else "blue",
            "open_closed": open_closed, "balance": format_currency(balance) if balance > 0 else "$0",
            "credit_limit": limit, "credit_limit_display": format_currency(limit) if limit > 0 else "\u2014",
            "status": a.get("status", ""), "account_type_detail": a.get("account_type_detail", acct_type),
            "responsibility": responsibility, "date_closed": a.get("date_closed", ""),
            "last_activity": a.get("last_activity", "\u2014"), "last_reported": a.get("last_reported", "\u2014"),
            "high_credit": format_currency(high_credit) if high_credit > 0 else "\u2014",
            "amount_label": amount_label, "amount_value": amount_value,
            "monthly_payment": a.get("monthly_payment", "\u2014"),
            "payment_status": a.get("payment_status", a.get("status", "\u2014")),
            "bureaus": bureaus_str,
        })
    keepers.sort(key=lambda x: x["age_months"], reverse=True)
    return keepers


def is_aaoa_eligible(a):
    """Determine if account contributes to AAOA.
    - Open revolving: YES (credit cards, lines of credit)
    - Closed revolving: NO (dead weight, no FICO value)
    - Open installment: YES
    - Closed installment: YES (it is the nature of installment accounts to close — they still confer age)
    """
    acct_type = str(a.get("type", "")).lower() + " " + str(a.get("account_type_detail", "")).lower()
    is_revolving = any(k in acct_type for k in ["credit card", "revolving", "charge", "store card"])
    is_installment = any(k in acct_type for k in ["auto", "loan", "installment", "mortgage", "lease"])
    if not is_revolving and not is_installment:
        return False
    # Closed installment accounts still confer age — only closed revolving is excluded
    oc = a.get("open_closed", "")
    if not oc: oc = parse_open_closed(a)
    if oc == "Closed" and is_revolving:
        return False
    return True


def calc_aaoa(accounts, now):
    ages = []
    for a in accounts:
        if not is_aaoa_eligible(a): continue
        dt = parse_opened(a.get("opened"))
        if dt:
            m = (now.year - dt.year) * 12 + (now.month - dt.month)
            if m > 0: ages.append(m)
    return sum(ages) / len(ages) if ages else 0


def build_stats(neg_count, total_debt_raw, clean, keepers, now):
    """Takes pre-partitioned clean list. Do NOT re-filter raw_accounts here."""
    avg_age = calc_aaoa(clean, now)
    max_util = "N/A"
    for a in clean:
        bal = parse_balance(a.get("balance", "0"))
        lim = parse_balance(a.get("credit_limit", "0"))
        if lim > 0:
            pct = int(bal / lim * 100)
            if max_util == "N/A" or pct > int(max_util.rstrip('%+')):
                max_util = f"{pct}%"
    debt_display = f"${total_debt_raw/1000:.1f}K" if total_debt_raw >= 1000 else format_currency(total_debt_raw)
    return {
        "negative_accounts": neg_count, "negative_accounts_color": "accent-red",
        "total_debt_display": debt_display, "total_debt_color": "accent-red",
        "utilization": max_util, "utilization_color": "accent-red",
        "avg_account_age": age_display(int(avg_age)), "age_color": "accent-amber" if avg_age > 36 else "accent-red",
    }


def build_plan_steps(neg_count, keeper_count, has_tradelines):
    steps = [
        {"label": "Step 1", "text": f"Remove {neg_count} negative accounts", "color": "accent-red"},
        {"label": "Step 2", "text": f"Keep {keeper_count} clean accounts", "color": "accent-blue"},
    ]
    if has_tradelines:
        steps.append({"label": "Step 3", "text": "Add 2 strong tradelines", "color": "accent-green"})
        steps.append({"label": "Result", "text": f"{keeper_count + 2}-account clean profile", "color": "accent-green"})
    else:
        steps.append({"label": "Step 3", "text": "Optimize utilization", "color": "accent-green"})
        steps.append({"label": "Result", "text": f"{keeper_count}-account clean profile", "color": "accent-green"})
    return steps


def build_tradeline_cards(combo):
    if not combo: return None
    cards = []
    for pick in [combo["pick1"], combo["pick2"]]:
        yr = pick.get("opened_year", 0)
        cards.append({
            "display_name": f"{yr} {pick['bank']}" if yr else pick["bank"],
            "issuer_description": f"{pick['bank']} \u2014 Authorized User",
            "credit_limit": format_currency(pick["limit"]),
            "account_age": f"{pick['age_months'] // 12} years",
            "opened_year": str(yr) if yr else "\u2014",
            "payment_history": "Perfect",
            "price": format_currency(pick["price"]),
        })
    return cards


def build_projected(combo, keepers, now):
    keeper_limit = sum(k.get("credit_limit", 0) for k in keepers)
    if combo:
        total_limit = keeper_limit + combo["new_total_limit"]
        return {"total_credit_limit": format_currency(total_limit) + "+", "avg_account_age": combo["new_aaoa_display"], "utilization": "<1%"}
    keeper_ages = [k["age_months"] for k in keepers if k.get("age_months", 0) > 0]
    avg = sum(keeper_ages) / len(keeper_ages) if keeper_ages else 0
    return {"total_credit_limit": format_currency(keeper_limit) if keeper_limit else "\u2014", "avg_account_age": age_display(int(avg)), "utilization": "<30%"}


def build_comparisons(stats, projected, neg_count, post_cleanup):
    return [
        {"label": "Negative Accounts", "before": str(neg_count), "after": "0"},
        {"label": "Utilization", "before": stats["utilization"], "after": projected["utilization"]},
        {"label": "Avg Account Age", "before": stats["avg_account_age"], "after": projected["avg_account_age"]},
        {"label": "Available Credit", "before": post_cleanup["available_credit"], "after": projected["total_credit_limit"]},
    ]


def build_timeline(neg_count, has_tradelines):
    tl = [
        {"time_range": "Days 1-7", "color": "accent-blue", "title": "Disputes filed.", "description": f"Dispute letters sent to all three bureaus covering {neg_count} accounts."},
        {"time_range": "Days 30-45", "color": "accent-blue", "title": "Removals confirmed.", "description": "Bureaus have 30 days to investigate. Most removals happen within this window."},
    ]
    if has_tradelines:
        tl.append({"time_range": "Days 45-60", "color": "accent-green", "title": "Tradelines added.", "description": "Once removals are confirmed, the two authorized user tradelines are added to your profile."})
        tl.append({"time_range": "Days 60-90", "color": "accent-green", "title": "Score update.", "description": "Tradelines report within 1-2 statement cycles. Your new score reflects across all bureaus."})
    else:
        tl.append({"time_range": "Days 45-60", "color": "accent-green", "title": "Score stabilizes.", "description": "Removed accounts stop dragging your score. Your profile reflects a clean history."})
        tl.append({"time_range": "Days 60-90", "color": "accent-green", "title": "New opportunities.", "description": "With a cleaner profile, apply for credit with better approval odds and rates."})
    return tl


def build_included_items(neg_count, tradeline_cards, clean):
    items = [
        "Full 3-bureau credit report analysis &amp; strategy document",
        f"{neg_count} accounts to be disputed and removed across TransUnion, Equifax &amp; Experian",
        "Bureau-by-bureau dispute targeting for maximum removal probability",
    ]
    if tradeline_cards:
        for tl in tradeline_cards:
            items.append(f"{tl['display_name']} tradeline ({tl['account_age']}, {tl['credit_limit']} limit) \u2014 {tl['price']}")
    has_util = any(parse_balance(a.get("balance","0")) / max(parse_balance(a.get("credit_limit","0")),1) > 0.5
                   for a in clean if parse_balance(a.get("credit_limit","0")) > 0)
    if has_util:
        items.append("Credit utilization optimization guidance")
    items.append("Ongoing monitoring &amp; follow-up through all dispute rounds")
    return items


def build_pricing(neg_count, discount=None, discount_note=None):
    credit_repair_price = 1000 if neg_count <= 5 else 1500
    lines = [{"description": f"Credit Repair ({neg_count} accounts)", "amount": format_currency(credit_repair_price), "is_discount": False}]
    total = credit_repair_price
    if discount:
        label = f"{discount_note} Discount*" if discount_note else "Discount*"
        lines.append({"description": label, "amount": f"-{format_currency(discount)}", "is_discount": True})
        total = max(0, credit_repair_price - discount)
    return {
        "lines": lines,
        "total": total,
        "total_display": format_currency(total),
        "subtitle": f"Full credit repair \u2014 {neg_count} account removals",
        "discount_note": discount_note if discount else None,
    }


# --- FICO Score 8 Calibration Constants (see fico_weights.md) ---
FICO = {
    "impact": {"chargeoff": 55, "collection": 45, "late120": 40, "late90": 30, "late60": 20, "late30": 12, "generic": 25},
    "age_decay": [(72, 0.10), (60, 0.20), (36, 0.40), (24, 0.60), (12, 0.80)],
    "diminishing": [1.0, 0.65, 0.45, 0.30, 0.20, 0.15, 0.10, 0.08],
    "diminishing_floor": 0.05,
    "scorecard_shift": {1: 15, 2: 20, 5: 30, "max": 45},
    "ceiling": 850, "damping_divisor": 200, "damping_min": 0.3,
    "thin_tradelines": 5, "thin_max_keepers": 7, "thin_max_age_mo": 24,
    "au_thin": 28, "au_lt5": 22, "au_lt10": 15, "au_thick": 7, "au_2nd": 0.55,
    "util_major": 28.9, "util_optimal": 8.9,
    "aaoa_plateau_mo": 90, "mature_mo": 36,
}


def _derog_age_months(a, now):
    """Compute age in months for a derogatory account from its 'opened' date.
    Falls back to explicit 'age_months' field, then 0 (treated as recent)."""
    explicit = a.get("age_months")
    if explicit is not None:
        return explicit
    opened = parse_opened(a.get("opened", ""))
    if opened:
        return months_between(opened, now)
    return 0


def calc_conservative(base_score, derog, now=None):
    """Delta-based conservative projection: start from base_score, add points per derog removed.
    Uses diminishing returns, age-based decay, scorecard-shift bonus, and ceiling damping.
    Grounded in FICO Score 8 empirical data (see fico_weights.md)."""
    if not derog:
        return base_score
    if now is None:
        now = datetime.now()

    imp = FICO["impact"]
    impact_table = []
    for a in derog:
        ld = a.get("late_days") or 0
        if a.get("is_chargeoff"):
            pts = imp["chargeoff"]
        elif a.get("is_collection"):
            pts = imp["collection"]
        elif ld >= 120:
            pts = imp["late120"]
        elif ld >= 90:
            pts = imp["late90"]
        elif ld >= 60:
            pts = imp["late60"]
        elif ld >= 30:
            pts = imp["late30"]
        else:
            pts = imp["generic"]

        # Age decay: older items have already aged out most of their penalty
        age_mo = _derog_age_months(a, now)
        for threshold, mult in FICO["age_decay"]:
            if age_mo > threshold:
                pts *= mult
                break

        impact_table.append(pts)

    impact_table.sort(reverse=True)

    total_gain = 0.0
    dim = FICO["diminishing"]
    for i, pts in enumerate(impact_table):
        mult = dim[i] if i < len(dim) else FICO["diminishing_floor"]
        total_gain += pts * mult

    # Scorecard-shift bonus
    shift = FICO["scorecard_shift"]
    n = len(derog)
    if n == 1:
        total_gain += shift[1]
    elif n == 2:
        total_gain += shift[2]
    elif n <= 5:
        total_gain += shift[5]
    else:
        total_gain += shift["max"]

    # Ceiling damping
    damping = min(max((FICO["ceiling"] - base_score) / FICO["damping_divisor"], FICO["damping_min"]), 1.0)
    total_gain *= damping

    return min(int(base_score + total_gain), FICO["ceiling"])


def calc_optimistic(base_score, keepers, combo, clean):
    """Delta-based optimistic projection: start from base_score, add points for
    profile improvements. FICO 8 rules: AU tradelines do NOT affect AAOA or utilization.
    AU benefit comes from payment history lines, account count, and marginal mix.
    Keepers should contain {type, age_months, credit_limit} dicts (scoring profiles)."""
    gain = 0.0

    keeper_count = len(keepers)
    total_count = keeper_count + (2 if combo else 0)
    if keeper_count < FICO["thin_tradelines"] and total_count >= FICO["thin_tradelines"]:
        gain += 15
    elif keeper_count < FICO["thin_tradelines"] and combo:
        gain += 8

    has_revolving = any(
        "revolv" in k.get("type", "").lower()
        or "credit card" in k.get("type", "").lower()
        or "charge" in k.get("type", "").lower()
        for k in keepers
    )
    has_installment = any(
        "loan" in k.get("type", "").lower()
        or "auto" in k.get("type", "").lower()
        or "installment" in k.get("type", "").lower()
        or "mortgage" in k.get("type", "").lower()
        for k in keepers
    )
    if combo and not has_revolving:
        gain += 8
    if not has_installment and not has_revolving:
        gain += 5

    keeper_ages = [k.get("age_months", 0) for k in keepers if k.get("age_months", 0) > 0]
    avg_keeper_age = sum(keeper_ages) / len(keeper_ages) if keeper_ages else 0
    capped_age = min(avg_keeper_age, FICO["aaoa_plateau_mo"])
    is_thin_file = keeper_count <= FICO["thin_max_keepers"] and capped_age < FICO["thin_max_age_mo"]

    max_keeper_age = max(keeper_ages) if keeper_ages else 0
    if max_keeper_age < FICO["mature_mo"] and combo and keeper_count < FICO["thin_tradelines"]:
        gain += 5

    if combo:
        if is_thin_file:
            au1_pts = FICO["au_thin"]
        elif keeper_count < FICO["thin_tradelines"]:
            au1_pts = FICO["au_lt5"]
        elif keeper_count < 10:
            au1_pts = FICO["au_lt10"]
        else:
            au1_pts = FICO["au_thick"]
        gain += au1_pts + (au1_pts * FICO["au_2nd"])

    keeper_limit = sum(k.get("credit_limit", 0) for k in keepers)
    total_balance = sum(parse_balance(a.get("balance", "0")) for a in clean)
    if keeper_limit > 0:
        current_util = total_balance / keeper_limit * 100
        if current_util > FICO["util_major"]:
            gain += 10
        elif current_util > FICO["util_optimal"]:
            gain += 5

    damping = min(max((FICO["ceiling"] - base_score) / FICO["damping_divisor"], FICO["damping_min"]), 1.0)
    gain *= damping

    return min(int(base_score + gain), FICO["ceiling"])


def build_score_projection(scores_list, derog, keepers, combo, clean, score_drop=None):
    now = datetime.now()
    numeric = [s["value"] for s in scores_list if isinstance(s["value"], (int, float))]
    base_score = min(numeric) if numeric else 500

    if score_drop:
        # Use empirical score drop as ground truth — more accurate than model estimate
        conservative = min(int(base_score + score_drop), FICO["ceiling"])
    else:
        conservative = calc_conservative(base_score, derog, now=now)

    # Extract lightweight scoring profiles — decouples from display DTOs
    keeper_profiles = [
        {"type": k.get("type", ""), "age_months": k.get("age_months", 0), "credit_limit": k.get("credit_limit", 0)}
        for k in keepers
    ]
    # When score_drop is known, optimistic gains build on the real recovered baseline
    optimistic_base = conservative if score_drop else base_score
    optimistic = calc_optimistic(optimistic_base, keeper_profiles, combo, clean)
    optimistic = max(optimistic, conservative)
    return conservative, optimistic, friendly_tier(optimistic)


def needs_tradelines(clean):
    """Takes pre-partitioned clean list. Returns (needs_bool, aaoa_months, big_revolving_count)."""
    now = datetime.now()
    aaoa_months = calc_aaoa(clean, now)
    big_revolving = 0
    for a in clean:
        if parse_open_closed(a) == "Closed": continue
        acct_type = str(a.get("type", "")).lower()
        is_revolving = any(k in acct_type for k in ["credit card", "revolving", "charge", "store card"])
        limit = parse_balance(a.get("credit_limit", "0"))
        if is_revolving and limit >= 10000: big_revolving += 1
    if aaoa_months / 12 >= 7 and big_revolving >= 2:
        return False, aaoa_months, big_revolving
    return True, aaoa_months, big_revolving


def build_context(data, derog, clean, combo, score_drop=None, discount=None, discount_note=None):
    """Build complete Jinja2 template context. Receives pre-partitioned account lists."""
    now = datetime.now()
    scores, score_low = build_scores(data)
    neg = build_negative_groups(derog)
    keepers = build_keeper_accounts(clean, now)
    tl_cards = build_tradeline_cards(combo)
    stats = build_stats(neg["negative_count"], neg["total_negative_debt_raw"], clean, keepers, now)
    plan = build_plan_steps(neg["negative_count"], len(keepers), tl_cards is not None)
    projected = build_projected(combo, keepers, now)
    score_cons, score_opt, score_tier_label = build_score_projection(scores, derog, keepers, combo, clean, score_drop=score_drop)
    keeper_limit = sum(k.get("credit_limit", 0) for k in keepers)
    post_cleanup = {
        "remaining_accounts": str(len(keepers)),
        "available_credit": format_currency(keeper_limit) if keeper_limit else "\u2014",
        "payment_history": "100%",
        "negative_marks": "0",
    }
    comparisons = build_comparisons(stats, projected, neg["negative_count"], post_cleanup)
    timeline = build_timeline(neg["negative_count"], tl_cards is not None)
    included = build_included_items(neg["negative_count"], tl_cards, clean)
    pricing = build_pricing(neg["negative_count"], discount=discount, discount_note=discount_note)
    client = data.get("client", {})
    return {
        "client": {"full_name": resolve_client_name(client), "last_name": resolve_client_last(client)},
        "score_current_low": score_low,
        "score_projected": score_cons, "score_conservative": score_cons,
        "score_optimistic": score_opt, "score_range": f"{score_cons} \u2013 {score_opt}",
        "score_projected_tier": score_tier_label,
        "score_drop": score_drop,
        "score_prior": (score_low + score_drop) if score_drop else None,
        "bureaus_display": Markup("TransUnion, Equifax &amp; Experian"),
        "score_model": data.get("score_model", "FICO Score 8"),
        "prepared_date": client.get("prepared_date", now.strftime("%m/%d/%Y")),
        "scores": scores, "stats": stats,
        "negative_groups": neg["negative_groups"],
        "negative_count": neg["negative_count"],
        "total_negative_debt": neg["total_negative_debt"],
        "plan_steps": plan, "keeper_accounts": keepers, "post_cleanup": post_cleanup,
        "tradelines": tl_cards, "projected": projected,
        "comparisons": comparisons, "timeline": timeline,
        "included_items": included,
        "pricing": pricing,
        "cta_text": Markup("Approve &amp; Begin Your Transformation"),
        "cta_subtext": "Questions? Call or reply to this message.",
    }
