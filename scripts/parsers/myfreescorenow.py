"""MyFreeScoreNow PDF 3-bureau credit report parser.

Parses browser-printed PDFs from member.myfreescorenow.com.
Uses positional text extraction (x/y coordinates) to reconstruct the 4-column
table layout: Labels | TransUnion | Experian | Equifax.

Each table row shares the same y-coordinate across all columns, so we group
spans by y to reconstruct rows, then match labels to bureau values.
"""
import re
from parsers import register
from parsers.base import CreditReportParser
from classify import score_tier as _score_tier


# Column boundaries (midpoints between observed column x-positions)
# Labels: x~30, TU: x~169, EXP: x~309, EQF: x~448
_COL_BOUNDS = (100, 240, 380)

_LABEL_MAP = {
    "Account #": "account_number",
    "High Balance:": "high_credit",
    "Last Verified:": "last_verified",
    "Last Veriﬁed:": "last_verified",
    "Date of Last Activity:": "last_activity",
    "Date Reported:": "last_reported",
    "Date Opened:": "opened",
    "Balance Owed:": "balance",
    "Closed Date:": "date_closed",
    "Account Rating:": "account_rating",
    "Account Description:": "responsibility",
    "Dispute Status:": "dispute_status",
    "Creditor Type:": "type",
    "Account Status:": "account_status",
    "Payment Status:": "payment_status",
    "Creditor Remarks:": "comments",
    "Payment Amount:": "monthly_payment",
    "Last Payment:": "last_payment",
    "Term Length:": "term_length",
    "Past Due Amount:": "past_due",
    "Account Type:": "account_type_detail",
    "Payment Frequency:": "payment_frequency",
    "Credit Limit:": "credit_limit",
}

_Y_TOLERANCE = 5  # pixels


def _col(x):
    """Map x-coordinate to column: 0=labels, 1=TU, 2=EXP, 3=EQF."""
    if x < _COL_BOUNDS[0]:
        return 0
    if x < _COL_BOUNDS[1]:
        return 1
    if x < _COL_BOUNDS[2]:
        return 2
    return 3


def _clean(val):
    if not val:
        return ""
    v = val.strip()
    return "" if v in ("--", "-", "\u2014", "®") else v


def _best(tu, exp, eqf):
    for v in (tu, exp, eqf):
        c = _clean(v)
        if c:
            return c
    return ""


def _normalize_date(raw):
    if not raw:
        return ""
    m = re.match(r'(\d{1,2})/\d{1,2}/(\d{4})', raw)
    if m:
        return f"{int(m.group(1)):02d}/{m.group(2)}"
    m = re.match(r'(\d{1,2})/(\d{4})', raw)
    if m:
        return f"{int(m.group(1)):02d}/{m.group(2)}"
    return raw


@register
class MyFreeScoreNowParser(CreditReportParser):
    PROVIDER_NAME = "myfreescorenow"
    SUPPORTED_EXTENSIONS = [".pdf", ".html"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"myfreescorenow" in c:
            return 0.9
        if b"freescorenow" in c:
            return 0.7
        return 0.0

    def parse(self, file_path):
        import fitz
        doc = fitz.open(file_path)

        # Extract positioned spans for account parsing
        spans = self._extract_spans(doc)

        # Also extract plain text for simpler sections
        text = "\n".join(doc[i].get_text() for i in range(doc.page_count))
        doc.close()

        return {
            "client": self._parse_client(text),
            "scores": self._parse_scores(text),
            "summary": self._parse_summary(text),
            "raw_accounts": self._parse_accounts(spans),
            "inquiries": self._parse_inquiries(text),
            "addresses": self._parse_addresses(text),
            "name_variations": self._parse_name_variations(text),
            "score_model": "VantageScore 3.0",
        }

    def _extract_spans(self, doc):
        """Extract all text spans with absolute (x, y) positions across pages."""
        spans = []
        for pg in range(doc.page_count):
            page = doc[pg]
            page_h = page.rect.height
            y_offset = pg * page_h  # absolute y across pages
            for block in page.get_text("dict")["blocks"]:
                if "lines" not in block:
                    continue
                for line in block["lines"]:
                    for span in line["spans"]:
                        t = span["text"].strip()
                        if not t or t == "\u00ae":
                            continue
                        spans.append({
                            "t": t,
                            "x": span["bbox"][0],
                            "y": y_offset + span["bbox"][1],
                            "pg": pg,
                        })
        return spans

    # ── Client ──────────────────────────────────────────────────────────

    def _parse_client(self, text):
        client = {"first_name": "", "last_name": "", "report_date": ""}

        # Name: after Transunion date in Personal Information
        m = re.search(
            r'Transunion\s*\n\s*\d{1,2}/\d{1,2}/\d{4}\s*\n\s*([A-Z][A-Z ]+?)\s*\n',
            text,
        )
        if m:
            parts = [p for p in m.group(1).strip().split() if p not in ("-", "\u2014")]
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = parts[-1]
            elif parts:
                client["last_name"] = parts[0]

        # Report date
        dm = re.search(r'Credit\s+Report\s+Date.*?\n.*?(\d{1,2}/\d{1,2}/\d{4})', text, re.S)
        if dm:
            client["report_date"] = dm.group(1)

        # DOB year: look for 4-digit year after "Date of Birth" that's plausible (1940-2010)
        dob = re.search(r'Date of Birth\s*\n.*?\n\s*(\d{4})\s*\n', text, re.S)
        if dob:
            yr = int(dob.group(1))
            if 1940 <= yr <= 2010:
                client["dob_year"] = str(yr)

        return client

    # ── Scores ──────────────────────────────────────────────────────────

    def _parse_scores(self, text):
        scores = []
        m = re.search(
            r'Transunion\s*\n\s*(\d{3})\s*\n\s*Experian\s*\n\s*(\d{3})\s*\n\s*Equifax\s*\n\s*(\d{3})',
            text,
        )
        if m:
            for bureau, v in [("TransUnion", m.group(1)), ("Experian", m.group(2)), ("Equifax", m.group(3))]:
                score = int(v)
                scores.append({"bureau": bureau, "value": score, "model": "VantageScore 3.0", "tier": _score_tier(score)})
        return scores

    # ── Summary ─────────────────────────────────────────────────────────

    def _parse_summary(self, text):
        summary = {}
        pats = {
            "total_accounts": r'Total\s+Accounts\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
            "open_accounts": r'Open\s+Accounts:\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
            "closed_accounts": r'Closed\s+Accounts:\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
            "delinquent": r'Delinquent:\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
            "derogatory": r'Derogatory:\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
            "inquiries": r'Inquiries\s*\(2\s*years?\):\s*\n\s*(\d+)\s*\n\s*(\d+)\s*\n\s*(\d+)',
        }
        for key, pat in pats.items():
            m = re.search(pat, text)
            if m:
                summary[key] = {"TU": int(m.group(1)), "EXP": int(m.group(2)), "EQF": int(m.group(3))}
        return summary

    # ── Account parsing ────────────────────────────────────────────────

    def _parse_accounts(self, spans):
        accounts = []

        # Find all "Account #" label positions (col 0)
        acct_indices = [i for i, s in enumerate(spans) if s["t"] == "Account #" and _col(s["x"]) == 0]

        for idx, ai in enumerate(acct_indices):
            creditor = self._find_creditor(spans, ai)
            if not creditor:
                continue

            # Block end: next account or end-of-section marker
            if idx + 1 < len(acct_indices):
                end_i = acct_indices[idx + 1]
            else:
                end_i = len(spans)
                for j in range(ai, len(spans)):
                    if spans[j]["t"] in ("Public Information", "Inquiries") and _col(spans[j]["x"]) == 0:
                        end_i = j
                        break

            block = spans[ai:end_i]
            fields = self._extract_fields_by_row(block)
            account = self._classify_account(creditor, fields)
            accounts.append(account)

        return self._merge_continuation_blocks(accounts)

    # ── Continuation-block merge ───────────────────────────────────────
    #
    # The MyFreeScoreNow print template renders a single tradeline across
    # TWO sequential Account # blocks whenever one bureau reports a
    # different creditor-display name than the others (e.g. Experian:
    # "NAVY FEDERAL CR UNION" vs TU/EQF: "NAVY FCU"). The isolated
    # bureau's data lands in its own block with complementary "--"
    # coverage on the other two bureaus. The parser emits both blocks
    # as distinct raw_accounts; this pass merges them back into one.

    _BUREAU_ORDER = ("TU", "EXP", "EQF")

    def _merge_continuation_blocks(self, accounts):
        """Collapse pairs of blocks that represent one tradeline.

        Returns a new list; never mutates input.
        """
        if len(accounts) < 2:
            return list(accounts)

        out = []
        consumed = set()
        for i, a in enumerate(accounts):
            if i in consumed:
                continue
            if len(a.get("bureaus", [])) == 3:
                out.append(a)
                continue
            for j in range(i + 1, len(accounts)):
                if j in consumed:
                    continue
                b = accounts[j]
                if len(b.get("bureaus", [])) == 3:
                    continue
                if self._is_continuation_pair(a, b):
                    a = self._merge_account_pair(a, b)
                    consumed.add(j)
                    import sys
                    print(
                        f"[myfreescorenow] merged continuation block: "
                        f"{a.get('creditor','?')} @ ${a.get('balance','?')} "
                        f"bureaus={a.get('bureaus')}",
                        file=sys.stderr,
                    )
                    break
            out.append(a)
        return out

    def _is_continuation_pair(self, a, b):
        """Return True if a and b are two rendered blocks of one tradeline."""
        from classify import parse_balance, parse_opened

        # 1. Complementary bureau coverage — strongest signal
        ba = set(a.get("bureaus", []))
        bb = set(b.get("bureaus", []))
        if not ba or not bb:
            return False
        if ba & bb:
            return False
        if not (ba | bb) <= set(self._BUREAU_ORDER):
            return False

        # 2. Account# digit-prefix match (≥4 leading digits)
        pa = self._digit_prefix(a.get("account_number", ""))
        pb = self._digit_prefix(b.get("account_number", ""))
        if len(pa) < 4 or len(pb) < 4:
            return False
        if pa[:4] != pb[:4]:
            return False

        # 3. Account type identical
        ta = (a.get("type") or "").strip().lower()
        tb = (b.get("type") or "").strip().lower()
        if not ta or not tb or ta != tb:
            return False

        # 4. High credit within $1
        ha = parse_balance(a.get("high_credit", "0"))
        hb = parse_balance(b.get("high_credit", "0"))
        if abs(ha - hb) > 1:
            return False

        # 5. Opened date within ±1 month
        oa = parse_opened(a.get("opened", ""))
        ob = parse_opened(b.get("opened", ""))
        if not oa or not ob:
            return False
        diff = abs((oa.year - ob.year) * 12 + (oa.month - ob.month))
        if diff > 1:
            return False

        return True

    def _merge_account_pair(self, a, b):
        """Produce one merged record from two continuation blocks.

        Union bureau lists, prefer non-blank field values (richer side
        first), max late_days, OR boolean flags, pick more-severe
        payment_status, longer account_number string.
        """
        _BLANK = ("", "--", "\u2014", None, [])

        def _richness(r):
            return sum(1 for v in r.values() if v not in _BLANK and v != 0 and v != "0")

        base = a if _richness(a) >= _richness(b) else b
        other = b if base is a else a

        result = dict(base)
        for k, v in other.items():
            bv = result.get(k)
            if bv in _BLANK and v not in _BLANK:
                result[k] = v

        # Bureaus: canonical-ordered union
        bureaus = sorted(
            set(a.get("bureaus", [])) | set(b.get("bureaus", [])),
            key=lambda x: self._BUREAU_ORDER.index(x) if x in self._BUREAU_ORDER else 99,
        )
        result["bureaus"] = bureaus

        # Late days: max
        la = a.get("late_days", 0) or 0
        lb = b.get("late_days", 0) or 0
        result["late_days"] = max(la, lb)

        # Booleans: OR
        for flag in ("is_chargeoff", "is_collection", "is_repo",
                     "is_bankruptcy", "is_derogatory", "is_au"):
            result[flag] = bool(a.get(flag)) or bool(b.get(flag))

        # Payment status: align with higher late_days
        if lb > la:
            result["payment_status"] = b.get("payment_status") or a.get("payment_status", "")
        else:
            result["payment_status"] = a.get("payment_status") or b.get("payment_status", "")

        # Account number: longer string (more digits retained)
        an_a = str(a.get("account_number", ""))
        an_b = str(b.get("account_number", ""))
        result["account_number"] = an_a if len(an_a) >= len(an_b) else an_b

        return result

    @staticmethod
    def _digit_prefix(s):
        """Leading run of digits before any mask or separator."""
        m = re.match(r"(\d+)", str(s or ""))
        return m.group(1) if m else ""

    def _find_creditor(self, spans, acct_idx):
        """Scan backwards from 'Account #' to find the creditor name."""
        noise = {
            "Transunion", "Experian", "Equifax", "NONE REPORTED",
            "Days Late - 7 Year History", "Two-Year Payment History",
            "OK", "30", "60", "90", "120", "150", "CO", "C/O",
        }
        months = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"}
        for j in range(acct_idx - 1, max(acct_idx - 40, -1), -1):
            t = spans[j]["t"]
            if not t:
                continue
            if t in noise or t in months:
                continue
            if re.match(r"^'?\d{2,4}$", t):
                continue
            if re.match(r'Page \d+ of \d+', t):
                continue
            if "myfreescorenow" in t.lower() or re.match(r'https?://', t):
                continue
            if re.match(r'\d{1,2}/\d{1,2}/\d{2,4},', t):
                continue
            if t.startswith("30:") or t.startswith("60:") or t.startswith("90:"):
                continue
            if t.startswith("Revolving Accounts") or t.startswith("Installment Accounts"):
                continue
            return t
        return None

    def _extract_fields_by_row(self, block):
        """Extract field values by matching y-coordinates across columns.

        For each label span (col 0) that matches a known field, find the
        TU/EXP/EQF values at the same y-coordinate in columns 1/2/3.
        """
        fields = {"bureaus": []}
        in_payment_history = False

        # Index block spans by (column, y) for fast lookup
        col_by_y = {1: {}, 2: {}, 3: {}}
        for s in block:
            t = s["t"]
            c = _col(s["x"])
            if c in (1, 2, 3):
                # Skip bureau headers in value columns
                if t in ("Transunion", "Experian", "Equifax"):
                    continue
                y_key = round(s["y"])
                # Keep first value at this y (handles rare duplicates)
                if y_key not in col_by_y[c]:
                    col_by_y[c][y_key] = t

        # Process label spans
        for s in block:
            t = s["t"]
            c = _col(s["x"])

            if "Two-Year Payment History" in t:
                in_payment_history = True
                continue
            if "Days Late" in t:
                in_payment_history = False
                continue
            if in_payment_history:
                continue

            # Only process label-column spans that are known fields
            if c != 0 or t not in _LABEL_MAP:
                continue

            key = _LABEL_MAP[t]
            y = round(s["y"])

            # Find values in each bureau column at matching y (with tolerance)
            tu = self._find_at_y(col_by_y[1], y)
            exp = self._find_at_y(col_by_y[2], y)
            eqf = self._find_at_y(col_by_y[3], y)

            best = _best(tu, exp, eqf)

            if key == "account_number":
                # Prefer the value with the most leading digits — a
                # real prefix like "430016********" is more informative
                # than a pure mask like "****", which _best() would pick
                # first. Preserves continuation-block merge signal.
                cleaned = [c for c in (_clean(tu), _clean(exp), _clean(eqf)) if c]
                if cleaned:
                    cleaned.sort(
                        key=lambda c: len(re.match(r"^\d*", c).group(0)),
                        reverse=True,
                    )
                    fields["account_number"] = cleaned[0]
                else:
                    fields["account_number"] = ""
                fields["bureaus"] = self._bureaus_from_values(
                    _clean(tu), _clean(exp), _clean(eqf),
                )
            elif key == "account_status":
                fields["status"] = best
                fields["account_status"] = best
            elif key == "opened":
                fields["opened"] = _normalize_date(best)
            elif key == "responsibility":
                fields["responsibility"] = best
            elif key == "comments":
                parts = [_clean(v) for v in (tu, exp, eqf) if _clean(v)]
                fields["comments"] = "; ".join(parts)
            else:
                fields[key] = best

        # Late days from Days Late section
        fields["late_days"] = self._extract_late_days(block)

        # AU detection
        if "authorized" in str(fields.get("responsibility", "")).lower():
            fields["responsibility"] = "Authorized User"

        # Use Account Type as type if it's more specific than Creditor Type
        atd = fields.get("account_type_detail", "")
        if atd:
            fields["type"] = atd

        return fields

    def _find_at_y(self, y_map, target_y):
        """Find a value at target_y with tolerance."""
        # Exact match first
        if target_y in y_map:
            return y_map[target_y]
        # Tolerance search
        for y_key, val in y_map.items():
            if abs(y_key - target_y) <= _Y_TOLERANCE:
                return val
        return ""

    def _extract_late_days(self, block):
        """Extract max late days from 'Days Late - 7 Year History' section."""
        max_late = 0
        in_section = False
        for s in block:
            t = s["t"]
            if "Days Late" in t:
                in_section = True
                continue
            if not in_section:
                continue
            m = re.match(r'(\d+):\s*(\d+)', t)
            if m:
                days, count = int(m.group(1)), int(m.group(2))
                if count > 0 and days > max_late:
                    max_late = days

        # Also check payment_status for chargeoff/collection
        for s in block:
            t = s["t"].lower()
            c = _col(s["x"])
            if c in (1, 2, 3) and ("collection" in t and "chargeo" in t):
                max_late = max(max_late, 120)
            if c in (1, 2, 3) and "late" in t:
                m = re.search(r'late\s+(\d+)', t, re.I)
                if m:
                    max_late = max(max_late, int(m.group(1)))

        return max_late

    # ── Inquiries ───────────────────────────────────────────────────────

    def _parse_inquiries(self, text):
        inquiries = []
        m = re.search(r'Inquiries\s*\n', text)
        if not m:
            return inquiries

        section = text[m.end():]
        # End at "Creditor Contacts Show" which appears after ALL inquiries
        end = re.search(r'Creditor Contacts Show', section)
        if end:
            section = section[:end.start()]

        lines = [l.strip() for l in section.split('\n') if l.strip()]

        # Aggressively filter noise: page headers, footers, navigation, disclaimers
        _noise = {
            "Creditor Name", "Date of Inquiry", "Credit Bureau",
            "Public Information", "None Reported",
            "FAQ", "Contact Us", "Cancellation Policy", "Refund Policy",
            "Service Agreement", "Terms of Use", "Security", "Privacy Policy",
            "Join LegalShield",
        }
        filtered = []
        for l in lines:
            if not l or l == '\u00ae':
                continue
            if re.match(r'Page \d+ of \d+', l):
                continue
            if re.match(r'https?://', l):
                continue
            if re.match(r'\d{1,2}/\d{1,2}/\d{2,4},', l):
                continue
            if "myfreescorenow" in l.lower():
                continue
            if l in _noise:
                continue
            if l.startswith("IMPORTANT DISCLAIMER"):
                continue
            if re.match(r'Become an A.liate', l):
                continue
            if re.match(r'A.liates$', l):
                continue
            # Skip multi-line disclaimer text
            if "estimates only" in l.lower() or "results are not guaranteed" in l.lower():
                continue
            filtered.append(l)

        i = 0
        while i + 2 < len(filtered):
            creditor, date_str, bureau_raw = filtered[i], filtered[i + 1], filtered[i + 2]
            if not re.match(r'\d{1,2}/\d{1,2}/\d{4}', date_str):
                i += 1
                continue
            bl = bureau_raw.lower()
            bc = "TU" if "transunion" in bl else ("EQF" if "equifax" in bl else "EXP")
            inquiries.append({"creditor": creditor, "date": date_str, "bureau": bc})
            i += 3
        return inquiries

    # ── Addresses ───────────────────────────────────────────────────────

    def _parse_addresses(self, text):
        addresses = []
        for label in ("Current Address", "Previous Address"):
            m = re.search(
                rf'{label}\s*\n(.*?)(?:Previous Address|Employer|Consumer Statement|Summary)',
                text, re.S,
            )
            if not m:
                continue
            for addr_m in re.finditer(
                r'(\d+\s+[A-Z][A-Z0-9 ]+?)\s*\n\s*([A-Z]+,?\s*[A-Z]{2}\s*\d{5})',
                m.group(1),
            ):
                addr = f"{addr_m.group(1).strip()}, {addr_m.group(2).strip()}"
                if addr not in addresses:
                    addresses.append(addr)
        return addresses

    # ── Name Variations ─────────────────────────────────────────────────

    def _parse_name_variations(self, text):
        """Extract AKA names from the Personal Information section.

        The AKA names appear in the bureau data columns after each bureau's
        primary name line. We scan the Personal Information section for
        all-caps name patterns that differ from the primary name.
        """
        variations = []
        # Find primary name
        primary = ""
        pm = re.search(
            r'Transunion\s*\n\s*\d{1,2}/\d{1,2}/\d{4}\s*\n\s*([A-Z][A-Z ]+?)\s*\n',
            text,
        )
        if pm:
            primary = pm.group(1).strip().upper()

        # Extract personal info section (between "Personal Information" and "Summary")
        pi = re.search(r'Personal Information\s*\n(.*?)(?:Consumer Statement|Summary)', text, re.S)
        if not pi:
            return variations

        section = pi.group(1)
        # Find all name-like lines (2+ words, all uppercase)
        for line in section.split('\n'):
            line = line.strip()
            if not line or len(line) < 5:
                continue
            # Must be all uppercase letters/spaces, 2+ words
            if not re.match(r'^[A-Z][A-Z ]+$', line):
                continue
            words = line.split()
            if len(words) < 2:
                continue
            # Skip known non-name patterns
            if any(w in line for w in ("NONE REPORTED", "ORLANDO", "PROVO",
                                       "MASSIVE", "CORDOBA", "MAXIMIN",
                                       "LLC", "INC")):
                continue
            # Skip addresses (contain numbers or common address words)
            if re.search(r'\d', line):
                continue
            name = line.upper()
            if name != primary and name not in variations:
                variations.append(name)
        return variations
