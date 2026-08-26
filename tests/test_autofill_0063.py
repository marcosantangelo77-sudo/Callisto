"""OX autofill #0063 — AGP seal fail-closed characterization (LONG).

A large, tests-only characterization sweep over the keyed-seal boundary of
``agp.AGPSession`` and the ``callisto ask`` CLI seal gate.

Central invariant under characterization:

    When CALLISTO_SEAL_KEY is set (keyed regime), ``verify_seal`` must NEVER
    accept the raw, public SHA-256 of the canonical payload. The public hash
    is forgeable by anyone who can write to storage; accepting it in a keyed
    regime would silently downgrade HMAC-SHA256 back to an unauthenticated
    checksum. Every path that could re-admit it must fail CLOSED instead:
    missing key ⇒ refuse, malformed key ⇒ refuse (never fall back), wrong
    key ⇒ mismatch.

Companion gates pinned here (never weakened):
  - seal() refuses empty/default conclusions and zero evidence
  - seal_veto reviewer crashes fail closed (AGPSealRefused)
  - verify_seal never raises; every failure mode returns False
  - rotation keys extend acceptance without re-admitting the public hash

No production code is modified by this module. Live betting is never armed;
this module touches only seal primitives and the ask gate.
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import os

import pytest

import agp
import callisto  # noqa: F401  (ask-gate pins below)
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

# A pool of valid 32-byte hex keys used throughout.
K_A = "aa" * 32
K_B = "bb" * 32
K_C = "cc" * 32
K_D = "dd" * 32
VALID_KEYS = [K_A, K_B, K_C, K_D]


# ──────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────

def make_session(query="autofill 0063 characterization query") -> AGPSession:
    """Walk a session through the full lifecycle to SESSION_CLOSE."""
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["characterization-0063"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="an observed fact for the 0063 seal",
        source_class=SourceClass.SECONDARY,
        confidence_score=0.72,
        domain=Domain.GENERAL,
        origin_agent="test-0063",
    ))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope=query, domain=Domain.GENERAL,
        conclusion="a real 0063 conclusion",
        confidence_score=0.72, evidence_count=1, contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def public_sha256(data: dict) -> str:
    """The forgeable public hash: sha256 over the canonical payload."""
    payload = agp._canonical_payload(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hmac_with_key(hexkey: str, data) -> str:
    """HMAC-SHA256 of the canonical payload with a hex key."""
    if isinstance(data, str):
        data = json.loads(data)
    payload = agp._canonical_payload(data)
    return hmac.new(
        bytes.fromhex(hexkey), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def forged_row(data: dict, digest: str) -> dict:
    d = dict(data)
    d["seal_hash"] = digest
    return d


@pytest.fixture(autouse=True)
def clean_seal_env(monkeypatch):
    """Every test starts from an unkeyed environment."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ──────────────────────────────────────────────────────────────────────
# 1. Keyed verify_seal must not accept unkeyed SHA-256
# ──────────────────────────────────────────────────────────────────────

class TestKeyedRejectsUnkeyedHash:
    @pytest.mark.parametrize("key", VALID_KEYS)
    def test_public_hash_forgery_rejected(self, key, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        s = make_session()
        row = forged_row(s.to_dict(), public_sha256(s.to_dict()))
        assert AGPSession.verify_seal(row) is False

    @pytest.mark.parametrize("key", VALID_KEYS)
    def test_public_hash_of_unsealed_dict_rejected(self, key, monkeypatch):
        """Even hashing a never-sealed dict's payload is not accepted."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        s = make_session()
        s._sealed = True
        d = s.to_dict()
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False

    def test_public_hash_over_mutated_payload_rejected(self, monkeypatch):
        """Recomputing the public hash over TAMPERED content doesn't help."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] = "FORGED CONCLUSION"
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False

    def test_unkeyed_legacy_row_rejected_once_keyed(self, monkeypatch):
        """Seal unkeyed, then introduce a key: legacy rows stop verifying."""
        s = make_session()
        s.seal()
        stored = json.dumps(s.to_dict())
        assert AGPSession.verify_seal(stored) is True   # still unkeyed
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_B)
        assert AGPSession.verify_seal(stored) is False  # now keyed

    def test_hmac_with_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        assert AGPSession.verify_seal(
            forged_row(s.to_dict(), hmac_with_key(K_B, s.to_dict()))
        ) is False

    def test_correct_key_verifies_and_differs_from_public(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_C)
        s = make_session()
        h = s.seal()
        assert h != public_sha256(s.to_dict())
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_json_string_form_verifies_under_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_C)
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_blank_env_is_unconfigured_not_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", " \t ")
        assert agp._seal_key_configured() is False
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True  # legacy regime

    def test_nonhex_key_never_falls_back_to_public_hash(self, monkeypatch):
        """The critical fail-closed case: key SET but malformed."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "definitely-not-hex-zz")
        s = make_session()
        d = dict(s.to_dict())
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False

    def test_uppercase_hex_key_accepted(self, monkeypatch):
        """bytes.fromhex tolerates uppercase; regime behaves identically."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A.upper())
        s = make_session()
        s.seal()
        # Sealing with uppercase key produces the lowercase-equivalent HMAC.
        stored = s.to_dict()
        assert stored["seal_hash"] == hmac_with_key(K_A, stored)
        assert AGPSession.verify_seal(stored) is True

    def test_short_hex_key_still_keys_regime(self, monkeypatch):
        """Any parseable hex counts as a key — even 1 byte. Public hash out."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "ff")
        s = make_session()
        d = dict(s.to_dict())
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False


# ──────────────────────────────────────────────────────────────────────
# 2. Fail-closed on invalid keys
# ──────────────────────────────────────────────────────────────────────

BAD_KEYS = [
    "not-hex!",
    "zz" * 32,
    "abc1234",              # odd length → fromhex fails
    "0x" + "ab" * 16,       # '0x' prefix not parseable by fromhex
]

# NOTE: 'ab cd 99' is NOT in BAD_KEYS — bytes.fromhex() skips ASCII
# whitespace between pairs, so it parses as b'\xab\xcd\x99'. Characterized
# separately below.


class TestFailClosedInvalidKeys:
    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_seal_refuses_bad_key(self, bad, monkeypatch):
        """seal() raises AGPSealKeyInvalid; no unkeyed digest is produced."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        with pytest.raises(AGPSealKeyInvalid):
            make_session().seal()

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_digest_helper_refuses_bad_key(self, bad, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        payload = json.dumps({"k": "v"}, sort_keys=True)
        with pytest.raises(AGPSealKeyInvalid):
            agp._seal_digest(payload)

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_verify_returns_false_on_bad_key(self, bad, monkeypatch):
        """verify_seal never raises: bad current key ⇒ False, always."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        s = make_session()
        d = dict(s.to_dict())
        # Try both a plausible HMAC (wrong key bytes unknowable) and public.
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False
        assert AGPSession.verify_seal(forged_row(d, hmac_with_key(K_A, d))
                                      ) is False

    def test_bad_current_key_valid_old_key_keeps_public_out(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", K_A)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "bad-key!!")
        s = make_session()
        d = dict(s.to_dict())
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False

    def test_empty_string_key_is_unconfigured(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        assert agp._seal_key_configured() is False
        assert agp._seal_keys() == []

    def test_whitespace_old_key_ignored(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "   ")
        assert agp._seal_keys() == [bytes.fromhex(K_A)]

    def test_invalid_old_key_does_not_poison_current(self, monkeypatch):
        """A bad OLD entry must not break verification under the CURRENT key
        (rotation list skips unparsable entries)."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_B)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "zz-bad," + K_A)
        s = make_session()
        s.seal()  # sealed under K_B (current)
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_sealed_flag_never_set_when_key_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "nothex!!")
        s = make_session()
        with pytest.raises(AGPSealKeyInvalid):
            s.seal()
        assert s._sealed is False
        assert s.seal_hash is None


# ──────────────────────────────────────────────────────────────────────
# 3. Legacy unkeyed regime stays byte-compatible
# ──────────────────────────────────────────────────────────────────────

class TestUnkeyedLegacyRegime:
    def test_no_key_digest_is_plain_sha256(self):
        s = make_session()
        h = s.seal()
        assert h == public_sha256(s.to_dict())

    def test_no_key_verify_passes_on_dict_and_json(self):
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_missing_seal_hash_rejected_even_unkeyed(self):
        s = make_session()
        s.seal()
        d = s.to_dict()
        del d["seal_hash"]
        assert AGPSession.verify_seal(d) is False

    @pytest.mark.parametrize("bad", [None, "", 12345, ["x"], {"a": 1}])
    def test_weird_seal_hash_values_rejected(self, bad):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["seal_hash"] = bad
        assert AGPSession.verify_seal(d) is False

    @pytest.mark.parametrize("junk", ["{not json", ""])
    def test_malformed_json_rejected(self, junk):
        assert AGPSession.verify_seal(junk) is False

    @pytest.mark.parametrize("junk", ["[1,2,3]", "null"])
    def test_non_object_json_currently_raises_attr_error(self, junk):
        """CHARACTERIZED GAP (not weakened here): verify_seal's docstring
        says 'never raises', but a JSON string that parses to a non-dict
        (list / null) escapes with AttributeError at data.get(). This test
        pins the CURRENT behavior so any future fail-closed hardening flips
        it deliberately. The strict fail-closed contract is marked xfail."""
        with pytest.raises(AttributeError):
            AGPSession.verify_seal(junk)

    @pytest.mark.parametrize("junk", ["[1,2,3]", "null"])
    @pytest.mark.xfail(reason="verify_seal raises on non-dict JSON; "
                              "strict 'never raises' contract not yet met",
                       strict=True)
    def test_non_object_json_should_return_false(self, junk):
        assert AGPSession.verify_seal(junk) is False

    @pytest.mark.parametrize("obj", [None, 12345, b"bytes", [1], object()])
    def test_non_dict_objects_rejected(self, obj):
        assert AGPSession.verify_seal(obj) is False

    def test_tampered_evidence_detected_unkeyed(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["evidence"][0]["confidence_score"] = 0.99
        assert AGPSession.verify_seal(d) is False

    def test_extra_field_breaks_payload(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["attacker_note"] = "injected"
        assert AGPSession.verify_seal(d) is False

    def test_deleted_field_breaks_payload(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        del d["query"]
        assert AGPSession.verify_seal(d) is False

    def test_sealed_at_tamper_detected(self):
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["sealed_at"] = "1999-01-01T00:00:00+00:00"
        assert AGPSession.verify_seal(d) is False


# ──────────────────────────────────────────────────────────────────────
# 4. Rotation keeps old HMACs alive; public hash stays out
# ──────────────────────────────────────────────────────────────────────

class TestRotation:
    def test_rotation_old_key_verifies_and_public_hash_rejected(
        self, monkeypatch
    ):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", K_A)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_B)
        assert AGPSession.verify_seal(stored) is True
        d = dict(stored)
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False

    def test_foreign_key_rejected_after_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", K_A)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_B)
        assert AGPSession.verify_seal(
            forged_row(dict(stored), hmac_with_key(K_C, stored))) is False

    def test_comma_separated_multi_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_C)
        monkeypatch.setenv(
            "CALLISTO_SEAL_KEY_OLD", f"{K_B},{K_A}")
        assert AGPSession.verify_seal(stored) is True

    def test_missing_from_rotation_list_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_C)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", K_B)  # lacks K_A
        assert AGPSession.verify_seal(stored) is False

    def test_rotation_with_invalid_current_fails_closed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        stored = s.to_dict()
        # Rotate INTO an invalid key: even the true old HMAC must not pass?
        # No — old keys remain listed, so the genuine old seal verifies;
        # but a PUBLIC hash must never sneak in either way.
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", K_A)
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "invalid-hex!")
        assert AGPSession.verify_seal(stored) is True  # via OLD list
        d = dict(stored)
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False


# ──────────────────────────────────────────────────────────────────────
# 5. Tamper detection across regimes
# ──────────────────────────────────────────────────────────────────────

def _tamper_conclusion(d): d["summary"]["conclusion"] = "HACKED"


def _tamper_confidence(d): d["summary"]["confidence_score"] = 0.99


def _tamper_evidence(d):
    d["evidence"] = [dict(d["evidence"][0], content="fabricated")]


def _tamper_session_id(d): d["session_id"] = "9999999999999999"


def _tamper_tier(d): d["summary"]["confidence_tier"] = "VERIFIED"


TAMPER_CASES = {
    "conclusion": _tamper_conclusion,
    "confidence": _tamper_confidence,
    "evidence_swap": _tamper_evidence,
    "session_id": _tamper_session_id,
    "tier_bump": _tamper_tier,
}


class TestTamperDetection:
    @pytest.mark.parametrize("name", sorted(TAMPER_CASES))
    def test_tamper_keyed(self, name, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_D)
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

    def test_reforged_digest_after_tamper_keyed_rejected(self, monkeypatch):
        """Rehashing after tampering cannot pass unless the attacker has the
        real key — neither public SHA-256 nor a foreign HMAC works."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] = "REWRITTEN"
        assert AGPSession.verify_seal(forged_row(d, public_sha256(d))) is False
        assert AGPSession.verify_seal(forged_row(d, hmac_with_key(K_B, d))
                                      ) is False

    def test_unicode_round_trip_preserves_verification(self, monkeypatch):
        """Non-ASCII content survives json round trip under ensure_ascii=False
        canonicalization."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_B)
        s = AGPSession("unicode query ✓ é 中文")
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.sources = ["u"]
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(Evidence(
            content="donnée accentuée ✓",
            source_class=SourceClass.SECONDARY,
            confidence_score=0.7,
            domain=Domain.GENERAL,
            origin_agent="test",
        ))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="unicode ✓", domain=Domain.GENERAL,
            conclusion="conclusión final ✓",
            confidence_score=0.70, evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict(),
                                                 ensure_ascii=False)) is True


# ──────────────────────────────────────────────────────────────────────
# 6. Seal-refusal gates remain intact (never weakened)
# ──────────────────────────────────────────────────────────────────────

class TestSealRefusalGatesIntact:
    def test_empty_conclusion_refused_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.summary.conclusion = ""
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_zero_evidence_refused_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.evidence.clear()
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_double_seal_raises_violation(self, monkeypatch):
        from agp import AGPViolation
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal()
        with pytest.raises(AGPViolation):
            s.seal()

    def test_veto_crash_fails_closed_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()

        def boom(sess, summ):
            raise RuntimeError("reviewer exploded")

        s.seal_veto = boom
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_veto_truthy_reason_fails_closed_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal_veto = lambda sess, summ: "suspicious conclusion"
        with pytest.raises(AGPSealRefused):
            s.seal()

    def test_veto_none_passes_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.seal_veto = lambda sess, summ: None
        assert s.seal() == s.to_dict()["seal_hash"]

    def test_filtered_gt_kept_refused_keyed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        s = make_session()
        s.filtered_evidence_count = len(s.evidence) + 1
        with pytest.raises(AGPSealRefused):
            s.seal()


# ──────────────────────────────────────────────────────────────────────
# 7. Structural pins on production source (gates exist, unweakened)
# ──────────────────────────────────────────────────────────────────────

class TestSourcePins:
    def test_verify_gates_public_hash_behind_configured_check(self):
        src = inspect.getsource(AGPSession.verify_seal)
        assert "_seal_key_configured()" in src
        cfg_pos = src.index("_seal_key_configured()")
        sha_pos = src.index("hashlib.sha256(payload.encode")
        assert cfg_pos < sha_pos, (
            "public SHA-256 candidate must sit behind the configured check")

    def test_verify_fails_closed_on_invalid_key_exception(self):
        src = inspect.getsource(AGPSession.verify_seal)
        assert "AGPSealKeyInvalid" in src
        assert "return False" in src

    def test_seal_digest_raises_rather_than_fallback(self):
        src = inspect.getsource(agp._seal_digest)
        assert "AGPSealKeyInvalid" in src
        assert "refus" in src.lower()

    def test_verify_uses_constant_time_compare(self):
        src = inspect.getsource(AGPSession.verify_seal)
        assert "compare_digest" in src

    def test_no_live_status_widening_in_agp(self):
        """Guard against accidental live-betting arming via this module's
        imports: any paper-trade status collection present must exclude
        'live'."""
        statuses = getattr(agp, "_PAPER_TRADE_SIGNAL_STATUSES", ())
        assert "live" not in statuses

    def test_generate_paper_trade_signal_not_widened_to_live(self):
        import importlib
        pt = None
        for name in ("paper_trade", "papertrade"):
            try:
                pt = importlib.import_module(name)
                break
            except ImportError:
                continue
        if pt is None:
            pytest.skip("no paper_trade module in tree")
        fn = getattr(pt, "generate_paper_trade_signal", None)
        if fn is None:
            pytest.skip("no generate_paper_trade_signal symbol")
        src = inspect.getsource(fn)
        # The function body must not treat 'live' as an accepted status.
        assert "status == 'live'" not in src
        assert 'status == "live"' not in src


# ──────────────────────────────────────────────────────────────────────
# 8. Ask CLI seal gate: unkeyed / invalid keys fail closed
# ──────────────────────────────────────────────────────────────────────

class TestAskGatePins:
    """The `ask` command refuses to run research when the seal regime is
    unkeyed or misconfigured — pin its source-level decision points."""

    def _gate_src(self) -> str:
        from tools.cli.ask import check_seal_key
        return inspect.getsource(check_seal_key)

    def test_ask_gate_checks_seal_key_env(self):
        src = self._gate_src()
        assert "CALLISTO_SEAL_KEY" in src

    def test_ask_gate_mentions_fail_closed_language(self):
        src = self._gate_src().lower()
        assert "unkeyed" in src or ("fail" in src and "closed" in src)

    def test_ask_gate_refuses_nonhex(self):
        src = self._gate_src()
        assert "fromhex" in src
        assert "not valid hex" in src

    def test_ask_cmd_wires_gate_before_research(self):
        """The command body must call check_seal_key() before anything else
        meaningful and return non-zero when it fails."""
        src = inspect.getsource(callisto._cmd_ask)
        gate_pos = src.index("check_seal_key()")
        router_pos = src.index("_load_router")
        assert gate_pos < router_pos
        ret_pos = src.index("return 2")
        assert gate_pos < ret_pos

    def test_agp_helpers_exist_and_are_consistent(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
        assert agp._seal_key_configured() is False
        monkeypatch.setenv("CALLISTO_SEAL_KEY", K_A)
        assert agp._seal_key_configured() is True
        assert agp._seal_keys() == [bytes.fromhex(K_A)]
