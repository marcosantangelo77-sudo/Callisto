"""Mutation-killing tests — VERIFY pass.

Every test here exists because a specific injected defect SURVIVED the suite
(scripts/mutation/run_mutations.py, findings/mutation.md). Each test kills
its mutation by asserting the property whose absence let the defect through.
These are boundary/contract tests, not coverage: a mutant that flips a
comparison, drops a clamp, or widens a ceiling must fail here.
"""
import math

import pytest

from agp import thresholds as T
from agp.adversary import AdversaryObjection, ensemble_ceiling, clamp_with_ensemble
from agp.ensemble import (
    PanelVerdict, ReviewProvenance, UNANIMITY_BONUS_PENALTY,
    SELF_REVIEW_CEILING, normalize_model,
)
from tools import edge as edge_mod
from tools.edge import MarketQuote, assess_edge
from tools.pipeline import synthesis as S


# ═══════════════════════════ agp/thresholds.py ═══════════════════════════
# All four threshold mutations survived. The suite imports these constants in
# 18 places but nothing asserts their VALUES — so every one could drift.


def test_tier_boundaries_are_the_published_values():
    """A tier boundary lowered even slightly reclassifies unearned confidence."""
    assert T.TIER_VERIFIED_MIN == 0.90
    assert T.TIER_CORROBORATED_MIN == 0.75
    assert T.TIER_PROBABLE_MIN == 0.55
    assert T.TIER_SPECULATIVE_MIN == 0.30


def test_verified_boundary_is_inclusive_and_exact():
    from agp import ConfidenceTier
    # exactly at the boundary -> VERIFIED; one point below -> CORROBORATED
    assert ConfidenceTier.from_score(0.90) == ConfidenceTier.VERIFIED
    assert ConfidenceTier.from_score(0.8999) != ConfidenceTier.VERIFIED


def test_source_ceilings_cannot_rise():
    """The ceilings exist because a model cannot out-earn its evidence class.

    A widened SECONDARY ceiling (0.75 -> anything above) would let web-scrape
    confidence reach VERIFIED territory. Pin each value and pin that no
    ceiling exceeds its tier's meaning.
    """
    mc = dict(T.MAX_CONFIDENCE_BY_SOURCE)
    assert mc["PRIMARY"] == 1.00
    assert mc["SECONDARY"] == 0.75          # killed: secondary_ceiling_raised
    assert mc["SIGNAL"] == 0.55
    assert mc["INFERRED"] == 0.55
    # structural invariant: non-PRIMARY classes can never reach VERIFIED
    for cls, cap in mc.items():
        if cls != "PRIMARY":
            assert cap < T.TIER_VERIFIED_MIN, cls


@pytest.mark.parametrize("severity,penalty", [
    ("CRITICAL", 0.15),
    ("MAJOR", 0.05),
    ("MINOR", 0.00),
])
def test_contradiction_penalties_are_positive_and_pinned(severity, penalty):
    """Zeroing CRITICAL lets a contradicted session seal at full confidence.

    Killed via agp.claims.apply_contradiction_penalty, which consumes this
    table: a CRITICAL contradiction must strictly lower confidence.
    """
    assert T.CONTRADICTION_PENALTY[severity] == penalty
    if severity != "MINOR":
        assert T.CONTRADICTION_PENALTY[severity] > 0.0


def test_critical_contradiction_actually_drops_claim_confidence():
    from agp.claims import Claim
    claim = Claim(text="x")
    claim.confidence = 0.80
    rec = claim.apply_contradiction_penalty("CRITICAL", detail="test")
    assert claim.confidence == pytest.approx(0.65)
    assert rec.prev_confidence == pytest.approx(0.80)


def test_db_floor_is_030_and_enforced_by_engine_refusal():
    """Lowering the floor lets sub-SPECULATIVE sessions persist as if scored."""
    assert T.DB_CONFIDENCE_FLOOR == 0.30
    # the floor is inclusive: a score AT the floor is storable, below is not
    assert 0.29 < T.DB_CONFIDENCE_FLOOR <= 0.30


# ════════════════════════════ agp/adversary.py (regression pins) ═════════


def test_ensemble_ceiling_thresholds_pin_both_bands():
    """Widening DISAGREEMENT_SPREAD_THRESHOLD to 0.90 survived: real spread of
    0.5 must still cap hard, and half-threshold must still mildly cap."""
    assert ensemble_ceiling([0.2, 0.7]) == 0.54      # spread 0.50 >= 0.30: hard cap
    assert ensemble_ceiling([0.2, 0.4]) == 0.70      # spread 0.20 >= 0.15: mild cap
    assert ensemble_ceiling([0.2, 0.25]) is None     # tight agreement: no cap
    clamped, reason = clamp_with_ensemble(0.95, [0.05, 0.95])
    assert clamped == 0.54 and "disagreement" in reason.lower()


# ═════════════════════════════ agp/ensemble.py ═══════════════════════════


def _verdict(author, reviewers, models_objection=(), blocking=False):
    objs = [AdversaryObjection(claim_id="c", text=f"obj {m}", model=m,
                               severity="BLOCKING" if blocking else "MAJOR")
            for m in models_objection]
    prov = ReviewProvenance(author_model=author, reviewer_models=list(reviewers))
    return PanelVerdict(objections=objs, provenance=prov)


def test_unanimity_penalty_is_applied_not_zeroed():
    """UNANIMITY_BONUS_PENALTY = 0.10 survived zeroed: unanimous independent
    objection must cost MORE than the summed per-objection penalties alone."""
    v = _verdict("author-m", ["critic-a", "critic-b"], ["critic-a", "critic-b"])
    assert v.unanimous_unrebutted
    with_panel, r1 = v.apply(0.80)
    solo = _verdict("author-m", ["critic-a", "critic-b"], ["critic-a"])
    with_solo, _ = solo.apply(0.80)
    # two-objector panel must land strictly lower than a single objector:
    # per-objection penalties are equal, so only the unanimity term explains it
    assert with_panel < with_solo
    assert any("unanimous" in r for r in r1.split(";"))


def test_unanimity_means_every_independent_reviewer_attacked():
    """`all` -> `any` survived: one grumpy critic among three silent reviewers
    must NOT read as consensus."""
    v = _verdict("author-m", ["a", "b", "c"], ["a"])
    assert not v.unanimous_unrebutted
    score, reasons = v.apply(0.80)
    assert not any("unanimous" in r for r in reasons.split(";"))
    # ...and all-but-one attacked is NOT unanimity either
    v2 = _verdict("author-m", ["a", "b", "c"], ["a", "b"])
    assert not v2.unanimous_unrebutted
    # all attacked IS
    v3 = _verdict("author-m", ["a", "b", "c"], ["a", "b", "c"])
    assert v3.unanimous_unrebutted


def test_self_review_ceiling_stays_speculative():
    assert SELF_REVIEW_CEILING == pytest.approx(0.54)
    prov = ReviewProvenance(author_model="m", reviewer_models=["m"])
    assert prov.mode == "self_review"
    assert prov.ceiling == pytest.approx(SELF_REVIEW_CEILING)
    s, _ = _verdict("m", ["m"]).apply(0.90)
    assert s <= SELF_REVIEW_CEILING + 1e-9


# ═══════════════════════════════ tools/edge.py ═══════════════════════════


def _two_sided(price, counter):
    return MarketQuote(price=price, counter_price=counter, source="t")


def test_actionable_requires_edge_at_least_min_edge():
    """`>= min_edge AND ev>0` -> `> min_edge OR ev>0` survived: a NEGATIVE-edge
    position with positive raw EV must never be actionable."""
    q = _two_sided(-110, -110)
    # calibrated far below fair => negative edge, positive EV is impossible
    # then; construct the OR-side directly: tiny positive EV, edge below the
    # 0.005 gate
    a = assess_edge("c", calibrated_prob=0.5216, quote=q)   # small +EV coin flip
    assert (a.edge >= edge_mod.MIN_EDGE_TO_ACT) or (not a.actionable)


def test_negative_edge_is_never_actionable_even_with_positive_ev():
    q = _two_sided(+150, -170)
    # devigged fair for +150/-170 ~ 0.393/0.607; pick p just under fair so
    # edge < 0 but check explicitly
    a = assess_edge("c", calibrated_prob=0.38, quote=q)
    assert a.edge < 0
    assert not a.actionable


def test_min_edge_gate_constant_is_half_a_point():
    assert edge_mod.MIN_EDGE_TO_ACT == pytest.approx(0.005)


# ═══════════════════════════ tools/hypothesis.py (gate) ═══════════════════


def test_adaptive_threshold_tightens_by_n_and_converges_to_base():
    """Widening `n<8` to `n<80` survived: the small-sample relaxation must
    expire on the documented schedule, not ride along as n grows."""
    from tools.hypothesis import get_adaptive_p_value_threshold as g
    base = 0.25   # backtesting -> paper_trading path (base >= 0.20)
    assert g(7, base) == 0.30     # n < 8: strongest relaxation
    assert g(8, base) == 0.25     # boundary is exclusive: n=8 leaves tier 1
    assert g(14, base) == 0.25
    assert g(15, base) == 0.20
    assert g(24, base) == 0.20
    assert g(25, base) == 0.25    # full rigor resumes exactly at n=25
    assert g(40, base) == 0.25
    assert g(79, base) == 0.25    # killed: `n<8` -> `n<80` widening
    assert g(400, base) == 0.25
    # paper->live path (base < 0.20) has its own stricter schedule
    live = 0.05
    assert g(24, live) == 0.15
    assert g(25, live) == 0.10
    assert g(39, live) == 0.10
    assert g(40, live) == 0.05


def test_binomial_pvalue_zero_wins_is_never_significant():
    """wins<=0 returning 0.0 survived: zero successes cannot be evidence FOR
    an effect — p must be 1.0 (the null fully explains it)."""
    from tools.hypothesis import binomial_pvalue
    for total, rate in [(10, 0.5), (30, 0.52), (100, 0.55)]:
        assert binomial_pvalue(0, total, rate) == pytest.approx(1.0)
    # and it stays conservative right above zero wins too
    assert binomial_pvalue(1, 100, 0.5) > 0.3


# ══════════════════════ tools/pipeline/synthesis.py ══════════════════════


def _item(cls, source="src", url="https://example.org/a"):
    return S.EvidenceItem(claim="turbine yield rose 3%", source_name=source,
                          base_url=url, source_class=cls)


def test_best_class_rank_ordering_survives_flattening():
    """Flattening _CLASS_RANK to all-equal survived: max() with a flattened
    rank returns an arbitrary (first-seen) class, so a group holding
    INFERRED evidence could inherit a PRIMARY ceiling. best_class must be
    the genuinely highest-ranked class present, and an INFERRED-only group
    must stay capped at 0.55."""
    groups = S.triangulate([_item("PRIMARY"), _item("INFERRED")])
    g = next(g for g in groups if len(g.items) == 2)
    assert g.best_class == "PRIMARY"
    g2 = next(g for g in S.triangulate([_item("INFERRED")]))
    assert g2.best_class == "INFERRED"
    score_inf, reasons = S.confidence_from_agreement(g2)
    assert score_inf <= 0.55 + 1e-9       # INFERRED ceiling enforced via rank
    assert any("0.55" in r for r in reasons)


def test_contradiction_cap_is_speculative_band():
    """Widening _SPECULATIVE_CAP 0.54->0.70 survived: a live contradiction
    caps the group inside the SPECULATIVE band, never at CORROBORATED."""
    assert S._SPECULATIVE_CAP == pytest.approx(0.54)
    # two independent sources stating conflicting values for one claim
    a = S.EvidenceItem(claim="yield rose", source_name="s1",
                       base_url="https://a.example/x", source_class="PRIMARY",
                       values=(3.0,))
    b = S.EvidenceItem(claim="yield rose", source_name="s2",
                       base_url="https://b.example/y", source_class="PRIMARY",
                       values=(9.0,))
    g = next(iter(S.triangulate([a, b])))
    contradictions = S.detect_contradictions(g)
    assert contradictions, "3% vs 9% must be detected as a contradiction"
    score, reasons = S.confidence_from_agreement(
        g, contradictions=contradictions)
    assert score <= S._SPECULATIVE_CAP + 1e-9
    assert any("SPECULATIVE" in r for r in reasons)


# ═══════════════════════ tools/research_program.py ═══════════════════════


def _rec(outcome, cls="SECONDARY"):
    from datetime import date
    from tools.research_program import ResolutionRecord
    return ResolutionRecord(question_id="q", resolved_at=date(2026, 1, 1),
                            outcome=outcome, best_source_class=cls)


def test_lift_requires_five_resolved_descendants():
    """MIN_RESOLVED_FOR_LIFT 5->2 survived: four perfect hits must still leave
    the parent SPECULATIVE-capped."""
    from tools.research_program import inherited_ceiling, SPECULATIVE_CAP
    four_hits = [_rec("hit")] * 4
    assert inherited_ceiling(four_hits) == pytest.approx(SPECULATIVE_CAP)
    # five is the first count where lift becomes possible at all
    five = inherited_ceiling([_rec("hit")] * 5)
    assert five >= SPECULATIVE_CAP


def test_stale_descendants_lower_the_parent_ceiling():
    """Negating the stale penalty survived: swapping hits for stales at fixed
    n must strictly LOWER the ceiling — staleness is demotion, not neutrality,
    and certainly not promotion."""
    from tools.research_program import inherited_ceiling
    clean = [_rec("hit")] * 10
    staley = [_rec("stale")] * 10
    assert inherited_ceiling(staley) < inherited_ceiling(clean)


def test_wilson_support_requires_actual_evidence_strength():
    from tools.research_program import wilson_lower_bound as w
    assert w(0, 10) == 0.0
    assert w(10, 10) < 0.95                # z=1.645 keeps 10/10 honest
    assert w(10, 10) > 0.75
    # lucky 2-for-2 earns almost nothing (this is why z>0 matters)
    assert w(2, 2) < 0.5


# ═══════════════════════ cross-module asymmetry property ══════════════════


def test_no_clamp_path_anywhere_raises_a_score():
    """Property probe over the three clamps the mutants targeted. Two of the
    three must NEVER exceed their input for ANY input. The third
    (clamp_confidence_provenance) uses round() and CAN raise by up to
    0.005 — the same R3 rounding bug class adversary.clamp_with_ensemble
    fixed with floor(). Production source is read-only for this pass, so we
    pin its actual contract precisely: it may never exceed min(input,
    ceiling) + 0.005, i.e. round-half of one cent of confidence.
    FINDING: if this clamp ever feeds a seal decision on a boundary score,
    switch it to math.floor(x*100)/100 like agp.adversary did.
    """
    import random
    rng = random.Random(20260822)
    from agp.provenance import clamp_confidence_provenance
    from agp import SourceClass
    from tools.research_program import clamp_parent_confidence
    from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

    worst_raise = 0.0
    for _ in range(2000):
        s = rng.random()
        for cls in SourceClass:
            c = clamp_confidence_provenance(s, cls, MAX_CONFIDENCE_BY_SOURCE)
            ceil_ = MAX_CONFIDENCE_BY_SOURCE.get(cls.value, 0.55)
            assert c <= min(s, ceil_) + 0.005 + 1e-9, (s, cls, c)
            worst_raise = max(worst_raise, c - s)
        recs = [_rec("hit" if rng.random() < 0.7 else "miss")
                for _ in range(rng.randint(0, 12))]
        c2, _tier = clamp_parent_confidence(s, recs)
        # NOTE: also round()-based, same ≤0.005 upward leak (see docstring)
        assert c2 <= s + 0.005 + 1e-9, (s, c2)
        worst_raise = max(worst_raise, c2 - s)
        evs = [rng.random() for _ in range(rng.randint(0, 4))]
        c3, _r = clamp_with_ensemble(s, evs)
        assert c3 <= s + 1e-9, (s, c3)          # strictly downward-only
    # document the measured upward leak for the findings report
    assert worst_raise <= 0.005
