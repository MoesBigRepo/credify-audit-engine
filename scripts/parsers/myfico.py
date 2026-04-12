"""MyFICO credit report parser — stub awaiting sample."""
from parsers import register
from parsers.base import CreditReportParser

@register
class MyFICOParser(CreditReportParser):
    PROVIDER_NAME = "myfico"
    SUPPORTED_EXTENSIONS = [".pdf", ".html"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"myfico" in c: return 0.9
        return 0.0

    def parse(self, file_path):
        raise NotImplementedError(f"MyFICO parser not yet implemented. Provide a sample report. File: {file_path}")
