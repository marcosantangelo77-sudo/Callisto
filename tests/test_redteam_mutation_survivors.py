"""Tests written FROM surviving mutants (red-team mutation pass, 2026-08-24).

Each test pins a behavior that a hand-rolled mutation harness showed no
existing test would notice if broken. See findings/redteam_mutation.md for
the full survivor table. Naming: the mutant that motivated the test is in
the docstring.

Scope note: these tests pin the CONFIDENCE path only. No confidence score
may be raised by any of this code; every assertion here enforces downward-
only behavior or exact boundary semantics.
"""
import math

import pytest

from agp.adversary import (
    DISAGREEMENT_CEILING,
    MILD_DISAGREEMENT_CEILING,
    AdversaryObjection,
    clamp_with_ensemble,
    ensemble_ceiling,
)
from agp.thresholds import floor_conf
from tools.edge_confidence import score_edge, score_parlay, EdgeConfidence


# ── floor_conf: the anti-laundering quantise itself was unpinned ─────────
class TestFloorConf:
    def test_floor_never_rounds_up(self):
        """Mutant FLOOR2ROUND L70: floor -> round survives — 0.269183 would
        become 0.27, an automated raise. The whole laundering defense."""
        assert floor_conf(0.269183) == 0.26  # round() gives 0.27 — must fail
        assert floor_conf(0.836) == 0.83     # round() gives 0.84

    def test_floor_is_downward_for_all_nines(self):
        assert floor_conf(0.999999) == 0.99

    def test_places_parameter_floors_not_rounds(self):
        assert floor_conf(0.555, places=1) == 0.5   # round -> 0.6


# ── ensemble ceiling boundaries (adversary.py) ───────────────────────────
class TestEnsembleCeilingBoundaries:
    def test_spread_exactly_at_threshold_caps_hard(self):
        """Mutant GE2GT L118/L120: >= -> > survived. Boundary values are the
        contract: spread exactly at threshold MUST cap at DISAGREEMENT_CEILING."""
        xs = [0.2, 0.2 + 0.30]  # spread == DISAGREEMENT_SPREAD_THRESHOLD (0.30)
        assert ensemble_ceiling(xs) == DISAGREEMENT_CEILING
        # exactly half-threshold must give MILD ceiling, not None
        xs2 = [0.3, 0.3 + 0.15]
        assert ensemble_ceiling(xs2) == MILD_DISAGREEMENT_CEILING

    def test_clamp_applies_ceiling_when_equal(self):
        """Mutant LE2LT L130: <= -> < survived. A score exactly AT the ceiling
        is allowed through unclamped-with-reason; one tick above is clamped."""
        s, reason = clamp_with_ensemble(0.56, [0.0, 0.30])  # ceiling 0.55
        assert s == 0.55 and reason != ""
        s2, reason2 = clamp_with_ensemble(0.55, [0.0, 0.30])
        assert s2 == 0.55 and reason2 == ""

    def test_clamp_also_floors_quantise_on_passthrough(self):
        """clamp_with_ensemble's passthrough quantise must floor too."""
        s, _ = clamp_with_ensemble(0.836, [0.5, 0.51])
        assert s == 0.83


# ── apply_verdict asymmetry (adversary.py) ───────────────────────────────
class TestApplyVerdictAsymmetry:
    def test_no_objections_is_exact_identity(self):
        """Mutants LT2LE L494 / AND2OR L494 survived: with objs=[] nothing
        distinguishes 'unchanged' from 'raised'. Pin exact identity."""
        out, reason = __import__("agp.adversary", fromlist=["PanelVerdict"]).PanelVerdict.apply(0.836, [])
        assert out == 0.836 and reason == ""
        # also via the public path: empty list of objections, no penalty
        from agp.adversary import PanelVerdict
        out2, r2 = PanelVerdict.apply(0.836, [])
        assert out2 == 0.836 and r2 == ""

    def test_blocking_veto_returns_floored_original_not_raised(self):
        """The stale baseline tests asserted round-UP on this path. Pin the
        correct contract: veto reports the score floored, never raised."""
        from agp.adversary import PanelVerdict
        blk = AdversaryObjection(claim_id="c", text="veto", severity="BLOCKING")
        pv = PanelVerdict(objections=[blk])
        out, reason = pv.apply(0.836)
        assert out == 0.83
        assert reason == "veto"

    def test_minor_penalty_zero_leaves_score_bit_identical(self):
        """MINOR penalty is 0.0; apply_verdict must not use it as an excuse to
        re-round upward (mutant family around L492 floor_conf composition)."""
        from agp.adversary import PanelVerdict
        ob = AdversaryObjection(claim_id="c", text="nit", severity="MINOR")
        pv = PanelVerdict(objections=[ob])
        out, reason = pv.apply(0.836)
        assert out == 0.836
        assert reason == ""  # no demotion happened, so no demotion reason

    def test_major_penalty_lowers_and_reports(self):
        from agp.adversary import PanelVerdict
        ob = AdversaryObjection(claim_id="c", text="flaw", severity="MAJOR")
        pv = PanelVerdict(objections=[ob])
        out, reason = pv.apply(0.836)
        assert out == 0.79  # floor_conf(0.836 - 0.05)
        assert "adversary:" in reason


# ── relabel_evidence floor rule (provenance.py) ──────────────────────────
class TestRelabelFloorRule:
    def _relabel(self):
        pytest.importorskip("agp.provenance")
        from agp.provenance import relabel_evidence
        return relabel_evidence

    def _item(self, source_class, conf):
        ev = type("E", (), {})()
        ev.source_class = source_class
        ev.confidence_score = conf
        ev.urls = ()
        ev.content = ""
        ev.content_hash = ""
        ev.declared_url = ""
        return ev

    def test_below_floor_score_stays_below_floor(self):
        """Mutants GE2GT/MIN2MAX L212 survived: prior < floor path. A 0.05
        item must never be raised toward 0.30 by the DB floor logic."""
        relabel = self._relabel()
        from agp.provenance import ProvenanceLedger, SourceClass
        led = ProvenanceLedger()
        ev = self._item(SourceClass.PRIMARY, 0.05)
        ceilings = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55, "INFERRED": 0.55}
        relabel([ev], led, ceilings, floor=0.30)
        assert ev.confidence_score <= 0.05 + 1e-12

    def test_at_boundary_prior_equals_floor_uses_safe_branch(self):
        relabel = self._relabel()
        from agp.provenance import ProvenanceLedger, SourceClass
        led = ProvenanceLedger()
        ev = self._item(SourceClass.INFERRED, 0.30)
        ceilings = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55, "INFERRED": 0.55}
        relabel([ev], led, ceilings, floor=0.30)
        assert ev.confidence_score <= 0.30


# ── edge_confidence: the confidence path (money-adjacent) ────────────────
def _soft(n):
    books = ["FanDuel", "DraftKings", "BetMGM", "BetRivers", "Bovada",
             "MyBookie", "PointsBet", "Caesars"]
    return books[:n]


class TestEdgeConfidenceExactScoring:
    """score_edge had ZERO exact-score tests — only inequality assertions.
    Every additive adjustment and every tier boundary mutated freely."""

    def test_exact_score_two_books_moderate_edge(self):
        c = score_edge(3.0, 2, _soft(2), "h2h")
        # base .75 + book 0.0 + sharp 0.0 + market(h2h=.85): (1-.85)*.15=.0225
        # + method 0 + live 0 + time 0 ... raw should be exactly:
        factors = c.factors
        assert c.score == round(min(factors["raw_total"], 0.75), 3)
        # and independently computed:
        expected_raw = 0.75 + 0.0 + 0.0 + round((1 - 0.85) * 0.15, 3)
        assert factors["edge_magnitude"] == 0.75
        assert factors["book_count"] == 0.0
        assert abs(factors["market_efficiency"] - (1 - 0.85) * 0.15) < 1e-9

    def test_tier_boundaries_are_inclusive_lower_edge(self):
        """GE2GT mutants at all five tier thresholds survived: >= -> >.
        A score landing exactly ON a boundary must get the HIGHER tier."""
        for boundary, tier in [(0.90, "VERIFIED"), (0.75, "CORROBORATED"),
                               (0.55, "PROBABLE"), (0.30, "SPECULATIVE")]:
            ec = EdgeConfidence(score=boundary, tier=tier, source_class="PRIMARY",
                                ceiling=1.0, factors={}, reasoning="")
            # direct construction can't test the mapping; drive through
            # score_parlay which shares the same boundary ladder.
            legs = [EdgeConfidence(score=boundary, tier="VERIFIED",
                                   source_class="PRIMARY", ceiling=1.0,
                                   factors={}, reasoning="")]
            p = score_parlay([legs[0]])
            # single leg: combined = 0.6*b + 0.4*b = b exactly
            assert p.tier == tier, f"score {boundary} must be {tier}, got {p.tier}"

    def test_single_book_penalty_is_minus_ten_points(self):
        """SUB2ADD L201 (-0.10 -> +0.10) SURVIVED. Single-book edges were
        being BOOSTED by ten confidence points without any test noticing."""
        single = score_edge(3.0, 1, ["DraftKings"], "h2h")
        pair = score_edge(3.0, 2, _soft(2), "h2h")
        assert single.factors["book_count"] == pytest.approx(-0.10)
        assert pair.factors["book_count"] == pytest.approx(0.0)

    def test_books_compared_one_vs_two_source_class_boundary(self):
        """GE2GT/EQ2NE L156/159/197: 1 vs 2 books flips SIGNAL/SECONDARY."""
        assert score_edge(3.0, 1, ["A"], "h2h").source_class == "SIGNAL"
        assert score_edge(3.0, 2, ["A", "B"], "h2h").source_class == "SECONDARY"

    def test_edge_magnitude_band_edges_inclusive(self):
        """GE2GT mutants L170-L182: band boundaries 5.0/3.0/2.0/1.0 must be
        inclusive (>=), pinned via factors['edge_magnitude'] base value."""
        cases = [(5.0, 0.90), (3.0, 0.75), (2.0, 0.60), (1.0, 0.45)]
        for edge, base in cases:
            c = score_edge(edge, 2, _soft(2), "h2h")
            assert c.factors["edge_magnitude"] == base, f"edge {edge} -> base {base}"
        below = score_edge(0.9, 2, _soft(2), "h2h")
        assert below.factors["edge_magnitude"] == 0.45  # 1.0-band, not 0.30

    def test_sharp_bonus_is_plus_ten_and_cannot_double_count(self):
        """DIV2MUL L208 etc.: sharp_adj must be +0.10 flat, not scaled."""
        with_sharp = score_edge(3.0, 4, ["Pinnacle"] + _soft(3), "h2h")
        without = score_edge(3.0, 4, _soft(4), "h2h")
        delta = with_sharp.factors["sharp_book"] - without.factors["sharp_book"]
        assert delta == pytest.approx(0.10)

    def test_parlay_weakest_leg_dominates(self):
        """MIN2MAX L599/L609: min -> max on weakest-leg selection survived.
        Parlay class comes from the WEAKEST leg, not the strongest."""
        strong_leg = EdgeConfidence(score=0.95, tier="VERIFIED", source_class="PRIMARY",
                                    ceiling=1.0, factors={}, reasoning="")
        weak_leg = EdgeConfidence(score=0.40, tier="SPECULATIVE", source_class="SIGNAL",
                                  ceiling=0.55, factors={}, reasoning="")
        p = score_parlay([strong_leg, weak_leg])
        assert p.source_class == "SIGNAL"      # weakest leg's class
        assert p.ceiling == 0.55               # lowest ceiling
        assert p.score <= weak_leg.score       # never above weakest leg

    def test_parlay_empty_is_unverified_zero(self):
        p = score_parlay([])
        assert p.score == 0.0 and p.tier == "UNVERIFIED"

    def test_secondary_ceiling_hard_cap_with_every_boost(self):
        """MAX2MIN/MIN2MAX L530 family: even with cross-method confirmation,
        sharp-adjacent boosts, and 8 books, no-sharp stays <= 0.75 AND the
        cap binds exactly rather than being raised."""
        c = score_edge(10.0, 8, _soft(8), "h2h", cross_method_confirmed=True)
        assert c.score == pytest.approx(0.75)
        assert c.tier == "CORROBORATED"

    def test_market_efficiency_adjustment_direction(self):
        """MUL2DIV/SUB2ADD L217: less efficient market => HIGHER adj."""
        prop = score_edge(3.0, 2, _soft(2), "player_props")
        main = score_edge(3.0, 2, _soft(2), "h2h")
        assert prop.factors["market_efficiency"] > main.factors["market_efficiency"]
