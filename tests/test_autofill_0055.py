"""autofill #0055 — AGP seal fail-closed characterization.

Core invariant under test: **a keyed verify_seal must never accept an
unkeyed SHA-256 digest**, and any misconfigured key regime must FAIL
CLOSED (refuse / disable) rather than fall back to forgeable public
SHA-256 seals. No production gate is weakened by this module — every
test asserts the gates stay shut or behave exactly as characterized.

Regimes covered:
  R1  no CALLISTO_SEAL_KEY            → legacy unkeyed regime
  R2  valid hex CALLISTO_SEAL_KEY     → keyed HMAC regime (unkeyed refused)
  R3  invalid-hex CALLISTO_SEAL_KEY   → fail closed (nothing verifies,
                                        nothing seals)
  R4  rotation via CALLISTO_SEAL_KEY_OLD
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agp import (  # noqa: E402
    AGPSession,
    AGPSealKeyInvalid,
    AGPSealRefused,
    AGPViolation,
    Domain,
    SessionStep,
    SessionSummary,
    Evidence,
    SourceClass,
    _canonical_payload,
    _seal_digest,
    _seal_key_configured,
    _seal_keys,
)  # noqa: E402
from agp import AGPSession as _AGPSessionCls  # noqa: E402

verify_seal = _AGPSessionCls.verify_seal

KEY_A = "ab" * 32          # current-key hex
KEY_B = "cd" * 32          # rotation-key hex
BAD_HEX = "zz-not-hex"


# ── helpers ────────────────────────────────────────────────────────────────

def _unkeyed_digest(stored: dict) -> str:
    """The forgeable public SHA-256 of the canonical payload."""
    return hashlib.sha256(_canonical_payload(stored).encode("utf-8")).hexdigest()


def _hmac_digest(key_hex: str, stored: dict) -> str:
    return hmac_mod.new(
        bytes.fromhex(key_hex),
        _canonical_payload(stored).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_session(query: str = "characterization query") -> AGPSession:
    """Build a fully advanced session ready to seal."""
    s = AGPSession(query=query)
    s.domain = Domain.GENERAL
    for step in SessionStep:
        if step is not SessionStep.DECLARE_SCOPE:
            s.advance_to(step)
    s.add_evidence(Evidence(
        content="primary observation",
        source_class=SourceClass.PRIMARY,
        confidence_score=0.9,
        domain=Domain.GENERAL,
        origin_agent="test",
    ))
    s.summary = SessionSummary(
        scope=query, domain=Domain.GENERAL,
        conclusion="a real conclusion with content",
        confidence_score=0.8, evidence_count=1,
        contradiction_count=0)
    return s


@pytest.fixture
def clean_key_env(monkeypatch):
    """Start each test from a known key environment."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    return monkeypatch


# ── R2: keyed regime refuses unkeyed SHA-256 ──────────────────────────────

class TestKeyedRefusesUnkeyed:
    def test_unkeyed_sha256_rejected_when_keyed(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        stored = s.to_dict()
        assert verify_seal(stored) is True  # keyed digest verifies
        stored["seal_hash"] = _unkeyed_digest(stored)
        assert verify_seal(stored) is False  # unkeyed forgery refused

    def test_seal_produces_hmac_not_public_sha256(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        h = s.seal()
        assert h != _unkeyed_digest(s.to_dict())
        assert h == _hmac_digest(KEY_A, s.to_dict())

    def test_verify_accepts_current_key_hmac_only(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_B)
        s = _make_session()
        s.seal()
        d = s.to_dict()
        d["seal_hash"] = _hmac_digest(KEY_A, d)
        assert verify_seal(d) is False  # wrong key's HMAC refused

    def test_forged_unkeyed_json_string_refused(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        d = s.to_dict()
        d["seal_hash"] = _unkeyed_digest(d)
        assert verify_seal(json.dumps(d)) is False

    def test_attacker_without_key_cannot_forge(self, clean_key_env):
        """Anyone who can write the DB still cannot produce a valid seal
        without knowing the key: only the true HMAC passes."""
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        d = s.to_dict()
        for candidate in (
            _unkeyed_digest(d),
            hashlib.sha256(json.dumps(d).encode()).hexdigest(),
            _hmac_digest(KEY_B, d),
            "0" * 64,
            "",
            None,
        ):
            forged = dict(d)
            forged["seal_hash"] = candidate
            assert verify_seal(forged) is False

    def test_tampered_content_breaks_keyed_seal(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        d = s.to_dict()
        d["query"] = "tampered"
        assert verify_seal(d) is False

    def test_valid_key_verifies_its_own_seal(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        assert verify_seal(s.to_dict()) is True
        assert verify_seal(json.dumps(s.to_dict())) is True


# ── R3: invalid-hex key fails closed everywhere ───────────────────────────

class TestInvalidHexFailsClosed:
    def test_verify_rejects_everything_on_bad_hex(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        s = _make_session()
        # Even a previously-valid unkeyed seal must not pass.
        clean_key_env.delenv("CALLISTO_SEAL_KEY")
        h = s.seal()
        d = s.to_dict()
        assert verify_seal(d) is True  # legacy regime sanity
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        assert verify_seal(d) is False  # now fails closed

    def test_seal_raises_agpsealkeyinvalid_on_bad_hex(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        s = _make_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()

    def test_seal_digest_helper_raises_on_bad_hex(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        with pytest.raises(AGPSealKeyInvalid):
            _seal_digest("payload")

    def test_bad_hex_never_yields_keys(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        assert _seal_keys() == []
        assert _seal_key_configured() is True

    def test_blank_key_is_legacy_not_fail_closed_regime(self, clean_key_env):
        # CHARACTERIZED: _seal_key_configured() strips, so a whitespace-only
        # key counts as *unset* (legacy unkeyed regime), not as an invalid
        # keyed regime. The front door (tools.cli.ask.check_seal_key) also
        # strips and refuses blank keys there — defense lives in the CLI.
        clean_key_env.setenv("CALLISTO_SEAL_KEY", "   ")
        assert _seal_key_configured() is False
        assert _seal_keys() == []

    def test_bad_hex_does_not_corrupt_session_state(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", BAD_HEX)
        s = _make_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        # CHARACTERIZED CAVEAT: seal() stamps sealed_at BEFORE computing the
        # digest, so a failed keyed seal leaves sealed_at set. The critical
        # invariant holds: seal_hash stays None and no forgeable hash exists.
        assert s.sealed_at is not None
        assert s.seal_hash is None
        assert s.to_dict()["seal_hash"] is None
        d = s.to_dict()
        d["seal_hash"] = hashlib.sha256(
            _canonical_payload(d).encode()).hexdigest()
        assert verify_seal(d) is False  # unkeyed patch refused too


# ── R1: legacy unkeyed regime (no key at all) ─────────────────────────────

class TestLegacyUnkeyedRegime:
    def test_no_key_seal_is_public_sha256(self, clean_key_env):
        s = _make_session()
        h = s.seal()
        assert h == _unkeyed_digest(s.to_dict())

    def test_no_key_verify_accepts_unkeyed(self, clean_key_env):
        s = _make_session()
        s.seal()
        assert verify_seal(s.to_dict()) is True

    def test_key_configured_false_without_env(self, clean_key_env):
        assert _seal_key_configured() is False
        assert _seal_keys() == []


# ── R4: rotation keys ─────────────────────────────────────────────────────

class TestRotationKeys:
    def test_old_key_seal_still_verifies(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        clean_key_env.setenv("CALLISTO_SEAL_KEY_OLD", KEY_B)
        s = _make_session()
        d = s.to_dict()
        d["seal_hash"] = _hmac_digest(KEY_B, d)
        assert verify_seal(d) is True

    def test_rotation_list_comma_separated(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        clean_key_env.setenv("CALLISTO_SEAL_KEY_OLD",
                             f"not-hex , {KEY_B} ,  ")
        keys = _seal_keys()
        assert bytes.fromhex(KEY_A) in keys
        assert bytes.fromhex(KEY_B) in keys
        assert len(keys) == 2  # junk entries silently skipped

    def test_current_key_wins_over_rotation(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_B)
        clean_key_env.setenv("CALLISTO_SEAL_KEY_OLD", KEY_A)
        s = _make_session()
        assert s.seal() == _hmac_digest(KEY_B, s.to_dict())


# ── verify_seal edge cases (never raises) ─────────────────────────────────

class TestVerifySealEdges:
    def test_missing_seal_hash(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        d = _make_session().to_dict()
        d.pop("seal_hash")
        assert verify_seal(d) is False

    def test_none_seal_hash(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        d = _make_session().to_dict()
        d["seal_hash"] = None
        assert verify_seal(d) is False

    def test_nonstring_seal_hash(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        d = {"seal_hash": 12345}
        assert verify_seal(d) is False

    def test_malformed_json_string(self, clean_key_env):
        assert verify_seal("{not json") is False

    def test_unsupported_type_returns_false_not_raise(self, clean_key_env):
        for bad in (12345, [1, 2], None, 3.14, object()):
            try:
                assert verify_seal(bad) is False
            except TypeError:
                pytest.fail("verify_seal raised instead of failing closed")

    def test_empty_dict(self, clean_key_env):
        assert verify_seal({}) is False


# ── seal() refuse-to-seal-garbage gates stay intact ───────────────────────

class TestSealGatesIntact:
    def test_empty_conclusion_refused(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.summary.conclusion = ""
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_zero_evidence_refused(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.evidence.clear()
        s.summary.evidence_count = 0
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_double_seal_violation(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        with pytest.raises(AGPViolation):
            s.seal()

    def test_mutation_after_seal_forbidden(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal()
        with pytest.raises(AGPViolation):
            s.add_evidence(Evidence(
                content="late", source_class=SourceClass.PRIMARY,
                confidence_score=0.9, domain=Domain.GENERAL,
                origin_agent="test"))
        with pytest.raises(AGPViolation):
            s.advance_to(SessionStep.DECLARE_SCOPE)

    def test_seal_veto_crash_fails_closed(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        def boom(session, summary):
            raise RuntimeError("reviewer exploded")
        s.seal_veto = boom
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_seal_veto_reason_refuses(self, clean_key_env):
        clean_key_env.setenv("CALLISTO_SEAL_KEY", KEY_A)
        s = _make_session()
        s.seal_veto = lambda sess, summ: "conclusion unsupported"
        with pytest.raises(AGPSealRefused):
            s.seal()


# ── source-level guards: gates must not be weakened ───────────────────────

class TestSourceGuards:
    MODULE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "agp", "__init__.py")

    ASK = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools", "cli", "ask.py")

    def _src(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_verify_seal_documents_no_unkeyed_fallback_when_keyed(self):
        src = self._src(self.MODULE)
        seg = src[src.index("def verify_seal"):]
        assert "deliberately NOT accepted" in seg or \
               "must NOT be accepted" in seg

    def test_seal_digest_raises_instead_of_falling_back(self):
        src = self._src(self.MODULE)
        seg = src[src.index("def _seal_digest"):
                  src.index("class AGPSession")]
        assert "raise AGPSealKeyInvalid" in seg
        assert "refusing to produce" in seg

    def test_ask_gate_signature_present(self):
        src = self._src(self.ASK)
        assert "def check_seal_key() -> bool:" in src

    def test_ask_gate_mentions_forgeable(self):
        src = self._src(self.ASK)
        seg = src[src.index("def check_seal_key"):
                  src.index("def _result_record")]
        assert "forgeable" in seg
