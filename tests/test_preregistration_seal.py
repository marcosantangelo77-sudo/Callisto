"""Preregistration seals follow the keyed fail-closed regime.

Mirrors tests/test_seal_invalid_key.py for Preregistration:
- unset CALLISTO_SEAL_KEY  → legacy unkeyed SHA-256 seal/verify works;
- valid hex key            → HMAC-SHA256; unkeyed digest does NOT verify;
- set-but-invalid hex      → seal() refuses (raises), verify_seal() returns
  False instead of raising.
"""

import hashlib
import json

import pytest

from agp import AGPSealKeyInvalid
from agp.preregistration import Criteria, Preregistration

VALID_KEY = "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"


def _make_prereg() -> Preregistration:
    return Preregistration(
        query="does intervention X move metric Y?",
        criteria=Criteria(confirm_markers=["metric rose"],
                          refute_markers=["metric fell"]),
    )


def _clear_keys(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


def test_unset_key_legacy_unkeyed_roundtrip(monkeypatch):
    """No key configured: seal + verify use legacy unkeyed SHA-256."""
    _clear_keys(monkeypatch)
    p = _make_prereg()
    h = p.seal()
    assert h == hashlib.sha256(
        json.dumps({**p._payload(), "sealed_at": p.sealed_at},
                   sort_keys=True, ensure_ascii=False,
                   separators=(",", ":")).encode("utf-8")).hexdigest()
    assert p.verify_seal() is True


def test_valid_key_hmac_and_rejects_unkeyed(monkeypatch):
    """Valid hex key: HMAC seal verifies; an unkeyed SHA-256 of the same
    payload does not."""
    _clear_keys(monkeypatch)
    p = _make_prereg()
    p.sealed_at = "2026-01-01T00:00:00+00:00"
    payload_str = json.dumps(
        {**p._payload(), "sealed_at": p.sealed_at},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    unkeyed = hashlib.sha256(payload_str.encode("utf-8")).hexdigest()

    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    # Unkeyed hash written under a keyed regime must not verify.
    object.__setattr__(p, "seal_hash", unkeyed)
    assert p.verify_seal() is False

    # A properly HMAC-sealed preregistration verifies.
    import hmac as hmac_mod
    keyed = hmac_mod.new(bytes.fromhex(VALID_KEY),
                         payload_str.encode("utf-8"),
                         hashlib.sha256).hexdigest()
    object.__setattr__(p, "seal_hash", keyed)
    assert p.verify_seal() is True


def test_invalid_hex_key_seal_refuses(monkeypatch):
    """seal() with set-but-invalid hex raises rather than writing a
    forgeable unkeyed hash."""
    _clear_keys(monkeypatch)
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-xyz")
    p = _make_prereg()
    with pytest.raises(AGPSealKeyInvalid):
        p.seal()
    assert p.seal_hash is None
    assert not p._sealed


def test_invalid_hex_key_verify_returns_false_not_raise(monkeypatch):
    """verify_seal() never raises under an invalid key: it fails closed to
    False — including for a legitimately (unkeyed-era) sealed object."""
    _clear_keys(monkeypatch)
    p = _make_prereg()
    p.seal()
    assert p.seal_hash is not None

    monkeypatch.setenv("CALLISTO_SEAL_KEY", "zzz-invalid")
    assert p.verify_seal() is False

    persisted = Preregistration.from_dict(p.to_dict())
    assert persisted.verify_seal() is False
