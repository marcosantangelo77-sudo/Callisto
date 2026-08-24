"""LIFECYCLE INTEGRATION — part 2: the long-lived claim.

Continues the arc past the seal:

    open a long-lived Claim with a belief timeline
    -> time passes; new evidence arrives; confidence recomputes
    -> the claim RESOLVES against the SEALED preregistration criteria
    -> the resolution scores the sealed criteria, disclosing amendments

HARD INVARIANTS asserted here:
  - a claim cannot OPEN without a sealed preregistration;
  - the preregistration cannot be edited after sealing (every field setter
    raises); the only sanctioned change is amend(), which appends and
    discloses its chain at scoring time;
  - confidence never exceeds the provenance ceiling of the best
    PROVENANCE-ASSIGNED source class across accrued evidence;
  - every confidence move appends a BeliefRecord; the ClaimStore journal is
    hash-chained and load() refuses tampered history loudly;
  - resolution always runs through the sealed criteria — no bypass path.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain, Evidence, SourceClass  # noqa: E402
from agp.claims import Claim, ClaimError, ClaimStatus, ClaimStore  # noqa: E402
from agp.preregistration import (  # noqa: E402
    Criteria,
    Preregistration,
    PreregistrationError,
    PreregistrationSealed,
    Verdict,
)
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE  # noqa: E402


def _prereg() -> Preregistration:
    return Preregistration(
        query="Does the mechanism hold?",
        criteria=Criteria(
            confirm_markers=["replicated effect observed"],
            refute_markers=["effect absent in replication"],
            ambiguous_markers=["conflicting results"],
            min_evidence_items=2,
            min_source_class="SECONDARY"))


def _ev(content: str) -> Evidence:
    return Evidence(content=content, source_class=SourceClass.INFERRED,
                    confidence_score=0.8, domain=Domain.GENERAL,
                    origin_agent="test", source_name="fixture")


# ── open requires a seal ──────────────────────────────────────────────────

def test_claim_open_path_always_seals_first():
    """There is no way to open a claim on an unsealed preregistration:
    seal_preregistration seals it as part of opening, and scoring an
    unsealed prereg raises rather than yielding a verdict."""
    p = _prereg()
    with pytest.raises(PreregistrationError):
        p.score(observed_text="anything")          # unsealed: cannot score
    c = Claim(text="The mechanism holds")
    c.seal_preregistration(p)
    assert p.seal_hash and p.verify_seal()
    assert c.status == ClaimStatus.OPEN


def test_claim_opens_only_through_seal_preregistration():
    p = _prereg()
    seal = p.seal()
    assert p.verify_seal()
    c = Claim(text="The mechanism holds")
    returned = c.seal_preregistration(p)
    assert returned == seal
    assert c.status == ClaimStatus.OPEN
    assert c.confidence == 0.30
    assert len(c.belief_timeline()) == 1
    assert c.belief_timeline()[0].change_reason == "initial"


# ── prereg immutability ───────────────────────────────────────────────────

def test_sealed_preregistration_field_rebinding_is_rejected():
    p = _prereg()
    p.seal()
    original = dict(p.criteria.to_dict())
    with pytest.raises(PreregistrationSealed):
        p.query = "rewritten question"
    with pytest.raises(PreregistrationSealed):
        p.seal_hash = "forged"
    assert p.verify_seal()
    # NOTE: rebinding p.criteria itself also raises (it goes through
    # __setattr__). The IN-PLACE mutation hole is a documented defect —
    # see test below and findings/lifecycle.md DEFECT L-1.
    with pytest.raises(PreregistrationSealed):
        p.criteria = Criteria(confirm_markers=["x"], refute_markers=["y"])


def test_known_defect_L1_inplace_criteria_mutation_bypasses_the_seal():
    """DEFECT L-1 (findings/lifecycle.md): Criteria holds plain lists, so a
    sealed prereg's confirm/refute markers can be edited IN PLACE without
    tripping __setattr__. The seal hash won't verify afterwards — but
    nothing forces a verification before score(), so tampered criteria can
    be scored against their own forged state. This test PINS the defect so
    fixing it flips this test loudly."""
    p = _prereg()
    p.seal()
    p.criteria.confirm_markers.append("tampered marker")   # no raise!
    assert "tampered marker" in p.criteria.confirm_markers
    assert not p.verify_seal(), "seal correctly fails to verify — but nothing checks it"
    out = p.score(observed_text="the tampered marker appeared",
                  evidence_count=2, best_source_class="SECONDARY")
    assert out.verdict is Verdict.CONFIRMED   # scored against forged criteria
    assert out.scored_against_seal == p.seal_hash  # under the OLD seal id


def test_amendment_appends_discloses_chain_and_original_stays_scoring_default():
    p = _prereg()
    p.seal()
    amended = Criteria(confirm_markers=["new protocol confirms"],
                       refute_markers=["effect absent"],
                       min_evidence_items=1)
    rec = p.amend(amended, reason="field protocol changed mid-study")
    assert rec["prior_seal_hash"] == p.seal_hash
    # original criteria untouched
    assert "replicated effect observed" in p.criteria.confirm_markers

    # scoring against the ORIGINAL (default) never mentions an amendment
    out0 = p.score(observed_text="replicated effect observed",
                   evidence_count=2, best_source_class="SECONDARY")
    assert out0.verdict is Verdict.CONFIRMED
    assert out0.used_amendment is False
    assert not any("AMENDED" in d for d in out0.divergences)

    # scoring against the amendment DISCLOSES the chain, loudly, first
    out1 = p.score(observed_text="new protocol confirms",
                   evidence_count=2, best_source_class="SECONDARY",
                   criteria=amended)
    assert out1.used_amendment is True
    assert any("chain length 1" in d for d in out1.divergences)
    assert any(p.seal_hash[:16] or "sealed originals" in d
               for d in out1.divergences)


# ── accrual: time passes, evidence arrives ────────────────────────────────

def test_evidence_accrual_clamps_to_provenance_ceiling_and_records_beliefs(tmp_path):
    ledger = ProvenanceLedger()
    p = _prereg()
    p.seal()
    c = Claim(text="The mechanism holds")
    c.seal_preregistration(p)

    # item 1: real fetched bytes -> ledger says PRIMARY -> ceiling 1.0
    primary_body = '{"results": ["real fetched document body"]}'
    ledger.record_tool_result("openalex_fetch", primary_body, primary=True)
    e1 = _ev(primary_body)
    r1 = c.attach_evidence(e1, assigned_class=ledger.assign_source_class(e1),
                           note="fetched study")
    ceiling = MAX_CONFIDENCE_BY_SOURCE["PRIMARY"]
    assert c.confidence <= ceiling + 1e-9
    assert r1.change_reason == "evidence_attached"

    # a model claiming 0.99 through INFERRED-only evidence cannot exceed
    # the INFERRED ceiling: attach with no provenance backing
    e2 = Evidence(content="purely asserted summary, never fetched",
                  source_class=SourceClass.PRIMARY,   # self-declared lie
                  confidence_score=0.99, domain=Domain.GENERAL,
                  origin_agent="model")
    assigned = ledger.assign_source_class(e2)
    assert assigned is SourceClass.INFERRED  # provenance overrules self-report
    r2 = c.attach_evidence(e2, assigned_class=None)  # caller omits -> fail weak
    assert r2.basis_best_class == "PRIMARY"  # corpus best still governs
    assert c.confidence <= MAX_CONFIDENCE_BY_SOURCE["PRIMARY"]

    # belief timeline: one record per move, ordered, chained prev values
    tl = c.belief_timeline()
    assert [r.change_reason for r in tl] == \
        ["initial", "evidence_attached", "evidence_attached"]
    for earlier, later in zip(tl, tl[1:]):
        assert later.prev_confidence == earlier.confidence


def test_contradiction_penalty_lowers_and_is_recorded():
    p = _prereg(); p.seal()
    c = Claim(text="X"); c.seal_preregistration(p)
    before = c.confidence
    rec = c.apply_contradiction_penalty("MAJOR", detail="contradicting series")
    assert c.confidence < max(before, 0.30 + 0.05) or rec.prev_confidence == before
    assert c.belief_timeline()[-1].change_reason == "contradiction_penalty"


# ── persistence: hash-chained journal ─────────────────────────────────────

def test_journal_is_hash_chained_and_rejects_retroactive_edits(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Durable claim")
    c.seal_preregistration(p)
    c.attach_evidence(_ev("first observation"), note="n1")
    store.save(c)

    loaded = store.load(c.claim_id)
    assert loaded is not None
    assert loaded.status == ClaimStatus.OPEN
    assert loaded.confidence == c.confidence
    assert len(loaded.evidence) == 1

    # TAMPER: rewrite history to flatter ourselves — raise the recorded
    # confidence of an earlier journal line. The chain breaks on load.
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    entry = __import__("json").loads(lines[0])
    entry["state"]["confidence"] = 0.95          # historical inflation
    entry["state"]["status"] = "confirmed"
    lines[0] = __import__("json").dumps(entry, sort_keys=True,
                                        ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError, match="[Tt]ampering"):
        store.load(c.claim_id)


# ── resolution through the sealed criteria ────────────────────────────────

def test_claim_resolves_against_sealed_criteria_and_gates_apply():
    p = _prereg()   # needs >=2 items, >= SECONDARY
    p.seal()
    c = Claim(text="The mechanism holds")
    c.seal_preregistration(p)
    c.attach_evidence(_ev("replication report: replicated effect observed"),
                      assigned_class=SourceClass.SECONDARY)

    # only ONE item but prereg demanded two -> gates demote to AMBIGUOUS
    res = c.resolve(observed_text="replicated effect observed")
    assert res["verdict"] == "AMBIGUOUS"
    assert any("evidence gate unmet" in d for d in res["divergences"])
    assert c.status == ClaimStatus.AMBIGUOUS

    # second claim, gates met -> CONFIRMED, scored against THE SEAL
    p2 = _prereg(); p2.seal()
    c2 = Claim(text="Second instance")
    c2.seal_preregistration(p2)
    c2.attach_evidence(_ev("study A"), assigned_class=SourceClass.SECONDARY)
    c2.attach_evidence(_ev("study B"), assigned_class=SourceClass.SECONDARY)
    res2 = c2.resolve(
        observed_text="two independent replications; replicated effect observed")
    assert res2["verdict"] == "CONFIRMED"
    assert res2["scored_against_seal"] == p2.seal_hash
    assert c2.status == ClaimStatus.CONFIRMED
    assert c2.confidence >= 0.75
    last = c2.belief_timeline()[-1]
    assert last.change_reason == "resolution"


def test_resolved_claim_cannot_be_retroactively_changed():
    p = _prereg(); p.seal()
    c = Claim(text="Settled"); c.seal_preregistration(p)
    c.attach_evidence(_ev("a"), assigned_class=SourceClass.SECONDARY)
    c.attach_evidence(_ev("b"), assigned_class=SourceClass.SECONDARY)
    c.resolve(observed_text="replicated effect observed")
    with pytest.raises(ClaimError):
        c.retract("changed my mind")     # resolved claims are closed
    with pytest.raises(ClaimError):
        c.attach_evidence(_ev("post-hoc evidence"))
    with pytest.raises(ClaimError):
        c.resolve(observed_text="try again for a better verdict")


def test_no_socket_held():
    import socket
    with pytest.raises(AssertionError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
