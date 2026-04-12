"""Test cases for FICO score projection model."""
import pytest
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))
from datetime import datetime
from strategize import calc_conservative, calc_optimistic, build_score_projection, _derog_age_months, FICO


def _make_derog(n_collections=0, n_chargeoffs=0, n_late30=0, n_late60=0, n_late90=0, n_late120=0):
    """Build a derog list for testing."""
    derog = []
    for _ in range(n_collections):
        derog.append({"creditor": "COLL", "is_collection": True, "is_chargeoff": False, "late_days": 0, "balance": "$500"})
    for _ in range(n_chargeoffs):
        derog.append({"creditor": "CO", "is_collection": False, "is_chargeoff": True, "late_days": 120, "balance": "$2000"})
    for _ in range(n_late30):
        derog.append({"creditor": "LATE30", "is_collection": False, "is_chargeoff": False, "late_days": 30, "balance": "$0"})
    for _ in range(n_late60):
        derog.append({"creditor": "LATE60", "is_collection": False, "is_chargeoff": False, "late_days": 60, "balance": "$0"})
    for _ in range(n_late90):
        derog.append({"creditor": "LATE90", "is_collection": False, "is_chargeoff": False, "late_days": 90, "balance": "$0"})
    for _ in range(n_late120):
        derog.append({"creditor": "LATE120", "is_collection": False, "is_chargeoff": False, "late_days": 120, "balance": "$0"})
    return derog


def _make_keepers(n, avg_age_months=48, has_revolving=True, has_installment=True, avg_limit=5000):
    """Build keeper accounts for testing."""
    keepers = []
    for i in range(n):
        acct_type = "Credit Card" if (has_revolving and i % 2 == 0) else "Installment Loan"
        if not has_revolving:
            acct_type = "Installment Loan"
        if not has_installment:
            acct_type = "Credit Card"
        keepers.append({
            "name": f"ACCT_{i}",
            "type": acct_type,
            "age_months": avg_age_months,
            "credit_limit": avg_limit if "card" in acct_type.lower() or "revolving" in acct_type.lower() else 0,
            "balance": "0",
            "responsibility": "Individual",
        })
    return keepers


def _make_combo(age1_months=87, limit1=10400, age2_months=471, limit2=14300):
    """Build a tradeline combo for testing."""
    return {
        "pick1": {"bank": "Citi", "limit": limit1, "age_months": age1_months},
        "pick2": {"bank": "Citi", "limit": limit2, "age_months": age2_months},
        "new_aaoa_months": 84,  # legacy field -- should NOT be used for FICO 8
        "new_total_limit": limit1 + limit2,
        "combo_price": 665,
    }


class TestCalcConservative:
    def test_no_derog_returns_base(self):
        assert calc_conservative(700, []) == 700

    def test_single_collection_removal(self):
        derog = _make_derog(n_collections=1)
        result = calc_conservative(580, derog)
        assert 620 <= result <= 660, f"Expected 620-660, got {result}"

    def test_multiple_collections_diminishing(self):
        derog = _make_derog(n_collections=4)
        result = calc_conservative(520, derog)
        assert 620 <= result <= 700, f"Expected 620-700, got {result}"

    def test_max_derog_removal(self):
        derog = _make_derog(n_collections=8, n_chargeoffs=2)
        result = calc_conservative(450, derog)
        assert 640 <= result <= 720, f"Expected 640-720, got {result}"

    def test_ceiling_damping(self):
        """High base score should see compressed gains."""
        derog = _make_derog(n_late30=1)
        result = calc_conservative(750, derog)
        assert result <= 780, f"Expected <=780, got {result}"

    def test_never_exceeds_850(self):
        derog = _make_derog(n_collections=10)
        result = calc_conservative(600, derog)
        assert result <= 850


class TestCalcOptimistic:
    def test_thin_file_with_au_tradelines(self):
        """Leandro scenario: 700 base, 5 accts, <1yr avg, 0 derog, +2 AU."""
        keepers = _make_keepers(5, avg_age_months=6, avg_limit=1000)
        combo = _make_combo()
        clean = [{"balance": "$300", "credit_limit": "$1000"}] * 5
        result = calc_optimistic(700, keepers, combo, clean)
        assert 740 <= result <= 770, f"Expected 740-770, got {result}"

    def test_thin_file_derog_plus_au(self):
        """520 base after derog removal -> 680ish, then +2 AU should push to 680-730."""
        keepers = _make_keepers(3, avg_age_months=6, avg_limit=500)
        combo = _make_combo()
        clean = [{"balance": "$200", "credit_limit": "$500"}] * 3
        result = calc_optimistic(680, keepers, combo, clean)
        assert 700 <= result <= 745, f"Expected 700-745, got {result}"

    def test_thick_file_one_late_removal(self):
        """680 base, 12 accts, 8yr avg, 1 late removed. No tradelines."""
        keepers = _make_keepers(12, avg_age_months=96, avg_limit=8000)
        clean = [{"balance": "$1000", "credit_limit": "$8000"}] * 12
        result = calc_optimistic(700, keepers, None, clean)
        assert 700 <= result <= 730, f"Expected 700-730, got {result}"

    def test_thick_file_clean_minimal_au_impact(self):
        """780 base, 15 accts, 10yr avg, 0 derog. AU should add minimal."""
        keepers = _make_keepers(15, avg_age_months=120, avg_limit=15000)
        combo = _make_combo()
        clean = [{"balance": "$500", "credit_limit": "$15000"}] * 15
        result = calc_optimistic(780, keepers, combo, clean)
        assert 780 <= result <= 800, f"Expected 780-800, got {result}"

    def test_never_exceeds_850(self):
        keepers = _make_keepers(20, avg_age_months=180, avg_limit=20000)
        combo = _make_combo()
        clean = [{"balance": "$0", "credit_limit": "$20000"}] * 20
        result = calc_optimistic(800, keepers, combo, clean)
        assert result <= 850

    def test_no_combo_no_crash(self):
        """combo=None should work fine."""
        keepers = _make_keepers(5, avg_age_months=48, avg_limit=5000)
        clean = [{"balance": "$500", "credit_limit": "$5000"}] * 5
        result = calc_optimistic(650, keepers, None, clean)
        assert 650 <= result <= 720


class TestDerogAgeMonths:
    """Tests for _derog_age_months helper."""

    def test_explicit_age_months_used(self):
        now = datetime.now()
        assert _derog_age_months({"age_months": 36}, now) == 36

    def test_explicit_zero_not_skipped(self):
        """age_months=0 should return 0, not fall through to opened."""
        now = datetime.now()
        assert _derog_age_months({"age_months": 0, "opened": "01/2020"}, now) == 0

    def test_opened_date_parsed(self):
        now = datetime(2026, 4, 7)
        result = _derog_age_months({"opened": "04/2024"}, now)
        assert 23 <= result <= 25  # ~24 months

    def test_no_data_returns_zero(self):
        now = datetime.now()
        assert _derog_age_months({}, now) == 0

    def test_invalid_opened_returns_zero(self):
        now = datetime.now()
        assert _derog_age_months({"opened": "garbage"}, now) == 0


class TestCalcConservativeAgeDecay:
    """Tests that old derogs recover fewer points than recent ones."""

    def test_recent_derog_full_impact(self):
        derog = [{"is_collection": True, "late_days": 0, "age_months": 6}]
        result = calc_conservative(600, derog)
        # Recent collection: full 45pts + 15 shift = 60, no damping at 600
        assert result >= 650

    def test_old_derog_minimal_impact(self):
        derog = [{"is_collection": True, "late_days": 0, "age_months": 73}]
        result = calc_conservative(600, derog)
        # 6yr-old: 45 * 0.10 = 4.5pts + 15 shift = 19.5
        assert result <= 625

    def test_old_vs_recent_gap(self):
        recent = [{"is_collection": True, "late_days": 0, "age_months": 3}]
        old = [{"is_collection": True, "late_days": 0, "age_months": 73}]
        recent_result = calc_conservative(600, recent)
        old_result = calc_conservative(600, old)
        assert recent_result > old_result + 20  # significant gap


class TestBuildScoreProjection:
    """Integration tests for build_score_projection."""

    def test_optimistic_never_below_conservative(self):
        scores = [{"value": 520, "bureau": "EXP", "tier": "Poor", "color": "red"}]
        derog = _make_derog(n_collections=4)
        keepers = _make_keepers(3, avg_age_months=6, avg_limit=500)
        clean = [{"balance": "$200", "credit_limit": "$500"}] * 3
        cons, opt, tier = build_score_projection(scores, derog, keepers, None, clean)
        assert opt >= cons

    def test_base_from_min_score(self):
        scores = [
            {"value": 700, "bureau": "TU", "tier": "Good", "color": "green"},
            {"value": 650, "bureau": "EXP", "tier": "Fair", "color": "amber"},
        ]
        keepers = _make_keepers(5, avg_age_months=48, avg_limit=5000)
        clean = [{"balance": "$500", "credit_limit": "$5000"}] * 5
        cons, opt, tier = build_score_projection(scores, [], keepers, None, clean)
        # Base should be 650 (minimum), so conservative = 650 (no derog)
        assert cons == 650

    def test_fallback_base_500(self):
        scores = []
        keepers = _make_keepers(5, avg_age_months=48, avg_limit=5000)
        clean = [{"balance": "$500", "credit_limit": "$5000"}] * 5
        cons, opt, tier = build_score_projection(scores, [], keepers, None, clean)
        assert cons == 500

    def test_non_numeric_scores_ignored(self):
        scores = [
            {"value": "N/A", "bureau": "TU", "tier": "", "color": "red"},
            {"value": 700, "bureau": "EXP", "tier": "Good", "color": "green"},
        ]
        keepers = _make_keepers(5, avg_age_months=48, avg_limit=5000)
        clean = [{"balance": "$500", "credit_limit": "$5000"}] * 5
        cons, opt, tier = build_score_projection(scores, [], keepers, None, clean)
        assert cons == 700

    def test_returns_tier_label(self):
        scores = [{"value": 750, "bureau": "EXP", "tier": "Good", "color": "green"}]
        keepers = _make_keepers(10, avg_age_months=96, avg_limit=10000)
        clean = [{"balance": "$500", "credit_limit": "$10000"}] * 10
        cons, opt, tier = build_score_projection(scores, [], keepers, None, clean)
        assert isinstance(tier, str)
        assert len(tier) > 0


class TestFicoConfig:
    """Verify FICO config dict is properly structured."""

    def test_config_has_all_keys(self):
        required = ["impact", "age_decay", "diminishing", "ceiling", "au_thin", "util_major"]
        for key in required:
            assert key in FICO, f"Missing FICO config key: {key}"

    def test_diminishing_returns_length(self):
        assert len(FICO["diminishing"]) == 8

    def test_ceiling_is_850(self):
        assert FICO["ceiling"] == 850


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
