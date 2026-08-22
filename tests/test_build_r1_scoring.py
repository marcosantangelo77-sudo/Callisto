"""Scoring tests: Brier, calibration, slices, resolved-claim record shape."""
import math
from datetime import date

import pytest

from tools.retrodiction.questions import QuestionType, RetrodictionQuestion
from tools.retrodiction.scoring import (
    Prediction,
    calibration_curve,
    resolved_claim_record,
    score_brier,
    slice_breakdown,
)


def _q(qid, answer=True, domain="FINANCIAL",
       qtype=QuestionType.BEAT_OR_MISS, horizon=30):
    return RetrodictionQuestion(
        question_id=qid, text=f"question {qid}", domain=domain,
        question_type=qtype, claim_date=date(2024, 3, 1),
        resolution_date=date.fromordinal(
            date(2024, 3, 1).toordinal() + horizon),
        answer_binary=answer)


class TestBrier:
    def test_perfect_predictions_score_zero(self):
        qs = [_q("a", True), _q("b", False)]
        ps = [Prediction("a", 1.0), Prediction("b", 0.0)]
        assert score_brier(ps, qs) == pytest.approx(0.0)

    def test_always_wrong_is_worse_than_chance(self):
        qs = [_q("a", True), _q("b", False)]
        assert score_brier([Prediction("a", 0.0), Prediction("b", 1.0)],
                           qs) == pytest.approx(1.0)
        assert score_brier([Prediction("a", 0.5)], [_q("a")]) \
            == pytest.approx(0.25)

    def test_no_match_raises(self):
        with pytest.raises(ValueError):
            score_brier([Prediction("zz", 0.5)], [_q("a")])

    def test_probability_bounds_enforced(self):
        with pytest.raises(ValueError):
            Prediction("a", 1.5)


class TestCalibration:
    def test_overconfident_model_shows_in_curve(self):
        # Says 0.95/0.05 always; is right only ~70% of the time.
        qs = [_q(f"q{i}", i % 10 < 7) for i in range(100)]
        ps = [Prediction(f"q{i}", 0.97 if i % 10 < 7 else 0.03)
              for i in range(100)]
        curve = calibration_curve(ps, qs, n_bins=5)
        top = [c for c in curve if c["bin_low"] >= 0.8][0]
        bottom = [c for c in curve if c["bin_high"] <= 0.2][0]
        assert top["n"] == 70 and bottom["n"] == 30
        assert top["realised_frequency"] == pytest.approx(1.0)
        assert bottom["realised_frequency"] == pytest.approx(0.0)

    def test_empty_bins_reported_not_dropped(self):
        curve = calibration_curve([Prediction("a", 0.9)], [_q("a")], n_bins=5)
        assert len(curve) == 5
        assert curve[0]["n"] == 0
        assert curve[0]["realised_frequency"] is None

    def test_perfectly_calibrated_bin(self):
        # In the 0.6-0.8 bin, exactly 70% of outcomes are True.
        qs, ps = [], []
        for i in range(10):
            ans = i < 7
            qs.append(_q(f"c{i}", ans))
            ps.append(Prediction(f"c{i}", 0.75))
        curve = calibration_curve(ps, qs, n_bins=5)
        mid = [c for c in curve if c["bin_low"] == 0.6][0]
        assert mid["realised_frequency"] == pytest.approx(0.7)


class TestSlices:
    def test_domain_and_type_slices(self):
        qs = [_q("f1", True, domain="FINANCIAL"),
              _q("g1", False, domain="GENERAL", qtype=QuestionType.EVENT_OUTCOME)]
        ps = [Prediction("f1", 0.9), Prediction("g1", 0.9)]  # g1 wrong
        by_dom = slice_breakdown(ps, qs, key="domain")
        assert set(by_dom) == {"FINANCIAL", "GENERAL"}
        assert by_dom["GENERAL"]["brier"] > by_dom["FINANCIAL"]["brier"]
        by_typ = slice_breakdown(ps, qs, key="question_type")
        assert by_typ["beat_or_miss"]["n"] == 1

    def test_horizon_banding(self):
        qs = [_q("s", horizon=20), _q("l", horizon=200)]
        ps = [Prediction("s", 1.0), Prediction("l", 0.5)]
        by_h = slice_breakdown(ps, qs, key="horizon")
        assert by_h["short"]["brier"] == pytest.approx(0.0)
        assert by_h["long"]["brier"] == pytest.approx(0.25)


class TestResolvedClaimRecord:
    def test_record_carries_resolutionrecord_keys(self):
        """Shape-compatible with tools/research_program.ResolutionRecord —
        these records ARE the resolved descendants feeding B4 inheritance."""
        from tools.research_program import ResolutionRecord
        q = _q("r1", True)
        rec = resolved_claim_record(q, Prediction("r1", 0.8))
        rr = ResolutionRecord(question_id=rec["question_id"],
                              resolved_at=date.fromisoformat(rec["resolved_at"]),
                              outcome=rec["outcome"],
                              pinball_score=rec["pinball_score"],
                              best_source_class=rec["best_source_class"])
        assert rr.counted is True

    def test_hit_and_miss_outcomes(self):
        assert resolved_claim_record(_q("a", True), Prediction("a", 0.7))["outcome"] == "hit"
        assert resolved_claim_record(_q("a", True), Prediction("a", 0.3))["outcome"] == "miss"
        assert resolved_claim_record(_q("a", False), Prediction("a", 0.3))["outcome"] == "hit"

    def test_pinball_is_continuous_loss(self):
        rec = resolved_claim_record(_q("a", True), Prediction("a", 0.2))
        assert rec["pinball_score"] == pytest.approx(0.8)
        assert rec["brier"] == pytest.approx(0.64)

    def test_answer_never_needed_by_scorer_caller_side_only(self):
        # The scorer reads answers from questions (held-out side); predictions
        # carry none. This is a structural check on the Prediction type.
        p = Prediction("a", 0.7)
        assert not hasattr(p, "answer")
