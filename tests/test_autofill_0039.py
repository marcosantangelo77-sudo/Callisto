"""OX autofill #0039 — AGP keyed-seal fail-closed characterization (LONG).

This module is a large, tests-only characterization of the AGP seal
boundary with one central invariant:

    When a key regime is active (CALLISTO_SEAL_KEY set), ``verify_seal``
    must NEVER accept an unkeyed SHA-256 digest of the payload.

An unkeyed SHA-256 "seal" is not a seal — anyone with DB write access can
recompute it over tampered bytes because the verification code is public.
The whole point of keying is that forging requires the secret key. So:

1. Keyed regime + public-hash forgery  -> rejected.
2. Keyed regime + legacy unkeyed row   -> rejected (keying changes regime).
3. Set-but-invalid (non-hex) key       -> FAIL CLOSED: seal() raises
   AGPSealKeyInvalid; verify_seal returns False and never falls back to
   the public digest or raises.
4. Unkeyed legacy regime (no env at all) keeps verifying SHA-256 rows so
   old data still reads back — but only when NO key was intended.
5. Rotation via CALLISTO_SEAL_KEY_OLD admits old-key HMACs without ever
   re-admitting the public hash.
6. Tamper detection fires in every regime.
7. The live-betting hard gate is untouched: this module never widens
   _PAPER_TRADE_SIGNAL_STATUSES and never arms 'live'.

No production file is modified by these pins. Where a pin could tempt a
weakening, we assert the gate holds instead (fail closed).
"""
from __future__ import annotations

import hashlib
import hmac
import inspect
import json

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
OTHER_KEY = "9e" * 32


# ──────────────────────────────────────────────────────────────────────
# helpers / fixtures
# ──────────────────────────────────────────────────────────────────────

def make_session(query="autofill 0039 characterization query") -> AGPSession:
    """Walk a session through the full lifecycle to SESSION_CLOSE."""
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["characterization"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="an observed fact for the 0039 seal",
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


def public_sha256(data) -> str:
    """The forgeable public hash any DB-writer can compute from a row."""
    if isinstance(data, str):
        data = json.loads(data)
    payload = agp._canonical_payload(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hmac_with_key(hexkey: str, data) -> str:
    """HMAC-SHA256 of the canonical payload under an explicit hex key."""
    if isinstance(data, str):
        data = json.loads(data)
    payload = agp._canonical_payload(data)
    return hmac.new(
        bytes.fromhex(hexkey), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()


@pytest.fixture(autouse=True)
def clean_seal_env(monkeypatch):
    """Every test starts in the pristine unkeyed legacy regime."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ══════════════════════════════════════════════════════════════════════
# 1. THE CORE PIN: keyed verify_seal must not accept unkeyed SHA-256
# ══════════════════════════════════════════════════════════════════════

class TestKeyedVerifyRejectsUnkeyed:
    @pytest.mark.parametrize("key", VALID_KEYS)
    def test_public_hash_forgery_rejected(self, key, monkeypatch):
        """Attacker computes sha256(canonical payload); keyed verify says no."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", key)
        d = dict(make_session().to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_public_hash_over_mutated_payload_rejected(self, monkeypatch):
        """Even hashing the TAMPERED content buys nothing under a key."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["conclusion"] = "FORGED CONCLUSION"
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_public_hash_json_string_form_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[1])
        d = dict(make_session().to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(json.dumps(d)) is False

    def test_unkeyed_legacy_row_rejected_under_key(self, monkeypatch):
        """Seal unkeyed first; setting a key afterwards must invalidate it."""
        s = make_session()
        s.seal()
        stored = json.dumps(s.to_dict())
        assert AGPSession.verify_seal(stored) is True   # still unkeyed
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        assert AGPSession.verify_seal(stored) is False  # now keyed regime

    def test_hmac_of_wrong_key_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        d = dict(make_session().to_dict())
        d["seal_hash"] = hmac_with_key(OTHER_KEY, d)
        assert AGPSession.verify_seal(d) is False

    def test_public_hash_differs_from_keyed_seal(self, monkeypatch):
        """Sanity: keying actually changes the digest."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        h = s.seal()
        assert h != public_sha256(s.to_dict())
        assert h == hmac_with_key(VALID_KEYS[0], s.to_dict())

    def test_correct_key_accepted(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_rotation_old_key_does_not_readmit_public_hash(self, monkeypatch):
        """Rotation keys widen acceptance to old HMACs — never to sha256."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ",".join(VALID_KEYS[1:]))
        d = dict(make_session().to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False


class TestKeyedRegimeDetection:
    def test_configured_true_when_key_set(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        assert agp._seal_key_configured() is True

    def test_not_configured_when_unset(self):
        assert agp._seal_key_configured() is False

    def test_whitespace_only_counts_as_unconfigured(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   \t ")
        assert agp._seal_key_configured() is False

    def test_keys_list_parses_current_and_old(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD",
                           f" {VALID_KEYS[1]} , {VALID_KEYS[2]} ,junk")
        keys = agp._seal_keys()
        assert keys == [bytes.fromhex(VALID_KEYS[0]),
                        bytes.fromhex(VALID_KEYS[1]),
                        bytes.fromhex(VALID_KEYS[2])]

    def test_invalid_current_yields_empty_keys_but_configured(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-not-hex")
        assert agp._seal_key_configured() is True
        assert agp._seal_keys() == []


# ══════════════════════════════════════════════════════════════════════
# 2. Fail-closed on invalid keys
# ══════════════════════════════════════════════════════════════════════

BAD_KEYS = [
    "not-hex!",
    "zz" * 32,           # right length, wrong alphabet
    "abc1234",           # odd length
    "0x" + "ab" * 16,    # fromhex does not accept the 0x prefix
]


class TestFailClosedInvalidKeys:
    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_seal_refuses_on_bad_key(self, bad, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        with pytest.raises(AGPSealKeyInvalid):
            make_session().seal()

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_digest_helper_refuses_on_bad_key(self, bad, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        with pytest.raises(AGPSealKeyInvalid):
            agp._seal_digest('{"k": "v"}')

    @pytest.mark.parametrize("bad", BAD_KEYS)
    def test_verify_false_not_exception_on_bad_key(self, bad, monkeypatch):
        """verify_seal never raises — bad key ⇒ False, fail closed."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", bad)
        d = dict(make_session().to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_bad_key_plus_valid_rotation_still_forgery_free(self, monkeypatch):
        """Bad current key + valid OLD keys: public hash still rejected."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "zz-not-hex")
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[0])
        d = dict(make_session().to_dict())
        d["seal_hash"] = public_sha256(d)
        assert AGPSession.verify_seal(d) is False

    def test_verify_of_hmac_row_under_bad_key_is_false(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        stored = json.dumps(s.to_dict())
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "broken-hex!!")
        # The stored HMAC was made under a DIFFERENT key than any usable one;
        # with the current key unusable there is no fallback path at all.
        assert AGPSession.verify_seal(stored) is False


# ══════════════════════════════════════════════════════════════════════
# 3. Legacy unkeyed regime preserved exactly when no key intended
# ══════════════════════════════════════════════════════════════════════

class TestLegacyUnkeyedRegime:
    def test_seal_equals_public_sha256_when_no_key(self):
        s = make_session()
        h = s.seal()
        assert h == public_sha256(s.to_dict())

    def test_verify_accepts_legacy_row_when_no_key(self):
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(json.dumps(s.to_dict())) is True

    def test_digest_helper_unkeyed_matches_sha256(self):
        payload = json.dumps({"b": 2, "a": 1}, sort_keys=True)
        assert agp._seal_digest(payload) == \
            hashlib.sha256(payload.encode()).hexdigest()

    def test_whitespace_env_is_still_legacy_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "  ")
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True


# ══════════════════════════════════════════════════════════════════════
# 4. Rotation semantics
# ══════════════════════════════════════════════════════════════════════

class TestRotationKeys:
    def test_old_key_hmac_accepted_after_rotation(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[1])
        d = dict(make_session().to_dict())
        d["seal_hash"] = hmac_with_key(VALID_KEYS[1], d)
        assert AGPSession.verify_seal(d) is True

    def test_multiple_old_keys_all_tried(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD",
                           f"{VALID_KEYS[1]},{VALID_KEYS[2]}")
        for k in VALID_KEYS[1:3]:
            d = dict(make_session().to_dict())
            d["seal_hash"] = hmac_with_key(k, d)
            assert AGPSession.verify_seal(d) is True, k

    def test_rotation_without_current_key_still_verifies_old(self, monkeypatch):
        """Old rows keep working during a key hand-off window."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[1])
        d = dict(make_session().to_dict())
        d["seal_hash"] = hmac_with_key(VALID_KEYS[1], d)
        assert AGPSession.verify_seal(d) is True

    def test_rotation_does_not_break_current_key_seals(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEYS[1])
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True

    def test_garbage_old_keys_ignored(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", ", ,, junk,,")
        s = make_session()
        s.seal()
        assert AGPSession.verify_seal(s.to_dict()) is True


# ══════════════════════════════════════════════════════════════════════
# 5. Tamper detection across regimes
# ══════════════════════════════════════════════════════════════════════

class TestTamperDetection:
    @pytest.mark.parametrize("regime", ["none", "keyed"])
    def test_field_tamper_detected(self, regime, monkeypatch):
        if regime == "keyed":
            monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["query"] = d["query"].replace("0039", "TAMPERED")
        assert AGPSession.verify_seal(d) is False

    @pytest.mark.parametrize("regime", ["none", "keyed"])
    def test_summary_tamper_detected(self, regime, monkeypatch):
        if regime == "keyed":
            monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["summary"]["confidence_score"] = 0.99
        assert AGPSession.verify_seal(d) is False

    def test_missing_hash_rejected(self):
        d = dict(make_session().to_dict())
        d.pop("seal_hash", None)
        assert AGPSession.verify_seal(d) is False

    def test_empty_hash_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        d = dict(make_session().to_dict())
        d["seal_hash"] = ""
        assert AGPSession.verify_seal(d) is False

    def test_non_string_hash_rejected(self):
        d = dict(make_session().to_dict())
        d["seal_hash"] = 12345
        assert AGPSession.verify_seal(d) is False

    def test_malformed_json_string_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        assert AGPSession.verify_seal("{not json") is False

    def test_non_dict_input_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        assert AGPSession.verify_seal(42) is False          # type: ignore[arg-type]
        assert AGPSession.verify_seal(None) is False        # type: ignore[arg-type]

    def test_swapped_hashes_between_sessions_rejected(self, monkeypatch):
        """Cross-session hash swap can never pass."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s1, s2 = make_session("q one"), make_session("q two")
        s1.seal()
        s2.seal()
        d2 = dict(s2.to_dict())
        d2["seal_hash"] = s1.seal_hash
        assert AGPSession.verify_seal(d2) is False


# ══════════════════════════════════════════════════════════════════════
# 6. Preregistration seal shares the same fail-closed boundary
# ══════════════════════════════════════════════════════════════════════

class TestPreregistrationSealBoundary:
    def _make_prereg(self):
        from agp.preregistration import Criteria, Preregistration
        crit = Criteria(
            confirm_markers=["confirmed"],
            refute_markers=["refuted"],
            threshold=0.6,
            direction="gte",
        )
        return Preregistration(query="0039 prereg", criteria=crit)

    def test_seal_under_key_is_hmac_not_public(self, monkeypatch):
        from agp.preregistration import _canonical
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        p = self._make_prereg()
        p.seal()
        expected_pub = hashlib.sha256(_canonical({
            **p._payload(), "sealed_at": p.sealed_at}).encode()).hexdigest()
        assert p.seal_hash != expected_pub
        assert p.verify_seal() is True

    def test_public_hash_planted_in_prereg_rejected(self, monkeypatch):
        from agp.preregistration import _canonical
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        p = self._make_prereg()
        p.sealed_at = "2026-08-26T00:00:00+00:00"
        p.seal_hash = hashlib.sha256(_canonical({
            **p._payload(), "sealed_at": p.sealed_at}).encode()).hexdigest()
        assert p.verify_seal() is False

    def test_bad_key_prereg_verify_false_never_raises(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "not-hex-zz")
        p = self._make_prereg()
        with pytest.raises(AGPSealKeyInvalid):
            p.seal()   # fail closed: no forgeable unkeyed digest produced

    def test_prereg_tamper_detected_under_key(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        p = self._make_prereg()
        p.seal()
        from agp.preregistration import PreregistrationSealed
        try:
            p.query = "tampered"   # sealed-immutable field
            assert p.verify_seal() is False  # mutated without guard
        except PreregistrationSealed:
            assert p.verify_seal() is True   # immutability held


# ══════════════════════════════════════════════════════════════════════
# 7. Source-level pins: gates must not be weakened by future edits
# ══════════════════════════════════════════════════════════════════════

class TestSourceLevelGatesHold:
    def _src(self, relpath):
        import pathlib
        return pathlib.Path(agp.__file__).parent.joinpath(relpath).read_text()

    def test_verify_source_keeps_regime_guard(self):
        src = self._src("__init__.py")
        assert "_seal_key_configured()" in src
        assert "hashlib.sha256(payload.encode" in src

    def test_verify_source_wraps_digest_in_fail_closed_try(self):
        src = self._src("__init__.py")
        assert "AGPSealKeyInvalid" in src
        assert "return False" in src.split("def verify_seal")[1]

    def test_digest_helper_refuses_to_produce_unkeyed_under_key(self):
        src = self._src("__init__.py")
        tail = src.split("def _seal_digest")[1]
        assert "AGPSealKeyInvalid" in tail, (
            "_seal_digest must keep its refuse-to-forge branch")

    def test_no_live_in_paper_signal_statuses(self):
        import tools.signals.paper as paper
        src = inspect.getsource(paper)
        statuses = paper._PAPER_TRADE_SIGNAL_STATUSES
        assert isinstance(statuses, frozenset)
        assert statuses <= {"paper_trading"}
        assert "live" not in {s.lower() for s in statuses}
        assert '"live"' not in src.split("_PAPER_TRADE_SIGNAL_STATUSES = ")[1]

    def test_generate_paper_trade_gate_present(self):
        """The method must route through the paper-only hard gate, either by
        referencing _PAPER_TRADE_SIGNAL_STATUSES directly or via
        reject_non_paper() (which reads it)."""
        from tools.backtest import BacktestEngine
        fn_src = inspect.getsource(
            BacktestEngine.generate_paper_trade_signal)
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in fn_src \
            or "reject_non_paper(" in fn_src

    def test_exception_types_exist_for_fail_closed_paths(self):
        assert issubclass(AGPSealKeyInvalid, Exception)
        assert issubclass(AGPSealRefused, Exception)


# ══════════════════════════════════════════════════════════════════════
# 8. Canonical payload stability (verify depends on exact reproduction)
# ══════════════════════════════════════════════════════════════════════

class TestCanonicalPayload:
    def test_seal_hash_normalized_out(self):
        d = {"a": 1, "seal_hash": "whatever"}
        assert json.loads(agp._canonical_payload(d))["seal_hash"] is None

    def test_deterministic_across_calls(self):
        d = {"z": 1, "a": [3, 2, 1], "m": {"k": "v"}}
        assert agp._canonical_payload(d) == agp._canonical_payload(dict(d))

    def test_key_order_irrelevant(self):
        assert agp._canonical_payload({"a": 1, "b": 2}) == \
            agp._canonical_payload({"b": 2, "a": 1})

    def test_roundtrip_through_verify(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        once = dict(s.to_dict())
        twice = json.loads(json.dumps(once))
        assert agp._canonical_payload(once) == agp._canonical_payload(twice)
        assert AGPSession.verify_seal(twice) is True


# ══════════════════════════════════════════════════════════════════════
# 9. End-to-end forgery scenarios (the redteam narrative)
# ══════════════════════════════════════════════════════════════════════

class TestForgeryScenariosFailClosed:
    def test_db_writer_forge_attempt(self, monkeypatch):
        """Attacker has DB write access but not the key."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        row = json.loads(json.dumps(s.to_dict()))
        row["summary"]["conclusion"] = "totally legitimate"
        row["seal_hash"] = public_sha256(row)      # recompute public digest
        assert AGPSession.verify_seal(row) is False

    def test_downgrade_attempt_unset_env_via_empty_value(self, monkeypatch):
        """Setting CALLISTO_SEAL_KEY='' means 'no key', so legacy verifies —
        but a keyed-era HMAC row must then NOT verify (no key matches)."""
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "")
        d = dict(make_session().to_dict())
        d["seal_hash"] = hmac_with_key(VALID_KEYS[0], d)
        assert AGPSession.verify_seal(d) is False

    def test_attacker_cannot_pass_by_deleting_seal_fields(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        d = dict(make_session().to_dict())
        d.pop("sealed_at", None)
        d.pop("seal_hash", None)
        assert AGPSession.verify_seal(d) is False

    def test_unicode_tamper_detected(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEYS[0])
        s = make_session()
        s.seal()
        d = dict(s.to_dict())
        d["query"] += "\u200b"   # invisible char smuggle
        assert AGPSession.verify_seal(d) is False
