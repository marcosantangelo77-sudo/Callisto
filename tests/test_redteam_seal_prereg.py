"""RED TEAM — preregistration immutability (hypotheses 1 & 2).

The claim: a sealed Preregistration cannot be mutated after sealing, and
scoring always runs against the sealed criteria.

Every test below is a CONFIRMED repro executed against
agp/preregistration.py before being written down.
"""
import copy
import dataclasses
import pickle

import pytest

from agp.preregistration import (
    Criteria,
    Preregistration,
    PreregistrationSealed,
    Verdict,
)


def _sealed_prereg() -> Preregistration:
    p = Preregistration(
        query="does policy X move metric Y?",
        criteria=Criteria(
            confirm_markers=["metric rose"],
            refute_markers=["metric fell"],
            min_evidence_items=2,
            min_source_class="SECONDARY",
        ),
    )
    p.seal()
    return p


# ══ Attack P1: forged amendment injected through the mutable list ═════════

class TestAmendmentListForgery:
    """amendments is in _SEALED_MUTABLE — writable post-seal BY DESIGN —
    but NOTHING ever verifies an entry the object did not create itself.
    amend() seals each record; score()/effective_criteria() verify none."""

    def test_forged_amendment_becomes_effective_criteria(self):
        p = _sealed_prereg()
        forged = {
            "reason": "retroactive relaxation",
            "amended_at": "2099-01-01T00:00:00+00:00",
            "new_criteria": {
                "confirm_markers": ["anything at all"],
                "refute_markers": ["x"],
                "min_evidence_items": 0,          # gates removed too
                "min_source_class": "INFERRED",
            },
            "prior_seal_hash": p.seal_hash,
            "seal": "forged-not-verified",         # amend() would HMAC this
        }
        p.amendments.append(forged)

        assert p.effective_criteria.to_dict()["confirm_markers"] == \
            ["anything at all"]
        out = p.score(observed_text="anything at all happened",
                      evidence_count=0, best_source_class="INFERRED")
        # Scored against the FORGED criteria: evidence gate 0 < 0 passes,
        # class gate passes, marker fires -> CONFIRMED with zero evidence.
        assert out.verdict == Verdict.CONFIRMED, (
            "forged amendment produced a verdict the sealed original "
            "could never give")
        assert out.used_amendment is True
        # The disclosure line names the chain length but verifies nothing.
        assert any("AMENDED criteria" in d for d in out.divergences)

    def test_spliced_amendment_reorders_chain_undetected(self):
        """A legit amendment followed by a spliced EARLIER forged one:
        chain length is disclosed, ordering/seals never checked."""
        p = _sealed_prereg()
        p.amend(Criteria(confirm_markers=["real"], refute_markers=["r"]),
                "legitimate reason")
        p.amendments.insert(0, {
            "reason": "f", "amended_at": "earlier",
            "new_criteria": {"confirm_markers": ["fake"], "refute_markers":
                             ["y"], "min_evidence_items": 0},
            "prior_seal_hash": "0" * 64, "seal": "0" * 64,
        })
        # Latest amendment wins, so the real one still governs — but the
        # history presented to any auditor now contains an unverifiable
        # record indistinguishable in shape from a sanctioned one.
        assert len(p.amendments) == 2


# ══ Attack P2: __setattr__ lock defeats ═══════════════════════════════════

class TestSetattrLockDefeats:

    def test_normal_setattr_is_blocked(self):
        with pytest.raises(PreregistrationSealed):
            _sealed_prereg().query = "tampered"

    def test_object_setattr_bypasses_lock(self):
        """REPRODUCIBLE BREAK: object.__setattr__ skips __setattr__ entirely;
        the lock is one overridden method, not a property/frozen-dataclass."""
        p = _sealed_prereg()
        object.__setattr__(p, "query", "tampered question")
        assert p.query == "tampered question"

    def test_dict_mutation_changes_scoring_without_detection(self):
        """criteria lives in __dict__ as a mutable object; mutating IT is
        untouched by the attribute lock. verify_seal() would catch it — but
        score() never calls verify_seal(), so scoring happily runs against
        criteria that no longer match the seal."""
        p = _sealed_prereg()
        p.__dict__["criteria"].confirm_markers.append("injected-later")
        out = p.score(observed_text="the record says injected-later",
                      evidence_count=5, best_source_class="PRIMARY")
        assert out.verdict == Verdict.CONFIRMED
        assert p.verify_seal() is False  # detectable, but score() didn't look

    def test_score_does_not_verify_seal(self):
        """The structural hole behind the previous test: nothing in the
        score path recomputes the seal before trusting self.criteria."""
        p = _sealed_prereg()
        p.__dict__["query"] = "rewritten"
        assert p.verify_seal() is False
        # score() still executes normally:
        p.score(observed_text="", evidence_count=0)


# ══ Attack P3: copy / pickle present tampered objects as sealed ═══════════

class TestCopyPickle:

    def test_deepcopy_yields_freely_mutable_sealed_copy(self):
        p2 = copy.deepcopy(_sealed_prereg())
        p2.criteria.refute_markers.clear()
        p2.criteria.confirm_markers = ["whatever the model found"]
        # The copy still CARRIES _sealed=True and the original seal_hash,
        # so anything downstream checking 'is this sealed?' is satisfied.
        assert p2._sealed is True

    def test_pickle_roundtrip_of_presealed_tampered_object(self):
        """Fabricate fields BEFORE pickling (pre-seal writes are legal),
        flip _sealed manually, unpickle: a 'sealed' preregistration whose
        criteria contain markers chosen AFTER seeing the evidence."""
        p = Preregistration(
            query="q",
            criteria=Criteria(confirm_markers=["up"], refute_markers=["down"]))
        p.criteria.confirm_markers.append("injected-after-seeing-data")
        p.seal_hash = "f" * 64      # fabricated, never a real seal
        p._sealed = True            # freeze the lie into the artifact
        p3 = pickle.loads(pickle.dumps(p))
        assert p3._sealed is True
        assert p3.verify_seal() is False
        out = p3.score(observed_text="contains injected-after-seeing-data",
                       evidence_count=1, best_source_class="SECONDARY")
        assert out.verdict == Verdict.CONFIRMED


# ══ Attack P4: persistence loads without verifying ════════════════════════

class TestFromDictNoVerification:

    def test_from_dict_accepts_tampered_criteria_as_sealed(self):
        p = _sealed_prereg()
        d = p.to_dict()
        d["criteria"]["confirm_markers"] = ["post-hoc marker"]
        d["criteria"]["min_evidence_items"] = 0
        d["criteria"]["min_source_class"] = "INFERRED"
        # seal_hash left pointing at the ORIGINAL criteria — mismatch is
        # detectable but from_dict never checks it.
        loaded = Preregistration.from_dict(d)
        assert loaded._sealed is True
        assert loaded.verify_seal() is False
        out = loaded.score(observed_text="post-hoc marker seen",
                           evidence_count=0, best_source_class="INFERRED")
        assert out.verdict == Verdict.CONFIRMED

    def test_from_dict_sets_sealed_flag_from_presence_alone(self):
        d = _sealed_prereg().to_dict()
        d["seal_hash"] = "totally-fabricated"
        loaded = Preregistration.from_dict(d)
        assert loaded._sealed is True   # bool(seal_hash), no verification
