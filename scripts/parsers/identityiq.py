"""IdentityIQ PDF 3-bureau credit report parser."""
import re
from parsers import register
from parsers.base import CreditReportParser


from classify import score_tier as _score_tier


@register
class IdentityIQParser(CreditReportParser):
    PROVIDER_NAME = "identityiq"
    SUPPORTED_EXTENSIONS = [".pdf", ".html"]

    @classmethod
    def detect(cls, file_path, content):
        content_lower = content.lower()
        if b"identityiq" in content_lower: return 0.95
        if b"myscoreiq" in content_lower and file_path.lower().endswith(".pdf"): return 0.9
        if b"identity iq" in content_lower: return 0.8
        return 0.0

    def parse(self, file_path):
        pages = self._extract_text_pdf(file_path)
        text = "\n".join(pages)
        return {
            "client": self._parse_client(text),
            "scores": self._parse_scores(text),
            "summary": self._parse_summary(text),
            "raw_accounts": self._parse_accounts(text),
            "inquiries": self._parse_inquiries(text),
            "addresses": [],
            "name_variations": [],
        }

    def _parse_client(self, text):
        client = {"first_name": "", "last_name": "", "report_date": "", "reference": ""}

        ref = re.search(r'Reference\s*#[:\s]*(\S+)', text)
        if ref: client["reference"] = ref.group(1)

        date = re.search(r'Report\s*Date[:\s]*([\d/]+)', text)
        if date: client["report_date"] = date.group(1)

        name = re.search(r'Name:\s+(\S+(?:\s+\S+)*?)(?:\s{2,}|\n)', text)
        if name:
            raw = re.sub(r'[\s-]+$', '', name.group(1).strip())
            parts = [p for p in raw.split() if p not in ("-", "\u2014")]
            if len(parts) >= 2:
                client["first_name"] = parts[0]
                client["last_name"] = parts[-1]
            elif parts:
                client["last_name"] = parts[0]

        return client

    def _parse_scores(self, text):
        scores = []
        # Look for "Credit Score:" row with 3 values
        match = re.search(r'Credit\s+Score:\s+(\d{3})\s+(\d{3})\s+(\d{3})', text)
        if match:
            for bureau, val in [("TransUnion", match.group(1)), ("Experian", match.group(2)), ("Equifax", match.group(3))]:
                v = int(val)
                scores.append({"bureau": bureau, "value": v, "tier": _score_tier(v)})
        return scores

    def _parse_summary(self, text):
        summary = {}
        patterns = {
            "total_accounts": r'Total\s+Accounts:\s+(\d+)\s+(\d+)\s+(\d+)',
            "open_accounts": r'Open\s+Accounts:\s+(\d+)\s+(\d+)\s+(\d+)',
            "delinquent": r'Delinquent:\s+(\d+)\s+(\d+)\s+(\d+)',
            "derogatory": r'Derogatory:\s+(\d+)\s+(\d+)\s+(\d+)',
            "inquiries": r'Inquiries\s*\(2\s*years?\):\s+(\d+)\s+(\d+)\s+(\d+)',
        }
        for key, pat in patterns.items():
            m = re.search(pat, text)
            if m:
                summary[key] = {"TU": int(m.group(1)), "EXP": int(m.group(2)), "EQF": int(m.group(3))}
        return summary

    def _parse_accounts(self, text):
        """Split text into account blocks and parse each.

        IdentityIQ PDF structure per account block:
          CREDITOR NAME           (line before bureau header)
          TransUnion Experian Equifax   (bureau header)
          Account #: val1  val2  val3
          Account Type: ...
          ...field rows...
          Two-Year payment history
          Month ...
          Year ...
          TransUnion OK OK ...
          Experian OK OK ...
          Equifax OK OK ...
        """
        accounts = []

        # Find each account block: creditor line followed by bureau header and Account #
        block_pattern = re.compile(
            r'^(.+)\n\s*TransUnion\s+Experian\s+Equifax\s*\n\s*Account\s*#:',
            re.MULTILINE,
        )
        block_starts = list(block_pattern.finditer(text))
        if not block_starts:
            return accounts

        for idx, match in enumerate(block_starts):
            creditor = match.group(1).strip()

            # Skip noise lines (file paths, page headers)
            if "file:///" in creditor or "Credit Report" in creditor:
                continue

            # Block text runs from "Account #:" to the start of the next block (or end)
            acct_offset = match.group().index("Account")
            block_start = match.start() + acct_offset
            if idx + 1 < len(block_starts):
                block_end = block_starts[idx + 1].start()
            else:
                # End at Inquiries section or end of text
                inq = text.find("Inquiries\n", block_start)
                block_end = inq if inq != -1 else len(text)

            block = text[block_start:block_end]

            # Check for original creditor in parentheses
            original_creditor = ""
            oc = re.search(r'\(Original Creditor:\s*(.+?)\)', creditor)
            if oc:
                original_creditor = oc.group(1).strip()
                creditor = re.sub(r'\s*\(Original Creditor:.*?\)', '', creditor).strip()

            fields = self._extract_block_fields(block)
            fields["original_creditor"] = original_creditor

            account = self._classify_account(creditor, fields)
            accounts.append(account)

        return accounts

    def _extract_block_fields(self, block):
        """Extract fields from a text block using regex patterns.

        Each field line has format: "Label: TU_value  EXP_value  EQF_value"
        Values are separated by 2+ spaces.
        """
        fields = {"bureaus": []}

        # 3-column patterns: "Label: TU_val EXP_val EQF_val"
        # Extract account number from "Account #:" line
        acct_match = re.search(r'Account\s*#:\s+(.+?)(?:\n|$)', block)
        if acct_match:
            parts = self._split_3col(acct_match.group(1).strip())
            fields["account_number"] = self._best_value(*parts) if parts else ""
            if len(parts) >= 3:
                fields["bureaus"] = self._bureaus_from_values(parts[0], parts[1], parts[2])

        patterns = {
            "type": r'Account\s+Type:\s+(.+?)(?:\n|$)',
            "account_type_detail": r'Account\s+Type\s*-\s*Detail:\s+(.+?)(?:\n|$)',
            "responsibility": r'Bureau\s+Code:\s+(.+?)(?:\n|$)',
            "status": r'Account\s+Status:\s+(.+?)(?:\n|$)',
            "monthly_payment": r'Monthly\s+Payment:\s+(.+?)(?:\n|$)',
            "opened": r'Date\s+Opened:\s+(.+?)(?:\n|$)',
            "balance": r'Balance:\s+(.+?)(?:\n|$)',
            "high_credit": r'High\s+Credit:\s+(.+?)(?:\n|$)',
            "credit_limit": r'Credit\s+Limit:\s+(.+?)(?:\n|$)',
            "past_due": r'Past\s+Due:\s+(.+?)(?:\n|$)',
            "payment_status": r'Payment\s+Status:\s+(.+?)(?:\n|$)',
            "last_reported": r'Last\s+Reported:\s+(.+?)(?:\n|$)',
            "last_activity": r'Date\s+Last\s+Active:\s+(.+?)(?:\n|$)',
            "comments": r'Comments:\s+(.+?)(?:\n|$)',
        }

        for key, pat in patterns.items():
            m = re.search(pat, block)
            if not m:
                continue
            vals_line = m.group(1).strip()
            parts = self._split_3col(vals_line)
            if len(parts) >= 3:
                tu, exp, eqf = parts[0], parts[1], parts[2]
                if key == "status":
                    fields["status"] = self._best_value(tu, exp, eqf)
                    fields["account_status"] = fields["status"]
                else:
                    fields[key] = self._best_value(tu, exp, eqf)
                if key == "type":
                    fields["bureaus"] = self._bureaus_from_values(tu, exp, eqf)
            elif parts:
                val = parts[0]
                if key == "status":
                    fields["status"] = val
                    fields["account_status"] = val
                else:
                    fields[key] = val

        # Convert date format mm/dd/yyyy -> mm/yyyy
        opened = fields.get("opened", "")
        if opened and re.match(r'\d{2}/\d{2}/\d{4}', opened):
            fields["opened"] = opened[:2] + "/" + opened[6:]

        # Extract payment history late days from bureau rows
        # Look for TransUnion/Experian/Equifax rows with OK/30/60/90/120 values
        history_pattern = re.compile(
            r'^(TransUnion|Experian|Equifax)\s+((?:(?:OK|30|60|90|120|150|CO|C/O|-)\s*)+)$',
            re.MULTILINE,
        )
        late_values = []
        for hm in history_pattern.finditer(block):
            row_vals = hm.group(2).strip().split()
            late_values.extend(row_vals)

        # Fallback: scan the payment history section for any late markers
        if not late_values:
            history_section = re.search(
                r'(?:payment\s+history|Month.*?Year)(.*?)(?:\n[A-Z]{2,}[A-Z /]|\Z)',
                block, re.S | re.I,
            )
            if history_section:
                late_values = re.findall(r'\b(30|60|90|120)\b', history_section.group(1))

        fields["late_days"] = self._max_late_from_history(late_values)

        return fields

    def _parse_inquiries(self, text):
        """Extract inquiries from the Inquiries section of IdentityIQ PDF."""
        inquiries = []
        inq_section = re.search(r'Inquiries\n(.*?)(?:Consumer\s+Statement|$)', text, re.S)
        if not inq_section:
            return inquiries
        section = inq_section.group(1)
        # Pattern: "CREDITOR NAME  MM/DD/YYYY  MM/DD/YYYY  MM/DD/YYYY"
        # or "CREDITOR NAME\nTransUnion Experian Equifax\nMM/DD/YYYY MM/DD/YYYY MM/DD/YYYY"
        for m in re.finditer(r'^([A-Z][A-Z0-9 /&\.\'\-,]+?)\s*\n\s*TransUnion\s+Experian\s+Equifax\s*\n\s*(\S+)\s+(\S+)\s+(\S+)', section, re.M):
            creditor = m.group(1).strip()
            for date, bureau in [(m.group(2), "TU"), (m.group(3), "EXP"), (m.group(4), "EQF")]:
                if date and date not in ("-", "\u2014"):
                    inquiries.append({"creditor": creditor, "date": date, "bureau": bureau})
        return inquiries

    def _split_3col(self, line):
        """Split a 3-bureau value line into [TU, EXP, EQF] parts."""
        parts = re.split(r'\s{2,}', line)
        if len(parts) >= 3:
            return parts[:3]
        if '$' in line:
            parts = re.split(r'\s+(?=\$)', line)
            if len(parts) >= 3:
                return parts[:3]
        date_parts = re.findall(r'\d{2}/\d{2}/\d{4}|\d{2}/\d{4}', line)
        if len(date_parts) >= 3:
            return date_parts[:3]
        simple = line.split()
        if len(simple) >= 3 and all(len(w) < 20 for w in simple):
            return simple[:3]
        return [line]
