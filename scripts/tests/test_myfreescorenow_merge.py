"""Tests for MyFreeScoreNowParser._merge_continuation_blocks.

The PDF physically renders a single tradeline as two sequential Account #
blocks when one bureau reports a different creditor-display name
(Experian: 'NAVY FEDERAL CR UNION' vs TU/EQF: 'NAVY FCU'). The parser
emits both blocks as distinct raw_accounts; the merge pass collapses them.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from parsers.myfreescorenow import MyFreeScoreNowParser


def _acct(**overrides):
    base = {
        "creditor": "NAVY FCU",
        "type": "Auto Loan",
        "account_type_detail": "Auto Loan",
        "responsibility": "Individual",
        "status": "Open",
        "account_status": "Open",
        "opened": "07/2024",
        "account_number": "430016********",
        "balance": "65065",
        "credit_limit": "0",
        "high_credit": "76899",
        "past_due": "0",
        "monthly_payment": "1277",
        "payment_status": "Late 60 Days",
        "last_reported": "",
        "last_activity": "",
        "bureaus": ["TU", "EQF"],
        "late_days": 60,
        "is_derogatory": True,
        "is_chargeoff": False,
        "is_collection": False,
        "is_repo": False,
        "is_bankruptcy": False,
        "is_au": False,
        "comments": "",
        "original_creditor": "",
        "details": "",
    }
    base.update(overrides)
    return base


class TestMergeContinuationBlocks:
    def setup_method(self):
        self.p = MyFreeScoreNowParser()

    def test_jones_case_merges(self):
        # Block #26: TU+EQF, NAVY FCU, "430016********"
        a = _acct(creditor="NAVY FCU", bureaus=["TU", "EQF"],
                  account_number="430016********",
                  opened="07/2024", late_days=60,
                  payment_status="Late 60 Days")
        # Block #27: EXP only, NAVY FEDERAL CR UNION, "430016*****"
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****",
                  opened="07/2024", late_days=30,
                  payment_status="Late 30 Days")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 1
        merged = result[0]
        assert set(merged["bureaus"]) == {"TU", "EXP", "EQF"}
        assert merged["late_days"] == 60  # max preserved
        assert merged["payment_status"] == "Late 60 Days"

    def test_overlapping_bureaus_stay_separate(self):
        a = _acct(bureaus=["TU", "EQF"], account_number="430016********")
        b = _acct(bureaus=["TU", "EXP"], account_number="430016*****")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_same_single_bureau_stay_separate(self):
        a = _acct(bureaus=["TU"], account_number="430016********")
        b = _acct(bureaus=["TU"], account_number="430016*****")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_different_high_credit_stay_separate(self):
        # Jones's two legitimate Navy FCU auto loans
        a = _acct(creditor="NAVY FCU", bureaus=["TU", "EXP", "EQF"],
                  account_number="406095****",
                  balance="22899", high_credit="22899",
                  opened="07/2023", status="Closed")
        b = _acct(creditor="NAVY FCU", bureaus=["TU", "EQF"],
                  account_number="430016********",
                  balance="65065", high_credit="76899",
                  opened="07/2024")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_account_prefix_mismatch_stay_separate(self):
        a = _acct(bureaus=["TU", "EQF"], account_number="430016********")
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="900099*****")  # different prefix
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_opened_too_far_apart_stay_separate(self):
        a = _acct(bureaus=["TU", "EQF"], opened="07/2024",
                  account_number="430016********")
        b = _acct(bureaus=["EXP"], opened="01/2024",
                  account_number="430016*****")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_three_bureau_blocks_passthrough(self):
        # Already-merged records should not re-merge
        a = _acct(bureaus=["TU", "EXP", "EQF"])
        b = _acct(creditor="NAVY FEDERAL CR UNION",
                  bureaus=["TU", "EXP", "EQF"])
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 2

    def test_idempotent(self):
        a = _acct(bureaus=["TU", "EQF"], account_number="430016********",
                  late_days=60)
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****", late_days=30)
        once = self.p._merge_continuation_blocks([a, b])
        twice = self.p._merge_continuation_blocks(once)
        assert once == twice

    def test_empty_and_single(self):
        assert self.p._merge_continuation_blocks([]) == []
        a = _acct()
        assert self.p._merge_continuation_blocks([a]) == [a]

    def test_chargeoff_flag_ors(self):
        a = _acct(bureaus=["TU", "EQF"], account_number="430016********",
                  is_chargeoff=True)
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****", is_chargeoff=False)
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 1
        assert result[0]["is_chargeoff"] is True

    def test_account_number_prefers_longer(self):
        a = _acct(bureaus=["TU", "EQF"], account_number="430016********")
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****")
        result = self.p._merge_continuation_blocks([a, b])
        assert len(result) == 1
        # Longer string retained
        assert result[0]["account_number"] == "430016********"

    def test_digit_prefix_helper(self):
        assert self.p._digit_prefix("430016********") == "430016"
        assert self.p._digit_prefix("****") == ""
        assert self.p._digit_prefix("") == ""
        assert self.p._digit_prefix("123-45-6789") == "123"
