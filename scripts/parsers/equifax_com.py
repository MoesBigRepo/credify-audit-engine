"""Equifax.com PDF single-bureau credit report parser."""
import re
from parsers import register
from parsers.base import CreditReportParser


@register
class EquifaxComParser(CreditReportParser):
    PROVIDER_NAME = "equifax"
    SUPPORTED_EXTENSIONS = [".pdf"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"identityiq" in c or b"myscoreiq" in c:
            return 0.0
        if b"equifax.com" in c and b"prepared for" in c:
            return 0.92
        if b"confirmation #" in c and b"equifax" in c:
            return 0.88
        return 0.0

    def parse(self, file_path):
        pages = self._extract_text_pdf(file_path)
        text = "\n".join(pages)
        return {
            "client": self._parse_client(text),
            "scores": self._parse_scores(text),
            "summary": {},
            "raw_accounts": self._parse_accounts(text),
            "inquiries": [],
            "addresses": [],
            "name_variations": [],
        }

    def _parse_client(self, text):
        client = {"first_name": "", "last_name": "", "report_date": ""}
        m = re.search(r'Prepared\s+for:\s*\n\s*([A-Z][A-Z \-]+)', text)
        if m:
            parts = m.group(1).strip().split()
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = " ".join(parts[1:])
            elif parts:
                client["last_name"] = parts[0]
        m = re.search(r'Date:\s*([A-Za-z]+\s+\d{1,2},?\s+\d{4})', text)
        if m:
            client["report_date"] = m.group(1).strip()
        return client

    def _parse_scores(self, text):
        scores = []
        m = re.search(r'FICO.*?Score\s*8[:\s]*(\d{3})', text)
        if m:
            from classify import score_tier
            v = int(m.group(1))
            scores.append({"bureau": "Equifax", "value": v, "tier": score_tier(v)})
        return scores

    def _parse_accounts(self, text):
        accounts = []
        # Split on creditor blocks: "            CREDITOR NAME\nAddress..."
        # Each account starts with creditor name followed by address and pipe-delimited fields
        section = re.search(r'Credit Accounts\n.*?\n(.*?)(?:Inquiries|Collections|Public Records|$)', text, re.S)
        if not section:
            return accounts
        body = section.group(1)

        # Split on blocks that start with a creditor line (indented, followed by address)
        blocks = re.split(r'\n\s{4,}([A-Z][A-Za-z0-9 /&\.\'\-,]+)\n(?:[A-Z0-9]|PO Box)', body)
        for i in range(1, len(blocks), 2):
            creditor = blocks[i].strip()
            if i + 1 >= len(blocks):
                break
            block = blocks[i + 1]
            if not creditor or len(creditor) < 2:
                continue
            # Skip noise
            if any(k in creditor for k in ["Payment History", "Month History", "Credit Accounts"]):
                continue

            fields = self._extract_fields(block)
            account = self._classify_account(creditor, fields)
            accounts.append(account)
        return accounts

    def _extract_fields(self, block):
        fields = {"bureaus": ["EQF"]}
        # Pipe-delimited fields: "Label:  Value | Label:  Value"
        # Also single-line fields: "Label:  Value"
        pipe_pattern = re.compile(r'([A-Za-z /]+?):\s{1,}(.+?)(?:\s*\||\s*\n|$)')
        for m in pipe_pattern.finditer(block):
            label = m.group(1).strip()
            val = m.group(2).strip()
            if not val or val in ("-", "\u2014"):
                continue
            label_lower = label.lower()
            if "account number" in label_lower:
                fields["account_number"] = val
            elif "balance" == label_lower:
                fields["balance"] = val
            elif label_lower == "owner":
                fields["responsibility"] = val
            elif "credit limit" in label_lower:
                fields["credit_limit"] = val
            elif "high credit" in label_lower:
                fields["high_credit"] = val
            elif "loan/account type" in label_lower or "account type" in label_lower:
                fields["type"] = val
            elif label_lower == "status":
                fields["status"] = val
                fields["payment_status"] = val
                if "agreed" in val.lower() or "current" in val.lower():
                    fields["account_status"] = "Open"
                elif "closed" in val.lower() or "paid" in val.lower():
                    fields["account_status"] = "Closed"
            elif "date opened" in label_lower:
                fields["opened"] = self._convert_eq_date(val)
            elif "date closed" in label_lower:
                fields["date_closed"] = val
                fields["account_status"] = "Closed"
            elif "date of last activity" in label_lower:
                fields["last_activity"] = val
            elif "date reported" in label_lower:
                fields["last_reported"] = self._convert_eq_date(val)
            elif "scheduled payment" in label_lower:
                fields["monthly_payment"] = val
            elif "amount past due" in label_lower:
                fields["past_due"] = val
            elif "charge off amount" in label_lower and val != "$0":
                fields["is_chargeoff"] = True

        # Late days from payment history
        late_values = re.findall(r'\b(30|60|90|120|150|180)\b', block)
        fields["late_days"] = self._max_late_from_history(late_values)

        return fields

    def _convert_eq_date(self, raw):
        """Convert 'MM/DD/YYYY' or 'June 6, 2025' to 'MM/YYYY'."""
        if not raw:
            return ""
        m = re.match(r'(\d{2})/\d{2}/(\d{4})', raw)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        m = re.match(r'([A-Za-z]+)\s+\d{1,2},?\s+(\d{4})', raw)
        if m:
            months = {"january": "01", "february": "02", "march": "03", "april": "04",
                      "may": "05", "june": "06", "july": "07", "august": "08",
                      "september": "09", "october": "10", "november": "11", "december": "12"}
            mm = months.get(m.group(1).lower(), "")
            if mm:
                return f"{mm}/{m.group(2)}"
        return raw
