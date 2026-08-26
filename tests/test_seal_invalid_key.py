"""Fail-closed behavior for an invalid CALLISTO_SEAL_KEY.

Regime rules:
- No CALLISTO_SEAL_KEY (unset/blank)  → legacy unkeyed SHA-256 seal/verify.
- Set but invalid hex                 → keyed regime with no usable key:
    * verify_seal() must NOT accept a public unkeyed SHA-256,
    * seal() must refuse rather than write a forgeable hash.
- Valid hex                           → HMAC-SHA256; unkeyed digest rejected.
- CALLISTO_SEAL_KEY_OLD valid keys still verify during rotation.
"""

import hashlib
import hmac

import pytest

from agp import AGPSealKeyInvalid, AGPSession

VALID_KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"
OLD_KEY = "0102030405060708090a0b0c0d0e0f10"


@pytest.fixture
def sealed_session():
    from tests.test_agp_seal import _build_sealable_session

    return _build_sealable_session()


def _unkeyed_digest(session) -> str:
    from agp import _canonical_payload, _seal_digest
    import os

    # Compute what a forgeable public SHA-256 of this payload would be.
    data = session.to_dict()
    payload = _canonical_payload(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_invalid_hex_key_verify_rejects_unkeyed(sealed_session, monkeypatch):
    """Key set to invalid hex: a public SHA-256 of the payload is rejected."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    stored = sealed_session.to_dict()
    # Forge an unkeyed seal over the same payload.
    forged = dict(stored)
    forged["seal_hash"] = _unkeyed_digest(sealed_session)

    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-xyz")
    assert AGPSession.verify_seal(forged) is False


def test_invalid_hex_key_seal_refuses(monkeypatch):
    """seal() with invalid hex raises instead of writing a forgeable hash."""
    from tests.test_agp_seal import _build_sealable_session

    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-xyz")
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    s = _make_unsealed_session()
    with pytest.raises(AGPSealKeyInvalid):
        s.seal()
    assert s.seal_hash is None


def test_invalid_hex_key_verify_rejects_own_seal(sealed_session, monkeypatch):
    """Even a legitimately-sealed (unkeyed-era) session fails verification
    once an invalid key is configured — fail-closed, not silent accept."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "zzz-invalid")
    assert AGPSession.verify_seal(sealed_session.to_dict()) is False


def test_unset_key_legacy_unkeyed_still_verifies(sealed_session, monkeypatch):
    """No key at all: legacy unkeyed seals verify."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    assert AGPSession.verify_seal(sealed_session.to_dict()) is True


def test_valid_key_hmac_rejects_unkeyed(sealed_session, monkeypatch):
    """Valid key: HMAC regime — the unkeyed digest of the same payload is
    rejected (the previously inverted test)."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    stored = sealed_session.to_dict()  # sealed under no key → unkeyed hash
    assert AGPSession.verify_seal(stored) is False


def test_valid_key_hmac_roundtrip(sealed_session, monkeypatch):
    """Valid key: sealing produces an HMAC that verifies."""
    from tests.test_agp_seal import _build_sealable_session

    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    s = _build_sealable_session()
    expected = hmac.new(
        bytes.fromhex(VALID_KEY),
        __import__("agp")._canonical_payload(s.to_dict()).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert s.seal_hash == expected
    assert AGPSession.verify_seal(s.to_dict()) is True


def test_old_key_rotation_still_verifies(sealed_session, monkeypatch):
    """Current key invalid but OLD key valid: old-key HMAC seals verify."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    s = _build_with_key(OLD_KEY, monkeypatch)
    # Rotate: current key changes, old key retained.
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "deadbeef")
    monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{OLD_KEY},badhex")
    assert AGPSession.verify_seal(s.to_dict()) is True


# ── helpers ──────────────────────────────────────────────────────────────

def _make_unsealed_session():
    from agp import Domain, Evidence, SessionStep, SessionSummary, SourceClass

    s = AGPSession("query")
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["example.org"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="fact",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.7,
        domain=Domain.TECHNICAL,
        origin_agent="test",
        source_name="https://example.org",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=s.scope,
        domain=Domain.TECHNICAL,
        conclusion="A conclusion.",
        confidence_score=0.7,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def _build_with_key(key_hex, monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", key_hex)
    s = _make_unsealed_session()
    s.seal()
    return s
