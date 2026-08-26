"""autofill characterization #0015 — AGP seal fail-closed (keyed vs unkeyed).

Characterizes the seal-keying contract of ``AGPSession.seal`` /
``AGPSession.verify_seal`` and the supporting key-parsing helpers
(``_seal_key_configured``, ``_seal_keys``, ``_seal_digest``):

1. When CALLISTO_SEAL_KEY is configured (a keyed regime), verify_seal must
   NEVER accept the raw public SHA-256 of the canonical payload — that would
   let anyone with DB write access forge a seal without knowing the key.
2. A configured-but-invalid (non-hex) key FAILS CLOSED: no unkeyed fallback,
   seal() refuses to write, verify_seal() returns False.
3. In the legacy unkeyed regime (no key at all), both the public SHA-256 and
   any HMAC candidates are accepted so old seals still verify.
4. Key rotation via CALLISTO_SEAL_KEY_OLD works and never re-admits the
   unkeyed digest while a keyed regime is active.
5. The `callisto ask` CLI refuses (fails closed) on missing/blank/non-hex
   keys — see tests/test_cli_ask_seal.py, which runs alongside this module.

Tests-only module: no production code is touched; gates must stay as-is.
"""
from __future__ import annotations

import hashlib
import hmac as hmac_mod
import json

import pytest

from agp import (
    AGPSealKeyInvalid,
    AGPSealRefused,
    AGPSession,
    AGPViolation,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)

VALID_KEY = "ab" * 32            # 64 hex chars = 32 bytes
OLD_KEY = "cd" * 32              # rotation key
OTHER_KEY = "11" * 32


# ── helpers ────────────────────────────────────────────────────────────────

def _build_sealable_session() -> AGPSession:
    """Walk a session through all steps with real evidence + conclusion."""
    s = AGPSession("characterization #0015 scope")
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["example.org"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="keyed seals require the secret, not the repo",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.8,
        domain=Domain.TECHNICAL,
        origin_agent="ox-alpha",
        source_name="https://example.org/seals",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=s.scope,
        domain=Domain.TECHNICAL,
        conclusion="A keyed HMAC regime rejects forgeable unkeyed SHA-256 seals.",
        confidence_score=0.75,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def _public_sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _hmac_hex(key_hex: str, payload: str) -> str:
    return hmac_mod.new(
        bytes.fromhex(key_hex), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def _canonical_payload_of(data: dict) -> str:
    from agp import _canonical_payload
    return _canonical_payload(data)


@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    """Every test starts from an explicit key environment."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)
    yield


# ── key parsing / regime detection ────────────────────────────────────────

class TestSealKeyRegimeDetection:
    def test_no_key_is_unconfigured(self):
        from agp import _seal_key_configured
        assert _seal_key_configured() is False

    def test_valid_key_is_configured(self, monkeypatch):
        from agp import _seal_key_configured
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert _seal_key_configured() is True

    def test_invalid_key_still_counts_as_configured(self, monkeypatch):
        from agp import _seal_key_configured
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-not-hex")
        assert _seal_key_configured() is True

    def test_blank_key_is_unconfigured(self, monkeypatch):
        from agp import _seal_key_configured
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        assert _seal_key_configured() is False

    def test_seal_keys_empty_when_unset(self):
        from agp import _seal_keys
        assert _seal_keys() == []

    def test_seal_keys_parses_current_key(self, monkeypatch):
        from agp import _seal_keys
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert _seal_keys() == [bytes.fromhex(VALID_KEY)]

    def test_invalid_hex_key_yields_no_usable_key(self, monkeypatch):
        """Configured-but-unusable: empty list + configured=True = fail-closed
        regime. This pair is exactly what blocks the unkeyed fallback."""
        from agp import _seal_key_configured, _seal_keys
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
        assert _seal_key_configured() is True
        assert _seal_keys() == []

    def test_rotation_keys_parsed_from_old_var(self, monkeypatch):
        from agp import _seal_keys
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{OLD_KEY}, {OTHER_KEY}")
        keys = _seal_keys()
        assert keys[0] == bytes.fromhex(VALID_KEY)
        assert bytes.fromhex(OLD_KEY) in keys
        assert bytes.fromhex(OTHER_KEY) in keys
        assert len(keys) == 3

    def test_bad_old_keys_skipped_silently(self, monkeypatch):
        from agp import _seal_keys
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "garbage,,also-bad")
        assert _seal_keys() == [bytes.fromhex(VALID_KEY)]


class TestSealDigest:
    def test_unkeyed_regime_uses_public_sha256(self):
        from agp import _seal_digest
        payload = '{"a": 1}'
        assert _seal_digest(payload) == _public_sha256(payload)

    def test_keyed_regime_uses_hmac(self, monkeypatch):
        from agp import _seal_digest
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        payload = '{"a": 1}'
        d = _seal_digest(payload)
        assert d == _hmac_hex(VALID_KEY, payload)
        assert d != _public_sha256(payload)

    def test_invalid_key_raises_fail_closed(self, monkeypatch):
        from agp import _seal_digest
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
        with pytest.raises(AGPSealKeyInvalid):
            _seal_digest('{"a": 1}')


# ── THE core invariant: keyed verify_seal must not accept unkeyed SHA-256 ──

class TestKeyedVerifyRejectsUnkeyedDigest:
    """If CALLISTO_SEAL_KEY is set, the raw SHA-256 of the canonical payload
    is public knowledge — accepting it makes every seal forgeable."""

    def test_verify_refuses_public_sha256_under_keyed_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        data = {"session_id": "s1", "summary": {"conclusion": "c"},
                "sealed_at": "2026-01-01T00:00:00Z"}
        payload = _canonical_payload_of(data)
        data["seal_hash"] = _public_sha256(payload)  # forged, no key needed
        assert AGPSession.verify_seal(data) is False

    def test_forged_unkeyed_seal_rejected_in_json_form(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        data = {"session_id": "s2", "evidence": [], "sealed_at": None}
        payload = _canonical_payload_of(data)
        forged = dict(data, seal_hash=_public_sha256(payload))
        assert AGPSession.verify_seal(json.dumps(forged)) is False

    def test_verify_accepts_correct_hmac_under_keyed_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        data = {"session_id": "s3", "sealed_at": "2026-01-01T00:00:00Z"}
        payload = _canonical_payload_of(data)
        sealed = dict(data, seal_hash=_hmac_hex(VALID_KEY, payload))
        assert AGPSession.verify_seal(sealed) is True

    def test_wrong_key_hmac_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        data = {"session_id": "s4"}
        payload = _canonical_payload_of(data)
        sealed = dict(data, seal_hash=_hmac_hex(OTHER_KEY, payload))
        assert AGPSession.verify_seal(sealed) is False


class TestFailClosedInvalidKey:
    """A configured-but-unusable key is a keyed regime with no usable key:
    everything must refuse rather than fall back to unkeyed SHA-256."""

    def test_verify_returns_false_not_exception(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
        data = {"session_id": "s5", "sealed_at": "t"}
        payload = _canonical_payload_of(data)
        # even the *correct* public sha256 must not verify here
        data["seal_hash"] = _public_sha256(payload)
        assert AGPSession.verify_seal(data) is False

    def test_verify_never_raises_on_any_input(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "!!bad!!")
        for bad in ("{not json", None, 42, [], {}, "", "x"):
            try:
                result = AGPSession.verify_seal(bad)  # type: ignore[arg-type]
            except Exception as e:  # pragma: no cover - contract violation
                raise AssertionError(f"verify_seal raised on {bad!r}: {e}")
            assert result is False

    def test_seal_raises_rather_than_writing_unkeyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
        s = _build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        assert s.seal_hash is None  # nothing forgeable was written

    def test_session_left_unsealed_after_failed_seal(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-bad-hex")
        s = _build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        assert s._sealed is False


# ── legacy unkeyed regime behavior ────────────────────────────────────────

class TestLegacyUnkeyedRegime:
    def test_seal_produces_public_sha256_when_no_key(self):
        s = _build_sealable_session()
        h = s.seal()
        assert h == _public_sha256(_canonical_payload_of(s.to_dict()))

    def test_verify_accepts_legacy_public_sha256(self):
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_verify_accepts_handmade_unkeyed_seal(self):
        data = {"session_id": "legacy", "sealed_at": "2025-01-01T00:00:00Z",
                "evidence": []}
        payload = _canonical_payload_of(data)
        data["seal_hash"] = _public_sha256(payload)
        assert AGPSession.verify_seal(data) is True

    def test_roundtrip_through_json(self):
        s = _build_sealable_session()
        s.seal()
        blob = json.dumps(s.to_dict())
        assert AGPSession.verify_seal(blob) is True

    def test_tamper_detected_even_unkeyed(self):
        s = _build_sealable_session()
        s.seal()
        d = s.to_dict()
        d["summary"]["conclusion"] = "tampered"
        assert AGPSession.verify_seal(d) is False

    def test_setting_later_key_invalidates_old_unkeyed_seal(self, monkeypatch):
        """Deliberate trade-off characterized: enabling a keyed regime does
        NOT grandfather pre-keying unkeyed seals — they stop verifying."""
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True  # still unkeyed
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        assert AGPSession.verify_seal(s.to_dict()) is False


# ── rotation ──────────────────────────────────────────────────────────────

class TestKeyRotation:
    @pytest.fixture
    def rotated_env(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", OLD_KEY)
        return monkeypatch

    def test_seal_uses_current_key_only(self, rotated_env):
        s = _build_sealable_session()
        h = s.seal()
        assert h == _hmac_hex(OTHER_KEY, _canonical_payload_of(s.to_dict()))

    def test_verify_with_current_key(self, rotated_env):
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_verify_with_old_key_seal(self, rotated_env):
        data = {"session_id": "old-sealed", "sealed_at": "2025-06-01T00:00Z"}
        payload = _canonical_payload_of(data)
        data["seal_hash"] = _hmac_hex(OLD_KEY, payload)
        assert AGPSession.verify_seal(data) is True

    def test_rotation_does_not_readmit_unkeyed(self, rotated_env):
        data = {"session_id": "forged-during-rotation"}
        payload = _canonical_payload_of(data)
        data["seal_hash"] = _public_sha256(payload)
        assert AGPSession.verify_seal(data) is False

    def test_unknown_key_rejected_during_rotation(self, rotated_env):
        data = {"session_id": "other"}
        payload = _canonical_payload_of(data)
        data["seal_hash"] = _hmac_hex(VALID_KEY, payload)
        assert AGPSession.verify_seal(data) is False

    def test_all_bad_old_keys_plus_good_current(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "xx,yy,zz")
        s = _build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True


# ── end-to-end seal/verify under each regime ──────────────────────────────

class TestEndToEndRegimes:
    def test_keyed_full_lifecycle(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _build_sealable_session()
        h = s.seal()
        assert len(h) == 64 and h != _public_sha256(
            _canonical_payload_of(s.to_dict()))
        assert AGPSession.verify_seal(s.to_dict()) is True
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_double_seal_violation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _build_sealable_session()
        s.seal()
        with pytest.raises(AGPViolation, match="already sealed"):
            s.seal()

    def test_seal_before_session_close_violation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = AGPSession("early")
        with pytest.raises(AGPViolation, match="SESSION_CLOSE"):
            s.seal()

    def test_garbage_conclusion_still_refused_under_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = _build_sealable_session()
        s.summary.conclusion = ""
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_zero_evidence_still_refused_under_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
        s = AGPSession("no-evidence")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="no-evidence", domain=Domain.GENERAL, conclusion="c",
            confidence_score=0.6, evidence_count=0, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        with pytest.raises(AGPSealRefused, match="zero evidence"):
            s.seal()


# ── tamper matrix under the keyed regime ──────────────────────────────────

KEYED = pytest.mark.usefixtures("keyed_env")


@pytest.fixture
def keyed_env(monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    return VALID_KEY


class TestKeyedTamperMatrix:
    @pytest.fixture
    def sealed_data(self, keyed_env):
        s = _build_sealable_session()
        s.seal()
        return s.to_dict()

    def _tamper(self, sealed_data, mutate):
        d = json.loads(json.dumps(sealed_data))  # deep copy
        mutate(d)
        return d

    def test_summary_conclusion(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d["summary"].__setitem__(
                             "conclusion", "rewritten history"))
        assert AGPSession.verify_seal(d) is False

    def test_evidence_content(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d["evidence"][0].__setitem__(
                             "content", "fabricated"))
        assert AGPSession.verify_seal(d) is False

    def test_evidence_confidence(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d["evidence"][0].__setitem__(
                             "confidence_score", 0.99))
        assert AGPSession.verify_seal(d) is False

    def test_extra_evidence_appended(self, sealed_data):
        d = self._tamper(sealed_data, lambda d: d["evidence"].append({}))
        assert AGPSession.verify_seal(d) is False

    def test_sealed_at(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("sealed_at",
                                                 "2099-01-01T00:00:00Z"))
        assert AGPSession.verify_seal(d) is False

    def test_filtered_count(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("filtered_evidence_count", 7))
        assert AGPSession.verify_seal(d) is False

    def test_swapped_seal_hash_from_other_session(self, sealed_data):
        other = _build_sealable_session()
        other.seal()
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash", other.seal_hash))
        assert AGPSession.verify_seal(d) is False

    def test_truncated_seal_hash(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash",
                                                 d["seal_hash"][:32]))
        assert AGPSession.verify_seal(d) is False

    def test_uppercase_seal_hash_rejected_case_sensitively(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash",
                                                 d["seal_hash"].upper()))
        assert AGPSession.verify_seal(d) is False

    def test_missing_seal_hash_field(self, sealed_data):
        d = self._tamper(sealed_data, lambda d: d.pop("seal_hash"))
        assert AGPSession.verify_seal(d) is False

    def test_null_seal_hash(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash", None))
        assert AGPSession.verify_seal(d) is False

    def test_empty_string_seal_hash(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash", ""))
        assert AGPSession.verify_seal(d) is False

    def test_non_string_seal_hash(self, sealed_data):
        d = self._tamper(sealed_data,
                         lambda d: d.__setitem__("seal_hash", 12345))
        assert AGPSession.verify_seal(d) is False


# ── production-gate pinning (source-level, read-only) ────────────────────

class TestProductionGatePins:
    """Pin the fail-closed structure itself so it cannot silently regress."""

    SOURCE_PATH = "agp/__init__.py"

    def _source(self):
        import pathlib
        return pathlib.Path(self.SOURCE_PATH).read_text(encoding="utf-8")

    def test_verify_has_no_unconditional_unkeyed_candidate(self):
        src = self._source()
        # The only sha256 fallback append must be guarded by the
        # `_seal_key_configured()` negation.
        assert "if not _seal_key_configured():" in src

    def test_seal_digest_raises_on_unusable_key(self):
        src = self._source()
        assert "raise AGPSealKeyInvalid" in src

    def test_verify_catches_keyinvalid_and_returns_false(self):
        src = self._source()
        assert "except AGPSealKeyInvalid" in src
        assert "return False" in src.split("except AGPSealKeyInvalid")[1][:400]

    def test_compare_digest_used_for_constant_time(self):
        src = self._source()
        assert "hmac.compare_digest" in src

    def test_ask_gate_module_present(self):
        import pathlib
        ask_src = pathlib.Path("tools/cli/ask.py").read_text(encoding="utf-8")
        assert "def check_seal_key" in ask_src
        assert "unkeyed" in ask_src.lower()

    def test_callisto_cli_wires_the_seal_gate(self):
        import pathlib
        callisto_src = pathlib.Path("callisto.py").read_text(encoding="utf-8")
        assert "check_seal_key" in callisto_src

    def test_live_status_list_untouched_by_this_task(self):
        """Guard rail for the task rules themselves: paper-trade statuses
        must never contain 'live'."""
        import re
        src = self._source()
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*[^)]*\)", src)
        if m:  # pragma: no branch - pin only when defined here
            assert "live" not in m.group(0).lower().replace("_live_", "")
