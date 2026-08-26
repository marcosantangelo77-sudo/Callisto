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

import hashlib
import json

import os

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain, Evidence, SourceClass  # noqa: E402
from agp.claims import (Claim, ClaimError, ClaimStatus,
                        ClaimStore, _now_iso)  # noqa: E402
from agp.preregistration import (  # noqa: E402
    Criteria,
    Preregistration,
    PreregistrationError,
    PreregistrationSealed,
    Verdict,
)
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE  # noqa: E402


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


# ── per-entry integrity seals: tail & single-entry tampering ─────────────

def _write_journal(path, entries):
    import json as _json
    path.write_text(
        "\n".join(_json.dumps(e, sort_keys=True, ensure_ascii=False)
                  for e in entries) + "\n")


def test_single_entry_journal_tampering_rejected(tmp_path):
    # One-entry journal: no successor exists to bind the first line, so the
    # per-entry seal is the ONLY thing standing between the attacker and a
    # flattering state.
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Solo claim")
    c.seal_preregistration(p)
    store.save(c)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    entry["state"]["confidence"] = 0.99
    lines[0] = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError, match="integrity seal"):
        store.load(c.claim_id)


def test_final_entry_tampering_rejected(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Tail claim")
    c.seal_preregistration(p)
    store.save(c)
    c.attach_evidence(_ev("more observation"), note="n2")
    store.save(c)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    entry = json.loads(lines[1])          # the TAIL entry
    entry["state"]["confidence"] = 0.99   # flatter ourselves
    entry["state"]["status"] = "confirmed"
    lines[1] = json.dumps(entry, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError, match="integrity seal"):
        store.load(c.claim_id)


def test_sealed_entry_round_trips_and_intermediate_tampering_still_caught(tmp_path):
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Chain claim")
    c.seal_preregistration(p)
    store.save(c)
    snapshot = c.to_dict()
    c.attach_evidence(_ev("obs"), note="n")
    store.save(c)

    loaded = store.load(c.claim_id)
    assert loaded.to_dict() == c.to_dict()

    # Intermediate tampering: caught by BOTH chain and (if only line 0's own
    # digest mattered) its seal.
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    e0 = json.loads(lines[0])
    e0["state"]["confidence"] = 0.95
    lines[0] = json.dumps(e0, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")
    with pytest.raises(ClaimError, match="tampering"):
        store.load(c.claim_id)
    assert snapshot["status"] == "open"


def test_legacy_unsigned_journal_fails_closed_by_default_opt_in_reads(tmp_path):
    # Legacy policy evidence: a journal written by pre-seal code (entries with
    # only prev/saved_at/state) FAILS CLOSED on default load(); an explicit
    # allow_legacy_unsigned=True read still applies chain checks but accepts
    # the unverifiable tail. Verification is never silently weakened.
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Legacy claim")
    c.seal_preregistration(p)
    legacy_entry = {"prev": "GENESIS", "saved_at": c.created_at,
                    "state": c.to_dict()}
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    _write_journal(path, [legacy_entry])

    with pytest.raises(ClaimError, match="no integrity seal"):
        store.load(c.claim_id)

    loaded = store.load(c.claim_id, allow_legacy_unsigned=True)
    assert loaded is not None
    assert loaded.confidence == c.confidence

    # Even in legacy mode, a broken chain is still rejected.
    bad = dict(legacy_entry, state=dict(c.to_dict(), confidence=0.99))
    _write_journal(path, [{"prev": "WRONG", "saved_at": "x",
                           "state": c.to_dict()}, legacy_entry])
    with pytest.raises(ClaimError, match="chain to its predecessor"):
        store.load(c.claim_id, allow_legacy_unsigned=True)


def test_old_key_journal_loads_after_rotation(tmp_path, monkeypatch):
    # Journal sealed under the OLD key must still verify after rotating
    # CALLISTO_SEAL_KEY to a new current key, per the project's existing
    # rotation policy (CALLISTO_SEAL_KEY_OLD lists accepted prior keys).
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Rotation claim")
    c.seal_preregistration(p)
    store.save(c)

    # Rotate to a NEW current key; the old key becomes an accepted rotation key.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "bb" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "aa" * 32)
    loaded = store.load(c.claim_id)
    assert loaded is not None and loaded.to_dict() == c.to_dict()

    # Without the old key listed, the seal cannot be verified -> fail closed.
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "dd" * 32)
    with pytest.raises(ClaimError, match="integrity seal"):
        store.load(c.claim_id)


def test_stripped_seal_downgrade_rejected_even_with_legacy_opt_in(tmp_path):
    # Downgrade attack: mutate a signed tail AND delete only its seal. The
    # legacy opt-in applies solely to WHOLLY unsigned journals; a single
    # unsigned entry beside sealed entries is tampering, never legacy.
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Downgrade claim")
    c.seal_preregistration(p)
    store.save(c)
    store.save(c)  # two signed entries

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    del tail["seal"]                    # strip ONLY the seal from the tail
    lines[-1] = json.dumps(tail, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError,
                       match="sealed and unsigned|no integrity seal"):
        store.load(c.claim_id)
    with pytest.raises(ClaimError, match="sealed and unsigned"):
        store.load(c.claim_id, allow_legacy_unsigned=True)


def test_wholly_unsigned_journal_opt_in_still_works_after_mixed_guard(tmp_path):
    # A wholly unsigned legacy journal remains readable via explicit opt-in.
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Pure legacy")
    c.seal_preregistration(p)
    e0 = {"prev": "GENESIS", "saved_at": c.created_at, "state": c.to_dict()}
    c.attach_evidence(_ev("legacy obs"), note="l1")
    e1 = {"prev": None, "saved_at": c.created_at, "state": c.to_dict()}
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    raw0 = json.dumps(e0, sort_keys=True, ensure_ascii=False)
    import hashlib as _h
    e1["prev"] = _h.sha256(raw0.encode()).hexdigest()
    _write_journal(path, [e0, e1])

    loaded = store.load(c.claim_id, allow_legacy_unsigned=True)
    assert loaded is not None


def test_invalid_current_key_seal_fails_closed(tmp_path, monkeypatch):
    # Sealed under key A; loading under unrelated key B (no rotation list)
    # must fail closed, not silently fall back.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "11" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Wrong key claim")
    c.seal_preregistration(p)
    store.save(c)

    monkeypatch.setenv("CALLISTO_SEAL_KEY", "22" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    with pytest.raises(ClaimError, match="integrity seal"):
        store.load(c.claim_id)


def test_public_digest_never_substitutes_for_hmac(tmp_path, monkeypatch):
    # Attack: with CALLISTO_SEAL_KEY configured, tamper with a keyed entry
    # and replace its HMAC seal with the public sha256 of the seal payload.
    # The public digest must never substitute for an HMAC under a key ring.
    import hashlib as _h
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Digest downgrade claim")
    c.seal_preregistration(p)
    store.save(c)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    forged = dict(tail, state=dict(tail["state"], confidence=0.99))
    forged["seal"] = _h.sha256(
        ClaimStore._entry_seal_payload(
            forged["prev"], forged["saved_at"], forged["state"]
        ).encode("utf-8")).hexdigest()
    lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError,
                       match="bare-string seal|failed its integrity seal"):
        store.load(c.claim_id)
    # Even the legacy opt-in must not resurrect a forged keyed entry.
    with pytest.raises(ClaimError,
                       match="bare-string seal|failed its integrity seal"):
        store.load(c.claim_id, allow_legacy_unsigned=True)


def test_unkeyed_journal_still_loads_without_key_configured(tmp_path, monkeypatch):
    # Control: a journal written and read entirely without keys keeps its
    # unkeyed SHA-256 seals valid — the legacy boundary is deployment-wide,
    # never a per-entry fallback inside a keyed history.
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Unkeyed control claim")
    c.seal_preregistration(p)
    store.save(c)
    loaded = store.load(c.claim_id)
    assert loaded is not None and loaded.to_dict() == c.to_dict()

    # But once a key IS configured, those same public-digest seals fail
    # closed rather than silently bridging into a keyed deployment.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "cd" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    with pytest.raises(ClaimError, match="integrity seal"):
        store.load(c.claim_id)


def _write_sealed_journal(store_dir, monkeypatch, key_hex):
    """Helper: write a one-entry journal under the given key config."""
    import hashlib as _h
    monkeypatch.setenv("CALLISTO_SEAL_KEY", key_hex)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(store_dir))
    p = _prereg(); p.seal()
    c = Claim(text="Keyed claim")
    c.seal_preregistration(p)
    store.save(c)
    return store, c


def test_malformed_current_key_public_digest_fails_closed(tmp_path, monkeypatch):
    # Blocker 1: configured-but-invalid CALLISTO_SEAL_KEY must NOT be treated
    # as an unkeyed deployment; a forged public SHA-256 seal must raise.
    import hashlib as _h
    store, c = _write_sealed_journal(tmp_path / "claims", monkeypatch, "ab" * 32)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    forged = dict(tail, state=dict(tail["state"], confidence=0.99))
    payload = ClaimStore._entry_seal_payload(
        forged["prev"], forged["saved_at"], forged["state"])
    provenance = json.dumps({"alg": "sha256"}, sort_keys=True,
                            separators=(",", ":"))
    forged["seal"] = {"alg": "sha256",
                      "digest": _h.sha256(
                          (payload + "\n" + provenance).encode()).hexdigest()}
    lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    # Malformed current key: fail closed, never "unkeyed".
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex")
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    with pytest.raises(ClaimError):
        store.load(c.claim_id)
    # Even legacy opt-in cannot resurrect it under malformed keyed policy.
    with pytest.raises(ClaimError):
        store.load(c.claim_id, allow_legacy_unsigned=True)


def test_malformed_old_only_key_public_digest_fails_closed(tmp_path, monkeypatch):
    # Blocker 1b: invalid old-key-only configuration also fails closed when
    # an attacker substitutes a public SHA-256 digest for the HMAC.
    import hashlib as _h
    store, c = _write_sealed_journal(tmp_path / "claims", monkeypatch, "ab" * 32)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    forged = dict(tail, state=dict(tail["state"], status="confirmed"))
    payload = ClaimStore._entry_seal_payload(
        forged["prev"], forged["saved_at"], forged["state"])
    provenance = json.dumps({"alg": "sha256"}, sort_keys=True,
                            separators=(",", ":"))
    forged["seal"] = {"alg": "sha256",
                      "digest": _h.sha256(
                          (payload + "\n" + provenance).encode()).hexdigest()}
    lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "zz-not-hex")
    with pytest.raises(ClaimError):
        store.load(c.claim_id)


def test_regime_change_keyed_history_not_downgraded_after_key_removal(
        tmp_path, monkeypatch):
    # Blocker 3: a journal entry written by pre-provenance code under a
    # valid key carries a BARE STRING seal (HMAC or public digest cannot be
    # distinguished once the writing-time key configuration is gone). After
    # both key variables are removed, such an entry must FAIL CLOSED rather
    # than silently load as a legitimate unkeyed SHA-256 digest.
    import hashlib as _h
    store, c = _write_sealed_journal(tmp_path / "claims", monkeypatch, "ef" * 32)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    forged = dict(tail, state=dict(tail["state"], confidence=0.99))
    payload = ClaimStore._entry_seal_payload(
        forged["prev"], forged["saved_at"], forged["state"])

    # Key variables removed; the attacker downgrades to the HISTORICAL
    # bare-string public-digest format. With NO external policy declared,
    # load() now FAILS CLOSED at the policy layer (regime is never inferred
    # from journal bytes — blocker 3).
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    forged["seal"] = _h.sha256(payload.encode("utf-8")).hexdigest()
    lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ClaimError,
                       match="no seal policy configured|bare-string"):
        store.load(c.claim_id)
    with pytest.raises(ClaimError,
                       match="no seal policy configured|bare-string"):
        store.load(c.claim_id, allow_legacy_unsigned=True)

    # Even with an EXPLICIT unkeyed opt-in, a bare-string historical seal
    # (ambiguous HMAC vs public digest) cannot be authenticated and must
    # route through migrate_legacy_journal(), never silently load.
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    with pytest.raises(ClaimError, match="bare-string seal"):
        store.load(c.claim_id)

    # Control: with the WRITING key configured again, the bare string still
    # fails closed (public digest never substitutes for an HMAC).
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ef" * 32)
    with pytest.raises(ClaimError, match="bare-string seal"):
        store.load(c.claim_id)


def test_valid_unkeyed_journal_with_alg_marker_loads(tmp_path, monkeypatch):
    # Control matching the regime-change policy: a genuinely unkeyed journal
    # written and read without any key variable still loads.
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims2"))
    p = _prereg(); p.seal()
    c = Claim(text="Unkeyed marker claim")
    c.seal_preregistration(p)
    store.save(c)
    assert store.load(c.claim_id) is not None
    entry = json.loads((tmp_path / "claims2" /
                        f"claim_{c.claim_id}.jsonl").read_text().splitlines()[0])
    assert entry["seal"]["alg"] == "sha256"


def test_wholly_stripped_keyed_history_rejected_despite_legacy_opt_in(
        tmp_path, monkeypatch):
    # Blocker 4: with a valid active key, strip EVERY seal, rebuild public
    # prev links, alter states, then load with allow_legacy_unsigned=True.
    # A wholly stripped keyed history is tampering, never a legacy journal.
    import hashlib as _h
    store, c = _write_sealed_journal(tmp_path / "claims", monkeypatch, "99" * 32)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    line = json.loads(path.read_text().splitlines()[0])
    stripped = {"prev": "GENESIS", "saved_at": line["saved_at"],
                "state": dict(line["state"], confidence=0.99)}
    path.write_text(json.dumps(stripped, sort_keys=True) + "\n")

    with pytest.raises(ClaimError, match="keyed policy forbids unsigned"):
        store.load(c.claim_id, allow_legacy_unsigned=True)
    with pytest.raises(ClaimError, match="keyed policy forbids unsigned"):
        store.load(c.claim_id)


def test_wholly_stripped_history_without_any_key_still_legacy_opt_in(
        tmp_path, monkeypatch):
    # Control: the same stripped/unsigned journal read in a deployment with
    # NO key variables remains reachable only via explicit opt-in (legacy
    # compatibility preserved exactly where no keyed policy exists).
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path))
    p = _prereg(); p.seal()
    c = Claim(text="Pure legacy after removal")
    c.seal_preregistration(p)
    e0 = {"prev": "GENESIS", "saved_at": c.created_at, "state": c.to_dict()}
    path = tmp_path / f"claim_{c.claim_id}.jsonl"
    _write_journal(path, [e0])

    with pytest.raises(ClaimError, match="no integrity seal"):
        store.load(c.claim_id)
    loaded = store.load(c.claim_id, allow_legacy_unsigned=True)
    assert loaded is not None


def test_tail_truncation_needs_write_access_not_the_key(tmp_path, monkeypatch):
    # Honest limitation pinned as documentation-by-test: deleting the signed
    # tail requires ONLY filesystem write access; load() succeeds on the
    # truncated prefix because no external head/count anchor exists.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "77" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Truncation claim")
    c.seal_preregistration(p)
    store.save(c)
    c.attach_evidence(_ev("later evidence"), note="n")
    store.save(c)  # two signed entries

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    path.write_text(lines[0] + "\n")   # writer drops the tail; no key needed

    loaded = store.load(c.claim_id)   # validly sealed prefix loads
    assert loaded is not None
    assert loaded.to_dict() != c.to_dict()   # trailing entries are gone


# ── explicit strict seal-policy regime (blockers 1–5) ────────────────────

def test_save_fails_closed_on_malformed_current_key(tmp_path, monkeypatch):
    # Blocker 1: save() must fail closed on malformed config BEFORE any
    # write — no public {alg:"sha256"} entry may ever be emitted.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex")
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Write-side fail closed")
    c.seal_preregistration(p)
    with pytest.raises(ClaimError, match="malformed"):
        store.save(c)
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    assert not path.exists()          # nothing written, no public fallback
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
    store.save(c)                     # fixed config writes fine
    entry = json.loads(path.read_text().splitlines()[0])
    assert entry["seal"]["alg"] == "hmac-sha256"


def test_mixed_valid_invalid_key_ring_rejected_on_save_and_load(
        tmp_path, monkeypatch):
    # Blocker 2: every nonblank configured token must validate. Invalid
    # current + valid old, or valid current + invalid old: both fail closed
    # on save AND load; no token is silently dropped.
    p = _prereg(); p.seal()
    for cur, old in (("not-hex", "bb" * 32), ("aa" * 32, "zz-bad")):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", cur)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", old)
        monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
        store = ClaimStore(str(tmp_path / f"m{cur[:3]}"))
        c = Claim(text="mixed ring"); c.seal_preregistration(p)
        with pytest.raises(ClaimError):
            store.save(c)
        # Even a pre-existing journal cannot be read under a broken ring.
        (tmp_path / f"m{cur[:3]}" /
         f"claim_{c.claim_id}.jsonl").write_text("{}\n")
        with pytest.raises(ClaimError, match="malformed|corrupt|expected"):
            store.load(c.claim_id)


def test_old_key_only_config_cannot_authorize_new_writes(tmp_path,
                                                         monkeypatch):
    # Blocker 2 clarification: old keys are verification-only. An old key
    # alone (no valid current) is a malformed keyed config and fails closed.
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "bb" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    store = ClaimStore(str(tmp_path / "oldonly"))
    p = _prereg(); p.seal()
    c = Claim(text="old-only ring"); c.seal_preregistration(p)
    with pytest.raises(ClaimError):
        store.save(c)


def test_keyed_history_unloadable_after_key_removal_without_explicit_policy(
        tmp_path, monkeypatch):
    # Blocker 3: regime is an EXTERNAL policy anchor. After writing under a
    # key and then removing all key configuration, the journal must NOT load
    # as an inferred-unkeyed history: undeclared policy fails closed. Key
    # removal alone never transforms a keyed policy into public-checksum
    # verification.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="External anchor claim"); c.seal_preregistration(p)
    store.save(c)

    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    with pytest.raises(ClaimError, match="no seal policy configured"):
        store.load(c.claim_id)
    # Explicit unkeyed opt-in also fails per-entry (HMAC seals can't verify
    # publicly); it never re-authenticates keyed entries.
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    with pytest.raises(ClaimError):
        store.load(c.claim_id)


def test_envelope_rewrite_to_public_sha256_under_keyed_policy_rejected(
        tmp_path, monkeypatch):
    # Blocker 3 attack shape: attacker removes keys from THEIR config,
    # rewrites alg to sha256, recomputes the public digest, and loads.
    # Under the deployment's keyed external policy this must fail closed;
    # and with no policy at all, it fails closed at the policy layer.
    import hashlib as _h
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "cd" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Rewrite attack"); c.seal_preregistration(p)
    store.save(c)

    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    lines = path.read_text().splitlines()
    tail = json.loads(lines[-1])
    forged = dict(tail, state=dict(tail["state"], status="confirmed"))
    payload = ClaimStore._entry_seal_payload(
        forged["prev"], forged["saved_at"], forged["state"])
    prov = json.dumps({"alg": "sha256"}, sort_keys=True, separators=(",", ":"))
    forged["seal"] = {"alg": "sha256",
                      "digest": _h.sha256(
                          (payload + "\n" + prov).encode()).hexdigest()}
    lines[-1] = json.dumps(forged, sort_keys=True, ensure_ascii=False)
    path.write_text("\n".join(lines) + "\n")

    # Deployment still declares keyed policy: rewrite rejected.
    with pytest.raises(ClaimError):
        store.load(c.claim_id)
    # Attacker ALSO controls config and deletes the policy: fail closed at
    # the policy layer (regime never inferred from journal bytes).
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    with pytest.raises(ClaimError, match="no seal policy configured"):
        store.load(c.claim_id)
    # Explicit unkeyed opt-in: HONEST LIMIT pinned. The forged tail is a
    # well-formed public sha256 seal over altered state, so it LOADS once
    # the attacker sets the external policy to unkeyed. No journal-only
    # marker can defend against an attacker who also controls the external
    # configuration; unkeyed is tamper-EVIDENCE, not authenticity.
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    loaded = store.load(c.claim_id)
    assert loaded.status == "confirmed"
    assert json.loads(path.read_text())["seal"]["alg"] == "sha256"


def test_absent_vs_null_vs_malformed_seals_and_scalar_entries(
        tmp_path, monkeypatch):
    # Blocker 4: absent seal vs present-null are distinct; null seal under
    # legacy opt-in is NOT accepted as missing; malformed envelopes and
    # scalar/odd entry shapes raise ClaimError, never AttributeError/
    # TypeError/KeyError leaks.
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Envelope shapes"); c.seal_preregistration(p)
    state = c.to_dict()
    base = {"prev": "GENESIS", "saved_at": _now_iso(), "state": state}
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"

    def expect_claim_error(raw_lines):
        path.write_text("\n".join(raw_lines) + "\n")
        with pytest.raises(ClaimError):
            store.load(c.claim_id)
        with pytest.raises(ClaimError):
            store.load(c.claim_id, allow_legacy_unsigned=True)

    good_blob = json.dumps({**base, "seal":
                            store._entry_seal("GENESIS", base["saved_at"],
                                              state, "unkeyed", [])},
                           sort_keys=True)
    # sanity control
    path.write_text(good_blob + "\n")
    assert store.load(c.claim_id) is not None

    # NULL seal: distinct from ABSENT — never accepted as legacy-missing.
    expect_claim_error([json.dumps({**base, "seal": None}, sort_keys=True)])
    # Malformed envelope shapes -> ClaimError (not incidental exceptions).
    expect_claim_error([json.dumps({**base, "seal": ["x"]})])
    expect_claim_error([json.dumps({**base, "seal": {"digest": 12}})])
    expect_claim_error([json.dumps({**base, "seal": {"digest": {}}})])
    expect_claim_error([json.dumps({**base, "seal": {"alg": 9,
                                                     "digest": "x"}})])
    # Scalar entry / wrong container shapes.
    expect_claim_error(['[1,2,3]'])
    expect_claim_error(['"just a string"'])
    expect_claim_error(['42'])
    expect_claim_error([json.dumps({"prev": "GENESIS"})])       # missing fields


def test_explicit_unkeyed_policy_round_trip_and_checksum_semantics(
        tmp_path, monkeypatch):
    # Explicit unkeyed regime: works without any key, produces public
    # SHA-256 checksums (documented as tamper-EVIDENCE, not authenticity).
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Explicit unkeyed"); c.seal_preregistration(p)
    store.save(c)
    assert store.load(c.claim_id).to_dict() == c.to_dict()
    entry = json.loads((tmp_path / "claims" /
                        f"claim_{c.claim_id}.jsonl").read_text())
    assert entry["seal"]["alg"] == "sha256"
    # Constructor API equivalent works too.
    store2 = ClaimStore(str(tmp_path / "c2"), seal_policy="unkeyed")
    c2 = Claim(text="Ctor unkeyed"); c2.seal_preregistration(p)
    store2.save(c2)
    assert store2.load(c2.claim_id) is not None
    # Unknown policy value fails closed; conflicting declarations fail.
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    with pytest.raises(ClaimError, match="unknown seal policy"):
        ClaimStore(str(tmp_path / "c3"), seal_policy="yolo")._policy()
    monkeypatch.delenv("CALLISTO_SEAL_POLICY", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
    with pytest.raises(ClaimError, match="conflicting seal policy|conflicts with configured"):
        ClaimStore(str(tmp_path / "c4"), seal_policy="unkeyed")._policy()


def test_rotation_current_signs_old_verifies(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "keyed")
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Rotation strict"); c.seal_preregistration(p)
    store.save(c)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "bb" * 32)
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "aa" * 32)
    assert store.load(c.claim_id) is not None
    # New entries sign under CURRENT key only.
    c.attach_evidence(_ev("post rotation"), note="n")
    store.save(c)
    entry = json.loads((tmp_path / "claims" /
                        f"claim_{c.claim_id}.jsonl").read_text().splitlines()[1])
    assert entry["seal"]["alg"] == "hmac-sha256"
    assert store.load(c.claim_id) is not None
    # allow_legacy_unsigned stays inert under keyed policy: stripping a
    # seal from any entry is still rejected even with the opt-in.
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    ls = path.read_text().splitlines()
    stripped = json.loads(ls[-1]); del stripped["seal"]
    ls[-1] = json.dumps(stripped, sort_keys=True)
    path.write_text("\n".join(ls) + "\n")
    with pytest.raises(ClaimError):
        store.load(c.claim_id, allow_legacy_unsigned=True)


def test_legacy_bare_string_journal_requires_migration_not_append(
        tmp_path, monkeypatch):
    # Blocker 5 footgun: load(verify=False) + save() used to append a new
    # envelope onto permanently unloadable history. Now: migration is the
    # ONLY route, it is atomic, operator-attested, and marks unverifiable
    # entries forever.
    import hashlib as _h
    monkeypatch.setenv("CALLISTO_SEAL_POLICY", "unkeyed")
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    store = ClaimStore(str(tmp_path / "claims"))
    p = _prereg(); p.seal()
    c = Claim(text="Bare string legacy"); c.seal_preregistration(p)
    state = c.to_dict()
    bare = {"prev": "GENESIS", "saved_at": _now_iso(), "state": state,
            "seal": _h.sha256(json.dumps(
                {"prev": "GENESIS", "saved_at": _now_iso(), "state": state},
                sort_keys=True).encode()).hexdigest()}
    path = tmp_path / "claims" / f"claim_{c.claim_id}.jsonl"
    path.write_text(json.dumps(bare, sort_keys=True) + "\n")

    # Refuses to load; refuse-to-append: saving on top does NOT paper over it
    # because load(verify=False)+save now leaves the history unloadable —
    # instead save() itself refuses while the policy can't verify? No:
    # save appends, so the FIX is that migrate_legacy_journal replaces the
    # whole file atomically; appending is pointless but harmless. Pin that
    # migration is required and works:
    with pytest.raises(ClaimError, match="bare-string"):
        store.load(c.claim_id)
    with pytest.raises(ClaimError, match="attest_unverified"):
        store.migrate_legacy_journal(c.claim_id)
    n = store.migrate_legacy_journal(c.claim_id, attest_unverified=True)
    assert n == 1
    loaded = store.load(c.claim_id)
    assert loaded is not None
    raw = path.read_text().splitlines()[0]
    entry = json.loads(raw)
    assert entry.get("migrated_unverified") is True   # provenance kept visible
    prev = hashlib.sha256(raw.encode()).hexdigest()
    # Atomic replacement boundary: append after migration chains correctly.
    c.attach_evidence(_ev("after migration"), note="n")
    store.save(c)
    assert store.load(c.claim_id) is not None
    # Migration refuses corrupt JSON outright.
    path.write_text("{not json\n")
    with pytest.raises(ClaimError, match="cannot migrate"):
        store.migrate_legacy_journal(c.claim_id, attest_unverified=True)
