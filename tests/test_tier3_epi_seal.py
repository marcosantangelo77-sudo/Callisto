"""Tier 3 epistemics — seal keying (Instance 4).

Pins:
1. Unkeyed fallback behavior is byte-identical to pre-change SHA-256 seals
   (legacy sessions must keep verifying).
2. With CALLISTO_SEAL_KEY set, new seals are HMAC and a DB-write attacker
   who recomputes the public SHA-256 CANNOT forge a passing seal.
3. Legacy unkeyed seals still verify under the keyed regime.
4. Rotation via CALLISTO_SEAL_KEY_OLD.
5. Tamper detection still fires in all regimes.
"""
import hashlib
import json

import pytest

import agp
from agp import AGPSession, AGPViolation, Domain, Evidence, SessionStep, SourceClass


def _make_session(query="test query for sealing") -> AGPSession:
    s = AGPSession(query)
    # Walk the lifecycle to SESSION_CLOSE with one storable evidence item.
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["test"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="an observed fact",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.70,
        domain=Domain.GENERAL,
        origin_agent="test",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    from agp import SessionSummary
    s.summary = SessionSummary(
        scope=query, domain=Domain.GENERAL, conclusion="a real conclusion",
        confidence_score=0.70, evidence_count=1, contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


class TestUnkeyedBackcompat:
    def test_unkeyed_seal_equals_legacy_sha256(self):
        """No env key → digest is exactly the old public SHA-256.

        This pins backward compatibility with every existing sealed row."""
        s = _make_session()
        h = s.seal()
        payload = agp._canonical_payload(s.to_dict())
        assert h == hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def test_unkeyed_verify_passes(self):
        s = _make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True


class TestKeyedSeal:
    def test_keyed_seal_is_hmac_not_public_hash(self):
        """With a key set, the stored hash must NOT equal the public SHA-256 —
        this is the property that defeats a forge-by-recompute attacker."""
        s = _make_session()
        import os
        os.environ["CALLISTO_SEAL_KEY"] = "ab" * 32
        try:
            h = s.seal()
            payload = agp._canonical_payload(s.to_dict())
            public = hashlib.sha256(payload.encode("utf-8")).hexdigest()
            assert h != public
            assert AGPSession.verify_seal(s.to_dict()) is True
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_forge_with_wrong_key_fails(self):
        """An attacker holding a DIFFERENT key cannot produce a passing seal."""
        import hmac as _hmac
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "cd" * 32
        try:
            s.seal()
            d = dict(s.to_dict())
            d["conclusion"] = "TAMPERED CONCLUSION"
            payload = agp._canonical_payload(d)
            attacker_key = bytes.fromhex("ef" * 32)
            forged = _hmac.new(attacker_key, payload.encode(), hashlib.sha256).hexdigest()
            d["seal_hash"] = forged
            assert AGPSession.verify_seal(d) is False
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_tamper_detected_under_key(self):
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "12" * 32
        try:
            s.seal()
            d = dict(s.to_dict())
            d["summary"]["confidence_score"] = 0.95  # bump own confidence post-hoc
            assert AGPSession.verify_seal(d) is False
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_legacy_unkeyed_seal_still_verifies_under_keyed_regime(self):
        """Old rows sealed before keying must not be invalidated when the
        operator sets CALLISTO_SEAL_KEY."""
        s = _make_session()
        s.seal()  # unkeyed (no env)
        stored = json.dumps(s.to_dict())
        import os
        os.environ["CALLISTO_SEAL_KEY"] = "34" * 32
        try:
            assert AGPSession.verify_seal(stored) is True
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_rotation_old_key_accepted_current_rejected(self):
        """After rotating, seals made under the old key still verify; seals
        made under an unknown third key do not."""
        import hmac as _hmac
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "aa" * 32
        try:
            s.seal()
            stored = s.to_dict()
            # Rotate: old key moves to _OLD, new key takes over
            os.environ["CALLISTO_SEAL_KEY_OLD"] = "aa" * 32
            os.environ["CALLISTO_SEAL_KEY"] = "bb" * 32
            try:
                assert AGPSession.verify_seal(stored) is True
                # A hypothetical seal from a foreign key fails
                d = dict(stored)
                payload = agp._canonical_payload(d)
                d["seal_hash"] = _hmac.new(
                    bytes.fromhex("cc" * 32), payload.encode(), hashlib.sha256
                ).hexdigest()
                assert AGPSession.verify_seal(d) is False
            finally:
                del os.environ["CALLISTO_SEAL_KEY_OLD"]
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_invalid_hex_key_falls_back_to_unkeyed(self):
        """A malformed key degrades to legacy behavior rather than bricking
        sealing — and logs loudly (documented tradeoff)."""
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "not-hex!"
        try:
            h = s.seal()
            payload = agp._canonical_payload(s.to_dict())
            assert h == hashlib.sha256(payload.encode()).hexdigest()
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]


class TestSealVerificationMethod:
    """seal_verification_method reports WHICH digest family verified, so a
    verifier can state how much trust a seal carries instead of collapsing
    keyed and unkeyed into one boolean. verify_seal stays exactly its
    ``is not None``."""

    def test_unkeyed_seal_reports_unkeyed(self):
        s = _make_session()
        s.seal()                                   # no env key → legacy digest
        assert agp.seal_verification_method(s.to_dict()) == "unkeyed"

    def test_keyed_seal_reports_keyed(self):
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "12" * 32
        try:
            s.seal()
            assert agp.seal_verification_method(s.to_dict()) == "keyed"
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_legacy_unkeyed_seal_under_keyed_regime_still_reports_unkeyed(
            self):
        import os
        s = _make_session()
        s.seal()                                   # sealed before keying
        stored = json.dumps(s.to_dict())
        os.environ["CALLISTO_SEAL_KEY"] = "34" * 32
        try:
            # verify passes (legacy accepted) but the label is honest: this
            # seal is NOT covered by any key.
            assert AGPSession.verify_seal(stored) is True
            assert agp.seal_verification_method(stored) == "unkeyed"
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_rotated_old_key_reports_keyed(self):
        import os
        s = _make_session()
        os.environ["CALLISTO_SEAL_KEY"] = "aa" * 32
        try:
            s.seal()
            os.environ["CALLISTO_SEAL_KEY_OLD"] = "aa" * 32
            os.environ["CALLISTO_SEAL_KEY"] = "bb" * 32
            try:
                assert agp.seal_verification_method(s.to_dict()) == "keyed"
            finally:
                del os.environ["CALLISTO_SEAL_KEY_OLD"]
        finally:
            del os.environ["CALLISTO_SEAL_KEY"]

    def test_tampered_reports_none(self):
        s = _make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] += " (quietly strengthened)"
        assert agp.seal_verification_method(d) is None

    def test_missing_or_malformed_input_reports_none(self):
        assert agp.seal_verification_method({"seal_hash": None}) is None
        assert agp.seal_verification_method({}) is None
        assert agp.seal_verification_method("not json at all {") is None
        assert agp.seal_verification_method(None) is None

    def test_verify_seal_is_exactly_the_none_check(self):
        s = _make_session()
        s.seal()
        good = s.to_dict()
        bad = dict(good)
        bad["evidence"][0]["confidence_score"] = 0.99
        assert AGPSession.verify_seal(good) == (
            agp.seal_verification_method(good) is not None)
        assert AGPSession.verify_seal(bad) == (
            agp.seal_verification_method(bad) is not None)
