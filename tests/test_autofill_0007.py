"""Autofill characterization #0007 — AGP seal fail-closed (LONG).

Characterizes the keyed-seal contract of ``AGPSession.verify_seal`` /
``_seal_digest`` / ``AGPSession.seal``:

1. When CALLISTO_SEAL_KEY is set (keyed regime), verify_seal must NEVER
   accept the raw public SHA-256 of the canonical payload. An unkeyed
   digest is forgeable by anyone with DB write access, so accepting one
   under a keyed regime would silently reopen the forgery hole.
2. Fail-closed on a set-but-invalid CALLISTO_SEAL_KEY: seal() must raise
   (never fall back to writing an unkeyed seal) and verify_seal must
   return False (never accept unkeyed digests as a fallback).
3. Legacy unkeyed regime (no key at all) still verifies legacy seals,
   but new seals are unkeyed — characterized so any future tightening
   is a deliberate, visible change.
4. Key rotation via CALLISTO_SEAL_KEY_OLD.
5. General tamper / malformed-input robustness under every regime.

These tests are characterization only: they pin the CURRENT fail-closed
behavior. They must not weaken any production gate; if any assertion
here starts failing because gates got looser, that's a regression.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from agp import (
    AGPSealKeyInvalid,
    AGPSealRefused,
    AGPSession,
    ConfidenceTier,
    Domain,
    EMPTY_SYNTHESIS_MARKER,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)

VALID_KEY = "ab" * 32
ALT_KEY = "cd" * 32
INVALID_HEX_KEY = "not-valid-hex-zz"


# ── helpers ──────────────────────────────────────────────────────────────


def _build_sealable_session(query: str = "unladen swallow airspeed?") -> AGPSession:
    """Walk a session through all steps so seal() accepts it."""
    s = AGPSession(query)
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["example.org"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="African swallow is ~24 mph",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.72,
        domain=Domain.TECHNICAL,
        origin_agent="test",
        source_name="https://example.org/swallow",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=s.scope,
        domain=Domain.TECHNICAL,
        conclusion="African swallow airspeed is ~24 mph unladen.",
        confidence_score=0.7,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def _public_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hmac_hex(key_hex: str, payload: str) -> str:
    return hmac.new(
        bytes.fromhex(key_hex), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _with_hmac(data: dict, key_hex: str) -> dict:
    """Return a copy of ``data`` sealed with an HMAC under ``key_hex``,
    computed exactly the way verify_seal recomputes it (over the
    canonical payload with seal_hash normalized to None)."""
    out = dict(data)
    out["seal_hash"] = None
    out["seal_hash"] = _hmac_hex(key_hex, _canonical_payload(out))
    return out


def _canonical_payload(data: dict) -> str:
    payload_dict = dict(data)
    payload_dict["seal_hash"] = None
    return json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)


def _stored_unkeyed(session: AGPSession) -> dict:
    """Return the session dict re-sealed with the FORGEABLE public SHA-256.

    This is exactly what an attacker with DB write access could produce:
    strip the real seal, recompute the plain hash over the canonical
    payload, and store it back.
    """
    data = session.to_dict()
    data["seal_hash"] = None
    data["sealed_at"] = "2026-08-26T00:00:00+00:00"
    data["seal_hash"] = _public_sha256(_canonical_payload(data))
    return data


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


@pytest.fixture
def bad_key(monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", INVALID_HEX_KEY)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ── 1. keyed regime must reject unkeyed SHA-256 ─────────────────────────


class TestKeyedRegimeRejectsUnkeyed:
    def test_forged_public_hash_rejected_when_keyed(self, keyed):
        """The core property: with CALLISTO_SEAL_KEY set, a plain SHA-256
        over the canonical payload must NOT verify."""
        s = _build_sealable_session()
        forged = _stored_unkeyed(s)
        assert forged["seal_hash"] == _public_sha256(_canonical_payload(forged))
        assert AGPSession.verify_seal(forged) is False

    def test_forged_public_hash_json_string_rejected(self, keyed):
        s = _build_sealable_session()
        forged_json = json.dumps(_stored_unkeyed(s))
        assert AGPSession.verify_seal(forged_json) is False

    def test_legit_hmac_accepted_when_keyed(self, keyed):
        s = _build_sealable_session()
        s.seal()
        data = s.to_dict()
        assert data["seal_hash"] == _hmac_hex(
            VALID_KEY, _canonical_payload(data))
        assert AGPSession.verify_seal(data) is True

    def test_wrong_key_hmac_rejected(self, keyed):
        s = _build_sealable_session()
        data = _with_hmac(s.to_dict(), ALT_KEY)
        assert AGPSession.verify_seal(data) is False

    def test_tampered_payload_under_correct_key_rejected(self, keyed):
        s = _build_sealable_session()
        s.seal()
        data = s.to_dict()
        import copy as _copy
        for mutate in (
            lambda d: d["summary"].__setitem__("conclusion", "tampered"),
            lambda d: d["summary"].__setitem__("conclusion",
                                               d["summary"]["conclusion"] + "!"),
            lambda d: d.pop("summary"),
            lambda d: d.__setitem__("query", "swapped query"),
        ):
            tampered = _copy.deepcopy(data)
            mutate(tampered)
            assert AGPSession.verify_seal(tampered) is False
        # untouched original still verifies
        assert AGPSession.verify_seal(_copy.deepcopy(data)) is True

    def test_seal_produces_keyed_digest_not_public_hash(self, keyed):
        s = _build_sealable_session()
        digest = s.seal()
        payload = _canonical_payload(s.to_dict())
        assert digest == _hmac_hex(VALID_KEY, payload)
        assert digest != _public_sha256(payload)

    def test_verify_does_not_accept_unkeyed_even_alongside_valid(self, keyed):
        """Even if some candidate matches, the unkeyed digest alone stored
        in a fresh dict must fail — no partial acceptance."""
        s = _build_sealable_session()
        payload = _canonical_payload(s.to_dict())
        both = s.to_dict()
        # attacker replaces HMAC with the free public digest
        both["seal_hash"] = _public_sha256(payload)
        assert AGPSession.verify_seal(both) is False


class TestInvalidKeyFailsClosed:
    def test_seal_raises_on_invalid_hex_key(self, bad_key):
        s = _build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()

    def test_seal_leaves_no_usable_hash_on_failure(self, bad_key):
        s = _build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        # the session must not carry a forgeable unkeyed seal after refusal
        if s.sealed_at is not None:
            assert AGPSession.verify_seal(s.to_dict()) is False

    def test_verify_rejects_unkeyed_digest_under_invalid_key(self, bad_key):
        """Set-but-invalid key is still a keyed regime: the public hash
        must be rejected rather than silently accepted as fallback."""
        s = _build_sealable_session()
        forged = _stored_unkeyed(s)
        assert AGPSession.verify_seal(forged) is False

    def test_blank_key_is_not_a_keyed_regime(self, monkeypatch):
        """Characterization: whitespace-only CALLISTO_SEAL_KEY counts as
        UNSET (_seal_key_configured strips before checking), so the legacy
        unkeyed digest is still accepted. Only a non-blank invalid key
        triggers fail-closed."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        s = _build_sealable_session()
        forged = _stored_unkeyed(s)
        assert AGPSession.verify_seal(forged) is True

    def test_verify_rejects_legacy_hmac_of_other_data(self, bad_key):
        """A raw HMAC over a NON-canonical payload (e.g. with seal_hash
        still embedded) must not verify — only the canonical form counts."""
        s = _build_sealable_session()
        data = s.to_dict()
        raw_hmac = hmac.new(
            bytes.fromhex(ALT_KEY),
            json.dumps(data, sort_keys=True).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        data["seal_hash"] = raw_hmac
        assert AGPSession.verify_seal(data) is False

    def test_invalid_primary_falls_through_to_rotation_key(self, monkeypatch):
        """Characterization of CURRENT behavior: an invalid primary key is
        skipped in _seal_keys(), so with a valid CALLISTO_SEAL_KEY_OLD the
        first usable key is the rotation key and seal() succeeds using it.
        It still never produces an unkeyed digest — fail-closed against
        forgery holds even in this degraded regime."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", INVALID_HEX_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ALT_KEY)
        s = _build_sealable_session()
        digest = s.seal()
        data = s.to_dict()
        payload = _canonical_payload(data)
        # sealed with the ROTATION key, not unkeyed, not the primary
        assert digest == _hmac_hex(ALT_KEY, payload)
        assert digest != _public_sha256(payload)
        assert AGPSession.verify_seal(data) is True
        forged = _stored_unkeyed(s)
        assert AGPSession.verify_seal(forged) is False

    def test_invalid_primary_and_no_rotation_raises(self, monkeypatch):
        """Invalid primary + no rotation keys = keyed regime with zero
        usable keys: seal() must raise AGPSealKeyInvalid."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", INVALID_HEX_KEY)
        monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
        s = _build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()


# ── 2. legacy unkeyed regime (no key configured) ─────────────────────────


class TestLegacyUnkeyedRegime:
    def test_no_key_seal_is_public_sha256(self, no_key):
        s = _build_sealable_session()
        digest = s.seal()
        payload = _canonical_payload(s.to_dict())
        assert digest == _public_sha256(payload)

    def test_no_key_verify_accepts_legacy_seal(self, no_key):
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_no_key_verify_accepts_json_roundtrip(self, no_key):
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_no_key_still_rejects_garbage_hashes(self, no_key):
        s = _build_sealable_session()
        s.seal()
        for junk in ("0" * 64, "f" * 64, "", "deadbeef", "z" * 64):
            data = s.to_dict()
            data["seal_hash"] = junk
            assert AGPSession.verify_seal(data) is False

    def test_old_key_alone_verifies_but_new_seals_are_unkeyed(self, monkeypatch):
        """CALLISTO_SEAL_KEY unset but OLD set: rotation-only regime.
        Old-key seals verify; freshly minted seals remain unkeyed legacy."""
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ALT_KEY)
        s = _build_sealable_session()
        digest = s.seal()
        payload = _canonical_payload(s.to_dict())
        assert digest == _public_sha256(payload)
        data = _with_hmac(s.to_dict(), ALT_KEY)
        assert AGPSession.verify_seal(data) is True


class TestKeyRotation:
    def test_current_and_old_both_verify(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ALT_KEY)
        base = {"a": 1}
        current_seal = _with_hmac(base, VALID_KEY)
        old_seal = _with_hmac(base, ALT_KEY)
        assert AGPSession.verify_seal(current_seal) is True
        assert AGPSession.verify_seal(old_seal) is True

    def test_multiple_old_keys_comma_separated(self, monkeypatch):
        third = "ee" * 32
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{ALT_KEY}, {third}")
        for k in (VALID_KEY, ALT_KEY, third):
            d = _with_hmac({"a": 1}, k)
            assert AGPSession.verify_seal(d) is True

    def test_invalid_old_entries_skipped_not_fatal(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{INVALID_HEX_KEY}, {ALT_KEY}")
        d = _with_hmac({"a": 1}, ALT_KEY)
        assert AGPSession.verify_seal(d) is True
        bad = {"seal_hash": "0" * 64, "a": 1}
        assert AGPSession.verify_seal(bad) is False

    def test_whitespace_only_old_list_ignored(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", " , ,")
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_unkeyed_digest_rejected_during_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ALT_KEY)
        s = _build_sealable_session()
        forged = _stored_unkeyed(s)
        assert AGPSession.verify_seal(forged) is False


# ── 3. input robustness across regimes ───────────────────────────────────


REGIME_FIXTURES = ["no_key", "keyed", "bad_key"]


class TestVerifySealMalformedInputs:
    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_none_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        assert AGPSession.verify_seal(None) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_int_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        assert AGPSession.verify_seal(42) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_malformed_json_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        assert AGPSession.verify_seal("{not json") is False

    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_missing_seal_hash_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        s = _build_sealable_session()
        data = s.to_dict()
        data.pop("seal_hash")
        assert AGPSession.verify_seal(data) is False

    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_nonstring_seal_hash_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = 12345
        assert AGPSession.verify_seal(data) is False

    @pytest.mark.parametrize("fixture_name", REGIME_FIXTURES)
    def test_empty_dict_rejected(self, fixture_name, request):
        request.getfixturevalue(fixture_name)
        assert AGPSession.verify_seal({}) is False

    def test_never_raises_on_weird_input(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        weird_inputs = [
            b"bytes-not-str",           # type: ignore[list-item]
            [1, 2, 3],                  # type: ignore[list-item]
            {"seal_hash": None},
            {"seal_hash": ["x"]},
            "{}",
        ]
        for w in weird_inputs:
            try:
                result = AGPSession.verify_seal(w)  # type: ignore[arg-type]
            except Exception as e:  # pragma: no cover - contract says never raises
                pytest.fail(f"verify_seal raised {e!r} on {w!r}")
            assert result is False

    @pytest.mark.parametrize("payload", ["null", "[]"])
    @pytest.mark.xfail(reason="KNOWN EDGE: JSON that parses to a non-dict "
                       "(None / list) escapes the try/except and raises "
                       "AttributeError at data.get(); violates the "
                       "never-raises contract", strict=True,
                       raises=AttributeError)
    def test_json_nondict_input_does_not_raise(self, payload):
        """Documents a real gap in the never-raises contract: valid JSON
        that is not an object (null, list) reaches .get() on a non-dict.
        Pinned via xfail so fixing production flips this to pass."""
        assert AGPSession.verify_seal(payload) is False

    def test_weird_inputs_never_raise(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        weird_inputs = [
            b"bytes-not-str",           # type: ignore[list-item]
            [1, 2, 3],                  # type: ignore[list-item]
            {"seal_hash": None},
            {"seal_hash": ["x"]},
        ]
        for w in weird_inputs:
            try:
                result = AGPSession.verify_seal(w)  # type: ignore[arg-type]
            except Exception as e:  # pragma: no cover - contract says never raises
                pytest.fail(f"verify_seal raised {e!r} on {w!r}")
            assert result is False


# ── 4. seal() gating interactions ────────────────────────────────────────


class TestSealGatingInteractions:
    def test_refusal_precedes_key_error(self, keyed):
        """A garbage session must hit AGPSealRefused before any key work —
        either way it fails closed, but characterize the precedence."""
        s = AGPSession("empty query")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope=s.scope,
            domain=Domain.GENERAL,
            conclusion=EMPTY_SYNTHESIS_MARKER,
            confidence_score=0.3,
            evidence_count=0,
            contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_double_seal_refuses(self, keyed):
        from agp import AGPViolation
        s = _build_sealable_session()
        first = s.seal()
        with pytest.raises(AGPViolation):
            s.seal()  # re-sealing must refuse, not mint a second digest
        assert s.seal_hash == first

    def test_verify_after_manual_field_edit_fails(self, keyed):
        s = _build_sealable_session()
        s.seal()
        data = s.to_dict()
        data["query"] = "different query"
        assert AGPSession.verify_seal(data) is False

    def test_verify_detects_evidence_swap(self, keyed):
        s = _build_sealable_session()
        s.seal()
        data = s.to_dict()
        ev = data.get("evidence") or []
        if ev:
            ev[0] = dict(ev[0])
            if isinstance(ev[0], dict) and "content" in ev[0]:
                ev[0]["content"] = "fabricated"
            assert AGPSession.verify_seal(data) is False

    def test_sealed_flag_consistent_with_hash(self, keyed):
        s = _build_sealable_session()
        assert s.sealed_at is None
        s.seal()
        assert s.sealed_at is not None
        assert s.seal_hash
        assert AGPSession.verify_seal(s.to_dict()) is True


# ── 5. cross-regime matrix ───────────────────────────────────────────────


class TestCrossRegimeMatrix:
    def test_unkeyed_seal_from_legacy_db_rejected_once_key_set(self, no_key, monkeypatch):
        """Simulates migration: rows sealed pre-keying become unverifiable
        the moment an operator sets CALLISTO_SEAL_KEY. Fail-closed means
        those rows are treated as untrusted, not silently accepted."""
        s = _build_sealable_session()
        s.seal()  # legacy unkeyed seal
        legacy_row = json.dumps(s.to_dict())
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert AGPSession.verify_seal(legacy_row) is False

    def test_keyed_seal_survives_json_storage_roundtrip(self, keyed):
        s = _build_sealable_session()
        s.seal()
        restored = json.loads(json.dumps(s.to_dict()))
        assert AGPSession.verify_seal(restored) is True

    def test_unicode_content_survives_seal_verify(self, keyed):
        s = _build_sealable_session("¿cuánto mide una golondrina? 🕊")
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True
        data = s.to_dict()
        data["query"] = "¿cuánto mide una golondrina? 🕊🕊"  # one extra bird
        assert AGPSession.verify_seal(data) is False

    def test_digest_length_and_format(self, keyed):
        s = _build_sealable_session()
        digest = s.seal()
        assert len(digest) == 64
        int(digest, 16)  # valid hex
