"""ChexSystems / FTC Identity Theft Report parser — minimal handler."""
import re
from parsers import register
from parsers.base import CreditReportParser


@register
class ChexSystemsParser(CreditReportParser):
    PROVIDER_NAME = "chexsystems"
    SUPPORTED_EXTENSIONS = [".pdf"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"chexsystems" in c: return 0.9
        if b"identity theft" in c and b"ftc report" in c: return 0.7
        if b"victim of identity theft" in c and b"accounts affected" in c: return 0.65
        return 0.0

    def parse(self, file_path):
        pages = self._extract_text_pdf(file_path)
        text = "\n".join(pages)
        client = {"first_name": "", "last_name": ""}
        # Try to extract name from "Contact Information" section
        m = re.search(r'(?:Name|Contact)\s*(?:Information)?\s*\n\s*([A-Z][a-z]+ [A-Z][a-z]+)', text)
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
