"""MyScoreIQ HTML 3-bureau credit report parser."""
import re
from parsers import register
from parsers.base import CreditReportParser


from classify import score_tier as _score_tier


def _cell_text(td):
    """Get clean text from a table cell, stripping Angular noise."""
    if not td: return ""
    return re.sub(r'\s+', ' ', td.get_text(separator=" ", strip=True)).strip()


def _row_values(row):
    """Extract (label, tu, exp, eqf) from a 4-column table row."""
    cells = row.find_all("td")
    if len(cells) < 4: return None, None, None, None
    return _cell_text(cells[0]), _cell_text(cells[1]), _cell_text(cells[2]), _cell_text(cells[3])


@register
class MyScoreIQParser(CreditReportParser):
    PROVIDER_NAME = "myscoreiq"
    SUPPORTED_EXTENSIONS = [".html"]

    @classmethod
    def detect(cls, file_path, content):
        if b"myscoreiq.com" in content.lower(): return 0.95
        if b"myscoreiq" in content.lower(): return 0.8
        return 0.0

    def parse(self, file_path):
        soup = self._extract_html(file_path)
        return {
            "client": self._parse_client(soup),
            "scores": self._parse_scores(soup),
            "summary": self._parse_summary(soup),
            "raw_accounts": self._parse_accounts(soup),
            "inquiries": self._parse_inquiries(soup),
            "addresses": self._parse_addresses(soup),
            "name_variations": self._parse_name_variations(soup),
        }

    def _parse_client(self, soup):
        """Extract client name, DOB, report date, reference."""
        client = {"first_name": "", "last_name": "", "report_date": "", "reference": ""}

        # Reference # and report date from reportTop table
        top = soup.find("table", class_="reportTop_content")
        if top:
            cells = top.find_all("td")
            for i, c in enumerate(cells):
                txt = _cell_text(c)
                if "Reference" in txt and i + 1 < len(cells):
                    client["reference"] = _cell_text(cells[i + 1])
            # Report date - find the ng element with date
            date_ng = top.find("ng", class_="ng-binding")
            if date_ng:
                client["report_date"] = date_ng.get_text(strip=True)

        # Name from Personal Information section - find the Name: row
        pi_tables = soup.find_all("table", class_="rpt_table4column")
        for tbl in pi_tables:
            for row in tbl.find_all("tr"):
                label, tu, exp, eqf = _row_values(row)
                if not label: continue
                if label == "Name:":
                    name = self._best_value(eqf, tu, exp)
                    name = re.sub(r'[\s-]+$', '', name)  # strip trailing dashes/whitespace
                    parts = [p for p in name.split() if p not in ("-", "—")]
                    if len(parts) >= 2:
                        client["first_name"] = parts[0]
                        client["last_name"] = parts[-1]
                    elif parts:
                        client["last_name"] = parts[0]
                    break
            if client["last_name"]:
                break

        return client

    def _parse_scores(self, soup):
        """Extract FICO scores for TU, EXP, EQF."""
        scores = []
        score_section = soup.find("div", id="CreditScore")
        if not score_section: return scores

        table = score_section.find_next("table", class_="rpt_table4column")
        if not table: return scores

        for row in table.find_all("tr"):
            label, tu, exp, eqf = _row_values(row)
            if not label: continue
            if "Score 8:" in label and "Scale" not in label and "Auto" not in label and "Bankcard" not in label:
                for bureau, val in [("TransUnion", tu), ("Experian", exp), ("Equifax", eqf)]:
                    try:
                        v = int(val.strip())
                        scores.append({"bureau": bureau, "value": v, "tier": _score_tier(v)})
                    except (ValueError, AttributeError):
                        pass
                break
        return scores

    def _parse_summary(self, soup):
        """Extract summary stats per bureau."""
        summary = {}
        section = soup.find("div", id="Summary")
        if not section: return summary

        table = section.find_next("table", class_="rpt_table4column")
        if not table: return summary

        field_map = {
            "Total Accounts:": "total_accounts",
            "Open Accounts:": "open_accounts",
            "Delinquent:": "delinquent",
            "Derogatory:": "derogatory",
            "Inquiries(2 years):": "inquiries",
        }

        for row in table.find_all("tr"):
            label, tu, exp, eqf = _row_values(row)
            if not label: continue
            key = field_map.get(label.strip())
            if not key: continue
            try:
                summary[key] = {
                    "TU": int(tu) if tu and tu != "-" else 0,
                    "EXP": int(exp) if exp and exp != "-" else 0,
                    "EQF": int(eqf) if eqf and eqf != "-" else 0,
                }
            except ValueError:
                pass
        return summary

    def _parse_accounts(self, soup):
        """Extract all accounts from Account History section."""
        accounts = []
        headers = soup.find_all("div", class_="sub_header")

        for hdr in headers:
            creditor = hdr.get_text(strip=True)
            if not creditor or creditor in ("Customer Statement", "Personal Information", "Score Factors"):
                continue

            # Check for original creditor
            original_creditor = ""
            oc_match = re.search(r'\(Original Creditor:\s*(.+?)\)', creditor)
            if oc_match:
                original_creditor = oc_match.group(1).strip()
                creditor = re.sub(r'\s*\(Original Creditor:.*?\)', '', creditor).strip()

            table = hdr.find_next("table", class_="rpt_table4column")
            if not table: continue

            fields = self._extract_account_fields(table)
            fields["original_creditor"] = original_creditor

            # Payment history - find the history table after this account
            late_days = self._extract_late_days(hdr)
            fields["late_days"] = late_days

            # Determine bureaus from account # row (most reliable indicator)
            account = self._classify_account(creditor, fields)
            accounts.append(account)

        return accounts

    def _extract_account_fields(self, table):
        """Extract field values from a 4-column account table."""
        fields = {"bureaus": []}
        field_map = {
            "Account Type:": "type",
            "Account Type - Detail:": "account_type_detail",
            "Bureau Code:": "responsibility",
            "Account Status:": ("status", "account_status"),
            "Monthly Payment:": "monthly_payment",
            "Date Opened:": "opened",
            "Balance:": "balance",
            "High Credit:": "high_credit",
            "Credit Limit:": "credit_limit",
            "Past Due:": "past_due",
            "Payment Status:": "payment_status",
            "Last Reported:": "last_reported",
            "Date Last Active:": "last_activity",
            "Comments:": "comments",
        }

        bureaus_determined = False
        for row in table.find_all("tr"):
            label, tu, exp, eqf = _row_values(row)
            if not label: continue
            label = label.strip()

            # Use Account # row to determine bureaus AND capture account number
            if label == "Account #:" and not bureaus_determined:
                fields["bureaus"] = self._bureaus_from_values(tu, exp, eqf)
                fields["account_number"] = self._best_value(tu, exp, eqf)
                bureaus_determined = True
                continue

            mapping = field_map.get(label)
            if not mapping: continue

            best = self._best_value(tu, exp, eqf)
            if isinstance(mapping, tuple):
                for key in mapping:
                    fields[key] = best
            else:
                fields[mapping] = best

            # For payment_status, prefer the most descriptive one
            if label == "Payment Status:":
                longest = max([tu or "", exp or "", eqf or ""], key=len)
                if longest.strip() and longest.strip() != "-":
                    fields["payment_status"] = longest.strip()

        # Convert opened date format MM/DD/YYYY to MM/YYYY
        opened = fields.get("opened", "")
        if opened and re.match(r'\d{2}/\d{2}/\d{4}', opened):
            fields["opened"] = opened[:2] + "/" + opened[6:]

        return fields

    def _extract_late_days(self, header_div):
        """Extract max late days from payment history grid after an account."""
        history_table = header_div.find_next("table", class_="addr_hsrty")
        if not history_table: return 0

        late_values = []
        for row in history_table.find_all("tr"):
            cells = row.find_all("td")
            for cell in cells:
                css = cell.get("class", [])
                text = cell.get_text(strip=True)
                # Check CSS classes first (more reliable)
                for cls in css:
                    if "hstry-30" in cls: late_values.append("30")
                    elif "hstry-60" in cls: late_values.append("60")
                    elif "hstry-90" in cls: late_values.append("90")
                    elif "hstry-120" in cls: late_values.append("120")
                # Also check text content
                if text in ("30", "60", "90", "120"):
                    late_values.append(text)

        return self._max_late_from_history(late_values)

    def _parse_inquiries(self, soup):
        """Extract hard inquiries from the Inquiries section."""
        inquiries = []
        section = soup.find("div", id="Inquiries")
        if not section:
            return inquiries
        table = section.find("table", class_="rpt_content_table")
        if not table:
            return inquiries
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            creditor = _cell_text(cells[0])
            date = _cell_text(cells[2])
            bureau = _cell_text(cells[3])
            if creditor and date:
                inquiries.append({"creditor": creditor, "date": date, "bureau": bureau})
        return inquiries

    def _parse_addresses(self, soup):
        """Extract current and previous addresses from Personal Information."""
        addresses = []
        seen = set()
        pi_tables = soup.find_all("table", class_="rpt_table4column")
        for tbl in pi_tables:
            for row in tbl.find_all("tr"):
                label, tu, exp, eqf = _row_values(row)
                if not label:
                    continue
                if "Current Address" in label or "Previous Address" in label:
                    addr_type = "current" if "Current" in label else "previous"
                    cells = row.find_all("td")
                    for col_idx, bureau in [(1, "TU"), (2, "EXP"), (3, "EQF")]:
                        if col_idx >= len(cells):
                            continue
                        # Each ng-repeat is one address entry
                        containers = cells[col_idx].find_all("ng-repeat") or [cells[col_idx]]
                        for container in containers:
                            parts = []
                            for ng in container.find_all("ng-if"):
                                t = ng.get_text(strip=True)
                                if t and not re.match(r'^\d{2}/\d{4}$', t):
                                    parts.append(t)
                            addr = re.sub(r'\s+', ' ', ', '.join(parts)).strip()
                            addr = re.sub(r'\s*-\s*$', '', addr)
                            if addr and addr not in seen:
                                seen.add(addr)
                                addresses.append({"address": addr, "type": addr_type, "bureaus": [bureau]})
        return addresses

    def _parse_name_variations(self, soup):
        """Extract 'Also Known As' name variations from Personal Information."""
        variations = []
        seen = set()
        pi_tables = soup.find_all("table", class_="rpt_table4column")
        for tbl in pi_tables:
            for row in tbl.find_all("tr"):
                label = _cell_text(row.find("td"))
                if not label or "Also Known As" not in label:
                    continue
                cells = row.find_all("td")
                for col_idx, bureau in [(1, "TU"), (2, "EXP"), (3, "EQF")]:
                    if col_idx >= len(cells):
                        continue
                    for div in cells[col_idx].find_all("div"):
                        name = re.sub(r'\s+', ' ', div.get_text(separator=" ", strip=True)).strip()
                        name = re.sub(r'[\s-]+$', '', name)
                        if name and name not in ("-", "\u2014") and name not in seen:
                            seen.add(name)
                            variations.append({"name": name, "bureaus": [bureau]})
        return variations
