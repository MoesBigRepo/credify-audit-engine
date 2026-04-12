"""MyFreeScoreNow credit report parser — stub awaiting sample."""
from parsers import register
from parsers.base import CreditReportParser

@register
class MyFreeScoreNowParser(CreditReportParser):
    PROVIDER_NAME = "myfreescorenow"
    SUPPORTED_EXTENSIONS = [".pdf", ".html"]

    @classmethod
    def detect(cls, file_path, content):
        c = content.lower()
        if b"myfreescorenow" in c: return 0.9
        if b"freescorenow" in c: return 0.7
        return 0.0

    def parse(self, file_path):
        raise NotImplementedError(f"MyFreeScoreNow parser not yet implemented. Provide a sample report. File: {file_path}")
