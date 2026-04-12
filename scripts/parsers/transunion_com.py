"""TransUnion.com PDF single-bureau credit report parser."""
import re
from parsers import register
from parsers.base import CreditReportParser


_DEDUP_RE = re.compile(r'(.)\1{3,}')


def _dedup(text):
    """Fix bold text duplication (each char repeated 4x in TU PDFs)."""
    return _DEDUP_RE.sub(r'\1', text)


@register
class TransUnionComParser(CreditReportParser):
    PROVIDER_NAME = "transunion"
    SUPPORTED_EXTENSIONS = [".pdf"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"identityiq" in c or b"myscoreiq" in c: return 0.0
        if b"transunion online service center" in c: return 0.95
        if b"service.transunion.com" in c: return 0.9
        if b"personal credit report for" in c and b"transunion.com" in c: return 0.92
        if b"your transunion credit report" in c: return 0.85
        return 0.0

    def parse(self, file_path):
        pages = self._extract_text_pdf(file_path)
        text = "\n".join(_dedup(p) for p in pages)
        return {
            "client": self._parse_client(text),
            "scores": self._parse_scores(text, pages),
            "summary": {},
            "raw_accounts": self._parse_accounts(text),
            "inquiries": [],
            "addresses": [],
            "name_variations": [],
        }

    def _parse_scores(self, text, pages):
        from classify import score_tier
        scores = []
        # Pattern: "VantageScore 3.0" or "FICO Score" followed by 3-digit number
        for m in re.finditer(r'(?:VantageScore\s*3\.0|FICO[^\n]*?Score[^\n]*?)\s*[\n:]\s*(\d{3})', text):
            v = int(m.group(1))
            if 300 <= v <= 850:
                scores.append({"bureau": "TransUnion", "model": "VantageScore 3.0", "value": v, "tier": score_tier(v)})
                break  # take first match only
        if not scores:
            # Fallback: look for any 3-digit score near "credit score" text
            for m in re.finditer(r'(?:credit\s+score|your\s+score)[:\s]*(\d{3})', text, re.I):
                v = int(m.group(1))
                if 300 <= v <= 850:
                    scores.append({"bureau": "TransUnion", "value": v, "tier": score_tier(v)})
                    break
        return scores

    def _parse_client(self, text):
        client = {"first_name": "", "last_name": "", "report_date": "", "reference": ""}
        m = re.search(r'Personal Credit Report for:\s*\n?\s*([A-Z][A-Z \-]+)', text)
        if m:
            parts = m.group(1).strip().split()
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = parts[-1]
            elif parts:
                client["last_name"] = parts[0]
        m = re.search(r'File Number:\s*\n?\s*(\d+)', text)
        if m:
            client["reference"] = m.group(1)
        m = re.search(r'Date Created:\s*\n?\s*(\d{2}/\d{2}/\d{4})', text)
        if m:
            client["report_date"] = m.group(1)
        return client

    def _parse_accounts(self, text):
        accounts = []
        # Find account blocks: CREDITOR_NAME MASKED_ACCOUNT_NUMBER
        # Pattern: line with all-caps creditor name followed by masked acct#
        acct_pattern = re.compile(
            r'^([A-Z][A-Z0-9 /&\.\'\-,]+?)\s+(\S*[\*X]+\S*)\s*$',
            re.MULTILINE
        )
        matches = list(acct_pattern.finditer(text))
        if not matches:
            return accounts

        for idx, match in enumerate(matches):
            creditor = match.group(1).strip()
            # Skip noise lines
            if any(k in creditor for k in ["Page ", "TransUnion", "BNPL"]):
                continue
            # Skip dedup artifacts (X X X X X X)
            if re.match(r'^[X\s]+$', creditor):
                continue

            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
            block = text[start:end]

            fields = self._extract_fields(block)
            fields["late_days"] = self._extract_ratings(block)
            account = self._classify_account(creditor, fields)
            accounts.append(account)
        return accounts

    def _extract_fields(self, block):
        fields = {"bureaus": ["TU"]}
        patterns = {
            "balance": r'Balance\s+\$?([\d,]+)',
            "monthly_payment": r'Monthly\s+Payment\s+\$?([\d,]+)',
            "responsibility": r'Responsibility\s+(.+?)(?:\n|$)',
            "type": r'Account\s+Type\s+(.+?)(?:\n|$)',
            "opened": r'Date\s+Opened\s+(\d{2}/\d{2}/\d{4})',
            "last_reported": r'Date\s+Updated\s+(\d{2}/\d{2}/\d{4})',
            "high_credit": r'High\s+Balance.*?of\s+\$?([\d,]+)',
            "date_closed": r'Date\s+Closed\s+(\d{2}/\d{2}/\d{4})',
        }
        for key, pat in patterns.items():
            m = re.search(pat, block)
            if m:
                fields[key] = m.group(1).strip()

        # Pay Status — strip >brackets<
        m = re.search(r'Pay\s+Status\s+>?(.+?)<?\s*$', block, re.M)
        if m:
            status = m.group(1).strip().rstrip('<').strip()
            fields["status"] = status
            fields["account_status"] = status
            fields["payment_status"] = status

        # Loan Type
        m = re.search(r'Loan\s+Type\s+(.+?)(?:\n|$)', block)
        if m:
            fields["account_type_detail"] = m.group(1).strip()

        # Convert opened date MM/DD/YYYY -> MM/YYYY
        opened = fields.get("opened", "")
        if opened and re.match(r'\d{2}/\d{2}/\d{4}', opened):
            fields["opened"] = opened[:2] + "/" + opened[6:]

        # Determine open/closed
        if fields.get("date_closed"):
            fields["account_status"] = "Closed"
        elif "status" in fields and "open" in fields["status"].lower():
            fields["account_status"] = "Open"

        return fields

    def _extract_ratings(self, block):
        """Extract max late days from Rating rows."""
        lates = []
        for m in re.finditer(r'^Rating\s+Rating.*?\n(.+?)$', block, re.M):
            vals = m.group(1).strip().split()
            lates.extend(vals)
        # Also catch single Rating lines
        for m in re.finditer(r'Rating\s*\n((?:(?:OK|30|60|90|120|C/O|CO|COL|-)\s*)+)', block):
            vals = m.group(1).strip().split()
            lates.extend(vals)
        return self._max_late_from_history(lates)
