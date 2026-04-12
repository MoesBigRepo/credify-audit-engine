"""Base class for all credit report parsers."""
import re
from classify import is_account_derogatory, parse_balance


class CreditReportParser:
    """Abstract base for credit report parsers."""

    PROVIDER_NAME = "unknown"
    SUPPORTED_EXTENSIONS = []

    @classmethod
    def detect(cls, file_path, content):
        return 0.0

    def parse(self, file_path):
        raise NotImplementedError

    def _extract_html(self, fp):
        from bs4 import BeautifulSoup
        with open(fp, "r", encoding="utf-8", errors="ignore") as f:
            return BeautifulSoup(f.read(), "lxml")

    def _extract_text_pdf(self, fp):
        """PyMuPDF-first extraction (3-5x faster than pdfplumber). Fallback kept during validation."""
        try:
            import fitz
            doc = fitz.open(fp)
            pages = [p.get_text() for p in doc]
            doc.close()
            return pages
        except ImportError:
            import pdfplumber
            with pdfplumber.open(fp) as pdf:
                return [p.extract_text() or "" for p in pdf.pages]

    def _classify_account(self, creditor, fields):
        """Build a raw_account dict and apply full classification rules."""
        balance = parse_balance(fields.get("balance", "0"))
        credit_limit = parse_balance(fields.get("credit_limit", "0"))
        high_credit = parse_balance(fields.get("high_credit", "0"))
        past_due = parse_balance(fields.get("past_due", "0"))
        late_days = fields.get("late_days", 0) or 0

        status = str(fields.get("status", "")).strip()
        payment_status = str(fields.get("payment_status", "")).strip()
        responsibility = str(fields.get("responsibility", "Individual")).strip()
        comments = str(fields.get("comments", ""))
        original_creditor = str(fields.get("original_creditor", ""))

        is_au = "authorized" in responsibility.lower()
        is_chargeoff = any(
            k in t.lower()
            for t in [status, payment_status, comments]
            for k in ["charge-off", "charge off", "chargeoff", "charged off", "c/o"]
        )
        acct_type = str(fields.get("type", "")).lower()
        is_collection = bool(original_creditor) or "collection" in acct_type or "collection" in status.lower()
        is_repo = any(k in status.lower() or k in payment_status.lower() for k in ["repossession", "repo"])

        def _n(v):
            return str(int(v)) if v == int(v) else str(v)

        account = {
            "creditor": creditor,
            "type": fields.get("type", ""),
            "account_type_detail": fields.get("account_type_detail", fields.get("type", "")),
            "responsibility": responsibility,
            "status": status,
            "account_status": fields.get("account_status", status),
            "opened": fields.get("opened", ""),
            "account_number": fields.get("account_number", ""),
            "balance": _n(balance),
            "credit_limit": _n(credit_limit),
            "high_credit": _n(high_credit),
            "past_due": _n(past_due),
            "monthly_payment": fields.get("monthly_payment", "—"),
            "payment_status": payment_status or status,
            "last_reported": fields.get("last_reported", ""),
            "last_activity": fields.get("last_activity", "—"),
            "bureaus": fields.get("bureaus", []),
            "late_days": late_days,
            "is_derogatory": False,
            "is_chargeoff": is_chargeoff,
            "is_collection": is_collection,
            "is_repo": is_repo,
            "is_bankruptcy": False,
            "is_au": is_au,
            "comments": comments,
            "original_creditor": original_creditor,
            "details": fields.get("details", ""),
        }
        account["is_derogatory"] = is_account_derogatory(account)
        return account

    def _best_value(self, *vals):
        for v in vals:
            v = str(v).strip() if v else ""
            if v and v not in ("-", "\u2014", ""):
                return v
        return ""

    def _bureaus_from_values(self, tu, exp, eqf):
        b = []
        if tu and str(tu).strip() not in ("-", "\u2014", ""): b.append("TU")
        if exp and str(exp).strip() not in ("-", "\u2014", ""): b.append("EXP")
        if eqf and str(eqf).strip() not in ("-", "\u2014", ""): b.append("EQF")
        return b

    def _max_late_from_history(self, vals):
        m = 0
        for v in vals:
            v = str(v).strip().upper()
            if v == "30": m = max(m, 30)
            elif v == "60": m = max(m, 60)
            elif v == "90": m = max(m, 90)
            elif v in ("120", "150", "180", "CO", "C/O", "COL"): m = max(m, 120)
        return m
