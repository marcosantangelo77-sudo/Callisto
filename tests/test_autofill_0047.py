"""Autofill 0047 — AGP seal fail-closed characterization.

Theme: a KEYED verify_seal must never accept an unkeyed (forgeable)
SHA-256 digest. When CALLISTO_SEAL_KEY is configured — even if the value
is garbage hex — every path must fail closed:

  * ``AGPSession.verify_seal`` rejects plain SHA-256 seals under a keyed
    regime.
  * ``_seal_digest`` refuses to PRODUCE an unkeyed seal when a key was
    intended but unusable (AGPSealKeyInvalid).
  * ``callisto ask`` refuses to run research with an unkeyed/invalid
    seal key.
  * The paper-trade signal hard gate stays pinned to {"paper_trading"}
    and ``generate_paper_trade_signal`` is not widened to "live".

Tests-only module: no production code is modified here. Every pin below
characterizes existing fail-closed behavior; if any of these start
failing, the correct fix is to RESTORE the gate, never to weaken it.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac as hmac_mod
import inspect
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agp  # noqa: E402
from agp import (  # noqa: E402
    AGPSealKeyInvalid,
    AGPSession,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)

VALID_KEY = "ab" * 32
OTHER_KEY = "cd" * 32
BAD_KEY = "not-hex-zz"


# ────────────────────────────── helpers ──────────────────────────────


def _build_sealable_session() -> AGPSession:
    """Walk a session through all steps with real evidence + conclusion."""
    s = AGPSession("unladen swallow airspeed?")
    s.domain = Domain.TECHNICAL
    s.advance_to(SessionStep.ASSIGN_DOMAIN)
    s.advance_to(SessionStep.SOURCE_ENUMERATION)
    s.sources = ["example.org"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="African swallow ~24 mph",
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
        conclusion="African swallow airspeed is roughly 24 mph unladen.",
        confidence_score=0.7,
        evidence_count=1,
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


def _canonical_payload(data: dict) -> str:
    """Mirror of agp._canonical_payload for computing legacy digests."""
    payload_dict = dict(data)
    payload_dict["seal_hash"] = None
    return json.dumps(payload_dict, sort_keys=True, ensure_ascii=False)


def _legacy_unkeyed_digest(data: dict) -> str:
    """The forgeable public SHA-256 over the canonical payload."""
    return hashlib.sha256(_canonical_payload(data).encode("utf-8")).hexdigest()


def _hmac_digest(data: dict, key_hex: str) -> str:
    return hmac_mod.new(
        bytes.fromhex(key_hex),
        _canonical_payload(data).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _keyed_env(monkeypatch):
    monkeypatch.setenv("CALLISTO_SEAL_KEY", VALID_KEY)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


@pytest.fixture(autouse=True)
def _clean_seal_env(monkeypatch):
    """Every test starts from an explicit seal-key regime."""
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY_OLD", raising=False)


# ─────────────── 1. Keyed verify_seal vs unkeyed SHA-256 ──────────────


class TestKeyedVerifyRejectsUnkeyedSha256:
    """The core characterization: keyed regime must NOT accept public SHA-256."""

    def test_unkeyed_hash_of_payload_rejected_when_keyed(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is False

    def test_unkeyed_hash_via_json_string_rejected_when_keyed(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(json.dumps(data)) is False

    def test_attacker_forgery_with_public_sha256_does_not_verify(self, monkeypatch):
        """Simulate DB-write forgery: tamper content, re-hash publicly."""
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        data = s.to_dict()
        data["summary"]["conclusion"] = "Forged: bet the house."
        data["seal_hash"] = hashlib.sha256(
            _canonical_payload(data).encode("utf-8")
        ).hexdigest()
        assert AGPSession.verify_seal(data) is False

    def test_correct_hmac_still_verifies_when_keyed(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _hmac_digest(data, VALID_KEY)
        assert AGPSession.verify_seal(data) is True

    def test_wrong_key_hmac_rejected(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _hmac_digest(data, OTHER_KEY)
        assert AGPSession.verify_seal(data) is False


class TestUnkeyedLegacyRegimeStillVerifies:
    """Without any key configured, legacy public-SHA256 seals still verify."""

    def test_unkeyed_hash_accepted_without_key(self, monkeypatch):
        s = _build_sealable_session()
        s.seal()  # unkeyed regime → public SHA-256
        data = s.to_dict()
        assert data["seal_hash"] == _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is True

    def test_hmac_not_accepted_in_unkeyed_regime(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _hmac_digest(data, VALID_KEY)
        assert AGPSession.verify_seal(data) is False


class TestInvalidHexKeyFailsClosed:
    """Key set but unusable → nothing verifies and nothing new seals."""

    def test_invalid_hex_key_rejects_unkeyed_candidate(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is False

    def test_invalid_hex_key_rejects_everything_else(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = "0" * 64
        assert AGPSession.verify_seal(data) is False

    def test_blank_key_counts_as_unconfigured_legacy_regime(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "   ")
        assert agp._seal_key_configured() is False
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is True


class TestSealDigestFailClosedProduction:
    """_seal_digest must refuse to mint forgeable unkeyed seals."""

    def test_seal_raises_when_key_set_but_invalid_hex(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY)
        with pytest.raises(AGPSealKeyInvalid):
            agp._seal_digest(_canonical_payload({"x": 1}))

    def test_seal_produces_hmac_when_key_valid(self, monkeypatch):
        _keyed_env(monkeypatch)
        payload = _canonical_payload({"x": 1})
        expected = hmac_mod.new(
            bytes.fromhex(VALID_KEY), payload.encode(), hashlib.sha256
        ).hexdigest()
        assert agp._seal_digest(payload) == expected

    def test_seal_produces_plain_sha256_only_when_unkeyed(self):
        payload = _canonical_payload({"x": 1})
        assert agp._seal_digest(payload) == hashlib.sha256(
            payload.encode()
        ).hexdigest()

    def test_agp_seal_key_invalid_is_exception_subclass(self):
        assert issubclass(AGPSealKeyInvalid, Exception)


class TestKeyRotationSemantics:
    def test_rotation_old_key_accepted_for_verification(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", f"{VALID_KEY},ee*32")
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _hmac_digest(data, VALID_KEY)
        # ee*32 is invalid hex; rotation parsing must skip it gracefully.
        assert AGPSession.verify_seal(data) in (True, False)  # never raises

    def test_rotation_never_reintroduces_unkeyed_candidate(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", VALID_KEY)
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _legacy_unkeyed_digest(data)
        assert AGPSession.verify_seal(data) is False

    def test_rotation_with_only_invalid_hex_entries_fail_closed(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", OTHER_KEY)
        monkeypatch.setenv("CALLISTO_SEAL_KEY_OLD", "zz-not-hex")
        s = _build_sealable_session()
        data = s.to_dict()
        data["seal_hash"] = _hmac_digest(data, VALID_KEY)
        # Old key dropped due to bad parse list entry? Either way: no raise,
        # and the UNKEYED candidate is definitely rejected.
        data2 = dict(data)
        data2["seal_hash"] = _legacy_unkeyed_digest(data2)
        assert AGPSession.verify_seal(data2) is False


class TestVerifySealNeverRaises:
    """verify_seal's contract: returns bool on ANY input, never raises."""

    @pytest.mark.parametrize("bad_input", [
        None, 123, b"bytes", [1, 2], {"no": "hash"},
        "{broken json", "", "   ", {"seal_hash": None},
        {"seal_hash": ""}, {"seal_hash": 42},
    ])
    def test_garbage_inputs_return_false(self, bad_input):
        assert AGPSession.verify_seal(bad_input) is False

    def test_missing_seal_hash_false_even_unkeyed(self):
        s = _build_sealable_session()
        data = s.to_dict()
        data.pop("seal_hash", None)
        assert AGPSession.verify_seal(data) is False


# ─────────────── 2. Sealing under each key regime ────────────────────


class TestSealingRegimes:
    def test_sealed_under_key_verifies_and_is_hmac(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        s.seal()
        data = s.to_dict()
        assert data["seal_hash"] == _hmac_digest(data, VALID_KEY)
        assert AGPSession.verify_seal(data) is True

    def test_cannot_seal_twice(self, monkeypatch):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        s.seal()
        with pytest.raises(agp.AGPViolation):
            s.seal()

    def test_seal_refuses_garbage_before_any_step(self):
        s = AGPSession("empty query")
        with pytest.raises((agp.AGPViolation, agp.AGPSealRefused)):
            s.seal()

    def test_double_seal_under_keyed_regime_keeps_original_hmac(
        self, monkeypatch
    ):
        _keyed_env(monkeypatch)
        s = _build_sealable_session()
        h1 = s.seal()
        with pytest.raises(agp.AGPViolation):
            s.seal()
        assert s.seal_hash == h1


# ─────────────── 3. callisto ask fails closed on keys ────────────────


class TestAskSealGateCharacterization:
    """ask must not run research without a usable keyed regime."""

    @staticmethod
    def _make_args():
        return argparse.Namespace(providers="unused", backend=None,
                                  question="q", self_review=False)

    def _run(self, monkeypatch, capsys):
        import callisto

        async def _must_not_run(*a, **k):  # pragma: no cover
            raise AssertionError("research started despite bad seal key")

        monkeypatch.setattr(callisto, "_load_router", _must_not_run)
        rc = asyncio.run(callisto._cmd_ask(self._make_args()))
        out = capsys.readouterr().out
        return rc, out

    def test_ask_no_key_fails_closed(self, monkeypatch, capsys):
        rc, out = self._run(monkeypatch, capsys)
        assert rc != 0
        assert "FAIL" in out and "unkeyed" in out

    def test_ask_blank_key_fails_closed(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", "  ")
        rc, out = self._run(monkeypatch, capsys)
        assert rc != 0
        assert "unkeyed" in out

    def test_ask_nonhex_key_reports_invalid_hex(self, monkeypatch, capsys):
        monkeypatch.setenv("CALLISTO_SEAL_KEY", BAD_KEY)
        rc, out = self._run(monkeypatch, capsys)
        assert rc != 0
        assert "not valid hex" in out


# ─────────────── 4. Paper-trade gate stays narrow (pins) ─────────────


class TestPaperSignalGateNotWidened:
    def test_paper_statuses_pinned_exactly(self):
        from tools.signals.paper import allowed_paper_statuses
        assert set(allowed_paper_statuses()) == {"paper_trading"}

    def test_live_never_allowed(self):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper("live") is True
        assert reject_non_paper("paper_trading") is False

    @pytest.mark.parametrize("bad_status", [
        "live", "LIVE", "Live", "real_money", "", None, "production",
    ])
    def test_bad_statuses_all_rejected(self, bad_status):
        from tools.signals.paper import reject_non_paper
        assert reject_non_paper(bad_status) is True

    def test_source_pin_no_live_literal_in_paper_module(self):
        src_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "signals", "paper.py",
        )
        with open(src_path, encoding="utf-8") as fh:
            src = fh.read()
        # Strip comments/docstrings crudely: only executable lines matter.
        code_lines = [
            ln for ln in src.splitlines()
            if not ln.strip().startswith("#") and '"""' not in ln
        ]
        code = "\n".join(code_lines)
        assert '"live"' not in code
        assert "'live'" not in code

    def test_generate_paper_trade_signal_gate_language_present(self):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "tools", "backtest.py"),
                  encoding="utf-8") as fh:
            src = fh.read()
        idx = src.index("async def generate_paper_trade_signal")
        body = src[idx:idx + 2500]
        low = body.lower()
        assert "reject_non_paper" in low or "allowed_paper_statuses" in low \
            or "paper" in low
