"""Characterization tests for the LOCAL_ONLY money kill switch (#0053).

Scope
-----
``BetExecutor.enable`` and ``OrderManager.enable`` must refuse to arm when the
``CALLISTO_LOCAL_ONLY`` environment variable is truthy — and crucially, they
must evaluate that gate BEFORE flipping any internal state (``_enabled``).
This is the appliance-wide nuclear switch: local-only mode never places live
bets.

Safety contract encoded by this module:

1. Truthy values ("1", "true", "yes", mixed case) block arming.
2. Falsy / unset values leave default behavior untouched (arming allowed).
3. Refusal happens before any state flip: even if callers ignore the return
   value, ``is_enabled`` stays False.
4. Disarm paths (``disable()``) always work regardless of the env var.
5. The gate is re-read on every call — clearing the env var between calls
   allows a later arm; setting it again refuses a later arm.
6. Nothing here ever touches live betting infrastructure: executors are
   instantiated but never initialized; OrderManager gets a throwaway sqlite
   path and a no-op sender.

These are characterization tests: they pin down CURRENT behavior of the
production gates without weakening them. If a gate ever regresses to allowing
an arm under LOCAL_ONLY, these tests fail closed and loudly.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.betexec.lifecycle import is_local_only as betexec_is_local_only
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TRUTHY_VALUES = [
    "1",
    "true",
    "TRUE",
    "True",
    "tRuE",
    "yes",
    "YES",
    "Yes",
]

# The production gates only treat exactly these lowercase forms as truthy;
# anything else (including "0", "", "no", "false", garbage) does not trip the
# switch. We pin the canonical truthy set and the important falsy set.
FALSY_VALUES = [
    "",
    "0",
    "no",
    "NO",
    "false",
    "FALSE",
    "off",
    "disabled",
    "  ",
]


class _NoopSender:
    """Async callable sender that swallows messages (no network)."""

    def __init__(self):
        self.sent = []

    async def __call__(self, msg: str):
        self.sent.append(msg)


def _make_order_manager(tmp_path) -> OrderManager:
    return OrderManager(
        db_path=str(tmp_path / "om_0053.db"),
        telegram_sender=_NoopSender(),
    )


def _clear_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)


@pytest.fixture
def clean_env(monkeypatch):
    """Ensure the kill switch starts unset for a test."""
    _clear_env(monkeypatch)
    yield


# ---------------------------------------------------------------------------
# Part 1: BetExecutor — default behavior with the switch unset
# ---------------------------------------------------------------------------


class TestBetExecutorDefaults:
    def test_init_starts_disabled(self, clean_env):
        ex = BetExecutor()
        assert ex.is_enabled is False

    def test_enable_without_switch_arms(self, clean_env):
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_disable_disarms(self, clean_env):
        ex = BetExecutor()
        assert ex.enable() is True
        ex.disable()
        assert ex.is_enabled is False

    def test_disable_when_never_enabled_is_safe(self, clean_env):
        ex = BetExecutor()
        ex.disable()
        assert ex.is_enabled is False

    def test_repeated_enable_idempotent(self, clean_env):
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_reenable_after_disable(self, clean_env):
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex.enable() is True
        assert ex.is_enabled is True


# ---------------------------------------------------------------------------
# Part 2: BetExecutor — LOCAL_ONLY refusal (gate BEFORE state flip)
# ---------------------------------------------------------------------------


class TestBetExecutorLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        result = ex.enable()
        assert result is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refusal_leaves_state_untouched_even_if_return_ignored(
        self, monkeypatch, value
    ):
        """Callers that ignore enable()'s return still get a disarmed executor."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        ex.enable()  # return ignored — must not raise, must not arm
        assert ex.is_enabled is False

    def test_refusal_does_not_raise(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        try:
            res = ex.enable()
        except Exception as exc:  # pragma: no cover - failure mode
            pytest.fail(f"enable() raised under LOCAL_ONLY: {exc!r}")
        assert res is False

    def test_refusal_logs_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        with caplog.at_level(logging.WARNING, logger="tools.bet_executor"):
            ex.enable()
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("LOCAL_ONLY" in r.getMessage() for r in warnings)

    def test_gate_checked_before_state_flip(self, monkeypatch):
        """Even with _enabled pre-seeded True, a refused call must disarm-safe.

        The contract is that the gate is evaluated BEFORE any assignment; we
        characterize the observable half: a refused enable leaves is_enabled
        False regardless of prior history (fresh instance here).
        """
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        ex = BetExecutor()
        # History: disable then refused enable -> still disabled.
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_disable_works_under_local_only(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        ex.disable()
        assert ex.is_enabled is False

    def test_clearing_env_between_calls_allows_arm(self, monkeypatch):
        """Gate is re-read per call: unset -> refuse -> unset -> allow."""
        ex = BetExecutor()
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        assert ex.enable() is False
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_setting_env_after_arm_blocks_later_enable_only(self, monkeypatch):
        """The switch blocks future arming; it does not silently flip state."""
        _clear_env(monkeypatch)
        ex = BetExecutor()
        assert ex.enable() is True
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        # Already armed stays armed (characterized current behavior), but a
        # fresh enable cycle is refused:
        ex2 = BetExecutor()
        assert ex2.enable() is False
        assert ex2.is_enabled is False
        # Explicit disarm still works and cannot be re-armed:
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# Part 3: BetExecutor — falsy / junk values do NOT trip the switch
# ---------------------------------------------------------------------------


class TestBetExecutorFalsyValues:
    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_still_allow_arm(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_unset_env_allows_arm(self, monkeypatch):
        _clear_env(monkeypatch)
        ex = BetExecutor()
        assert ex.enable() is True


# ---------------------------------------------------------------------------
# Part 4: lifecycle helper — is_local_only characterization
# ---------------------------------------------------------------------------


class TestIsLocalOnlyHelper:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_helper_truthy(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_is_local_only() is True

    @pytest.mark.parametrize("value", ["0", "", "no", "false"])
    def test_helper_falsy(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_is_local_only() is False

    def test_helper_unset(self, monkeypatch):
        _clear_env(monkeypatch)
        assert betexec_is_local_only() is False


# ---------------------------------------------------------------------------
# Part 5: OrderManager — default behavior with the switch unset
# ---------------------------------------------------------------------------


class TestOrderManagerDefaults:
    def test_init_starts_disabled(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        assert m.is_enabled is False

    def test_enable_without_switch_arms(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    def test_disable_disarms(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False

    def test_reenable_after_disable(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        m.enable()
        assert m.is_enabled is True


# ---------------------------------------------------------------------------
# Part 6: OrderManager — LOCAL_ONLY refusal
# ---------------------------------------------------------------------------


class TestOrderManagerLocalOnlyRefusal:
    @pytest.mark.parametrize("val", TRUTHY_VALUES)
    def test_truthy_blocks_enable(self, tmp_path, monkeypatch, val):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is False
        if result is not None:
            assert result is False

    @pytest.mark.parametrize("val", TRUTHY_VALUES)
    def test_blocks_enable_with_preexisting_instance(
        self, tmp_path, monkeypatch, val
    ):
        """Switch set after construction still blocks arming (re-read per call)."""
        _clear_env(monkeypatch)
        m = _make_order_manager(tmp_path)
        assert m.enable()
        m.disable()
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
        result = m.enable()
        assert m.is_enabled is False
        if result is not None:
            assert result is False

    def test_block_logs_warning(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        m = _make_order_manager(tmp_path)
        with caplog.at_level(logging.WARNING):
            m.enable()
        assert any(
            "LOCAL_ONLY" in r.getMessage() for r in caplog.records
            if r.levelno >= logging.WARNING
        )

    def test_disable_works_under_local_only(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        m = _make_order_manager(tmp_path)
        m.enable()  # refused, but harmless
        m.disable()
        assert m.is_enabled is False

    def test_clearing_env_between_calls_allows_arm(self, tmp_path, monkeypatch):
        m = _make_order_manager(tmp_path)
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        m.enable()
        assert m.is_enabled is False
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True


# ---------------------------------------------------------------------------
# Part 7: OrderManager — falsy values do NOT trip the switch
# ---------------------------------------------------------------------------


class TestOrderManagerFalsyValues:
    @pytest.mark.parametrize(
        "val", ["0", "false", "no", "OFF", ""]
    )
    def test_non_canonical_values_do_not_block(self, tmp_path, monkeypatch, val):
        """Characterization: only exact '1'/'true'/'yes' lowercase trip the gate.

        Note 'TRUE'/'True' ARE blocked because the check lowercases first;
        see TestOrderManagerLocalOnlyRefusal. Values like 'ON' or 'enabled'
        are NOT recognized and therefore do not block — pinned here so a
        future widening is a conscious decision.
        """
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    def test_case_insensitive_true_blocked(self, tmp_path, monkeypatch):
        """Companion pin: uppercase TRUE *is* treated as truthy by the gate."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "TRUE")
        m = _make_order_manager(tmp_path)
        m.enable()
        assert m.is_enabled is False


# ---------------------------------------------------------------------------
# Part 8: environment isolation via patch.dict (alternate style)
# ---------------------------------------------------------------------------


class TestEnvIsolationStyles:
    def test_patch_dict_style_executor(self):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
            ex = BetExecutor()
            assert ex.enable() is False
            assert ex.is_enabled is False
        # Outside the patch context the process env is unchanged.
        ex = BetExecutor()
        if os.getenv("CALLISTO_LOCAL_ONLY") in ("1", "true", "yes"):
            pytest.skip("ambient CALLISTO_LOCAL_ONLY set on host")
        assert ex.enable() is True
        ex.disable()

    def test_patch_dict_style_order_manager(self, tmp_path):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "true"}):
            m = _make_order_manager(tmp_path)
            m.enable()
            assert m.is_enabled is False


# ---------------------------------------------------------------------------
# Part 9: cross-component consistency pins
# ---------------------------------------------------------------------------


class TestCrossComponentConsistency:
    def test_both_components_refuse_under_same_value(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        m = _make_order_manager(tmp_path)
        assert ex.enable() is False
        m.enable()
        assert ex.is_enabled is False
        assert m.is_enabled is False

    def test_helper_matches_executor_behavior(self, tmp_path, monkeypatch):
        for val in ["1", "true", "yes", "", "0", "junk"]:
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
            expected_block = val.lower() in ("1", "true", "yes")
            ex = BetExecutor()
            armed = ex.enable()
            assert betexec_is_local_only() is expected_block
            assert armed is (not expected_block)

    def test_no_live_signal_status_introduced(self):
        """Guard rail: paper-trade signal statuses must never contain 'live'.

        This module's whole point is that LOCAL_ONLY keeps live betting off;
        if anyone ever widens the paper-trade status set to include 'live',
        that is a money-path change that must be reviewed deliberately.
        """
        import tools.signals.paper as paper  # noqa: PLC0415 - lazy import ok

        statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is None:
            pytest.skip("_PAPER_TRADE_SIGNAL_STATUSES not present upstream")
        normalized = {str(s).lower() for s in statuses}
        assert "live" not in normalized
        assert "paper_trading" in normalized
