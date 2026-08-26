"""OX autofill #0023 — AGP seal fail-closed characterization.

Characterizes the keyed-seal boundary of ``agp.AGPSession``:

1. When CALLISTO_SEAL_KEY is set (keyed regime), ``verify_seal`` must NOT
   accept the raw public SHA-256 of the payload. Accepting it would let a
   DB-write attacker forge a passing seal without ever knowing the key.
2. A set-but-invalid (non-hex) key must fail CLOSED: no unkeyed fallback,
   no digest produced, verification returns False.
3. Unkeyed legacy behavior (no key at all) remains exactly SHA-256 so old
   rows keep verifying.
4. Rotation via CALLISTO_SEAL_KEY_OLD keeps old seals verifiable without
   re-admitting the public hash.
5. Tamper detection fires in every regime.

Tests-only module: no production code is modified by these pins. Where a
pin would demand weakening a gate, we instead assert the gate holds
(fail closed). Live betting is never armed; this module touches only the
seal primitives.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os

import pytest

import agp
from agp import (
    AGPSession,
    AGPSealKeyInvalid,
    AGPSealRefused,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)

VALID_KEYS = ["ab" * 32, "cd" * 32, "12" * 32, "34" * 32]


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def make_session(query="autofill 0023 characterization query") -> AGPSession:
    """Walk a session through the full lifecycle to SESSION_CLOSE."""
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["characterization"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="an observed fact for the seal",
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
    """The forgeable public hash an attacker can compute from a stored row."""
    payload = agp._canonical_payload(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hmac_with_key(hexkey: str, data) -> str:
    if isinstance(data, str):
        data = json.loads(data)
    payload = agp._canonical_payload(data)
    return hmac.new(
        bytes.fromhex(hexkey), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@pytest.fixture(autouse=True)
def clean_seal_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ──────────────────────────────────────────────────────────────────────
# 1. Keyed verify_seal must not accept the public SHA-256
# ──────────────────────────────────────────────────────────────────────

class TestKeyedVerifyRejectsUnkeyed:
    @pytest.mark.parametrize("key", VALID_KEYS[:3])
    def test_public_hash_forgery_rejected(self, key, monkeypatch):
        """Attacker seals nothing; they just recompute sha256(canonical)."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_public_hash_over_mutated_payload_rejected(self, monkeypatch):
        """Even a hash over the TAMPERED content is not accepted under a key."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] = "FORGED CONCLUSION"
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_unkeyed_legacy_row_rejected_under_key(self, monkeypatch):
        """Seal unkeyed, then set a key: the legacy row must stop verifying."""
        s = make_session()
        s.seal()
        stored = json.dumps(s.to_dict())
        assert AGPSession.verify_seal(stored) is True  # still unkeyed regime
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        assert AGPSession.verify_seal(stored) is False

    def test_hmac_of_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = hmac_with_key("bb" * 32, d)
        assert AGPSession.verify_seal(d) is False

    def test_correct_key_accepted_and_differs_from_public(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        h = s.seal()
        d = s.to_dict()
        assert h != public_sha256(d)
        assert AGPSession.verify_seal(d) is True

    def test_verify_accepts_json_string_form(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_blank_env_counts_as_unconfigured_not_keyed(self, monkeypatch):
        """Whitespace key == operator did not intend keying → legacy regime."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        assert agp._seal_key_configured() is False
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_set_but_nonhex_never_falls_back_to_public_hash(self, monkeypatch):
        """The critical fail-closed case: key set but malformed. verify_seal
        must not silently accept the public SHA-256."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-valid-hex-zz")
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False


# ──────────────────────────────────────────────────────────────────────
# 2. Fail-closed on invalid keys
# ──────────────────────────────────────────────────────────────────────

class TestFailClosedInvalidKeys:
    BAD_KEYS = [
        "not-hex!",
        "zz" * 32,          # odd-length-free but non-hex chars
        "abc1234",          # odd length → bytes.fromhex fails
        "0x" + "ab" * 16,   # '0x' prefix is not hex-parseable by fromhex
    ]

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_seal_refuses_on_bad_key(self, bad, monkeypatch):
        """seal() must raise AGPSealKeyInvalid, never produce an unkeyed digest."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        s = make_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_digest_helper_refuses_on_bad_key(self, bad, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        payload = json.dumps({"k": "v"}, sort_keys=True)
        with pytest.raises(AGPSealKeyInvalid):
            agp._seal_digest(payload)

    def test_verify_returns_false_not_exception_on_bad_key(self, monkeypatch):
        """verify_seal never raises — bad key ⇒ False (fail closed)."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-not-hex")
        s = make_session()
        s._sealed = True  # pretend sealed so to_dict carries fields
        d = s.to_dict()
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_bad_current_key_good_old_key_still_rejects_public_hash(
        self, monkeypatch
    ):
        """Even when CALLISTO_SEAL_KEY_OLD is valid, a malformed CURRENT key
        must keep the public-hash forgery out."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "aa" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "bad-key!!")
        s = make_session()
        d = dict(s.to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_empty_string_key_is_unconfigured_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        assert agp._seal_key_configured() is False
        assert agp._seal_keys() == []

    def test_whitespace_only_old_key_ignored(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "   ")
        assert agp._seal_keys() == [bytes.fromhex("ab" * 32)]


# ──────────────────────────────────────────────────────────────────────
# 3. Legacy unkeyed regime stays byte-compatible
# ──────────────────────────────────────────────────────────────────────

class TestUnkeyedLegacyRegime:
    def test_no_key_digest_is_plain_sha256(self):
        s = make_session()
        h = s.seal()
        assert h == public_sha256(s.to_dict())

    def test_no_key_verify_passes(self):
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_missing_seal_hash_rejected_even_unkeyed(self):
        s = make_session()
        s.seal()
        d = s.to_dict()
        del d["seal_hash"]
        assert AGPSession.verify_seal(d) is False

    def test_none_seal_hash_rejected(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["seal_hash"] = None
        assert AGPSession.verify_seal(d) is False

    def test_malformed_json_rejected(self):
        assert AGPSession.verify_seal("{not json") is False

    def test_non_dict_input_rejected(self):
        assert AGPSession.verify_seal(12345) is False

    def test_tampered_evidence_detected_unkeyed(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["evidence"][0]["confidence_score"] = 0.99
        assert AGPSession.verify_seal(d) is False

    def test_extra_field_changes_payload(self):
        """Adding a field breaks the canonical payload → mismatch."""
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["attacker_note"] = "injected"
        assert AGPSession.verify_seal(d) is False


# ──────────────────────────────────────────────────────────────────────
# 4. Rotation keeps old HMACs alive without re-admitting public hash
# ──────────────────────────────────────────────────────────────────────

class TestRotation:
    def test_rotation_old_key_still_verifies(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "aa" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "bb" * 32)
        assert AGPSession.verify_seal(stored) is True
        # And the public hash STILL fails after rotation.
        d = dict(stored)
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_foreign_key_rejected_after_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "aa" * 32)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "aa" * 32)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "bb" * 32)
        d = dict(stored)
        d["seal_hash"] = hmac_with_key("cc" * 32, d)
        assert AGPSession.verify_seal(d) is False

    def test_multiple_old_keys_comma_separated(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "11" * 32)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        # Rotate the CURRENT key away from the sealing key first.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "33" * 32)
        # A wrong comma-list that lacks this key must not verify.
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "22" * 32 + ",11" * 1)
        assert AGPSession.verify_seal(stored) is False
        # Proper two-key rotation list: old key present → verifies.
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD",
                           "22" * 32 + "," + "11" * 32)
        assert AGPSession.verify_seal(stored) is True


# ──────────────────────────────────────────────────────────────────────
# 5. Tamper detection across regimes
# ──────────────────────────────────────────────────────────────────────

TAMPER_CASES = {
    "conclusion": lambda d: d["summary"].__setitem__("conclusion", "HACKED"),
    "confidence": lambda d: d["summary"].__setitem__("confidence_score", 0.99),
    "evidence_swap": lambda d: d.__setitem__(
        "evidence", [dict(d["evidence"][0], content="fabricated")]),
    "session_id": lambda d: d.__setitem__("session_id", "9999999999999999"),
    "tier_bump": lambda d: d["summary"].__setitem__(
        "confidence_tier", "VERIFIED"),
}


class TestTamperDetection:
    @pytest.mark.parametrize("name", sorted(TAMPER_CASES))
    def test_tamper_keyed(self, name, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ad" * 32)
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        TAMPER_CASES[name](d)
        assert AGPSession.verify_seal(d) is False, name

    @pytest.mark.parametrize("name", sorted(TAMPER_CASES))
    def test_tamper_unkeyed(self, name):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        TAMPER_CASES[name](d)
        assert AGPSession.verify_seal(d) is False, name

    def test_reforged_hash_after_tamper_keyed_rejected(self, monkeypatch):
        """Recomputing ANY digest after tampering cannot pass under a key
        unless the attacker knows the real key."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ef" * 32)
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] = "REWRITTEN"
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False
        d["seal_hash"] = hmac_with_key("ff" * 32, d)
        assert AGPSession.verify_seal(d) is False


# ──────────────────────────────────────────────────────────────────────
# 6. Seal-refusal gates remain intact (never weakened)
# ──────────────────────────────────────────────────────────────────────

class TestSealRefusalGatesIntact:
    def test_empty_conclusion_refused_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
        s = make_session()
        s.summary.conclusion = ""
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_zero_evidence_refused_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
        s = make_session()
        s.evidence.clear()
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_veto_crash_fails_closed_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ab" * 32)
        s = make_session()
        s.seal_veto = lambda sess, summ: (_ for _ in ()).throw(RuntimeError("boom"))
        with pytest.raises(AGPSealRefused):
            s.seal()


# ──────────────────────────────────────────────────────────────────────
# 7. Source pins: the fail-closed logic exists in production code
# ──────────────────────────────────────────────────────────────────────

class TestSourcePins:
    def test_verify_does_not_admit_public_hash_when_keyed(self):
        """Structural pin: the unkeyed candidate is added ONLY inside a
        `_seal_key_configured()` false branch in verify_seal's source."""
        src = __import__("inspect").getsource(AGPSession.verify_seal)
        assert "_seal_key_configured()" in src
        # The unkeyed append sits behind that check (textually after it).
        cfg_pos = src.index("_seal_key_configured()")
        sha_pos = src.index("hashlib.sha256(payload.encode")
        assert cfg_pos < sha_pos

    def test_seal_digest_raises_rather_than_fallback(self):
        src = __import__("inspect").getsource(agp._seal_digest)
        assert "AGPSealKeyInvalid" in src
        assert "refusing" in src.lower()

    def test_no_live_status_widening_here(self):
        """This module never touches paper-trade status lists or live paths;
        guard against accidental imports of betting machinery."""
        assert not hasattr(agp, "_PAPER_TRADE_SIGNAL_STATUSES") or (
            "live" not in getattr(agp, "_PAPER_TRADE_SIGNAL_STATUSES", ())
        )
