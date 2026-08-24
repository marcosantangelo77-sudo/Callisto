"""REVIEW — preregistration seal never verified (A6 JOB-3 item 1, still open).

Family 1: a verification layer that never actually runs.

Preregistration.seal() HMACs query+criteria+created_at. Preregistration
.verify_seal() can detect tampering. But verify_seal() has ZERO production
callers, and from_dict() sets _sealed = bool(seal_hash) — presence of any
64-hex STRING counts as sealed. ClaimStore.load() verifies only the journal
prev-hash chain, which an attacker who can edit the journal recomputes for
free (plain SHA-256, unkeyed — A6 JOB-3 item 4). So: rewrite the criteria in
a saved claim's journal (recompute two hashes), reload, resolve against
criteria that were never sealed, and the claim scores CONFIRMED with zero
divergences while its seal is invalid.

Repros below FAIL on current master. The fix is one line at each seam:
verify_seal() must gate score() and/or ClaimStore.load().
"""
import hashlib
import json

import pytest

from agp.claims import Claim, ClaimStore, ClaimStatus
from agp.preregistration import Criteria, Preregistration


def _sealed_prereg():
    p = Preregistration(
        query="Will GDP growth exceed 2%?",
        criteria=Criteria(confirm_markers=["gdp grew"], refute_markers=["recession"],
                          min_evidence_items=1, min_source_class="INFERRED"))
    p.seal()
    return p


def test_tampered_criteria_score_confirmed_despite_invalid_seal():
    """Rewriting confirm_markers after sealing must NOT yield CONFIRMED."""
    p = _sealed_prereg()
    d = p.to_dict()
    assert p.verify_seal() is True  # sanity: honest state verifies

    # attacker edits the criteria in the serialized record
    d["criteria"]["confirm_markers"] = ["whatever i like"]
    p2 = Preregistration.from_dict(d)

    # the seal no longer matches the content...
    assert p2.verify_seal() is False
    # ...but score() runs anyway and CONFIRMS against the forged criteria.
    out = p2.score(observed_text="the economy did whatever i like",
                   evidence_count=3, best_source_class="PRIMARY")
    assert out.verdict.value != "CONFIRMED" or out.divergences, (
        "score() confirmed against criteria whose seal does not verify")


def test_tampered_journal_claim_loads_and_resolves_confirmed():
    """Full end-to-end: tamper the persisted claim journal, reload, resolve.

    The journal chain is unkeyed SHA-256, so an attacker recomputes it in
    two lines; load() then hands back a claim whose embedded preregistration
    was never verified, and resolve() mints CONFIRMED + confidence >= 0.75.
    """
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        store = ClaimStore(td)
        claim = Claim(text="GDP growth exceeds 2% this year")
        claim.seal_preregistration(_sealed_prereg())
        from agp import Domain, Evidence, SourceClass
        claim.attach_evidence(Evidence(
            content="gdp grew 2.4%", source_class=SourceClass.PRIMARY,
            confidence_score=0.8, domain=Domain.FINANCIAL,
            origin_agent="review", source_name="s"))
        store.save(claim)
        cid = claim.claim_id

        path = os.path.join(td, f"claim_{cid}.jsonl")
        lines = [ln for ln in open(path).read().splitlines() if ln.strip()]
        entry = json.loads(lines[-1])
        entry["state"]["preregistration"]["criteria"]["confirm_markers"] = [
            "whatever i like"]
        blob = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        new_last = json.dumps({"prev": "GENESIS", "saved_at": entry["saved_at"],
                               "state": entry["state"]},
                              sort_keys=True, ensure_ascii=False)
        with open(path, "w") as f:
            f.write(new_last + "\n")  # single-line journal: chain rebuilt free

        loaded = store.load(cid)          # chain check passes (attacker re-hashed)
        assert loaded is not None
        assert loaded.preregistration.verify_seal() is False  # seal IS broken
        loaded.resolve(observed_text="outcome: whatever i like")
        assert loaded.status != ClaimStatus.CONFIRMED or \
            loaded.preregistration.verify_seal(), (
            "claim resolved CONFIRMED against criteria whose seal does not "
            "verify (load() never checks the embedded preregistration seal)")


def test_score_refuses_when_seal_does_not_verify():
    """The invariant that SHOULD hold: scoring requires a verifying seal."""
    p = _sealed_prereg()
    d = p.to_dict()
    d["criteria"]["confirm_markers"] = ["forged marker"]
    p2 = Preregistration.from_dict(d)
    with pytest.raises(Exception):
        p2.score(observed_text="forged marker observed", evidence_count=3,
                 best_source_class="PRIMARY")
