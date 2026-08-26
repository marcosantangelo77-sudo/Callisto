"""B4 build tests — the inheritance rule (tools/research_program.py).

The centerpiece under test. Every test here encodes one property the owner
is buying:
  1. Zero resolved descendants -> SPECULATIVE cap forever.
  2. Ceiling is a monotone function of descendants' track record.
  3. The rule can only LOWER a score, never raise it (gate-weakening is
     structurally impossible).
  4. Domain-general: identical behavior for finance/biology/supply-chain
     records; records are plain dicts so any resolver (B1's OutcomeResolver
     included) can feed it.
"""

from datetime import date

import pytest

from tools.research_program import (
    ResolutionRecord,
    clamp_parent_confidence,
    inherited_ceiling,
    normalize_records,
    summarize_track_record,
    tier_ceiling_from_score,
    SPECULATIVE_CAP,
    N_FOR_PROBABLE,
)

D0 = date(2026, 8, 22)


def hits(n, **kw):
    return [ResolutionRecord(f"q{i}", D0, "hit", **kw) for i in range(n)]


def misses(n, **kw):
    return [ResolutionRecord(f"m{i}", D0, "miss", **kw) for i in range(n)]


class TestZeroResolvedCapsAtSpeculative:
    def test_no_descendants(self):
        assert inherited_ceiling([]) == SPECULATIVE_CAP

    def test_one_hit_is_not_enough(self):
        assert inherited_ceiling(hits(1)) == SPECULATIVE_CAP

    def test_below_lift_threshold_even_when_perfect(self):
        assert inherited_ceiling(hits(N_FOR_PROBABLE - 1)) == SPECULATIVE_CAP

    def test_void_outcomes_do_not_count(self):
        recs = hits(10) + [ResolutionRecord("v", D0, "void")]
        # voids excluded entirely: same as 10 hits
        assert inherited_ceiling(recs) == inherited_ceiling(hits(10))


class TestCeilingRisesWithTrackRecord:
    def _p(self, recs):
        return inherited_ceiling(recs)

    def test_perfect_record_beats_coin_flip(self):
        good = self._p(hits(20, best_source_class="PRIMARY"))
        mediocre = self._p(hits(10, best_source_class="PRIMARY") +
                           misses(10, best_source_class="PRIMARY"))
        assert good > mediocre > SPECULATIVE_CAP

    def test_longer_track_record_earns_more(self):
        short = self._p(hits(5, best_source_class="PRIMARY"))
        long_ = self._p(hits(40, best_source_class="PRIMARY"))
        assert long_ > short

    def test_bad_calibration_capped_low(self):
        awful = self._p(misses(40))
        assert awful <= SPECULATIVE_CAP + 1e-9   # can barely lift at best

    def test_quantile_descendants_contribute_pinball(self):
        sharp = self._p(
            hits(10, best_source_class="PRIMARY") +
            [ResolutionRecord("qf", D0, "hit", pinball_score=0.05,
                              best_source_class="PRIMARY")])
        sloppy = self._p(
            hits(10, best_source_class="PRIMARY") +
            [ResolutionRecord("qf", D0, "hit", pinball_score=0.45,
                              best_source_class="PRIMARY")])
        assert sharp > sloppy

    def test_staleness_penalizes(self):
        clean = self._p(hits(20, best_source_class="PRIMARY"))
        stale = self._p(
            hits(15, best_source_class="PRIMARY") +
            [ResolutionRecord(f"s{i}", D0, "stale",
                              best_source_class="PRIMARY")
             for i in range(5)])
        assert stale < clean


class TestProvenanceGateOnInheritance:
    def test_hearsay_descendants_cannot_make_parent_verified(self):
        great = hits(60, best_source_class="PRIMARY")
        hearsay = hits(60, best_source_class="INFERRED")
        assert inherited_ceiling(great) >= 0.90
        assert inherited_ceiling(hearsay) < 0.75

    def test_best_class_among_mixed_records_caps(self):
        mixed = hits(50, best_source_class="SECONDARY") + \
            hits(2, best_source_class="PRIMARY")
        # Best class is PRIMARY -> ceiling may reach the VERIFIED band...
        ceiling = inherited_ceiling(mixed)
        assert ceiling > SPECULATIVE_CAP
        # ...but a mostly-secondary record still earns less accuracy credit
        # toward that band than a fully primary one (hearsay dilutes).
        pure_primary = inherited_ceiling(
            hits(50, best_source_class="PRIMARY") +
            hits(2, best_source_class="PRIMARY"))
        assert ceiling <= pure_primary
        assert inherited_ceiling(
            hits(52)) <= inherited_ceiling(mixed)  # secondary cap binds


class TestOnlyLowersNeverRaises:
    @pytest.mark.parametrize("raw", [0.55, 0.75, 0.90, 0.99])
    def test_zero_resolved_clamps_everything_to_cap(self, raw):
        score, tier = clamp_parent_confidence(raw, [])
        assert score == pytest.approx(min(raw, SPECULATIVE_CAP), abs=0.01)
        assert score <= raw

    def test_never_exceeds_raw(self):
        recs = hits(100, best_source_class="PRIMARY")
        raw = 0.40   # modest claim with a great track record stays modest
        score, _ = clamp_parent_confidence(raw, recs)
        assert score <= raw

    def test_tier_labels(self):
        assert tier_ceiling_from_score(0.95) == "VERIFIED"
        assert tier_ceiling_from_score(0.80) == "CORROBORATED"
        assert tier_ceiling_from_score(0.60) == "PROBABLE"
        assert tier_ceiling_from_score(0.45) == "SPECULATIVE"
        assert tier_ceiling_from_score(0.10) == "UNVERIFIED"


class TestResolverSeam:
    """B1 owns resolution (OutcomeResolver on tools/hypothesis.py); we own
    confidence-inheritance. The seam is deliberately dict-shaped."""

    def test_accepts_plain_dicts(self):
        recs = [{"question_id": f"dq{i}", "resolved_at": "2027-01-15",
                 "outcome": "HIT", "best_source_class": "PRIMARY"}
                for i in range(12)]
        norm = normalize_records(recs)
        assert len(norm) == 12 and norm[0].outcome == "hit"
        assert inherited_ceiling(recs) > SPECULATIVE_CAP

    def test_accepts_datetime_resolved_at(self):
        from datetime import datetime
        rec = {"question_id": "x", "resolved_at": datetime(2027, 3, 1),
               "outcome": "miss"}
        assert len(normalize_records([rec])) == 1

    def test_rejects_garbage(self):
        with pytest.raises(TypeError):
            normalize_records([42])


class TestDomainGenerality:
    def test_protein_folding_program_inherits_identically(self):
        recs = [
            {"question_id": f"af-{i}", "resolved_at": date(2027, i % 12 + 1, 1),
             "outcome": "hit" if i % 4 else "miss",
             "pinball_score": 0.08, "best_source_class": "PRIMARY"}
            for i in range(24)]
        btc_like = [
            {"question_id": f"btc-{i}", "resolved_at": date(2027, i % 12 + 1, 1),
             "outcome": "hit" if i % 4 else "miss",
             "pinball_score": 0.08, "best_source_class": "PRIMARY"}
            for i in range(24)]
        assert inherited_ceiling(recs) == inherited_ceiling(btc_like)

    def test_supply_chain_claim_stays_speculative_with_no_children(self):
        score, tier = clamp_parent_confidence(
            0.85, [{"question_id": "none", "outcome": "void"}])
        assert score <= SPECULATIVE_CAP

class TestStaleNeverEarnsCredit:
    """2026-08-25 repair of the interrupted F7 fix: 'stale' means unresolved
    at deadline. Every route from a stale record into lift is closed."""

    def test_track_record_hit_rate_cannot_exceed_one(self):
        # n_resolved counts genuine resolutions only; subtracting stales
        # again would let 5 hits + 1 stale report an impossible 1.25.
        recs = hits(5) + [ResolutionRecord("s", D0, "stale")]
        tr = summarize_track_record(recs)
        assert tr.n_resolved == 5 and tr.n_stale == 1
        assert tr.hit_rate == pytest.approx(1.0)
        assert tr.hit_rate <= 1.0

    def test_stale_pinball_score_earns_no_calibration_credit(self):
        clean = inherited_ceiling(hits(6))
        poisoned = inherited_ceiling(
            hits(6) + [ResolutionRecord("s", D0, "stale", pinball_score=0.0)])
        assert poisoned == clean   # a perfect claimed score changes nothing

    def test_stale_misses_do_not_count_toward_wilson_support(self):
        # 5 hits lift; 5 hits + 5 stale-miss-shaped records must not lose
        # accuracy credit either — stales are simply absent from evidence.
        five_hits = inherited_ceiling(hits(5))
        with_stales = inherited_ceiling(hits(5) +
                                        [ResolutionRecord(f"s{i}", D0, "stale")
                                         for i in range(5)])
        assert with_stales < five_hits   # only the staleness penalty bites
        # ...but they never count as resolved misses inflating n:
        from tools.research_program import summarize_track_record as s
        tr = s(hits(5) + [ResolutionRecord("z", D0, "stale")])
        assert tr.n_resolved == 5

    def test_all_stale_is_identical_to_no_descendants(self):
        stales_only = [ResolutionRecord(f"s{i}", D0, "stale",
                                        best_source_class="PRIMARY")
                       for i in range(10)]
        assert inherited_ceiling(stales_only) == SPECULATIVE_CAP

    def test_four_hits_plus_any_number_of_stales_stay_speculative(self):
        base = inherited_ceiling(hits(4))
        for extra in (1, 5, 50):
            recs = hits(4) + [ResolutionRecord(f"s{i}", D0, "stale")
                              for i in range(extra)]
            assert inherited_ceiling(recs) == SPECULATIVE_CAP == base
