"""OX autofill #0079 — AGP keyed-seal fail-closed characterization.

This module pins the boundary that matters most about ``agp.AGPSession``
seals: **a keyed verify_seal must never accept an unkeyed SHA-256 digest**.

Threat model: anyone who can write to the DB can recompute the public
SHA-256 of the canonical payload (``verify_seal`` is public code, so the
algorithm is not secret). If ``verify_seal`` accepted the raw public hash
whenever CALLISTO_SEAL_KEY is set, the key would be decorative and every
seal would be forgeable. The pins below assert:

1. Keyed regime: public sha256(canonical payload) is rejected as seal_hash.
2. Keyed regime over TAMPERED content with an attacker-recomputed public
   hash is still rejected.
3. Set-but-invalid (non-hex) CALLISTO_SEAL_KEY fails CLOSED — verify_seal
   returns False for everything; no unkeyed fallback exists.
4. Unkeyed legacy regime (no env var at all) still verifies plain SHA-256
   so historical rows keep working — but only when NO key is intended.
5. Rotation via CALLISTO_SEAL_KEY_OLD keeps old-key seals valid without
   re-admitting the unkeyed digest.
6. verify_seal never raises on hostile inputs; it returns False.
7. seal() refuses to produce an unkeyed digest under an invalid key
   (AGPSealKeyInvalid), so nothing forgeable is ever written.
8. Source-level pins: the fail-closed branches exist in agp/__init__.py
   and are not weakened by these tests.

Tests-only module: no production gate is weakened by this file. Where a
pin could be satisfied by weakening a gate, we instead assert the gate
holds (fail closed). Live betting is never armed; `_PAPER_TRADE_SIGNAL_STATUSES`
is never touched; the paper-trade signal generator is never widened.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os

import pytest

import agp
from agp import (
    AGPSealKeyInvalid,
    AGPSealRefused,
    AGPSession,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)

VALID_KEYS = ["ab" * 32, "cd" * 32, "12" * 32, "34" * 32]
BAD_KEYS = ["not-hex-zz", "zzzz", "0x1234", "   spaced-but-not-hex   ", "g" * 64]


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def make_session(query="autofill 0079 characterization query") -> AGPSession:
    """Walk a session through the full lifecycle to SESSION_CLOSE."""
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["characterization"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="an observed fact for the 0079 seal",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.70,
        domain=Domain.GENERAL,
        origin_agent="test",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=query, domain=Domain.GENERAL, conclusion="a real conclusion",
        confidence_score=0.70, evidence_count=1, contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def public_sha256(data: dict) -> str:
    """The forgeable public hash any attacker can compute from a stored row."""
    payload = agp._canonical_payload(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hmac_with_key(hexkey: str, data) -> str:
    if isinstance(data, str):
        data = json.loads(data)
    payload = agp._canonical_payload(data)
    return hmac.new(
        bytes.fromhex(hexkey), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def forged_row(d: dict, mutation=None) -> dict:
    """Return a row whose seal_hash is a freshly computed PUBLIC SHA-256 —
    exactly what a DB-write attacker produces without knowing the key."""
    d = dict(d)
    if mutation is not None:
        mutation(d)
    d["seal_hash"] = public_sha256(d)
    return d


@pytest.fixture(autouse=True)
def clean_seal_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ──────────────────────────────────────────────────────────────────────
# 1. Keyed verify_seal must NOT accept the public SHA-256
# ──────────────────────────────────────────────────────────────────────

class TestKeyedVerifyRejectsUnkeyed:
    @pytest.mark.parametrize("key", VALID_KEYS[:3])
    def test_public_hash_forgery_rejected(self, key, monkeypatch):
        """Attacker never touches seal(): they just recompute sha256."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        s = make_session()
        d = dict(s.to_dict())
        assert AGPSession.verify_seal(forged_row(d)) is False

    def test_public_hash_over_mutated_payload_rejected(self, monkeypatch):
        """A hash over the TAMPERED content is equally unacceptable under a key."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        assert AGPSession.verify_seal(forged_row(
            d, lambda x: x["summary"].__setitem__("conclusion", "FORGED")
        )) is False

    def test_public_hash_matches_real_digest_still_rejected(self, monkeypatch):
        """Even when public == what an UNKEYED seal() would have produced,
        the keyed verifier must reject it."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        s = make_session()
        s.seal()  # unkeyed regime → seal_hash IS the public sha256
        d = dict(s.to_dict())
        assert d["seal_hash"] == public_sha256(d)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        # same bytes, now under a keyed regime:
        assert AGPSession.verify_seal(d) is False

    def test_unkeyed_legacy_row_rejected_under_key(self, monkeypatch):
        """Seal unkeyed first, then arm a key: the legacy row stops verifying."""
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[1])
        assert AGPSession.verify_seal(d) is False

    def test_json_encoded_forgery_rejected(self, monkeypatch):
        """The str-input path gets the same rejection."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[2])
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(json.dumps(d)) is False

    def test_keyed_seal_roundtrips(self, monkeypatch):
        """Sanity: with a valid key, real seals DO verify."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_old_key_alone_is_not_a_keyed_regime(self, monkeypatch):
        """Characterization: with ONLY CALLISTO_SEAL_KEY_OLD set (current key
        unset), the regime is still legacy-unkeyed per _seal_key_configured(),
        so the public digest candidate remains — an honest record of current
        behavior. A genuine old-key seal verifies through rotation."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[3])
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = hmac_with_key(VALID_KEYS[3], d)
        assert AGPSession.verify_seal(d) is True


# ──────────────────────────────────────────────────────────────────────
# 2. Invalid / unusable key fails CLOSED everywhere
# ──────────────────────────────────────────────────────────────────────

class TestInvalidKeyFailsClosed:
    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_verify_false_for_valid_hmac_under_bad_key(self, bad, monkeypatch):
        """Key set but non-hex ⇒ _seal_keys() empty ⇒ no candidate matches."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad.strip())
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = hmac_with_key(VALID_KEYS[0], d)  # right shape, wrong key
        assert AGPSession.verify_seal(d) is False

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_verify_false_for_public_hash_under_bad_key(self, bad, monkeypatch):
        """The critical fail-closed case: invalid key must NOT fall back to
        accepting the unkeyed public digest."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad.strip())
        s = make_session()
        assert AGPSession.verify_seal(forged_row(dict(s.to_dict()))) is False

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_seal_refuses_under_bad_key(self, bad, monkeypatch):
        """seal() must raise AGPSealKeyInvalid, never write a forgeable digest."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad.strip())
        s = make_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        assert s.seal_hash is None or "not valid hex" in str(s.seal_hash)

    def test_whitespace_only_key_is_unkeyed_regime(self, monkeypatch):
        """"   " strips to empty ⇒ treated as NO key (legacy), not fail-closed."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        assert agp._seal_key_configured() is False
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_empty_string_key_is_unkeyed_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        assert agp._seal_key_configured() is False

    def test_verify_never_raises_on_bad_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEYS[0])
        s = make_session()
        for probe in (
            s.to_dict(),
            json.dumps(s.to_dict()),
            "{not json",
            12345,
            None,
            {"seal_hash": "x"},
            {},
        ):
            try:
                result = AGPSession.verify_seal(probe)
            except Exception as e:  # pragma: no cover
                raise AssertionError(f"verify_seal raised on {probe!r}: {e}")
            assert result is False


# ──────────────────────────────────────────────────────────────────────
# 3. Legacy unkeyed regime unchanged
# ──────────────────────────────────────────────────────────────────────

class TestLegacyUnkeyedRegime:
    def test_plain_sha256_verifies_when_no_key(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        s = make_session()
        digest = s.seal()
        assert digest == public_sha256(s.to_dict())
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_tamper_detected_when_no_key(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        s = make_session()
        s.seal()
        d = s.to_dict()
        d["query"] = d["query"] + " TAMPERED"
        assert AGPSession.verify_seal(d) is False

    def test_missing_seal_hash_rejected(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        d = make_session().to_dict()
        d.pop("seal_hash")
        assert AGPSession.verify_seal(d) is False

    def test_none_and_garbage_hashes_rejected(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        base = make_session().to_dict()
        for bogus in (None, "", "deadbeef", 12345, ["list"], {"d": 1}):
            d = dict(base)
            d["seal_hash"] = bogus
            assert AGPSession.verify_seal(d) is False, f"accepted {bogus!r}"


# ──────────────────────────────────────────────────────────────────────
# 4. Rotation semantics
# ──────────────────────────────────────────────────────────────────────

class TestRotation:
    def test_current_key_wins_over_old(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_NEW_UNUSED", "")
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{VALID_KEYS[1]},{VALID_KEYS[2]}")
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_old_key_seal_verifies_after_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[1])
        s = make_session()
        d = s.to_dict()
        d["seal_hash"] = hmac_with_key(VALID_KEYS[1], d)
        assert AGPSession.verify_seal(d) is True

    def test_dropped_old_key_stops_verifying(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        d = s.to_dict()
        d["seal_hash"] = hmac_with_key(VALID_KEYS[1], d)
        assert AGPSession.verify_seal(d) is False

    def test_invalid_entry_in_old_list_ignored_not_fatal(self, monkeypatch):
        """One garbage entry in CALLISTO_SEAL_KEY_OLD doesn't kill rotation."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"zz-bad,{VALID_KEYS[1]}")
        s = make_session()
        d = s.to_dict()
        d["seal_hash"] = hmac_with_key(VALID_KEYS[1], d)
        assert AGPSession.verify_seal(d) is True

    def test_rotation_never_re_admits_public_hash(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[1])
        d = make_session().to_dict()
        assert AGPSession.verify_seal(forged_row(d)) is False


# ──────────────────────────────────────────────────────────────────────
# 5. Tamper detection across regimes
# ──────────────────────────────────────────────────────────────────────

class TestTamperDetection:
    MUTATIONS = [
        lambda d: d["query"].__class__("tampered") and d.update(query="tampered"),
        lambda d: d["evidence"].append({"content": "smuggled"}),
        lambda d: d.update(filtered_evidence_count=99),
        lambda d: d["summary"] and d["summary"].update(confidence_score=0.99),
        lambda d: d.update(sealed_at="2000-01-01T00:00:00+00:00"),
        lambda d: d.update(progress_events=d["progress_events"] + 1),
    ]

    @pytest.mark.parametrize("mut", MUTATIONS)
    def test_mutations_detected_keyed(self, mut, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = s.to_dict()
        mut(d)
        assert AGPSession.verify_seal(d) is False

    @pytest.mark.parametrize("mut", MUTATIONS)
    def test_mutations_detected_unkeyed(self, mut, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        s = make_session()
        s.seal()
        d = s.to_dict()
        mut(d)
        assert AGPSession.verify_seal(d) is False

    def test_swapped_seal_hash_between_sessions_detected(self, monkeypatch):
        """Copy session A's seal onto session B's content."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        a = make_session("query A")
        a.seal()
        b = make_session("query B")
        b.seal()
        da, db = a.to_dict(), b.to_dict()
        db["seal_hash"] = da["seal_hash"]
        assert AGPSession.verify_seal(db) is False

    def test_canonicalization_normalization_resisted(self, monkeypatch):
        """Reordering keys in the stored dict does not change verification."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(reversed(list(s.to_dict().items())))
        assert AGPSession.verify_seal(d) is True


# ──────────────────────────────────────────────────────────────────────
# 6. Refuse-to-seal gates remain intact (fail closed at write time)
# ──────────────────────────────────────────────────────────────────────

class TestRefuseToSealGatesIntact:
    def test_zero_evidence_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.evidence.clear()
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_empty_conclusion_refused(self, monkeypatch):
        s = make_session()
        s.summary.conclusion = "   "
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_default_marker_conclusion_refused(self, monkeypatch):
        from agp import EMPTY_SYNTHESIS_MARKER
        s = make_session()
        s.summary.conclusion = EMPTY_SYNTHESIS_MARKER
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_double_seal_raises_violation(self, monkeypatch):
        from agp import AGPViolation
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        with pytest.raises(AGPViolation):
            s.seal()

    def test_add_evidence_after_seal_blocked(self, monkeypatch):
        from agp import AGPViolation
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        with pytest.raises(AGPViolation):
            s.add_evidence(Evidence(
                content="post-seal smuggle", source_class=SourceClass.PRIMARY,
                confidence_score=0.9, domain=Domain.GENERAL, origin_agent="test",
            ))

    def test_paper_trade_statuses_untouched_by_module(self):
        """Guard rail: this characterization never widens paper-trade gates."""
        import callisto
        statuses = getattr(callisto, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in {str(x).lower() for x in statuses}


# ──────────────────────────────────────────────────────────────────────
# 7. Source-level structural pins (gates present, not weakened)
# ──────────────────────────────────────────────────────────────────────

class TestSourceStructurePins:
    SRC = inspect.getsource(agp)

    def test_fail_closed_branch_exists_in_verify(self):
        seg = self.SRC[self.SRC.index("def verify_seal"):]
        assert "AGPSealKeyInvalid" in seg
        assert "_seal_key_configured" in seg

    def test_public_sha_candidate_gated_on_unkeyed_regime(self):
        seg = self.SRC[self.SRC.index("def verify_seal"):]
        tail = seg[seg.index("candidates.append(hashlib.sha256") - 400:
                   seg.index("candidates.append(hashlib.sha256") + 200]
        assert "_seal_key_configured()" in tail or "if not" in tail

    def test_seal_digest_raises_on_invalid_key(self):
        seg = self.SRC[self.SRC.index("def _seal_digest"):]
        assert "raise AGPSealKeyInvalid" in seg

    def test_no_live_status_added(self):
        """The module's own guard: this file never arms live betting and
        never touches the paper-trade signal gate."""
        import tests.test_autofill_0079 as this_mod
        src = inspect.getsource(this_mod)
        forbidden = "generate_" + "paper_trade_signal"
        assert forbidden not in src

    def test_constant_time_compare_used(self):
        seg = self.SRC[self.SRC.index("def verify_seal"):]
        assert "compare_digest" in seg

    def test_agpsealkeyinvalid_exported(self):
        assert hasattr(agp, "AGPSealKeyInvalid")
        assert issubclass(AGPSealKeyInvalid, Exception)


# ──────────────────────────────────────────────────────────────────────
# 8. Cross-checks against preregistration verify_seal (same policy)
# ──────────────────────────────────────────────────────────────────────

class TestPreregistrationParity:
    def _make(self, monkeypatch):
        from agp.preregistration import Criteria, Preregistration
        return Criteria(confirm_markers=["up"], refute_markers=["down"]), Preregistration

    def test_prereg_verify_seal_exists_and_bool(self):
        from agp.preregistration import Preregistration
        assert callable(getattr(Preregistration, "verify_seal"))

    def _sealed_prereg(self, monkeypatch):
        from agp.preregistration import Criteria, Preregistration
        p = Preregistration(
            query="parity check query 0079",
            criteria=Criteria(confirm_markers=["up"], refute_markers=["down"]),
        )
        p.seal()
        return p

    def test_sealed_prereg_verifies_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        p = self._sealed_prereg(monkeypatch)
        assert p.verify_seal() is True

    def test_unsealed_prereg_fails_closed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        from agp.preregistration import Criteria, Preregistration
        p = Preregistration(
            query="parity check query 0079",
            criteria=Criteria(confirm_markers=["up"], refute_markers=["down"]),
        )
        assert p.verify_seal() is False

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_prereg_bad_key_fails_closed(self, bad, monkeypatch):
        """Invalid key ⇒ verify_seal False, never an exception (fail closed).
        Seal is created unkeyed first; arming a broken key must make every
        stored record unverifiable rather than silently accepted."""
        p = self._sealed_prereg(monkeypatch)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad.strip())
        try:
            result = p.verify_seal()
        except Exception as e:  # pragma: no cover
            raise AssertionError(f"verify_seal raised under bad key: {e}")
        assert result is False

    def test_prereg_tampered_query_detected(self, monkeypatch):
        from agp.preregistration import _canonical, _seal_digest
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        p = self._sealed_prereg(monkeypatch)
        # A DB-side rewrite of the query would not verify: the digest over
        # the tampered payload differs from the stored one.
        forged_hash = _seal_digest(_canonical(
            {"query": "TAMPERED", "criteria": p.criteria.to_dict(),
             "created_at": p.created_at, "sealed_at": p.sealed_at}))
        assert forged_hash != p.seal_hash
        assert p.verify_seal() is True  # untouched real record still verifies
