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


class TestResolverVocabularyBridge:
    """2026-08-23: the OutcomeResolver side speaks positive/negative/
    indeterminate; this module canonically speaks hit/miss/stale/void.
    Before the bridge, resolver-produced records were silently uncounted —
    inherited_ceiling returned SPECULATIVE_CAP for a perfect track record."""

    def test_positive_resolutions_lift_the_ceiling(self):
        recs = [{"question_id": f"x{i}", "resolved_at": "2027-01-01",
                 "outcome": "positive", "best_source_class": "PRIMARY"}
                for i in range(12)]
        assert inherited_ceiling(recs) > SPECULATIVE_CAP

    def test_bridge_matches_canonical_vocabulary_exactly(self):
        canon = [{"question_id": f"x{i}", "resolved_at": "2027-01-01",
                  "outcome": "hit", "best_source_class": "PRIMARY"}
                 for i in range(12)]
        bridged = [{"question_id": f"x{i}", "resolved_at": "2027-01-01",
                    "outcome": "positive", "best_source_class": "PRIMARY"}
                   for i in range(12)]
        assert inherited_ceiling(bridged) == inherited_ceiling(canon)

    def test_indeterminate_counts_as_stale_not_dropped(self):
        # stale demotes (up to -0.20); it must not silently vanish from n
        stale = [{"question_id": f"s{i}", "resolved_at": "2027-01-01",
                  "outcome": "stale"} for i in range(5)]
        indet = [{"question_id": f"s{i}", "resolved_at": "2027-01-01",
                  "outcome": "indeterminate"} for i in range(5)]
        assert inherited_ceiling(stale) == inherited_ceiling(indet)

    def test_unknown_outcome_token_raises(self):
        import pytest
        from tools.research_program import ResolutionRecord
        with pytest.raises(ValueError):
            ResolutionRecord(question_id="q", resolved_at=date(2027, 1, 1),
                             outcome="positve")  # typo

    def test_record_normalises_resolver_tokens(self):
        from tools.research_program import ResolutionRecord
        r = ResolutionRecord(question_id="q", resolved_at=date(2027, 1, 1),
                             outcome="POSITIVE")
        assert r.outcome == "hit" and r.counted

    def test_migration_tables_feed_resolver_end_to_end(self):
        """The domain-general tables exist and SqlitePredictionResolver can
        read them — before migration 016 nothing could store a resolution."""
        import asyncio, sqlite3, tempfile
        from tools.migrations.runner import discover_migrations
        migs = {m.name: m for m in discover_migrations()}
        mig = migs["domain_general_predictions"]
        db = tempfile.mktemp(suffix=".db")
        raw = sqlite3.connect(db)
        mig.up(raw)
        raw.execute("INSERT INTO predictions (claim_id,event_id,predicted_prob)"
                    " VALUES ('c1','e1',0.62)")
        raw.execute("INSERT INTO outcomes (prediction_id,resolved_outcome,"
                    "payoff,source) VALUES (1,'positive',1.0,'test')")
        raw.commit()
        import aiosqlite
        from tools.resolvers.generic import SqlitePredictionResolver

        async def main():
            aconn = await aiosqlite.connect(db)
            r = SqlitePredictionResolver(aconn)
            s = await r.summarize("c1")
            await aconn.close()
            return s

        s = asyncio.run(main())
        assert s.total == 1 and s.positive == 1 and s.fully_resolved
        raw.close()
