"""autofill #0071 — AGP keyed-seal fail-closed characterization.

Characterizes the seal-keying contract of ``agp``:

  * When CALLISTO_SEAL_KEY is configured (keyed regime), verify_seal() must
    NOT accept an unkeyed SHA-256 over the canonical payload — that digest is
    public code and would let any DB writer forge a seal.
  * A configured-but-invalid-hex key is a keyed regime with no usable key:
    both seal() (AGPSealKeyInvalid) and verify_seal() (False) fail closed.
  * With no key at all, the legacy unkeyed SHA-256 regime still works for
    backward compatibility.
  * Rotation keys (CALLISTO_SEAL_KEY_OLD) verify but never weaken the
    current-key requirement.
  * The refuse-to-seal gates (empty conclusion / zero evidence /
    filtered > kept / reviewer veto) stay intact under every key regime.

Tests-only module: no production code is modified. Every test pins the
fail-closed direction — if a gate ever weakens, these tests break loudly.
"""

import hashlib
import hmac
import json

import pytest

import agp as agp_mod
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

VALID_KEY_HEX = "ab" * 32          # 256-bit key, valid hex
OTHER_KEY_HEX = "cd" * 32          # second valid key for rotation tests
BAD_KEY_HEX = "zz-not-hex-!!"      # set but unusable


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    """Isolate each test from operator environment seal-key settings."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


def build_sealable_session(query="autofill 0071 seal characterization"):
    """Walk a session through all seven steps so seal() accepts it."""
    s = AGPSession(query)
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["example.org/0071"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="characterization evidence item",
        source_class=SourceClass.PRIMARY,
        confidence_score=0.8,
        domain=Domain.TECHNICAL,
        origin_agent="test-autofill-0071",
        source_name="fixture://0071",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=s.scope,
        domain=Domain.TECHNICAL,
        conclusion="Keyed seals never fall back to unkeyed digests.",
        confidence_score=0.75,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def unkeyed_digest(stored: dict) -> str:
    """The public, forgeable SHA-256 an attacker can compute without a key."""
    payload = agp_mod._canonical_payload(stored)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def keyed_digest(key_hex: str, stored: dict) -> str:
    payload = agp_mod._canonical_payload(stored)
    return hmac.new(
        bytes.fromhex(key_hex), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


# ── keyed regime: seal() writes HMAC, not SHA-256 ─────────────────────────

class TestKeyedRegimeSeal:
    def test_seal_under_key_is_hmac_not_sha256(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = build_sealable_session()
        h = s.seal()
        assert h == keyed_digest(VALID_KEY_HEX, s.to_dict())

    def test_seal_under_key_differs_from_public_sha256(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = build_sealable_session()
        h = s.seal()
        assert h != unkeyed_digest(s.to_dict())

    def test_sealed_session_verifies_in_keyed_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_sealed_session_verifies_via_json_roundtrip(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = build_sealable_session()
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_seal_is_deterministic_per_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        d1 = agp_mod._seal_digest(agp_mod._canonical_payload({"a": 1}))
        d2 = agp_mod._seal_digest(agp_mod._canonical_payload({"a": 1}))
        assert d1 == d2 == keyed_digest(VALID_KEY_HEX, {"a": 1})


# ── THE core pin: keyed verify_seal rejects unkeyed SHA-256 ────────────────

class TestKeyedVerifyRejectsUnkeyed:
    def _sealed_dict(self, monkeypatch):
        """Seal with NO key (legacy unkeyed), then flip to a keyed regime."""
        s = build_sealable_session()
        s.seal()
        data = s.to_dict()
        assert data["seal_hash"] == unkeyed_digest(data)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        return data

    def test_legacy_unkeyed_seal_rejected_once_key_set(self, monkeypatch):
        data = self._sealed_dict(monkeypatch)
        assert AGPSession.verify_seal(data) is False

    def test_forged_public_sha256_rejected_in_keyed_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        data = build_sealable_session().to_dict()
        data["seal_hash"] = unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is False

    def test_unkeyed_candidate_absent_even_with_rotation_keys(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", OTHER_KEY_HEX)
        data = build_sealable_session().to_dict()
        data["seal_hash"] = unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is False

    def test_wrong_key_hmac_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        data = build_sealable_session().to_dict()
        data["seal_hash"] = keyed_digest(OTHER_KEY_HEX, data)
        assert AGPSession.verify_seal(data) is False

    def test_tampered_payload_invalidates_keyed_seal(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = build_sealable_session()
        s.seal()
        data = s.to_dict()
        data["summary"]["conclusion"] = "tampered after sealing"
        assert AGPSession.verify_seal(data) is False

    def test_swapped_hash_between_sessions_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        a = build_sealable_session("query A"); a.seal()
        b = build_sealable_session("query B"); b.seal()
        da, db = a.to_dict(), b.to_dict()
        da["seal_hash"], db["seal_hash"] = db["seal_hash"], da["seal_hash"]
        assert AGPSession.verify_seal(da) is False
        assert AGPSession.verify_seal(db) is False

    def test_verify_does_not_raise_on_forge_attempt(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        data = build_sealable_session().to_dict()
        data["seal_hash"] = unkeyed_digest(data)
        try:
            ok = AGPSession.verify_seal(json.dumps(data))
        except Exception as exc:  # pragma: no cover — pins "never raises"
            pytest.fail(f"verify_seal raised {type(exc).__name__}; must return False")
        assert ok is False


# ── fail-closed: key set but invalid hex ───────────────────────────────────

class TestInvalidHexKeyFailsClosed:
    def test_seal_raises_agp_seal_key_invalid(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        s = build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()

    def test_failed_seal_leaves_session_unsealed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        s = build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        # sealed_at is stamped just before hashing, so a failed seal leaves
        # sealed_at set but seal_hash None and the session NOT sealed.
        assert s.seal_hash is None
        assert s._sealed is False

    def test_verify_rejects_anything_under_broken_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        data = {"seal_hash": unkeyed_digest({"x": 1})}
        assert AGPSession.verify_seal(data) is False

    def test_verify_rejects_valid_hmac_of_other_key_under_broken_key(self, monkeypatch):
        # Even a well-formed HMAC from some other key must not pass while the
        # configured key is unusable — the regime is broken, fail closed.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        data = {"seal_hash": keyed_digest(VALID_KEY_HEX, {"x": 1})}
        assert AGPSession.verify_seal(data) is False

    def test_whitespace_only_key_is_not_a_key_regime(self, monkeypatch):
        # "   " strips to empty → treated as unset (legacy), NOT fail-closed.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        s = build_sealable_session()
        h = s.seal()
        assert h == unkeyed_digest(s.to_dict())
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_bad_current_key_with_good_old_key_uses_rotation_key(self, monkeypatch):
        # Characterization: a bad current key + valid OLD key means seal()
        # falls through to the rotation key's HMAC — still keyed, never
        # unkeyed SHA-256.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", OTHER_KEY_HEX)
        s = build_sealable_session()
        h = s.seal()
        assert h == keyed_digest(OTHER_KEY_HEX, s.to_dict())
        assert h != unkeyed_digest(s.to_dict())
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_bad_current_key_alone_never_produces_unkeyed_seal(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        s = build_sealable_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()

    def test_keys_helper_returns_empty_for_bad_hex(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        assert agp_mod._seal_keys() == []
        assert agp_mod._seal_key_configured() is True

    def test_old_bad_hex_entries_are_skipped_silently(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"junk,{OTHER_KEY_HEX},")
        keys = agp_mod._seal_keys()
        assert bytes.fromhex(VALID_KEY_HEX) in keys
        assert bytes.fromhex(OTHER_KEY_HEX) in keys
        assert len(keys) == 2

    def test_seal_digest_raises_for_bad_hex_directly(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY_HEX)
        with pytest.raises(AGPSealKeyInvalid):
            agp_mod._seal_digest("{}")


# ── rotation: old keys verify, current key governs new seals ───────────────

class TestRotationKeys:
    def test_seal_uses_current_key_not_old(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", OTHER_KEY_HEX)
        s = build_sealable_session()
        h = s.seal()
        assert h == keyed_digest(VALID_KEY_HEX, s.to_dict())
        assert h != keyed_digest(OTHER_KEY_HEX, s.to_dict())

    def test_old_key_seal_verifies_during_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY_HEX)
        s = build_sealable_session(); s.seal(); data = s.to_dict()
        # Operator rotates to a new key; sessions sealed pre-rotation verify.
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", OTHER_KEY_HEX)
        assert AGPSession.verify_seal(data) is True

    def test_rotated_out_key_no_longer_accepted_after_expiry(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY_HEX)
        s = build_sealable_session(); s.seal(); data = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        # OLD not set anymore: expired key's seals are rejected.
        assert AGPSession.verify_seal(data) is False

    def test_multiple_old_keys_all_verify(self, monkeypatch):
        k3 = "ee" * 32
        monkeypatch.setenv("CALLISTO_SEAL_KEY", k3)
        s3 = build_sealable_session(); s3.seal(); d3 = s3.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{OTHER_KEY_HEX},{k3}")
        assert AGPSession.verify_seal(d3) is True


# ── legacy regime: no key at all still round-trips ─────────────────────────

class TestLegacyUnkeyedRegime:
    def test_seal_without_key_is_plain_sha256(self):
        s = build_sealable_session()
        h = s.seal()
        assert h == unkeyed_digest(s.to_dict())
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_verify_without_key_accepts_json_string(self):
        s = build_sealable_session(); s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_verify_without_key_rejects_random_hmac(self):
        data = build_sealable_session().to_dict()
        data["seal_hash"] = keyed_digest(VALID_KEY_HEX, data)
        assert AGPSession.verify_seal(data) is False

    def test_missing_seal_hash_rejected(self):
        data = build_sealable_session().to_dict()
        del data["seal_hash"]
        assert AGPSession.verify_seal(data) is False

    def test_none_seal_hash_rejected(self):
        data = build_sealable_session().to_dict()
        data["seal_hash"] = None
        assert AGPSession.verify_seal(data) is False

    def test_non_string_seal_hash_rejected(self):
        data = build_sealable_session().to_dict()
        data["seal_hash"] = 12345
        assert AGPSession.verify_seal(data) is False

    def test_malformed_json_rejected_not_raised(self):
        assert AGPSession.verify_seal("{not json") is False

    def test_empty_seal_hash_string_rejected(self):
        data = build_sealable_session().to_dict()
        data["seal_hash"] = ""
        assert AGPSession.verify_seal(data) is False


# ── refuse-to-seal gates hold in the keyed regime too ──────────────────────

class TestRefuseGatesHoldUnderKeyedRegime:
    def make_closed(self):
        s = build_sealable_session()
        return s

    def test_empty_conclusion_refused_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.summary.conclusion = ""
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_marker_conclusion_refused_with_key(self, monkeypatch):
        from agp import EMPTY_SYNTHESIS_MARKER
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.summary.conclusion = EMPTY_SYNTHESIS_MARKER
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_zero_evidence_refused_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.evidence.clear()
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_filtered_gt_kept_refused_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.filtered_evidence_count = 2
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_veto_refused_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.seal_veto = lambda session, summary: "vetoed by characterization"
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_crashing_veto_reviewer_fails_closed_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        def boom(session, summary):
            raise RuntimeError("reviewer down")
        s.seal_veto = boom
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_gates_hold_without_key_too(self):
        s = self.make_closed()
        s.filtered_evidence_count = 5
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_double_seal_raises_violation_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = self.make_closed()
        s.seal()
        with pytest.raises(AGPViolation):
            s.seal()

    def test_seal_before_session_close_raises_with_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        s = AGPSession("early")
        s.domain = Domain.SIGNAL
        with pytest.raises(AGPViolation):
            s.seal()


# ── module-level invariants (pins against accidental weakening) ────────────

class TestModuleInvariants:
    def test_agp_seal_key_invalid_exists_and_raises(self):
        assert issubclass(AGPSealKeyInvalid, Exception)
        with pytest.raises(AGPSealKeyInvalid):
            raise AGPSealKeyInvalid("pin")

    def test_paper_trade_statuses_never_contain_live(self):
        statuses = getattr(agp_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in {str(x).lower() for x in statuses}

    def test_canonical_payload_never_hashes_seal_hash_itself(self):
        data = {"a": 1, "seal_hash": "deadbeef"}
        payload = agp_mod._canonical_payload(data)
        parsed = json.loads(payload)
        assert parsed["seal_hash"] is None
        assert "deadbeef" not in payload

    def test_verify_signature_is_staticmethod(self):
        assert callable(AGPSession.verify_seal)
        assert AGPSession.__dict__["verify_seal"].__func__ is AGPSession.verify_seal

    def test_digest_length_is_sha256_width_in_both_regimes(self, monkeypatch):
        p = agp_mod._canonical_payload({"k": "v"})
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert len(agp_mod._seal_digest(p)) == 64
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY_HEX)
        assert len(agp_mod._seal_digest(p)) == 64
