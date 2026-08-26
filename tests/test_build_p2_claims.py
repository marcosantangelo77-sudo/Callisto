"""
Tests for agp/claims.py (long-lived claims) and ResearchProgram persistence.

Randomized invariants throughout — HANDOFF: probe properties with random
inputs, not chosen ones. No network, no real DB; file stores use tmp_path.
"""

import json
import random

import os

import pytest

from agp import Domain, Evidence, SourceClass, SourceClass as SC
from agp.claims import (
    Claim,
    ClaimError,
    ClaimStatus,
    ClaimStore,
    recompute_confidence,
)
from agp.preregistration import Criteria, Preregistration, Verdict
from agp.research_program import (
    EvidenceRequirement,
    Horizon,
    ProgramStatus,
    ProgramStore,
    QuestionKind,
    QuestionStatus,
    ResearchProgram,
    ResearchQuestion,
    SourceClassRank,
)
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE

RNG = random.Random(424242)


@pytest.fixture(autouse=True)
def _default_unkeyed_seal_policy(monkeypatch):
    """Tests default to the EXPLICIT unkeyed regime (public checksums).

    Tests that exercise keyed/malformed policies delete this variable
    themselves (monkeypatch.setenv/delenv wins because it runs later within
    the test). Production has no such default: undeclared policy fails
    closed.
    """
    if not any(v in os.environ for v in ("CALLISTO_SEAL_KEY",
                                         "CALLISTO_SEAL_KEY_OLD")):
        monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")


def rand_text(n=20):
    words = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
             "golf", "hotel", "india", "juliet"]
    return " ".join(RNG.choice(words) for _ in range(RNG.randint(1, n // 3)))


ALL_CLASSES = [SC.INFERRED, SC.SIGNAL, SC.SECONDARY, SC.PRIMARY]


def rand_evidence(cls=None) -> Evidence:
    c = cls or RNG.choice(ALL_CLASSES)
    ceilings = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55,
                "INFERRED": 0.55}
    return Evidence(
        content=rand_text(),
        source_class=c,
        confidence_score=round(RNG.uniform(0.30, min(0.99, ceilings[c.value])),
                               2),
        domain=RNG.choice(list(Domain)),
        origin_agent="test-agent",
    )


def open_claim(text=None) -> tuple[Claim, Preregistration]:
    crit = Criteria(confirm_markers=["confirmed by observation"],
                    refute_markers=["refuted by observation"],
                    min_evidence_items=2, min_source_class="INFERRED")
    pre = Preregistration(query=text or rand_text(), criteria=crit)
    claim = Claim(text=pre.query,
                  domain=RNG.choice(list(Domain)))
    claim.seal_preregistration(pre)
    return claim, pre


# ══ Claim lifecycle ══════════════════════════════════════════════════════

def test_claim_cannot_open_without_sealed_prereg():
    claim = Claim("some thesis")
    assert claim.status == ClaimStatus.DRAFT
    # Attaching evidence to a draft is refused: no commitment, no claim.
    with pytest.raises(ClaimError):
        claim.attach_evidence(rand_evidence())
    with pytest.raises(ClaimError):
        claim.resolve(observed_text="x")


def test_evidence_before_seal_and_resolution_paths_refused():
    claim = Claim("draft claim")
    with pytest.raises(ClaimError):
        claim.retract("")   # needs a reason


def test_randomized_claims_open_under_seal():
    for _ in range(25):
        claim, pre = open_claim()
        assert claim.status == ClaimStatus.OPEN
        assert pre.verify_seal()
        assert claim.belief_timeline()[0].change_reason == "initial"


# ══ Evidence accrual and confidence recomputation ═══════════════════════

def test_confidence_never_exceeds_class_ceiling_randomized():
    """Core invariant: after attaching random evidence, the recomputed
    confidence never exceeds the ceiling of the best PROVENANCE-assigned
    class in the corpus — regardless of what was claimed."""
    for _ in range(100):
        items = [type("A", (), {"assigned_class": RNG.choice(ALL_CLASSES)})()
                 for _ in range(RNG.randint(0, 6))]
        claimed = RNG.uniform(0.0, 1.5)
        got = recompute_confidence(items, claimed)  # type: ignore[arg-type]
        assert 0.30 <= got <= 1.0
        if not items:
            assert got <= MAX_CONFIDENCE_BY_SOURCE["INFERRED"]
        else:
            best = max(items, key=lambda i: ["INFERRED", "SIGNAL", "SECONDARY",
                                             "PRIMARY"].index(i.assigned_class.value)) \
                if items else None
            assert got <= MAX_CONFIDENCE_BY_SOURCE[best.assigned_class.value], (
                f"claimed={claimed} best={best.assigned_class} got={got}")


def test_attach_records_when_and_why_confidence_changed():
    claim, _ = open_claim()
    ev = rand_evidence(SC.PRIMARY)
    claim.attach_evidence(ev, assigned_class=SC.PRIMARY, note="fetched bytes")
    recs = claim.belief_timeline()
    assert len(recs) == 2
    r = recs[-1]
    assert r.change_reason == "evidence_attached"
    assert r.prev_confidence == recs[0].confidence
    assert r.basis_best_class == "PRIMARY"
    assert r.basis_evidence_count == 1
    assert r.at >= claim.created_at
    # PRIMARY evidence allows the confidence to reach its evidence score.
    assert claim.confidence == round(
        max(0.30, min(ev.confidence_score, MAX_CONFIDENCE_BY_SOURCE["PRIMARY"])), 2)


def test_unverified_provenance_cannot_masquerade_as_primary():
    """attach without ledger assignment clamps high self-declared classes to
    SIGNAL — the fail-toward-weaker default."""
    claim, _ = open_claim()
    ev = rand_evidence(SC.PRIMARY)
    ev.confidence_score = 0.95
    claim.attach_evidence(ev)          # no assigned_class passed
    assert claim._best_assigned_class() == SC.SIGNAL
    assert claim.confidence <= MAX_CONFIDENCE_BY_SOURCE["SIGNAL"]


def test_contradiction_penalty_only_lowers_and_floors():
    claim, _ = open_claim()
    claim.attach_evidence(rand_evidence(SC.SECONDARY),
                          assigned_class=SC.SECONDARY)
    before = claim.confidence
    claim.apply_contradiction_penalty("MAJOR")
    assert claim.confidence < before or before == 0.30
    for _ in range(10):                # repeated penalties floor at 0.30
        claim.apply_contradiction_penalty("CRITICAL")
    assert claim.confidence == 0.30
    with pytest.raises(ClaimError):
        claim.apply_contradiction_penalty("COSMIC")


# ══ Resolution through the sealed criteria ══════════════════════════════

def test_resolution_goes_through_preregistered_criteria():
    crit = Criteria(confirm_markers=["effect reproduced"],
                    refute_markers=["effect vanished"],
                    min_evidence_items=3, min_source_class="SECONDARY")
    claim = Claim("does X reproduce?")
    claim.seal_preregistration(
        Preregistration("does X reproduce?", crit))
    for _ in range(3):
        claim.attach_evidence(rand_evidence(SC.SECONDARY),
                              assigned_class=SC.SECONDARY)
    res = claim.resolve(observed_text="the effect reproduced in cohort B")
    assert res["verdict"] == Verdict.CONFIRMED.value
    assert claim.status == ClaimStatus.CONFIRMED
    assert claim.confidence >= 0.75
    last = claim.belief_timeline()[-1]
    assert last.change_reason == "resolution"
    # Resolved claims are closed.
    with pytest.raises(ClaimError):
        claim.attach_evidence(rand_evidence())
    with pytest.raises(ClaimError):
        claim.retract("too late")


def test_gate_unmet_resolution_is_ambiguous_not_confirmed():
    """Preregistered evidence gates bind at resolution time even when the
    text matches — randomized over gate sizes."""
    for _ in range(30):
        need = RNG.randint(2, 6)
        crit = Criteria(confirm_markers=["it held up"],
                        refute_markers=["it broke"], min_evidence_items=need,
                        min_source_class="INFERRED")
        claim = Claim("gated claim")
        claim.seal_preregistration(Preregistration("q", crit))
        attached = RNG.randint(0, need - 1)   # strictly below the gate
        for _i in range(attached):
            claim.attach_evidence(rand_evidence(), assigned_class=SC.INFERRED)
        res = claim.resolve(observed_text="it held up under every test")
        assert res["verdict"] == Verdict.AMBIGUOUS.value, (
            f"need={need} attached={attached}")
        assert any("gate" in d.lower() for d in res["divergences"])


def test_refutation_resolves_low():
    crit = Criteria(confirm_markers=["up"], refute_markers=["down"],
                    min_evidence_items=1, min_source_class="INFERRED")
    claim = Claim("directional claim")
    claim.seal_preregistration(Preregistration("q", crit))
    claim.attach_evidence(rand_evidence(SC.INFERRED),
                          assigned_class=SC.INFERRED)
    claim.resolve(observed_text="everything went down")
    assert claim.status == ClaimStatus.REFUTED
    assert claim.confidence <= 0.30


# ══ Belief history IS the calibration record ════════════════════════════

def test_belief_history_is_complete_monotonic_append_only():
    claim, _ = open_claim()
    n = RNG.randint(1, 8)
    prev_scores = [claim.confidence]
    for i in range(n):
        claim.attach_evidence(rand_evidence(), assigned_class=RNG.choice(ALL_CLASSES))
        if RNG.random() < 0.3:
            claim.apply_contradiction_penalty(
                RNG.choice(["CRITICAL", "MAJOR", "MINOR"]))
            prev_scores.append(claim.confidence)
        prev_scores.append(claim.confidence)
    tl = claim.belief_timeline()
    assert len(tl) == len(prev_scores)
    assert [r.confidence for r in tl] == prev_scores
    assert all(r.prev_confidence == prev_scores[i - 1] if i else
               r.prev_confidence is None for i, r in enumerate(tl))


def test_query_history_what_did_we_believe():
    claim, _ = open_claim()
    claim.attach_evidence(rand_evidence(SC.SECONDARY),
                          assigned_class=SC.SECONDARY)
    claim.apply_contradiction_penalty("MINOR", detail="sources conflict")
    answer = [
        {"when": r.at, "believed": r.confidence, "tier": r.tier,
         "basis": r.basis_evidence_count, "why": r.change_reason}
        for r in claim.belief_timeline()]
    assert answer[0]["why"] == "initial"
    assert answer[-1]["why"] == "contradiction_penalty"


# ══ Persistence: close and reopen weeks later ═══════════════════════════

def test_claim_store_round_trip_with_chain_verification(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    claim, _ = open_claim("BTC dominance thesis")
    claim.attach_evidence(rand_evidence(SC.PRIMARY), assigned_class=SC.PRIMARY)
    store.save(claim)
    claim.apply_contradiction_penalty("MAJOR", detail="counter-evidence")
    store.save(claim)

    loaded = store.load(claim.claim_id)
    assert loaded is not None
    assert loaded.to_dict() == claim.to_dict()
    assert len(loaded.belief_timeline()) == len(claim.belief_timeline())


def test_tampering_detected_on_read(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    claim, _ = open_claim("tamper target")
    claim.attach_evidence(rand_evidence(), assigned_class=SC.SECONDARY)
    store.save(claim)
    claim.apply_contradiction_penalty("MINOR")
    store.save(claim)

    path = tmp_path / "claims" / f"claim_{claim.claim_id}.jsonl"
    lines = path.read_text().splitlines()

    # Rewrite entry 1's state (retroactive edit of an earlier belief).
    entry = json.loads(lines[0])
    entry["state"]["confidence"] = 0.95     # flatter ourselves retroactively
    tampered = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join([tampered, lines[1]]) + "\n")

    with pytest.raises(ClaimError, match="tampering"):
        store.load(claim.claim_id)


def test_store_rejects_path_traversal(tmp_path):
    store = ClaimStore(str(tmp_path))
    for bad in ("../evil", "a/b", "", ".", ".."):
        with pytest.raises(ValueError):
            store._journal_path(bad)


def test_load_missing_returns_none(tmp_path):
    assert ClaimStore(str(tmp_path)).load("nonexistent") is None


def test_list_ids_round_trip(tmp_path):
    store = ClaimStore(str(tmp_path / "c"))
    ids = set()
    for _ in range(3):
        c, _ = open_claim()
        store.save(c)
        ids.add(c.claim_id)
    assert set(store.list_ids()) == ids


# ══ ResearchProgram persistence ═════════════════════════════════════════

def _random_program() -> ResearchProgram:
    prog = ResearchProgram(root_query=rand_text(),
                           domain=RNG.choice(["GENERAL", "FINANCIAL",
                                              "TECHNICAL"]))
    for i in range(RNG.randint(1, 3)):
        predictive = RNG.random() < 0.5
        q = ResearchQuestion(
            text=rand_text(),
            kind=QuestionKind.PREDICTIVE if predictive
            else RNG.choice([QuestionKind.DESCRIPTIVE, QuestionKind.CAUSAL]),
            priority=RNG.random(),
            evidence_requirements=EvidenceRequirement(
                min_source_class=RNG.choice(list(SourceClassRank)),
                min_independent_sources=RNG.randint(1, 4),
                quant_required=RNG.random() < 0.5),
            horizon=(Horizon(__import__("datetime").date(2027, 1, 1),
                             __import__("datetime").date(2028, 6, 1))
                     if predictive else None),
            lifecycle_link=RNG.choice([None, "hyp_123"]),
        )
        if RNG.random() < 0.5:
            q.children.append(ResearchQuestion(
                text=rand_text(),
                kind=RNG.choice(list(QuestionKind)),
                priority=RNG.random()))
            q.children[-1].horizon = Horizon(
                __import__("datetime").date(2027, 1, 1),
                __import__("datetime").date(2029, 1, 1)) \
                if q.children[-1].kind == QuestionKind.PREDICTIVE else None
            if q.children[-1].kind == QuestionKind.PREDICTIVE:
                q.children[-1].kind = QuestionKind.DESCRIPTIVE  # keep valid
                q.children[-1].horizon = None
        prog.questions.append(q)
    return prog


def test_program_round_trip_preserves_full_state():
    for _ in range(40):
        prog = _random_program()
        assert prog.validate() == [], prog.validate()
        d = prog.to_dict()
        back = ResearchProgram.from_dict(d)
        assert back.to_dict() == d
        assert back.fingerprint() == prog.fingerprint()


def test_program_close_and_reopen_weeks_later(tmp_path):
    store = ProgramStore(str(tmp_path / "programs"))
    prog = _random_program()
    # Mutate state post-creation: statuses flip like weeks passing.
    leaf = prog.leaves[0]
    leaf.status = QuestionStatus.ANSWERED
    prog.status = ProgramStatus.SUSPENDED
    store.save(prog)

    reopened = store.load(prog.program_id)
    assert reopened is not None
    assert reopened.status == ProgramStatus.SUSPENDED
    assert reopened.find(leaf.question_id).status == QuestionStatus.ANSWERED
    assert reopened.fingerprint() == prog.fingerprint()
    assert prog.program_id in store.list_ids()


def test_program_store_refuses_invalid_program(tmp_path):
    store = ProgramStore(str(tmp_path / "p"))
    bad = ResearchProgram(root_query="")           # empty root query
    with pytest.raises(ValueError):
        store.save(bad)


# ══ Domain generality smoke: three domains, identical behavior ═══════════

@pytest.mark.parametrize("domain", [Domain.FINANCIAL, Domain.GENERAL,
                                    Domain.TECHNICAL])
def test_identical_lifecycle_in_any_domain(domain):
    crit = Criteria(confirm_markers=["target reached"], refute_markers=["missed"],
                    threshold=100.0, direction="gte", min_evidence_items=1,
                    min_source_class="INFERRED")
    claim = Claim(f"a {'price' if domain == Domain.FINANCIAL else 'value'} "
                  f"prediction", domain=domain)
    claim.seal_preregistration(Preregistration("q", crit))
    claim.attach_evidence(rand_evidence(SC.SECONDARY),
                          assigned_class=SC.SECONDARY)
    claim.resolve(observed_value=120.0, observed_text="target reached")
    assert claim.status == ClaimStatus.CONFIRMED
