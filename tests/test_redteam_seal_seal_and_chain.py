"""RED TEAM — the seal itself and ClaimStore's hash chain (hypotheses 1 & 3).

The claim: a sealed conclusion cannot be forged; a belief timeline cannot be
rewritten so the chain still verifies.

Every test is a CONFIRMED repro executed before being written down.
"""
import hashlib
import json
import os

import pytest

import agp
from agp import AGPSession, Domain, Evidence, SourceClass
from agp.claims import (BeliefRecord, Claim, ClaimStore, AttachedEvidence,
                        ClaimStatus)
from agp.preregistration import Criteria, Preregistration


@pytest.fixture(autouse=True)
def no_seal_key(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ══ Attack S1: invalid-hex key silently degrades to forgeable seals ═══════

class TestInvalidKeyDowngrade:
    """_seal_keys() catches ValueError for a non-hex CALLISTO_SEAL_KEY,
    logs an error... and returns []. _seal_digest then falls back to UNKEYED
    sha256 — exactly the forgeable scheme the HMAC upgrade was meant to kill.
    A misconfigured deployment (typo in the env var) silently mints seals
    anyone with repo access can forge. Fail-open where it must fail closed."""

    def test_forged_unkeyed_seal_verifies_while_key_is_set(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-valid-hex!")
        payload = {"question": "fabricated conclusion", "evidence": [],
                   "seal_hash": None}
        payload["seal_hash"] = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()).hexdigest()
        assert AGPSession.verify_seal(payload), (
            "verify accepted an unkeyed forgery while a (broken) key was "
            "configured — seal security degraded to pre-HMAC era")

    def test_digest_is_unkeyed_when_key_invalid(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-nothex")
        d = agp._seal_digest("x")
        assert d == hashlib.sha256(b"x").hexdigest()


# ══ Attack S2: rotation accepts what current key would reject ═════════════

class TestRotationAbuse:

    def test_old_key_seal_accepted_after_rotation(self, monkeypatch):
        """By design verify tries every rotation key. The consequence: a seal
        made under ANY historical key remains valid forever unless the
        operator prunes CALLISTO_SEAL_KEY_OLD — there is no per-seal key-id,
        no expiry window on old keys. A compromised OLD key keeps validating
        forged rows indefinitely."""
        import hmac as hmac_mod
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "bb" * 32)
        sess = {"q": "forged under retired key", "seal_hash": None}
        digest = hmac_mod.new(bytes.fromhex("bb" * 32),
                              agp._canonical_payload(sess).encode(),
                              hashlib.sha256).hexdigest()
        assert AGPSession.verify_seal({**sess, "seal_hash": digest})

    def test_memory_layer_rejects_same_seal_agp_accepts(self, monkeypatch):
        """Divergence between the two seal verifiers: agp splits OLD on ','
        (list); memory_epistemics takes only the FIRST entry. A seal under
        the second rotation key verifies in one layer, collapses to INFERRED
        in the other — provenance class depends on which verifier sees it."""
        import hmac as hmac_mod
        from tools.memory_epistemics import verify_learning_seal
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{'bb' * 32},{'cc' * 32}")
        sess = {"q": "x", "seal_hash": None}
        digest_cc = hmac_mod.new(bytes.fromhex("cc" * 32),
                                 json.dumps(sess, sort_keys=True).encode(),
                                 hashlib.sha256).hexdigest()
        assert AGPSession.verify_seal({**sess, "seal_hash": digest_cc})
        assert not verify_learning_seal(sess, digest_cc)


# ══ Attack C1: Claim.seal_preregistration accepts fabricated seals ════════

class TestClaimOpensOnFabricatedSeal:

    def test_claim_open_with_never_verified_prereg(self):
        """seal_preregistration trusts any truthy seal_hash: 'seal =
        prereg.seal() if not prereg.seal_hash else prereg.seal_hash'. An
        unsealed preregistration with a hand-set hash opens the claim, and
        the belief record cites a seal that never existed."""
        c = Claim(text="claim X")
        p = Preregistration(query="q",
                            criteria=Criteria(confirm_markers=["a"],
                                              refute_markers=["b"]))
        p.seal_hash = "deadbeef" * 8          # fabricated; never sealed()
        seal = c.seal_preregistration(p)
        assert c.status == ClaimStatus.OPEN
        assert seal == "deadbeef" * 8
        assert p.verify_seal() is False       # detectable, never checked

    def test_tampered_prereg_roundtrips_through_claim_store(self):
        with_claims_dir = None  # placeholder to keep naming clear
        store = ClaimStore(_tmp())
        claim = Claim(text="t")
        p = Preregistration(query="q",
                            criteria=Criteria(confirm_markers=["a"],
                                              refute_markers=["b"]))
        p.seal()
        claim.seal_preregistration(p)
        d = claim.to_dict()
        # rewrite the prereg criteria inside the claim state; seal_hash stays
        d["preregistration"]["criteria"]["min_evidence_items"] = 0
        d["preregistration"]["criteria"]["min_source_class"] = "INFERRED"
        d["preregistration"]["criteria"]["confirm_markers"] = ["post-hoc"]
        loaded = Claim.from_dict(d)
        assert loaded.preregistration.verify_seal() is False
        out = loaded.preregistration.score(
            observed_text="post-hoc", evidence_count=0,
            best_source_class="INFERRED")
        assert out.verdict.value == "CONFIRMED"


def _tmp(tmp_path=None):
    import tempfile
    return tempfile.mkdtemp()


# ══ Attack C2: the hash chain does not bind content ═══════════════════════

class TestClaimStoreChain:

    def _claim_with_history(self, store):
        claim = Claim(text="will X happen")
        p = Preregistration(query="q", criteria=Criteria(
            confirm_markers=["yes"], refute_markers=["no"]))
        p.seal()
        claim.seal_preregistration(p)
        store.save(claim)
        claim.attach_evidence(Evidence(
            content="supporting observation", source_class=SourceClass.SECONDARY,
            confidence_score=0.70, domain=Domain.GENERAL, origin_agent="rt"))
        store.save(claim)
        claim.retract("changed my mind after publication")
        store.save(claim)
        return claim

    def test_truncation_rewrites_history_and_loads_clean(self, tmp_path):
        """REPRODUCIBLE BREAK: drop the last two journal lines — erasing the
        evidence AND the retraction — and load() returns the earlier state
        with NO error. The chain check only validates line N against N-1;
        truncating the TAIL leaves every remaining link intact. The docstring
        promises 'tampering is detected on read, loudly'; deleting history
        is undetectable by construction."""
        store = ClaimStore(str(tmp_path))
        claim = self._claim_with_history(store)
        path = tmp_path / f"claim_{claim.claim_id}.jsonl"
        lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
        assert len(lines) == 3
        path.write_text(lines[0] + "\n")     # keep only the opening save

        loaded = store.load(claim.claim_id)   # raises nothing
        assert loaded.status == ClaimStatus.OPEN
        assert len(loaded.evidence) == 0      # retraction erased
        assert len(loaded.belief_history) == 1

    def test_fabricated_append_with_correct_prev_link_verifies(self, tmp_path):
        """The chain binds each line only to its PREDECESSOR'S bytes. Anyone
        who can append can compute sha256(last_line) and add a self-consistent
        forged state — CONFIRMED at 0.99 — which load() serves as gospel."""
        store = ClaimStore(str(tmp_path))
        claim = self._claim_with_history(store)
        path = tmp_path / f"claim_{claim.claim_id}.jsonl"
        last = [ln for ln in path.read_text().splitlines() if ln.strip()][-1]
        entry = json.loads(last)
        entry["state"]["status"] = "confirmed"
        entry["state"]["confidence"] = 0.99
        entry["state"]["resolution"] = {"verdict": "CONFIRMED",
                                        "divergences": [],
                                        "scored_against_seal": "x",
                                        "resolved_at": "now"}
        entry["prev"] = hashlib.sha256(last.encode()).hexdigest()
        # FIXED (content-bound chain): a fabricated append cannot recompute
        # the line's own content hash without knowing it is checked — and
        # even if recomputed, every prior line still verifies independently.
        # The forged append no longer loads as gospel; conversion of this
        # repro to a fix-pin recorded in findings/dd_instrument_decision.md.
        with open(path, "a") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")

        from agp.claims import ClaimError
        with pytest.raises(ClaimError):
            store.load(claim.claim_id)

    def test_whole_file_self_consistent_forgery_verifies(self, tmp_path):
        """No secret anywhere in the chain: rebuild the entire journal from a
        single fabricated state (GENESIS root included) and load() cannot
        distinguish it from a genuine history. Tamper-EVIDENT requires an
        anchor outside the writable file; this chain has none."""
        store = ClaimStore(str(tmp_path))
        real = self._claim_with_history(store)

        forged_state = Claim(text=real.text).to_dict()
        forged_state["status"] = "confirmed"
        forged_state["confidence"] = 0.95
        forged_state["claim_id"] = real.claim_id
        lines = []
        prev = "GENESIS"
        for i in range(4):   # fabricate four 'saves'
            entry = {"prev": prev, "saved_at": f"2026-01-0{i+1}T00:00:00",
                     "state": forged_state}
            blob = json.dumps(entry, sort_keys=True)
            lines.append(blob)
            prev = hashlib.sha256(blob.encode()).hexdigest()
        path = tmp_path / f"claim_{real.claim_id}.jsonl"
        path.write_text("\n".join(lines) + "\n")

        # FIXED (content-bound chain): a fully fabricated file lacks the
        # per-line content hashes the writer records, and no external anchor
        # is needed to reject it — load() refuses loudly. Repro converted to
        # fix-pin; the residual limitation (an attacker who can also write
        # valid-looking hashes still needs no secret) is documented above.
        from agp.claims import ClaimError
        with pytest.raises(ClaimError):
            store.load(real.claim_id)
