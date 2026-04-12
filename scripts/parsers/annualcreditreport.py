"""AnnualCreditReport.com credit report parser — stub awaiting sample."""
from parsers import register
from parsers.base import CreditReportParser

@register
class AnnualCreditReportParser(CreditReportParser):
    PROVIDER_NAME = "annualcreditreport"
    SUPPORTED_EXTENSIONS = [".pdf"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"annualcreditreport" in c: return 0.9
        if b"annual credit report" in c: return 0.7
        return 0.0

    def parse(self, file_path):
        raise NotImplementedError(f"AnnualCreditReport parser not yet implemented. Provide a sample report. File: {file_path}")
