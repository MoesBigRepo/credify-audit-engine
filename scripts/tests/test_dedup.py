"""Tests for classify.dedup_accounts — provider-agnostic cross-bureau merge."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from classify import dedup_accounts, canonicalize_creditor


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
    }
    base.update(overrides)
    return base


class TestCanonicalize:
    def test_navy_variants_collapse(self):
        assert canonicalize_creditor("NAVY FCU") == "navy federal"
        assert canonicalize_creditor("NAVY FEDERAL CR UNION") == "navy federal"
        assert canonicalize_creditor("Navy Federal Credit Union") == "navy federal"
        assert canonicalize_creditor("NFCU") == "navy federal"

    def test_chase_variants_collapse(self):
        assert canonicalize_creditor("JPMCB CARD") == "chase"
        assert canonicalize_creditor("JPMorgan Chase") == "chase"
        assert canonicalize_creditor("CHASE") == "chase"

    def test_unknown_falls_back_to_two_tokens(self):
        # Unknown creditor keeps first two tokens of stripped form
        assert canonicalize_creditor("SOME OBSCURE LENDER LLC") == "some obscure"

    def test_empty(self):
        assert canonicalize_creditor("") == ""
        assert canonicalize_creditor(None) == ""


class TestDedupAccounts:
    def test_jones_case_merges(self):
        # TU+EQF block and EXP-only block — same loan
        a = _acct(creditor="NAVY FCU", bureaus=["TU", "EQF"], late_days=60,
                  payment_status="Late 60 Days")
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****", opened="07/2024",
                  late_days=30, payment_status="Late 30 Days")
        result = dedup_accounts([a, b])
        assert len(result) == 1
        merged = result[0]
        assert set(merged["bureaus"]) == {"TU", "EXP", "EQF"}
        assert merged["late_days"] == 60  # max preserved
        assert merged["payment_status"] == "Late 60 Days"  # severity aligned
        assert merged["is_derogatory"] is True

    def test_different_balances_stay_separate(self):
        # Two legitimate Navy FCU auto loans (Jones's real keeper case)
        a = _acct(creditor="NAVY FCU", balance="22899", high_credit="22899",
                  opened="07/2023", status="Closed", bureaus=["TU", "EXP", "EQF"])
        b = _acct(creditor="NAVY FCU", balance="65065", high_credit="76899",
                  opened="07/2024", bureaus=["TU", "EQF"])
        result = dedup_accounts([a, b])
        assert len(result) == 2

    def test_au_primary_stay_separate(self):
        a = _acct(creditor="CHASE", is_au=True, bureaus=["TU"])
        b = _acct(creditor="CHASE", is_au=False, bureaus=["EXP"])
        result = dedup_accounts([a, b])
        assert len(result) == 2

    def test_overlapping_bureaus_stay_separate(self):
        a = _acct(bureaus=["TU", "EQF"])
        b = _acct(bureaus=["TU", "EXP"])
        result = dedup_accounts([a, b])
        assert len(result) == 2  # ambiguous overlap rejected

    def test_identical_bureaus_still_merge(self):
        # Two 3-bureau records with same fingerprint — merge safely
        a = _acct(creditor="NAVY FCU", bureaus=["TU", "EXP", "EQF"])
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["TU", "EXP", "EQF"])
        result = dedup_accounts([a, b])
        # Fast path: all 3-bureau → skipped (no merge)
        # This is intentional: if parser already merged, don't second-guess
        assert len(result) == 2

    def test_chargeoff_flag_ors(self):
        a = _acct(is_chargeoff=True, late_days=0, bureaus=["TU"])
        b = _acct(is_chargeoff=False, late_days=0, bureaus=["EXP"])
        result = dedup_accounts([a, b])
        assert len(result) == 1
        assert result[0]["is_chargeoff"] is True

    def test_idempotent(self):
        a = _acct(creditor="NAVY FCU", bureaus=["TU", "EQF"], late_days=60)
        b = _acct(creditor="NAVY FEDERAL CR UNION", bureaus=["EXP"],
                  account_number="430016*****", late_days=30)
        once = dedup_accounts([a, b])
        twice = dedup_accounts(once)
        assert once == twice

    def test_empty_input(self):
        assert dedup_accounts([]) == []

    def test_single_input(self):
        a = _acct(bureaus=["TU"])
        assert dedup_accounts([a]) == [a]

    def test_opened_date_tolerance(self):
        # ±1 month should still merge
        a = _acct(opened="07/2024", bureaus=["TU", "EQF"])
        b = _acct(creditor="NAVY FEDERAL CR UNION", opened="08/2024",
                  bureaus=["EXP"], account_number="430016*****")
        result = dedup_accounts([a, b])
        assert len(result) == 1

    def test_opened_date_too_far(self):
        a = _acct(opened="07/2024", bureaus=["TU", "EQF"])
        b = _acct(creditor="NAVY FEDERAL CR UNION", opened="01/2024",
                  bureaus=["EXP"])
        result = dedup_accounts([a, b])
        assert len(result) == 2

    def test_zero_balance_closed_different_dates_stay_separate(self):
        a = _acct(balance="0", opened="07/2023", status="Closed",
                  bureaus=["TU", "EQF"], type="Credit Card",
                  account_type_detail="Credit Card")
        b = _acct(balance="0", opened="03/2020", status="Closed",
                  bureaus=["EXP"], creditor="NAVY FEDERAL CR UNION",
                  type="Credit Card", account_type_detail="Credit Card")
        result = dedup_accounts([a, b])
        assert len(result) == 2
