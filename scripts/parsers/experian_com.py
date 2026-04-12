"""Experian.com PDF single-bureau credit report parser."""
import re
from parsers import register
from parsers.base import CreditReportParser


_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


from classify import score_tier as _score_tier


def _convert_date(raw):
    """Convert 'Feb 11, 2026' or 'Feb 2026' to '02/2026'. Returns '' on failure."""
    if not raw or raw.strip() in ("-", "\u2014", ""):
        return ""
    raw = raw.strip()
    # "Feb 11, 2026"
    m = re.match(r'([A-Za-z]{3})\s+\d{1,2},?\s+(\d{4})', raw)
    if m:
        mm = _MONTH_MAP.get(m.group(1).lower(), "")
        if mm:
            return f"{mm}/{m.group(2)}"
    # "Feb 2026"
    m = re.match(r'([A-Za-z]{3})\s+(\d{4})', raw)
    if m:
        mm = _MONTH_MAP.get(m.group(1).lower(), "")
        if mm:
            return f"{mm}/{m.group(2)}"
    # Already "02/2026"
    if re.match(r'\d{2}/\d{4}', raw):
        return raw
    return ""


@register
class ExperianComParser(CreditReportParser):
    PROVIDER_NAME = "experian"
    SUPPORTED_EXTENSIONS = [".pdf", ".html"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"identityiq" in c or b"myscoreiq" in c:
            return 0.0
        if b"experian.com" in c:
            return 0.9
        if b"experian" in c and b"prepared for" in c:
            return 0.85
        # Browser-printed Experian PDFs: "Prepared For" + "FICO Score 8" without "experian" on page 1
        if b"prepared for" in c and b"fico" in c and b"score 8" in c:
            return 0.8
        if b"experian" in c and b"account info" in c:
            return 0.75
        # Raw Experian profile / dealer pull
        if b"scorecard" in c and b"previous addresses" in c:
            return 0.7
        # Dealer pull / auto lender format
        if b"fico auto" in c or b"fico score 2" in c:
            return 0.65
        if b"credit profile report" in c:
            return 0.65
        # Experian mail (PO Box 9701, Allen TX)
        if b"po box 9701" in c and b"allen" in c:
            return 0.6
        # Experian inquiry analysis HTML
        if b"inquiry dispute analysis" in c:
            return 0.6
        return 0.0

    def parse(self, file_path):
        if file_path.lower().endswith((".html", ".htm")):
            return self._parse_html(file_path)
        pages = self._extract_text_pdf(file_path)
        text = "\n".join(pages)
        return {
            "client": self._parse_client(text),
            "scores": self._parse_scores(text, pages),
            "summary": self._parse_summary(text),
            "raw_accounts": self._parse_accounts(text),
            "inquiries": self._parse_inquiries(text),
            "addresses": [],
            "name_variations": [],
        }

    def _parse_html(self, file_path):
        """Parse Experian HTML pages (e.g., inquiry dispute analysis)."""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
        client = {"first_name": "", "last_name": ""}
        # Extract name from title: "Inquiry Dispute Analysis - Leandro Katira"
        m = re.search(r'<title>.*?-\s*([A-Za-z]+ [A-Za-z]+)', text)
        if m:
            parts = m.group(1).strip().split()
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = parts[-1]
        return {
            "client": client,
            "scores": [],
            "summary": {},
            "raw_accounts": [],
            "inquiries": [],
            "addresses": [],
            "name_variations": [],
        }

    # -- Client ----------------------------------------------------------------

    def _parse_client(self, text):
        client = {"first_name": "", "last_name": "", "report_date": "", "dob_year": ""}

        # "Prepared For LUIS CERDA" or "Prepared For\nLUIS CERDA"
        m = re.search(r'Prepared\s+For\s+([A-Z][A-Z \-]+)', text)
        if not m:
            # Raw profile: "\xa0\xa0\n\xa0\xa0EDWIN ACOSTA" or "Name:\nEdwin Acosta"
            m = re.search(r'\xa0\xa0\n\xa0\xa0([A-Z][A-Z ]+)', text)
        if not m:
            # Credit Profile Report: "SAID,ENNIS 0858..." or "Best Name\n...\nENNIS A SAID"
            m = re.search(r'^([A-Z]+),\s*([A-Z]+)\s+\d', text, re.M)
            if m:
                client["first_name"] = m.group(2).strip()
                client["last_name"] = m.group(1).strip()
                m = None  # Skip the shared extraction below
        if not m:
            m = re.search(r'Best\s+Name\s*\n(?:Other.*?\n)?([A-Z][A-Z ]+)', text)
        if not m:
            m = re.search(r'Name:\s*\n\s*([A-Za-z][A-Za-z ]+)', text)
        if m:
            name_str = re.sub(r',', ' ', m.group(1).strip())
            parts = name_str.split()
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = " ".join(parts[1:])
            elif parts:
                client["last_name"] = parts[0]

        # "Date generated: Apr 4, 2026"
        m = re.search(r'Date generated:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})', text)
        if m:
            client["report_date"] = m.group(1).strip()

        # "Year of birth 1990"
        m = re.search(r'Year\s+of\s+birth\s+(\d{4})', text)
        if m:
            client["dob_year"] = m.group(1)

        # Also known as
        m = re.search(r'Also\s+known\s+as\s+([A-Z][A-Z ]+)', text)
        if m:
            client["aka"] = m.group(1).strip()

        return client

    # -- Scores ----------------------------------------------------------------

    def _parse_scores(self, text, pages):
        scores = []
        seen = set()

        # Pattern 1: Page 1 "At a glance" — "FICO Score 8\n730"
        p1 = pages[0] if pages else text
        m = re.search(r'FICO.*?Score\s*8\s*\n\s*(\d{3})', p1)
        if m:
            v = int(m.group(1))
            key = ("FICO Score 8", v)
            if key not in seen:
                seen.add(key)
                scores.append({
                    "bureau": "Experian",
                    "model": "FICO Score 8",
                    "value": v,
                    "tier": _score_tier(v),
                })

        # Pattern 2: Dedicated score pages — multiple FICO models
        for i, page in enumerate(pages):
            if i < 1:
                continue
            for m in re.finditer(
                r'FICO[^\n]*?((?:Auto\s+|Bankcard\s+)?Score\s+\d+)\s*\n'
                r'(.*?)(\d{3})\s',
                page,
                re.DOTALL,
            ):
                model_name = "FICO " + re.sub(r'\s+', ' ', m.group(1).strip())
                val = int(m.group(3))
                if 250 <= val <= 900:
                    key = (model_name, val)
                    if key not in seen:
                        seen.add(key)
                        scores.append({
                            "bureau": "Experian",
                            "model": model_name,
                            "value": val,
                            "tier": _score_tier(val),
                        })

        return scores

    # -- Summary ---------------------------------------------------------------

    def _parse_summary(self, text):
        summary = {}
        m = re.search(r'Open\s+accounts\s+(\d+)', text)
        if m:
            summary["open_accounts"] = int(m.group(1))
        m = re.search(r'Closed\s+accounts\s+(\d+)', text)
        if m:
            summary["closed_accounts"] = int(m.group(1))
        m = re.search(r'Accounts\s+ever\s+late\s+(\d+)', text)
        if m:
            summary["accounts_ever_late"] = int(m.group(1))
        m = re.search(r'Collections\s+(\d+)', text)
        if m:
            summary["collections"] = int(m.group(1))
        m = re.search(r'Total\s+debt\s+\$?([\d,]+)', text)
        if m:
            summary["total_debt"] = m.group(1).replace(",", "")
        return summary

    # -- Accounts --------------------------------------------------------------

    def _parse_accounts(self, text):
        """Split text into account blocks. Tries formats in order of specificity."""
        accounts = self._parse_accounts_standard(text)
        if not accounts:
            accounts = self._parse_accounts_cdi(text)
        if not accounts:
            accounts = self._parse_accounts_raw(text)
        if not accounts:
            accounts = self._parse_accounts_dealer(text)
        return accounts

    def _parse_accounts_standard(self, text):
        """Standard Experian.com format: 'Account info\\n' delimiter."""
        accounts = []
        blocks = re.split(r'Account info\n', text)

        for i, block in enumerate(blocks):
            if i == 0:
                continue
            prev = blocks[i - 1]
            creditor = self._extract_creditor_from_tail(prev)
            if not creditor:
                continue

            fields = self._extract_account_fields(block)
            account = self._classify_account(creditor, fields)
            accounts.append(account)

        return accounts

    def _extract_creditor_from_tail(self, prev_text):
        """Extract the creditor name from the tail end of the text before 'Account info'."""
        lines = prev_text.rstrip().split("\n")
        for j in range(len(lines) - 1, max(len(lines) - 8, -1), -1):
            line = lines[j].strip()
            if not line:
                continue
            if re.match(r'^(Page\s+\d|Prepared\s+For|\d+/\d+/\d+|Date\s+generated)', line, re.I):
                continue
            if re.match(r'^(Open\s+accounts|Closed\s+accounts|Collection\s+accounts)', line, re.I):
                continue
            if re.match(r'^(Exceptional|Unknown|Good|Fair|Poor|Very)', line, re.I):
                continue
            if re.match(r'^(Balance\s+updated|payment\s+history)', line, re.I):
                continue
            if re.match(r'^Comments$', line, re.I):
                continue
            if line == "-":
                continue
            if re.match(r'^\(\d{3}\)\s+\d{3}-\d{4}', line):
                continue
            if re.match(r'^(Address|Phone\s+number|Contact\s+info)', line, re.I):
                continue
            if re.match(r'^(Current|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|\d{4})\b', line, re.I):
                continue
            if re.match(r'^[\s\-]+$', line):
                continue
            if re.match(r'^(Closed|No\s+)', line, re.I):
                continue
            # Check for "DE 19801" style address tail lines
            if re.match(r'^[A-Z]{2}\s+\d{5}', line):
                continue
            # Check for city/state lines
            if re.match(r'^[A-Z]+,\s*[A-Z]{2}', line):
                continue
            # Check for PO BOX / street address lines
            if re.match(r'^(PO\s+BOX|\d+\s+[A-Z])', line, re.I):
                continue
            # "By mail only"
            if re.match(r'^By\s+mail', line, re.I):
                continue
            # CLS or payment history codes
            if re.match(r'^(CLS|CLSClosed)', line, re.I):
                continue

            # Creditor header: "CREDITOR NAME $AMOUNT" or just "CREDITOR NAME"
            creditor = re.sub(r'\s+\$[\d,]+\s*$', '', line).strip()
            if creditor and re.match(r'^[A-Z][A-Z0-9 /&\.\'\-]+$', creditor):
                return creditor

        return ""

    def _extract_account_fields(self, block):
        """Extract key-value fields from an account block."""
        fields = {
            "bureaus": ["EXP"],
        }

        # Balance — "Balance $NNN" but NOT "Balance updated" or "Highest balance"
        m = re.search(r'(?<!Highest\s)(?<!Original\s)(?<!\w)Balance\s+(\$[\d,]+)', block)
        if m:
            fields["balance"] = m.group(1)
        else:
            m = re.search(r'Balance\s+(-)\s', block)
            if m:
                fields["balance"] = "0"

        # Credit limit
        m = re.search(r'Credit\s+limit\s+(\$[\d,]+)', block, re.I)
        if m:
            fields["credit_limit"] = m.group(1)

        # Original balance (for loans)
        m = re.search(r'Original\s+balance\s+(\$[\d,]+)', block, re.I)
        if m:
            fields["original_balance"] = m.group(1)
            if "high_credit" not in fields:
                fields["high_credit"] = m.group(1)

        # Highest balance
        m = re.search(r'Highest\s+balance\s+(\$[\d,]+)', block, re.I)
        if m:
            fields["high_credit"] = m.group(1)

        # Account type — stop at "Monthly payment", "Terms", or newline
        m = re.search(r'Account\s+type\s+(.+?)\s+(?:Monthly\s+payment|Terms)', block, re.I)
        if not m:
            m = re.search(r'Account\s+type\s+(.+?)(?:\n|$)', block, re.I)
        if m:
            val = m.group(1).strip()
            if val and val != "-":
                fields["type"] = val

        # Monthly payment
        m = re.search(r'Monthly\s+payment\s+(\$[\d,]+)', block, re.I)
        if m:
            fields["monthly_payment"] = m.group(1)

        # Date opened
        m = re.search(r'Date\s+opened\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})', block, re.I)
        if m:
            fields["opened"] = _convert_date(m.group(1))

        # Open/closed
        m = re.search(r'Open/closed\s+(Open|Closed)', block, re.I)
        if m:
            fields["account_status"] = m.group(1).capitalize()

        # Status — "Open/Never late." or "Paid, Closed/Never late." — stop before "Terms" or "Responsibility"
        m = re.search(r'(?<!\w)Status\s+(.+?)(?:\s+Terms|\s+Responsibility|\s+Status\s+updated|\n)', block, re.M)
        if m:
            raw_status = m.group(1).strip().rstrip('.')
            if not raw_status.lower().startswith("updated"):
                fields["status"] = raw_status
                fields["payment_status"] = raw_status

        # Responsibility
        m = re.search(r'Responsibility\s+(Individual|Joint|Authorized\s+user)', block, re.I)
        if m:
            fields["responsibility"] = m.group(1).strip()

        # Original creditor — value sits between "Original creditor" and "Original balance" or "Balance"
        m = re.search(r'Original\s+creditor\s+(.+?)\s+(?:Original\s+balance|Balance|Company|Credit)', block, re.I)
        if m:
            val = m.group(1).strip()
            if val not in ("-", "—", "", "-"):
                fields["original_creditor"] = val

        # Status updated -> last_reported
        m = re.search(r'Status\s+updated\s+([A-Za-z]+\s+\d{4})', block, re.I)
        if m:
            fields["last_reported"] = _convert_date(m.group(1))

        # Balance updated -> fallback last_reported
        m = re.search(r'Balance\s+updated\s+([A-Za-z]+\s+\d{1,2},?\s+\d{4})', block, re.I)
        if m:
            if not fields.get("last_reported"):
                fields["last_reported"] = _convert_date(m.group(1))

        # Comments
        m = re.search(r'Comments\s*\n(.+?)(?:\n[A-Z]|\Z)', block, re.S)
        if m:
            comment_text = m.group(1).strip()
            if comment_text and comment_text != "-":
                fields["comments"] = comment_text

        # Payment history — look for late indicators
        history_section = re.search(
            r'Payment\s+history\s*\n(.*?)(?:Contact\s+info|$)',
            block, re.S | re.I,
        )
        if history_section:
            hist = history_section.group(1)
            late_values = re.findall(r'\b(30|60|90|120)\b', hist)
            fields["late_days"] = self._max_late_from_history(late_values)

        return fields

    def _parse_inquiries(self, text):
        """Extract inquiries from Experian PDF."""
        inquiries = []
        inq_section = re.search(r'(?:Inquiries|inquiries)\s*\n(.*?)(?:Consumer\s+statement|Public\s+records|$)', text, re.S | re.I)
        if not inq_section:
            return inquiries
        section = inq_section.group(1)
        for m in re.finditer(r'^([A-Z][A-Z0-9 /&\.\'\-,]+?)\s*\n.*?Inquiry\s+date:?\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})', section, re.M | re.S):
            creditor = m.group(1).strip()
            date = m.group(2).strip()
            if creditor and date:
                inquiries.append({"creditor": creditor, "date": date, "bureau": "EXP"})
        return inquiries

    # -- CDI Format (Credit Dispute Interface print) ---------------------------

    def _parse_accounts_cdi(self, text):
        """CDI format: 'Account Info\\nAccount Name\\nCREDITOR' with label/value on separate lines."""
        accounts = []
        blocks = re.split(r'\nAccount Info\n', text)
        if len(blocks) <= 1:
            return accounts

        for i, block in enumerate(blocks):
            if i == 0:
                continue
            fields = self._extract_cdi_fields(block)
            creditor = fields.pop("_creditor", "")
            if not creditor:
                continue
            account = self._classify_account(creditor, fields)
            accounts.append(account)
        return accounts

    def _extract_cdi_fields(self, block):
        """Extract fields from CDI format: each field is 'Label\\nValue' on separate lines."""
        fields = {"bureaus": ["EXP"]}
        # Cut block at next "Payment History" or "Contact Info" section
        cut = re.search(r'\nPayment History\n|\nContact Info\n', block)
        if cut:
            main = block[:cut.start()]
            hist = block[cut.start():]
        else:
            main = block
            hist = ""

        lines = main.split("\n")
        i = 0
        label_map = {
            "Account Name": "_creditor",
            "Account Number": "account_number",
            "Account Type": "type",
            "Responsibility": "responsibility",
            "Date Opened": "_date_opened",
            "Status": "status",
            "Status Updated": "_status_updated",
            "Balance": "balance",
            "Monthly Payment": "monthly_payment",
            "Credit Limit": "credit_limit",
            "Original Balance": "original_balance",
            "Highest Balance": "high_credit",
            "Terms": "_terms",
        }
        while i < len(lines):
            line = lines[i].strip()
            if line in label_map and i + 1 < len(lines):
                key = label_map[line]
                val = lines[i + 1].strip()
                if val and val not in ("-", "\u2014", ""):
                    fields[key] = val
                i += 2
            else:
                i += 1

        # Post-process
        if "status" in fields:
            fields["payment_status"] = fields["status"]
            if "closed" in fields["status"].lower() or "paid" in fields["status"].lower():
                fields["account_status"] = "Closed"
            elif "open" in fields["status"].lower():
                fields["account_status"] = "Open"

        # Convert date opened
        opened = fields.pop("_date_opened", "")
        if opened:
            fields["opened"] = _convert_date(opened)

        # Status updated -> last_reported
        updated = fields.pop("_status_updated", "")
        if updated:
            fields["last_reported"] = _convert_date(updated)

        fields.pop("_terms", None)

        # Original balance -> high_credit fallback
        if "original_balance" in fields and "high_credit" not in fields:
            fields["high_credit"] = fields["original_balance"]

        # Late days from payment history section
        if hist:
            late_values = re.findall(r'\b(30|60|90|120)\b', hist)
            fields["late_days"] = self._max_late_from_history(late_values)

        return fields

    # -- Raw Experian Profile format (dealer pre-qual printouts) ---------------

    def _parse_accounts_raw(self, text):
        """Raw Experian profile: 'Trades:' header followed by fixed-order fields per account."""
        accounts = []
        m = re.search(r'Trades:\n', text)
        if not m:
            return accounts
        # Skip the header row (Account Name, Account Number, Status, etc.)
        body = text[m.end():]
        # Skip header lines until we hit actual account data
        skip = re.search(r'Payment Pattern\n', body)
        if skip:
            body = body[skip.end():]
        # Also skip any "CREDIT REPORT" / "about:blank" noise
        body = re.sub(r'CREDIT REPORT.*?(?=\n[A-Z])', '', body, flags=re.S)
        body = re.sub(r'about:blank.*?\n', '', body)
        body = re.sub(r'\d+/\d+/\d+,\s+\d+:\d+\s+[AP]M\n', '', body)

        # Each account: CREDITOR_NAME\nACCT_CODE\nACCT_NUMBER\nSTATUS\nDATE_OPEN\nOPN_CLSD\nBALANCE\nORIG_AMT...
        acct_pattern = re.compile(
            r'^([A-Z][A-Z0-9 /\.\'\-,]+)\n'  # creditor (may be multi-word)
            r'(?:([A-Z]+/\d+)\n)?'            # optional code like LT/3690580
            r'(\S+)\n'                         # account number
            r'(CURR ACCT|PAID ACCT|CHARGE OFF|COLLECTION|.+?)\n'  # status
            r'(\d{2}/\d{2})\n'                # date opened MM/YY
            r'(Open|Closed|.+?)\n'            # open/closed
            r'(\$[\d,]+)\n'                    # current balance
            r'(\$[\d,]+)',                     # original amount
            re.MULTILINE,
        )
        for m in acct_pattern.finditer(body):
            creditor = m.group(1).strip()
            if any(k in creditor for k in ["Score Summary", "Special Messages", "Payment Pattern"]):
                continue
            fields = {
                "bureaus": ["EXP"],
                "account_number": m.group(3),
                "status": m.group(4),
                "payment_status": m.group(4),
                "opened": m.group(5),
                "account_status": m.group(6),
                "balance": m.group(7),
                "high_credit": m.group(8),
            }
            account = self._classify_account(creditor, fields)
            accounts.append(account)
        return accounts

    def _parse_accounts_dealer(self, text):
        """Dealer/lender pull: 'Auto Trade LineN -' blocks with label:value pairs."""
        accounts = []
        blocks = re.split(r'(?:Auto\s+)?Trade\s+Line\s*\d+\s*-', text)
        if len(blocks) <= 1:
            # Try "Credit Profile Report" format with Tradelines section
            tl_section = re.search(r'Tradelines.*?\n(.*?)(?:Inquiries|Public Records|$)', text, re.S)
            if tl_section:
                return self._parse_accounts_profile_report(tl_section.group(1))
            return accounts

        for block in blocks[1:]:
            fields = {"bureaus": ["EXP"]}
            for m in re.finditer(r'^([A-Za-z ]+?):\s*\n(.+?)$', block, re.M):
                label = m.group(1).strip().lower()
                val = m.group(2).strip()
                if not val or val in ("-", "N/A"):
                    continue
                if "original amount" in label:
                    fields["high_credit"] = val
                elif "trade status" in label:
                    fields["status"] = val
                    fields["payment_status"] = val
                    fields["account_status"] = val
                elif "date reported" in label:
                    fields["last_reported"] = _convert_date(val)
                elif "original terms" in label:
                    fields["type"] = f"Installment ({val})"
                elif "monthly" in label and "payment" in label:
                    fields["monthly_payment"] = val
                elif "estimated payoff" in label:
                    fields["balance"] = val
            # Derive creditor from context (not in this block format)
            creditor = f"Auto Loan {len(accounts) + 1}"
            if fields.get("status"):
                account = self._classify_account(creditor, fields)
                accounts.append(account)
        return accounts

    def _parse_accounts_profile_report(self, section):
        """Credit Profile Report format: tabular tradelines in the Tradelines section."""
        accounts = []
        # Pattern: creditor name followed by account details
        blocks = re.split(r'\n(?=[A-Z][A-Z0-9 /]{3,}\s+\d)', section)
        for block in blocks:
            lines = block.strip().split('\n')
            if not lines:
                continue
            # First line has creditor + account number
            first = lines[0].strip()
            parts = re.match(r'^([A-Z][A-Z0-9 /\.\'\-,]+?)\s+(\d[\d\-X*]+)', first)
            if not parts:
                continue
            creditor = parts.group(1).strip()
            fields = {
                "bureaus": ["EXP"],
                "account_number": parts.group(2),
            }
            rest = '\n'.join(lines[1:])
            # Extract common fields
            for m in re.finditer(r'(Balance|Limit|High Credit|Opened|Status|Payment).*?[\$]?([\d,]+)', rest):
                label = m.group(1).lower()
                val = m.group(2)
                if "balance" in label:
                    fields["balance"] = val
                elif "limit" in label:
                    fields["credit_limit"] = val
            account = self._classify_account(creditor, fields)
            accounts.append(account)
        return accounts
