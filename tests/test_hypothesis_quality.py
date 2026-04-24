"""Tests for tools.hypothesis_quality — the hypothesis-generation quality gate.

Validates:
  - Vague hypotheses (banned phrasing, no numeric threshold) are REJECTED
  - Under-specified hypotheses (missing sport/market/direction/sample/etc.)
    are REJECTED
  - Semantic duplicates (cosine sim >= 0.88 vs prior corpus) are REJECTED
  - Well-specified hypotheses are ACCEPTED
  - Rolling metrics record accepted vs rejected with reason histogram
  - /system/full-status accessor surfaces the snapshot
"""

from __future__ import annotations

import pytest

from tools.hypothesis_quality import (
    hypothesis_quality_check,
    check_schema,
    check_semantic_duplicate,
    get_metrics,
    DUPLICATE_SIM,
    MIN_SAMPLE,
    MAX_PVALUE,
    RejectReason,
)


GOOD_HYPOTHESIS = {
    "name": "mlb_home_dog_plus3p5_day_after_loss",
    "thesis": (
        "MLB home underdogs of +3.5 runs or better after a day-game loss "
        "cover the runline 55% of the time across 2024-2025 (n>=250), "
        "versus an implied 48%. Expected edge is 3.5% on DraftKings, "
        "tested via a one-sided binomial test at p<=0.05."
    ),
    "sport": "baseball_mlb",
    "market_type": "spreads",
    "direction": "home",
    "edge_threshold": 0.035,
    "min_sample_size": 250,
    "significance_level": 0.05,
    "stat_test": "binomial",
    "model_config": {
        "type": "consensus_devig",
        "devig_method": "power",
        "target_book": "draftkings",
        "consensus_min_books": 3,
        "context_factors": ["home_underdog", "day_after_loss", "runline_gte_3_5"],
    },
}


@pytest.fixture(autouse=True)
def reset_metrics():
    get_metrics().reset()
    yield
    get_metrics().reset()


class TestSchemaChecks:
    def test_good_hypothesis_accepted(self):
        res = check_schema(GOOD_HYPOTHESIS)
        assert res.accepted is True, f"unexpected reasons: {res.reasons}"

    def test_vague_hypothesis_rejected_banned_phrase(self):
        h = dict(GOOD_HYPOTHESIS)
        h["thesis"] = (
            "Home underdogs usually tend to cover — this is a gut feeling "
            "about 55% cover rate. They are a lock in 2024-2025."
        )
        res = check_schema(h)
        assert res.accepted is False
        assert any(r.startswith(RejectReason.BANNED_PHRASE) for r in res.reasons)

    def test_vague_hypothesis_rejected_no_numeric_threshold(self):
        h = dict(GOOD_HYPOTHESIS)
        h["thesis"] = (
            "Home underdogs cover more than expected after losses. "
            "They are a valuable bet on DraftKings in general situations."
        )
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.UNQUANTIFIED_THESIS in res.reasons

    def test_underspecified_missing_sport(self):
        h = dict(GOOD_HYPOTHESIS)
        h["sport"] = ""
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.INVALID_SPORT in res.reasons

    def test_underspecified_invalid_sport(self):
        h = dict(GOOD_HYPOTHESIS)
        h["sport"] = "quidditch_worldcup"
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.INVALID_SPORT in res.reasons

    def test_underspecified_missing_market(self):
        h = dict(GOOD_HYPOTHESIS)
        h["market_type"] = "unknown_market"
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.INVALID_MARKET in res.reasons

    def test_underspecified_missing_direction(self):
        h = dict(GOOD_HYPOTHESIS)
        h["direction"] = ""
        h["model_config"] = dict(h["model_config"])
        h["model_config"].pop("side_filter", None)
        h["thesis"] = (
            "MLB spreads of +3.5 runs in the first inning at parks with "
            "park factor above 105 diverge from implied by 3.5% at n=250 "
            "with binomial test at p<=0.05."
        )
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.MISSING_DIRECTION in res.reasons

    def test_underspecified_missing_conditions(self):
        h = dict(GOOD_HYPOTHESIS)
        h["model_config"] = {"type": "consensus_devig"}
        h.pop("variables", None)
        h.pop("cohort_filter", None)
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.MISSING_CONDITIONS in res.reasons

    def test_underspecified_sample_too_small(self):
        h = dict(GOOD_HYPOTHESIS)
        h["min_sample_size"] = 10
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.SAMPLE_TOO_SMALL in res.reasons

    def test_underspecified_pvalue_too_loose(self):
        h = dict(GOOD_HYPOTHESIS)
        h["significance_level"] = 0.5
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.PVALUE_TOO_LOOSE in res.reasons

    def test_underspecified_edge_out_of_range(self):
        h = dict(GOOD_HYPOTHESIS)
        h["edge_threshold"] = 0.75
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.EDGE_OUT_OF_RANGE in res.reasons

    def test_underspecified_edge_too_low(self):
        h = dict(GOOD_HYPOTHESIS)
        h["edge_threshold"] = 0.0001
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.EDGE_OUT_OF_RANGE in res.reasons

    def test_thesis_too_short(self):
        h = dict(GOOD_HYPOTHESIS)
        h["thesis"] = "Short thesis 55%."
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.THESIS_TOO_SHORT in res.reasons

    def test_stat_test_defaulted_for_spreads(self):
        h = dict(GOOD_HYPOTHESIS)
        h.pop("stat_test", None)
        h["model_config"] = dict(h["model_config"])
        h["model_config"].pop("stat_test", None)
        res = check_schema(h)
        assert res.accepted is True
        assert res.normalized["stat_test"] == "binomial"

    def test_stat_test_invalid(self):
        h = dict(GOOD_HYPOTHESIS)
        h["stat_test"] = "crystal_ball"
        h["market_type"] = "player_points"
        h["model_config"] = dict(h["model_config"])
        h["model_config"]["stat_test"] = "crystal_ball"
        res = check_schema(h)
        assert res.accepted is False
        assert RejectReason.INVALID_STAT_TEST in res.reasons


class TestSemanticDedup:
    def test_duplicate_rejected(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        prior = [[1.0, 0.0, 0.0, 0.0]]
        is_dup, sim, idx = check_semantic_duplicate(emb, prior)
        assert is_dup is True
        assert sim >= DUPLICATE_SIM
        assert idx == 0

    def test_orthogonal_not_duplicate(self):
        emb = [1.0, 0.0, 0.0, 0.0]
        prior = [[0.0, 1.0, 0.0, 0.0]]
        is_dup, sim, idx = check_semantic_duplicate(emb, prior)
        assert is_dup is False
        assert sim < DUPLICATE_SIM

    def test_empty_priors_pass(self):
        emb = [1.0, 0.0, 0.0]
        is_dup, sim, idx = check_semantic_duplicate(emb, [])
        assert is_dup is False
        assert sim == 0.0
        assert idx == -1

    def test_quality_check_combined_rejects_duplicate(self):
        emb = [1.0, 0.0, 0.0]
        prior_embs = [[1.0, 0.0, 0.0]]
        res = hypothesis_quality_check(
            GOOD_HYPOTHESIS,
            candidate_emb=emb,
            prior_embs=prior_embs,
            record_metric=False,
        )
        assert res.accepted is False
        assert any(r.startswith(RejectReason.DUPLICATE_SEMANTIC)
                   for r in res.reasons)


class TestMetrics:
    def test_accepted_recorded(self):
        hypothesis_quality_check(GOOD_HYPOTHESIS)
        snap = get_metrics().snapshot()
        assert snap["accepted_in_window"] == 1
        assert snap["rejected_in_window"] == 0
        assert snap["rejection_rate"] == 0.0

    def test_rejected_recorded_with_reason(self):
        h = dict(GOOD_HYPOTHESIS)
        h["sport"] = ""
        # Keep thesis long enough so INVALID_SPORT is the primary reason.
        h["thesis"] = (
            "This is a sufficiently long thesis with a numeric 55% figure so "
            "that it does not trip the too-short or unquantified gates. It "
            "exists only to isolate the missing-sport rejection."
        )
        hypothesis_quality_check(h)
        snap = get_metrics().snapshot()
        assert snap["rejected_in_window"] == 1
        assert snap["accepted_in_window"] == 0
        assert snap["rejection_rate"] == 1.0
        assert snap["reason_histogram"]
        # Invalid sport fires first in the ordered scan.
        assert RejectReason.INVALID_SPORT in snap["reason_histogram"]
        assert snap["recent_rejections"]
        assert snap["recent_rejections"][-1]["reason"] \
            == RejectReason.INVALID_SPORT

    def test_mixed_accept_reject_histogram(self):
        hypothesis_quality_check(GOOD_HYPOTHESIS)
        bad = dict(GOOD_HYPOTHESIS)
        bad["min_sample_size"] = 5
        hypothesis_quality_check(bad)
        worse = dict(GOOD_HYPOTHESIS)
        worse["thesis"] = "too short 5%."
        hypothesis_quality_check(worse)
        snap = get_metrics().snapshot()
        assert snap["last_n"] == 3
        assert snap["accepted_in_window"] == 1
        assert snap["rejected_in_window"] == 2
        assert pytest.approx(snap["rejection_rate"], abs=0.001) == 2 / 3

    def test_full_status_accessor(self):
        hypothesis_quality_check(GOOD_HYPOTHESIS)
        from tools.hypothesis_generator import get_quality_metrics_snapshot
        snap = get_quality_metrics_snapshot()
        assert isinstance(snap, dict)
        assert "rejection_rate" in snap
        assert "reason_histogram" in snap
        assert "recent_rejections" in snap
        assert snap["accepted_in_window"] >= 1
