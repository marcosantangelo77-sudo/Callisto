"""RED TEAM F1–F3: round/clamp/normalise-upward sweep.

The claim under attack: "no code path in this system inflates a confidence
score." These tests sweep boundaries and random inputs against every
clamp/round site in agp.provenance, agp.adversary, agp.ensemble,
tools.research_program.

Contract tested everywhere: f(x) <= x, exactly, no epsilon. A function whose
docstring says "only ever pulls DOWN" does not get a rounding allowance —
round(0.836, 2) == 0.84 is the historical bug (MORNING_REPORT §process-2).
"""
import math

import pytest

from agp import Domain, Evidence, SourceClass
from agp.adversary import Adversary, AdversaryObjection
from agp.ensemble import (
    PanelVerdict,
    ReviewProvenance,
    apply_panel_verdict,
    clamp_with_ensemble,
)
from agp.provenance import (
    ProvenanceLedger,
    clamp_confidence_provenance,
    relabel_evidence,
)
from tools.research_program import (
    ResolutionRecord,
    clamp_parent_confidence,
    inherited_ceiling,
)

CEILINGS = dict(agp_max={"PRIMARY": 1.0, "SECONDARY": 0.75,
                         "SIGNAL": 0.55, "INFERRED": 0.55})


def _ev(cls, conf):
    return Evidence(content="irrelevant", source_class=cls,
                    confidence_score=conf, domain=Domain.GENERAL,
                    origin_agent="redteam")


def _hyp_floats():
    """Random scores concentrated on rounding boundaries."""
    import random
    rng = random.Random(20260822)
    out = []
    for _ in range(400):
        out.append(round(rng.uniform(0.0, 1.0), rng.randint(1, 6)))
    # boundary-heavy cases
    for base in range(30, 101):
        for delta in (-0.001, -0.0001, 0.0001, 0.001, -0.005, 0.005):
            out.append(round((base / 100) + delta, 6))
        out.append(base / 100)
    return [x for x in out if 0.0 <= x <= 1.0]


SCORES = _hyp_floats()


# ── F1: Adversary.apply_verdict raises via round() on every path ─────────

@pytest.mark.parametrize("score", SCORES)
def test_apply_verdict_never_raises_empty_objections(score):
    """No objections must mean score unchanged-or-lower (floor, not round)."""
    out, reason = Adversary.apply_verdict(score, [])
    assert out <= score, f"apply_verdict inflated {score} -> {out}"


def test_apply_verdict_round_up_repro():
    """REPRODUCIBLE BREAK (F1): the exact historical bug class, still live.

    Adversary.apply_verdict(0.836, []) returns 0.84 — a raise of +0.004 on
    the *no-objection approval path*, i.e. the adversary's own docstring
    ('There is NO bonus path') is violated by its rounding.
    """
    out, _ = Adversary.apply_verdict(0.836, [])
    assert out == 0.84          # demonstrates the bug
    assert out <= 0.836         # the invariant it violates -> FAILS


@pytest.mark.parametrize("score", SCORES)
def test_apply_verdict_minor_penalty_path_never_raises(score):
    objs = [AdversaryObjection(claim_id="c", text=f"obj {score}",
                               severity="MINOR")]
    out, _ = Adversary.apply_verdict(score, objs)
    assert out <= score


# ── F2: clamp_parent_confidence rounds UP across tier boundaries ────────

@pytest.mark.parametrize("raw", [s for s in SCORES])
def test_clamp_parent_never_raises(raw):
    out, tier = clamp_parent_confidence(raw, [])
    assert out <= raw, f"clamp_parent_confidence inflated {raw} -> {out}"


def test_clamp_parent_round_promotes_tier():
    """REPRODUCIBLE BREAK (F2a): 0.7499 raw is below the CORROBORATED
    boundary (0.75) but round(x, 2) lifts it to exactly 0.75 — the parent
    is STORED and LABELLED CORROBORATED on a score it never earned.
    Same at the PROBABLE boundary: 0.5551 -> 0.56."""
    recs = [{"question_id": str(i), "outcome": "hit",
             "resolved_at": "2026-01-01",
             "best_source_class": "SECONDARY"} for i in range(5)]
    assert inherited_ceiling(recs) == 0.75
    out, tier = clamp_parent_confidence(0.7499, recs)
    assert (out, tier) == (0.75, "CORROBORATED")   # the bug
    assert out <= 0.7499                            # FAILS: +0.0001


def test_clamp_parent_probable_boundary_round_up():
    out, _ = clamp_parent_confidence(0.5551, [])
    assert out == 0.56       # 0.5551 rounded UP past the PROBABLE floor band
    assert out <= 0.5551     # FAILS


# ── F3: relabel_evidence floors a score UPWARD during demotion ──────────

@pytest.mark.parametrize("declared", [SourceClass.PRIMARY, SourceClass.SECONDARY])
@pytest.mark.parametrize("conf", [0.05, 0.10, 0.20, 0.29, 0.299])
def test_relabel_evidence_demotion_does_not_raise(declared, conf):
    ledger = ProvenanceLedger()   # empty ledger -> everything INFERRED
    ev = _ev(declared, conf)
    relabel_evidence([ev], ledger, CEILINGS["agp_max"])
    assert ev.confidence_score <= conf, (
        f"demotion RAISED confidence {conf} -> {ev.confidence_score} "
        f"(floor {0.30} applied on top of an already-lower score)")
    assert ev.source_class == SourceClass.INFERRED


def test_relabel_evidence_floor_boost_repro():
    """REPRODUCIBLE BREAK (F3): a PRIMARY-declared item with confidence 0.10
    that provenance demotes to INFERRED comes back with confidence 0.30.
    The DB-floor clamp is applied as max(floor, ...), so the 'demotion'
    path TRIPLES the item's confidence. Any aggregate that averages
    evidence confidence afterwards inherits the invented mass."""
    ledger = ProvenanceLedger()
    ev = _ev(SourceClass.PRIMARY, 0.10)
    n = relabel_evidence([ev], ledger, CEILINGS["agp_max"])
    assert n == 1                      # correctly reported as demoted...
    assert ev.confidence_score == 0.30  # ...while raising it 3x (FAILS)


# ── clamp_confidence_provenance: same round() defect ────────────────────

@pytest.mark.parametrize("score", SCORES)
@pytest.mark.parametrize("cls", list(SourceClass))
def test_provenance_clamp_never_raises_above_input(score, cls):
    out = clamp_confidence_provenance(score, cls, CEILINGS["agp_max"])
    ceil_ = CEILINGS["agp_max"].get(cls.value, 0.55)
    if score > ceil_:
        assert out <= ceil_
    else:
        assert out <= score, f"clamp_confidence_provenance raised {score}->{out}"


def test_provenance_clamp_round_up_repro():
    """PRIMARY ceiling is 1.0, so this is a pure identity+round: 0.836 -> 0.84."""
    out = clamp_confidence_provenance(0.836, SourceClass.PRIMARY, CEILINGS["agp_max"])
    assert out == 0.84
    assert out <= 0.836   # FAILS


# ── Ensemble paths: floor-rounding is claimed here — hold it to that ────

@pytest.mark.parametrize("score", SCORES)
def test_clamp_with_ensemble_tight_agreement_floor_only(score):
    out, reason = clamp_with_ensemble(score, [score, score])  # tight agreement
    assert out <= score


@pytest.mark.parametrize("score", SCORES)
def test_panel_apply_no_objections_never_raises(score):
    pv = PanelVerdict(objections=[], provenance=None)
    out, reason = pv.apply(score)
    assert out <= score, f"PanelVerdict.apply raised {score} -> {out}"


@pytest.mark.parametrize("score", SCORES)
def test_apply_panel_verdict_full_path_never_raises(score):
    pv = PanelVerdict(objections=[], provenance=None)
    out, _, _ = apply_panel_verdict(score, pv, evaluations=[score, score])
    assert out <= score


def test_compounding_round_trip_creep():
    """F1 compounding: each module's round-up is small, but a score that
    traverses several seals/reviews picks up +0.004 each time. Ten honest
    round-trips move 0.8351 to 0.84+ purely through 'neutral' code."""
    s = 0.8351
    for _ in range(10):
        s, _ = Adversary.apply_verdict(s, [])
    assert s <= 0.8351   # FAILS: s == 0.84
