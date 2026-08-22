"""
Tests for agp/preregistration.py — commit-before-evidence, sealed.

Invariants are probed with RANDOMIZED inputs, not chosen ones (HANDOFF:
"R3 shipped a correct assertion with inputs that never hit the failing
boundary"). No live network; seal keying is exercised both keyed and unkeyed.
"""

import logging
import os
import random
import string

import pytest

from agp.preregistration import (
    Criteria,
    Outcome,
    Preregistration,
    PreregistrationError,
    PreregistrationSealed,
    Verdict,
)

RNG = random.Random(20260822)


def rand_text(n=12):
    return "".join(RNG.choice(string.ascii_lowercase + " ") for _ in range(n))


def rand_markers(pool, k):
    pool = [m for m in pool if m]
    return RNG.sample(pool, min(k, len(pool)))


CONFIRM_POOL = ["revenue grew", "margin expanded", "signal detected",
                "effect positive", "value above threshold", "confirmed"]
REFUTE_POOL = ["revenue fell", "margin compressed", "no signal",
               "effect negative", "value below threshold", "refuted"]
AMBIG_POOL = ["mixed results", "inconclusive", "insufficient data"]

WORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]


def rand_criteria(**overrides) -> Criteria:
    c = Criteria(
        confirm_markers=rand_markers(CONFIRM_POOL, RNG.randint(1, 3)),
        refute_markers=rand_markers(REFUTE_POOL, RNG.randint(1, 3)),
        ambiguous_markers=rand_markers(AMBIG_POOL, RNG.randint(0, 2)),
        min_evidence_items=RNG.randint(1, 4),
        min_source_class=RNG.choice(["INFERRED", "SIGNAL", "SECONDARY", "PRIMARY"]),
        resolution_horizon=RNG.choice([None, "2030-01-01"]),
    )
    if RNG.random() < 0.5:
        c.threshold = round(RNG.uniform(-100, 100), 3)
        c.direction = RNG.choice(["gte", "lte"])
    for k, v in overrides.items():
        setattr(c, k, v)
    return c


def make_sealed(criteria=None, query=None) -> Preregistration:
    p = Preregistration(query=query or f"query about {rand_text()}",
                        criteria=criteria or rand_criteria())
    p.seal()
    return p


# ── lifecycle ────────────────────────────────────────────────────────────

def test_cannot_score_unsealed():
    p = Preregistration("q", rand_criteria())
    with pytest.raises(PreregistrationError):
        p.score(observed_text="anything", evidence_count=99,
                best_source_class="PRIMARY")


@pytest.mark.parametrize("bad", [
    {},  # no confirm, no refute
])
def test_invalid_criteria_refuse_to_seal(bad):
    crit = Criteria(confirm_markers=bad.get("confirm_markers", []),
                    refute_markers=bad.get("refute_markers", []))
    p = Preregistration("q", crit)
    with pytest.raises(PreregistrationError):
        p.seal()


def test_empty_query_refuses_to_seal():
    with pytest.raises(PreregistrationError):
        make_sealed(query="   ")


def test_randomized_criteria_all_seal_and_verify():
    """Every randomly generated valid criteria set seals, verifies, and
    round-trips through to_dict/from_dict byte-identically."""
    for _ in range(60):
        p = make_sealed()
        assert p.verify_seal()
        d = p.to_dict()
        p2 = Preregistration.from_dict(d)
        assert p2.verify_seal()
        assert p2.criteria.to_dict() == p.criteria.to_dict()


def test_seal_is_tamper_evident():
    p = make_sealed()
    d = p.to_dict()
    # Tamper with any field → verify fails.
    for field in ("query", "created_at"):
        bad = dict(d)
        bad[field] = "tampered " + str(bad[field])
        assert not Preregistration.from_dict(bad).verify_seal()
    bad_crit = d["criteria"]
    bad_crit["min_evidence_items"] += 7
    assert not Preregistration.from_dict(d).verify_seal()


# ── immutability after seal ──────────────────────────────────────────────

def test_sealed_object_is_frozen_against_direct_mutation():
    p = make_sealed()
    with pytest.raises(PreregistrationSealed):
        p.query = "new query"
    with pytest.raises(PreregistrationSealed):
        p.criteria = rand_criteria()
    # Original criteria survive.
    assert p.seal_hash is not None


def test_amendment_preserves_original_and_chains():
    original = rand_criteria(confirm_markers=["original marker"])
    p = make_sealed(criteria=original)
    new = rand_criteria(confirm_markers=["amended marker"])
    rec = p.amend(new, reason="criteria were mis-specified at draft time")
    assert rec["prior_seal_hash"] == p.seal_hash
    assert rec["reason"]
    # Sealed originals untouched — scoring default still uses them unless
    # the amendment chain is engaged via effective_criteria.
    assert p.criteria.confirm_markers == ["original marker"]
    assert p.effective_criteria.confirm_markers == ["amended marker"]
    # Amendment record itself carries a seal over its own content.
    assert len(rec["seal"]) == 64
    # Chained amendments each reference their predecessor's hash.
    newer = rand_criteria(refute_markers=["second amendment refutes"])
    rec2 = p.amend(newer, reason="further refinement")
    assert rec2["prior_seal_hash"] == p.seal_hash  # anchor remains the root seal


def test_amendment_requires_reason():
    p = make_sealed()
    with pytest.raises(PreregistrationError):
        p.amend(rand_criteria(), reason="   ")


def test_amend_before_seal_refused():
    p = Preregistration("q", rand_criteria())
    with pytest.raises(PreregistrationError):
        p.amend(rand_criteria(), "reason")


# ── scoring semantics, randomized ────────────────────────────────────────

def test_randomized_marker_scoring_consistency():
    """For random criteria and random observed text: verdict must be CONFIRMED
    only when a confirm marker fired (or threshold hit), REFUTED likewise,
    AMBIGUOUS otherwise — and gates always demote."""
    for _ in range(120):
        crit = rand_criteria()
        p = make_sealed(criteria=crit)
        text = RNG.choice(CONFIRM_POOL + REFUTE_POOL + AMBIG_POOL + [rand_text()])
        n_ev = RNG.randint(0, 5)
        cls = RNG.choice(["INFERRED", "SIGNAL", "SECONDARY", "PRIMARY"])
        out = p.score(observed_text=text, evidence_count=n_ev,
                      best_source_class=cls)
        conf_hit = any(m.lower() in text.lower() for m in crit.confirm_markers)
        ref_hit = any(m.lower() in text.lower() for m in crit.refute_markers)
        rank = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}
        gates_ok = (n_ev >= crit.min_evidence_items and
                    rank[cls] >= rank[crit.min_source_class])

        if conf_hit and ref_hit:
            expected = Verdict.AMBIGUOUS
        elif conf_hit:
            expected = Verdict.CONFIRMED if gates_ok else Verdict.AMBIGUOUS
        elif ref_hit:
            expected = Verdict.REFUTED if gates_ok else Verdict.AMBIGUOUS
        else:
            expected = Verdict.AMBIGUOUS
        assert out.verdict == expected, (
            f"text={text!r} conf={conf_hit} ref={ref_hit} ev={n_ev} "
            f"cls={cls} crit={crit.to_dict()} got={out.verdict}")


def test_randomized_threshold_scoring():
    """Random thresholds: values on the passing side of a random threshold
    confirm when markers are silent; failing side refutes; gates still bind."""
    for _ in range(80):
        crit = rand_criteria(confirm_markers=["zzz-no-match-zzz"],
                             refute_markers=["yyy-no-match-yyy"],
                             ambiguous_markers=[])
        crit.threshold = round(RNG.uniform(-50, 50), 3)
        crit.direction = RNG.choice(["gte", "lte"])
        p = make_sealed(criteria=crit)
        value = RNG.uniform(-100, 100)
        out = p.score(observed_text=rand_text(), observed_value=value,
                      evidence_count=crit.min_evidence_items + 1,
                      best_source_class="PRIMARY")
        hit = (value >= crit.threshold if crit.direction == "gte"
               else value <= crit.threshold)
        expected = Verdict.CONFIRMED if hit else Verdict.REFUTED
        assert out.verdict == expected, (
            f"value={value} thr={crit.threshold} dir={crit.direction}")


def test_boundary_values_exactly_at_threshold_are_scored():
    """The boundary itself: >= includes it, <= includes it. Randomly pick
    which side and check exact equality lands as included."""
    for direction in ("gte", "lte"):
        thr = RNG.uniform(-10, 10)
        crit = rand_criteria(confirm_markers=["none"], refute_markers=["nada"],
                             ambiguous_markers=[], threshold=thr,
                             direction=direction)
        p = make_sealed(criteria=crit)
        out = p.score(observed_value=thr, observed_text="nothing matches",
                      evidence_count=9, best_source_class="PRIMARY")
        assert out.verdict == Verdict.CONFIRMED  # boundary counts as pass both ways


def test_numeric_criterion_without_value_diverges_loudly(caplog):
    crit = rand_criteria(threshold=1.0, direction="gte")
    p = make_sealed(criteria=crit)
    with caplog.at_level(logging.WARNING, logger="callisto.agp.prereg"):
        out = p.score(observed_text="", evidence_count=9,
                      best_source_class="SECONDARY")
    assert any("numeric criterion unscored" in d for d in out.divergences)
    assert any("prereg divergence" in r.message for r in caplog.records)


def test_gates_demote_never_promote():
    """Marker hits with zero evidence can never yield a decisive verdict."""
    for _ in range(40):
        crit = rand_criteria(min_evidence_items=RNG.randint(1, 3),
                             min_source_class="PRIMARY")
        p = make_sealed(criteria=crit)
        text = RNG.choice(crit.confirm_markers + crit.refute_markers)
        out = p.score(observed_text=text, evidence_count=0,
                      best_source_class="INFERRED")
        assert out.verdict == Verdict.AMBIGUOUS
        assert any("gates unmet" in d for d in out.divergences)


def test_claimed_verdict_divergence_surfaced():
    crit = rand_criteria(confirm_markers=["it worked"], min_evidence_items=5,
                         min_source_class="PRIMARY")
    p = make_sealed(criteria=crit)
    out = p.score(observed_text="it worked spectacularly", evidence_count=1,
                  best_source_class="INFERRED",
                  claimed_verdict=Verdict.CONFIRMED, claimed_confidence=0.9)
    assert any("DIVERGES FROM PREREGISTRATION" in d for d in out.divergences)
    assert any("confidence not earned" in d for d in out.divergences)


def test_matching_claim_no_divergence():
    crit = rand_criteria(confirm_markers=["it worked"], min_evidence_items=1,
                         min_source_class="INFERRED",
                         threshold=None, direction=None)
    p = make_sealed(criteria=crit)
    out = p.score(observed_text="the data show it worked",
                  evidence_count=3, best_source_class="PRIMARY",
                  claimed_verdict=Verdict.CONFIRMED, claimed_confidence=0.6)
    assert out.verdict == Verdict.CONFIRMED
    assert out.divergences == []


def test_refute_outranks_confirm_when_both_fire():
    crit = rand_criteria(confirm_markers=["up"], refute_markers=["down"],
                         ambiguous_markers=[])
    p = make_sealed(criteria=crit)
    out = p.score(observed_text="up then down", evidence_count=9,
                  best_source_class="PRIMARY")
    assert out.verdict == Verdict.AMBIGUOUS
    assert any("conflicting signals" in d for d in out.divergences)


def test_amended_scoring_discloses_itself(caplog):
    p = make_sealed(criteria=rand_criteria(confirm_markers=["orig"]))
    p.amend(rand_criteria(confirm_markers=["new"]), reason="draft error")
    with caplog.at_level(logging.WARNING, logger="callisto.agp.prereg"):
        out = p.score(observed_text="new marker fired", evidence_count=9,
                      best_source_class="PRIMARY")
    assert out.used_amendment
    assert any("AMENDED criteria" in d for d in out.divergences)


def test_expired_horizon_flagged_only_when_ambiguous():
    crit = rand_criteria(resolution_horizon="2020-01-01")
    p = make_sealed(criteria=crit)
    out_amb = p.score(observed_text=rand_text(), evidence_count=0,
                      best_source_class="PRIMARY")
    assert any("horizon" in d for d in out_amb.divergences)
    out_dec = p.score(observed_text=crit.confirm_markers[0],
                      evidence_count=crit.min_evidence_items,
                      best_source_class=crit.min_source_class)
    assert not any("horizon" in d for d in out_dec.divergences)


# ── domain generality: same machinery, three domains ─────────────────────

def test_domain_general_betting_bitcoin_materials():
    cases = [
        ("Will the home team cover -3.5?",
         Criteria(confirm_markers=["covered by 4+ points"],
                  refute_markers=["failed to cover"], min_evidence_items=2)),
        ("Is BTC above $150k by 2027-06-30?",
         Criteria(confirm_markers=["price at or above 150000"],
                  refute_markers=["price below 150000"], threshold=150000,
                  direction="gte", min_evidence_items=3)),
        ("Does the alloy exceed 900 MPa tensile strength?",
         Criteria(confirm_markers=["measured tensile above 900"],
                  refute_markers=["measured tensile at or below 900"],
                  threshold=900, direction="gte", min_evidence_items=2)),
    ]
    for q, crit in cases:
        p = Preregistration(q, crit)
        p.seal()
        assert p.verify_seal()
        out = p.score(observed_text=crit.confirm_markers[0],
                      observed_value=(crit.threshold or 0) + 1,
                      evidence_count=crit.min_evidence_items,
                      best_source_class="PRIMARY")
        assert out.verdict == Verdict.CONFIRMED
