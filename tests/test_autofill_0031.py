"""Autofill characterization #0031 — AGP seal fail-closed (LONG).

Characterizes the keyed-seal regime around ``CALLISTO_SEAL_KEY``:

Core invariant under test
-------------------------
When an operator intends keyed sealing (CALLISTO_SEAL_KEY is set, even to a
value that turns out to be invalid hex), the seal machinery must NEVER fall
back to the forgeable, public unkeyed SHA-256 digest. In particular:

  * ``verify_seal`` must reject seals whose hash equals the plain SHA-256 of
    the canonical payload whenever a key regime is active — accepting them
    would let anyone who can write the store forge a valid seal without
    knowing the secret.
  * A set-but-invalid key fails CLOSED: verification returns False (or raises
    at digest-production time), it never silently downgrades to unkeyed.
  * The legacy unkeyed SHA-256 acceptance exists ONLY in the no-key-at-all
    regime, for backward compatibility with pre-keying sealed sessions.
  * ``ask`` refuses to run entirely when the key is missing/blank/invalid —
    the CLI gate mirrors the library gate.

These are characterization tests: they pin today's fail-closed behavior so
future edits cannot quietly widen the gates. Nothing here arms live betting;
no production code is modified by this module.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agp  # noqa: E402
from agp import (  # noqa: E402
    AGPSession,
    AGPSealKeyInvalid,
    _seal_digest,
    _seal_keys,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)
from agp import _seal_key_configured, _canonical_payload  # noqa: E402
from agp.preregistration import Criteria, Preregistration  # noqa: E402
from agp.preregistration import PreregistrationError  # noqa: E402

VALID_KEY = "ab" * 32          # 32-byte hex key
OTHER_KEY = "cd" * 32          # different valid key
INVALID_KEY = "not-valid-hex-zz"


# ─────────────────────────── helpers / fixtures ──────────────────────────


def _sha256_hex(payload: str) -> str:
    """The public, forgeable unkeyed digest — must be rejected under keying."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hmac_hex(key_hex: str, payload: str) -> str:
    return hmac_mod.new(
        bytes.fromhex(key_hex), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@pytest.fixture
def clean_seal_env(monkeypatch):
    """Start every seal test from a known env state (no keys configured)."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    return monkeypatch


def _sealed_session() -> AGPSession:
    """Build a fully-lifecycled session and seal it (current env keying)."""
    s = AGPSession(query="characterization query 0031")
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="supporting fact", source_class=SourceClass.SECONDARY,
        confidence_score=0.7, domain=Domain.TECHNICAL, origin_agent="test",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope="characterization 0031", domain=Domain.TECHNICAL,
        conclusion="result", confidence_score=0.6,
        evidence_count=1, contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    s.seal()
    return s


def _prereg_sealed() -> Preregistration:
    p = Preregistration(
        query="will marker X appear?",
        criteria=Criteria(confirm_markers=["yes"], refute_markers=["no"]),
    )
    p.seal()
    return p


# ─────────────────────── 1. Key-regime detection ─────────────────────────


class TestSealKeyConfigured:
    def test_unset_key_means_unconfigured(self, clean_seal_env):
        assert _seal_key_configured() is False

    def test_blank_key_means_unconfigured(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", "   ")
        assert _seal_key_configured() is False

    def test_any_nonempty_key_is_configured(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert _seal_key_configured() is True

    def test_even_invalid_hex_counts_as_configured(self, clean_seal_env):
        """A set-but-garbage key still signals keyed intent — and that intent
        must fail closed downstream, never downgrade to unkeyed."""
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        assert _seal_key_configured() is True


class TestSealKeys:
    def test_no_key_yields_no_keys(self, clean_seal_env):
        assert _seal_keys() == []

    def test_valid_key_parsed_as_bytes(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        keys = _seal_keys()
        assert len(keys) == 1
        assert keys[0] == bytes.fromhex(VALID_KEY)

    def test_invalid_hex_yields_no_usable_keys(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        assert _seal_keys() == []

    def test_old_rotation_key_included(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        clean_seal_env.setenv("CALLISTO_SEAL_KEY_OLD", OTHER_KEY)
        keys = _seal_keys()
        assert bytes.fromhex(VALID_KEY) in keys
        assert bytes.fromhex(OTHER_KEY) in keys

    def test_blank_old_entries_skipped(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        clean_seal_env.setenv("CALLISTO_SEAL_KEY_OLD", " , ,")
        assert _seal_keys() == [bytes.fromhex(VALID_KEY)]


# ─────────────────────── 2. Digest production ────────────────────────────


class TestSealDigest:
    def test_unkeyed_regime_uses_plain_sha256(self, clean_seal_env):
        payload = '{"a":1}'
        assert _seal_digest(payload) == _sha256_hex(payload)

    def test_keyed_regime_uses_hmac(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        payload = '{"a":1}'
        assert _seal_digest(payload) == _hmac_hex(VALID_KEY, payload)

    def test_keyed_digest_differs_from_public_sha256(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        payload = '{"tamper":"me"}'
        assert _seal_digest(payload) != _sha256_hex(payload)

    def test_invalid_key_raises_fail_closed(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        with pytest.raises(AGPSealKeyInvalid):
            _seal_digest('{"a":1}')

    def test_invalid_key_never_returns_unkeyed_digest(self, clean_seal_env):
        """The critical property: an unusable key must raise, never produce
        the forgeable public SHA-256."""
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        payload = '{"a":1}'
        try:
            digest = _seal_digest(payload)
        except AGPSealKeyInvalid:
            digest = None
        assert digest != _sha256_hex(payload)

    def test_wrong_key_produces_different_digest(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        d1 = _seal_digest('{"q":"x"}')
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        assert _seal_digest('{"q":"x"}') != d1


# ─────────────────── 3. AGPSession.verify_seal gating ────────────────────


class TestSessionVerifySealUnkeyedRejection:
    """Keyed regime must NOT accept the public unkeyed SHA-256 of a payload."""

    def _forged_unkeyed(self, session: AGPSession) -> tuple:
        data = dict(session.to_dict())
        data["seal_hash"] = None
        payload = _canonical_payload(data)
        return data, _sha256_hex(payload)

    def test_keyed_regime_rejects_unkeyed_hash(self, clean_seal_env):
        s = _sealed_session()  # unkeyed regime seal (plain SHA-256)
        data, forged = self._forged_unkeyed(s)
        data["seal_hash"] = forged
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        # The stored seal IS the correct public SHA-256 of the payload,
        # yet under keying it must be rejected.
        assert AGPSession.verify_seal(data) is False

    def test_keyed_regime_rejects_unkeyed_hash_json_string(self, clean_seal_env):
        s = _sealed_session()
        data, forged = self._forged_unkeyed(s)
        data["seal_hash"] = forged
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert AGPSession.verify_seal(json.dumps(data)) is False

    def test_invalid_key_regime_fails_closed(self, clean_seal_env):
        s = _sealed_session()
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        assert AGPSession.verify_seal(s.to_dict()) is False

    def test_invalid_key_rejects_even_correct_hmac_of_other_regime(
        self, clean_seal_env
    ):
        s = _sealed_session()
        data = dict(s.to_dict())
        data["seal_hash"] = None
        payload = _canonical_payload(data)
        data["seal_hash"] = _hmac_hex(VALID_KEY, payload)
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        assert AGPSession.verify_seal(data) is False

    def test_unkeyed_regime_still_accepts_its_own_seals(self, clean_seal_env):
        s = _sealed_session()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_keyed_regime_accepts_correctly_keyed_seal(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _sealed_session()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_keyed_regime_rejects_foreign_key_seal(self, clean_seal_env):
        """Sealed under one key, verified under another — must fail."""
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        s = _sealed_session()
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert AGPSession.verify_seal(s.to_dict()) is False

    def test_rotation_old_key_accepted(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", OLD := OTHER_KEY)
        s = _sealed_session()
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        clean_seal_env.setenv("CALLISTO_SEAL_KEY_OLD", OLD)
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_tampered_payload_rejected_under_keying(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _sealed_session()
        data = s.to_dict()
        if "query" in data:
            data["query"] = data["query"] + " tampered"
        assert AGPSession.verify_seal(data) is False

    def test_missing_seal_hash_rejected(self, clean_seal_env):
        s = _sealed_session()
        data = s.to_dict()
        data.pop("seal_hash", None)
        assert AGPSession.verify_seal(data) is False

    def test_none_seal_hash_rejected(self, clean_seal_env):
        s = _sealed_session()
        data = s.to_dict()
        data["seal_hash"] = None
        assert AGPSession.verify_seal(data) is False

    def test_malformed_json_rejected(self, clean_seal_env):
        assert AGPSession.verify_seal("{not json") is False

    def test_verify_never_raises_under_weird_input(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        for bad in (None, 42, b"bytes", {"seal_hash": 123}, []):
            try:
                result = AGPSession.verify_seal(bad)  # type: ignore[arg-type]
            except Exception as exc:  # pragma: no cover - characterization
                pytest.fail(f"verify_seal raised {exc!r} on {bad!r}")
            assert result is False or isinstance(result, bool)


# ─────────── 4. Preregistration.verify_seal — same fail-closed law ───────


class TestPreregVerifySeal:
    def _payload_and_unkeyed(self, p: Preregistration) -> str:
        obj = {
            "query": p.query,
            "criteria": p.criteria.to_dict(),
            "created_at": p.created_at,
            "sealed_at": p.sealed_at,
        }
        from agp.preregistration import _canonical
        return _canonical(obj)

    def test_sealed_prereg_verifies_without_key(self, clean_seal_env):
        assert _prereg_sealed().verify_seal() is True

    def test_keyed_seal_verifies_under_same_key(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        p = _prereg_sealed()
        assert p.verify_seal() is True

    def test_keyed_seal_rejected_under_other_key(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        p = _prereg_sealed()
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert p.verify_seal() is False

    def test_invalid_key_fails_closed_not_exception(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        p = _prereg_sealed()
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", INVALID_KEY)
        # Must surface as False (unverifiable), never raise.
        assert p.verify_seal() is False

    def test_unverifiable_before_seal(self, clean_seal_env):
        p = Preregistration(
            query="q",
            criteria=Criteria(confirm_markers=["a"], refute_markers=["b"]),
        )
        assert p.verify_seal() is False

    def test_tampered_query_breaks_seal(self, clean_seal_env):
        p = _prereg_sealed()
        object.__setattr__(p, "query", p.query + " rewritten")  # simulate tamper
        assert p.verify_seal() is False

    def test_tampered_criteria_breaks_seal(self, clean_seal_env):
        p = _prereg_sealed()
        fresh = Criteria(confirm_markers=["zzz"], refute_markers=["yyy"])
        object.__setattr__(p, "criteria", fresh)
        assert p.verify_seal() is False

    def test_seal_hash_matches_hmac_when_keyed(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        p = _prereg_sealed()
        expected = _hmac_hex(VALID_KEY, self._payload_and_unkeyed(p))
        assert p.seal_hash == expected

    def test_seal_hash_is_not_public_sha256_when_keyed(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        p = _prereg_sealed()
        assert p.seal_hash != _sha256_hex(self._payload_and_unkeyed(p))


# ───────────────── 5. Sealed-object immutability (context) ───────────────


class TestPreregImmutabilityContext:
    """Immutability backs the seal's evidentiary value; characterize it too."""

    def test_cannot_rewrite_query_after_seal(self, clean_seal_env):
        p = _prereg_sealed()
        with pytest.raises(Exception):
            p.query = "post-hoc rewrite"

    def test_cannot_swap_criteria_after_seal(self, clean_seal_env):
        p = _prereg_sealed()
        with pytest.raises(Exception):
            p.criteria = Criteria(confirm_markers=["x"], refute_markers=["y"])

    def test_double_seal_refused(self, clean_seal_env):
        p = _prereg_sealed()
        with pytest.raises(PreregistrationError):
            p.seal()

    def test_amend_requires_reason(self, clean_seal_env):
        p = _prereg_sealed()
        new = Criteria(confirm_markers=["c"], refute_markers=["r"])
        with pytest.raises(PreregistrationError):
            p.amend(new, reason="   ")

    def test_amend_preserves_original_criteria(self, clean_seal_env):
        p = _prereg_sealed()
        original = p.criteria.to_dict()
        new = Criteria(confirm_markers=["c2"], refute_markers=["r2"])
        p.amend(new, reason="scope narrowed")
        assert p.criteria.to_dict() == original
        assert p.amendments and p.amendments[-1]["new_criteria"] == new.to_dict()

    def test_original_seal_still_verifies_after_amend(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        p = _prereg_sealed()
        new = Criteria(confirm_markers=["c3"], refute_markers=["r3"])
        p.amend(new, reason="legitimate amendment")
        assert p.verify_seal() is True

    def test_empty_query_cannot_seal(self, clean_seal_env):
        p = Preregistration(
            query="   ",
            criteria=Criteria(confirm_markers=["a"], refute_markers=["b"]),
        )
        with pytest.raises(PreregistrationError):
            p.seal()

    def test_incomplete_criteria_cannot_seal(self, clean_seal_env):
        p = Preregistration(query="q", criteria=Criteria(confirm_markers=["a"]))
        with pytest.raises(PreregistrationError):
            p.seal()


# ─────────────────── 6. Static gate-widening tripwires ───────────────────


class TestGateWideningTripwires:
    """Source-level pins: the fail-closed comments/branches must survive."""

    def _source(self, path: str) -> str:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, path), "r", encoding="utf-8") as fh:
            return fh.read()

    def test_seal_digest_mentions_forgery_rationale(self):
        src = self._source("agp/__init__.py")
        assert "forgeable" in src

    def test_seal_digest_has_invalid_key_raise_branch(self):
        src = self._source("agp/__init__.py")
        assert "raise AGPSealKeyInvalid" in src
        assert "refusing to produce" in src

    def test_verify_documents_keyed_no_unkeyed_fallback(self):
        src = self._source("agp/__init__.py")
        assert "deliberately NOT accepted" in src or \
               "deliberately not accepted" in src

    def test_verify_gates_unkeyed_candidate_on_unconfigured(self):
        src = self._source("agp/__init__.py")
        needle = 'if not _seal_key_configured():'
        idx = src.find(needle)
        assert idx != -1, "unkeyed fallback lost its configuration guard"
        tail = src[idx:idx + 600]
        assert "sha256" in tail.lower(), (
            "unkeyed candidate branch no longer appends the legacy digest")

    def test_prereg_verify_fail_closed_comment_present(self):
        src = self._source("agp/preregistration.py")
        assert "Fail-closed" in src

    def test_prereg_verify_swallows_digest_failure_to_false(self):
        src = self._source("agp/preregistration.py")
        assert "except Exception:\n            return False" in src

    def test_ask_module_keeps_unkeyed_gate_language(self):
        src = self._source("tools/cli/ask.py")
        assert "unkeyed" in src.lower(), (
            "ask's unkeyed-seal refusal message disappeared")

    def test_agp_exports_seal_error_type(self):
        assert hasattr(agp, "AGPSealKeyInvalid")


# ─────────────────── 7. Cross-checking invariants ────────────────────────


class TestCrossInvariants:
    def test_unkeyed_digest_equals_manual_sha256(self, clean_seal_env):
        payload = '{"k":"v","n":7}'
        manual = hashlib.sha256(payload.encode()).hexdigest()
        assert _seal_digest(payload) == manual

    def test_keyed_digest_is_64_hex_chars(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert len(_seal_digest("{}")) == 64
        int(_seal_digest("{}"), 16)  # parses as hex

    def test_two_sessions_same_payload_diverge_under_key(self, clean_seal_env):
        """HMAC binds to the key; identical payloads across regimes differ."""
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        keyed = _seal_digest('{"same":true}')
        clean_seal_env.delenv("CALLISTO_SEAL_KEY")
        unkeyed = _seal_digest('{"same":true}')
        assert keyed != unkeyed

    def test_verify_accepts_only_current_or_rotation_keys(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _sealed_session()
        data = dict(s.to_dict())
        data["seal_hash"] = None
        payload = _canonical_payload(data)
        # Forge with a random third key: must be rejected.
        rogue = _hmac_hex("ee" * 32, payload)
        data["seal_hash"] = rogue
        assert AGPSession.verify_seal(data) is False

    def test_verify_is_deterministic(self, clean_seal_env):
        clean_seal_env.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _sealed_session()
        results = {AGPSession.verify_seal(s.to_dict()) for _ in range(5)}
        assert results == {True}

    def test_constant_time_compare_used_in_verify(self, clean_seal_env):
        src = inspect.getsource(AGPSession.verify_seal)
        assert "compare_digest" in src
