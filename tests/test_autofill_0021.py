"""autofill #0021 — LOCAL_ONLY money kill switch: characterization tests.

Characterizes the safety contract shared by two arming entry points:

* ``tools.bet_executor.BetExecutor.enable``
* ``tools.order_manager.OrderManager.enable``

Both must refuse to set ``_enabled = True`` when ``CALLISTO_LOCAL_ONLY`` is
set to any truthy value ("1", "true", "yes", case-insensitive), and both must
default to disabled so that an unconfigured environment never arms live
betting. These are characterization tests only — no browser, no network, no
live orders. The executor is instantiated but never ``initialize()``d.

Safety invariants under test:

I1.  Default construction leaves the component disabled.
I2.  enable() returns False and leaves _enabled False under LOCAL_ONLY.
I3.  Every truthy casing/variant of LOCAL_ONLY blocks arming.
I4.  Falsy / unset / empty values do NOT block arming.
I5.  Refusal happens BEFORE any mutation of _enabled (no partial arm).
I6.  Repeated enable() calls under LOCAL_ONLY keep refusing.
I7.  disable() after a refused enable stays disabled.
I8.  submit paths raise / refuse when not enabled (defense in depth).
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Environment matrices
# ---------------------------------------------------------------------------

TRUTHY_VALUES = [
    "1",
    "true",
    "True",
    "TRUE",
    "tRuE",
    "yes",
    "Yes",
    "YES",
    "yEs",
]

FALSY_VALUES = [
    "0",
    "false",
    "False",
    "FALSE",
    "no",
    "No",
    "NO",
    "",
    " ",
    "off",
    "null",
]

ENV_VAR = "CALLISTO_LOCAL_ONLY"


class _NullSender:
    """Async-callable telegram sender stand-in."""

    async def __call__(self, msg: str) -> None:
        return None


def _make_order_manager(tmp_path):
    return OrderManager(
        db_path=str(tmp_path / "om.db"),
        telegram_sender=_NullSender(),
    )


@pytest.fixture(autouse=True)
def _clean_local_only_env(monkeypatch):
    """Every test starts with CALLISTO_LOCAL_ONLY absent from the env."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    yield


# ===========================================================================
# Part 1 — BetExecutor.enable() kill-switch characterization
# ===========================================================================


class TestBetExecutorDefaults:
    def test_constructor_default_disabled(self):
        ex = BetExecutor()
        assert ex.is_enabled is False
        assert ex._enabled is False

    def test_is_enabled_property_reads_private_flag(self):
        ex = BetExecutor()
        assert ex.is_enabled == ex._enabled

    def test_enable_signature_takes_no_args(self):
        sig = inspect.signature(BetExecutor.enable)
        assert list(sig.parameters) == ["self"]

    def test_enable_returns_bool(self):
        ex = BetExecutor()
        result = ex.enable()
        assert isinstance(result, bool)
        assert result is True

    def test_source_contains_kill_switch_check(self):
        src = inspect.getsource(BetExecutor.enable)
        assert ENV_VAR in src
        assert "_enabled" in src

    def test_source_checks_env_before_setting_enabled(self):
        """The env check must lexically precede the _enabled assignment."""
        src = inspect.getsource(BetExecutor.enable)
        check_pos = src.find(ENV_VAR)
        set_pos = src.find("self._enabled = True")
        assert check_pos != -1
        assert set_pos != -1
        assert check_pos < set_pos


class TestBetExecutorLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refusal_leaves_flag_untouched(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        ex = BetExecutor()
        before = ex._enabled
        ex.enable()
        assert ex._enabled is before is False

    def test_refusal_does_not_raise_when_return_ignored(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        ex = BetExecutor()
        ex.enable()  # callers may ignore the return value
        assert ex.is_enabled is False

    def test_repeated_enable_still_refuses(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "true")
        ex = BetExecutor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex.is_enabled is False

    def test_warning_logged_on_refusal(self, monkeypatch, caplog):
        import logging

        monkeypatch.setenv(ENV_VAR, "1")
        ex = BetExecutor()
        with caplog.at_level(logging.WARNING):
            ex.enable()
        assert any("LOCAL_ONLY" in r.message for r in caplog.records)

    def test_refusal_survives_module_reload_semantics(self, monkeypatch):
        """The gate reads os.getenv at call time, not import time."""
        monkeypatch.setenv(ENV_VAR, "1")
        mod = importlib.import_module("tools.bet_executor")
        importlib.reload(mod)
        ex = mod.BetExecutor()
        assert ex.enable() is False

    def test_patch_dict_environment_also_blocks(self):
        with patch.dict(os.environ, {ENV_VAR: "yes"}):
            ex = BetExecutor()
            assert ex.enable() is False
            assert ex.is_enabled is False

    def test_env_removed_after_construction_allows_arm(self, monkeypatch):
        """Gate is evaluated inside enable(), keyed on live environ state."""
        ex = BetExecutor()
        monkeypatch.setenv(ENV_VAR, "1")
        assert ex.enable() is False
        monkeypatch.delenv(ENV_VAR)
        assert ex.enable() is True
        assert ex.is_enabled is True


class TestBetExecutorNonBlockingValues:
    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_unset_env_arms(self):
        ex = BetExecutor()
        assert ex.enable() is True

    def test_disable_then_enable_cycle(self):
        ex = BetExecutor()
        assert ex.enable() is True
        ex.disable()
        assert ex.is_enabled is False
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_falsy_value_with_whitespace_only(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "   ")
        ex = BetExecutor()
        assert ex.enable() is True


class TestBetExecutorDefenseInDepth:
    """Even if _enabled were somehow true, placement paths must refuse or
    require explicit enabled state. These characterize the guard rails."""

    def test_safety_checks_require_enabled(self):
        src = inspect.getsource(BetExecutor.preflight_check)
        assert "self._enabled" in src

    def test_disabled_executor_rejected_by_preflight(self):
        ex = BetExecutor()
        coro = ex.preflight_check(
            sport="baseball_mlb",
            odds=120, edge=0.10, stake=10.0,
        )
        asyncio.get_event_loop_policy()
        loop = asyncio.new_event_loop()
        try:
            ok, reason = loop.run_until_complete(coro)
        finally:
            loop.close()
        assert ok is False
        assert "disabled" in reason.lower()

    def test_disabled_executor_has_no_live_state(self):
        ex = BetExecutor()
        assert getattr(ex, "_logged_in", False) is False
        assert getattr(ex, "_db", None) is None
        assert getattr(ex, "_page", None) is None

    def test_disable_resets_enabled_flag(self):
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex._enabled is False

    def test_kill_switch_message_mentions_local_only_mode(self):
        from tools import bet_executor as be_mod

        src = inspect.getsource(be_mod.BetExecutor.enable)
        assert re.search(r"local.only", src, re.IGNORECASE)


# ===========================================================================
# Part 2 — OrderManager.enable() kill-switch characterization
# ===========================================================================


class TestOrderManagerDefaults:
    def test_constructor_default_disabled(self, tmp_path):
        m = _make_order_manager(tmp_path)
        assert m.is_enabled is False
        assert m._enabled is False

    def test_is_enabled_property_matches_flag(self, tmp_path):
        m = _make_order_manager(tmp_path)
        assert m.is_enabled == m._enabled

    def test_enable_signature_takes_no_args(self, tmp_path):
        sig = inspect.signature(OrderManager.enable)
        assert list(sig.parameters) == ["self"]

    def test_enable_returns_bool(self, tmp_path):
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert isinstance(result, bool)
        assert result is True

    def test_source_contains_kill_switch_check(self):
        src = inspect.getsource(OrderManager.enable)
        assert ENV_VAR in src
        assert "_enabled" in src

    def test_source_checks_env_before_setting_enabled(self):
        src = inspect.getsource(OrderManager.enable)
        check_pos = src.find(ENV_VAR)
        set_pos = src.find("self._enabled = True")
        assert check_pos != -1
        assert set_pos != -1
        assert check_pos < set_pos

    def test_mirrors_bet_executor_gate(self):
        """Both gates accept the same truthy set — compare sources."""
        om_src = inspect.getsource(OrderManager.enable)
        be_src = inspect.getsource(BetExecutor.enable)
        for token in ('"1"', '"true"', '"yes"'):
            assert token in om_src
            assert token in be_src


class TestOrderManagerLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        m = _make_order_manager(tmp_path)
        assert m.enable() is False
        assert m.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refusal_leaves_flag_untouched(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        m = _make_order_manager(tmp_path)
        before = m._enabled
        m.enable()
        assert m._enabled is before is False

    def test_repeated_enable_still_refuses(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "YES")
        m = _make_order_manager(tmp_path)
        for _ in range(5):
            assert m.enable() is False
        assert m.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(ENV_VAR, "1")
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False

    def test_warning_logged_on_refusal(self, tmp_path, monkeypatch, caplog):
        import logging

        monkeypatch.setenv(ENV_VAR, "true")
        m = _make_order_manager(tmp_path)
        with caplog.at_level(logging.WARNING):
            m.enable()
        assert any("LOCAL_ONLY" in r.message for r in caplog.records)

    def test_patch_dict_environment_also_blocks(self, tmp_path):
        with patch.dict(os.environ, {ENV_VAR: "TRUE"}):
            m = _make_order_manager(tmp_path)
            assert m.enable() is False
            assert m.is_enabled is False

    def test_env_removed_after_construction_allows_arm(
        self, tmp_path, monkeypatch
    ):
        m = _make_order_manager(tmp_path)
        monkeypatch.setenv(ENV_VAR, "yes")
        assert m.enable() is False
        monkeypatch.delenv(ENV_VAR)
        assert m.enable() is True
        assert m.is_enabled is True


class TestOrderManagerNonBlockingValues:
    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        m = _make_order_manager(tmp_path)
        assert m.enable() is True
        assert m.is_enabled is True

    def test_unset_env_arms(self, tmp_path):
        m = _make_order_manager(tmp_path)
        assert m.enable() is True


# ===========================================================================
# Part 3 — Order submission refuses while disarmed
# ===========================================================================


_SIG = {"signal_id": "sig0021", "sport": "baseball_mlb"}


class TestOrderSubmissionRefusal:
    @pytest.mark.asyncio
    async def test_submit_order_raises_when_disarmed_default(self, tmp_path):
        m = _make_order_manager(tmp_path)
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp0021a",
                    signal=_SIG,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()

    @pytest.mark.asyncio
    async def test_submit_order_raises_after_local_only_refused_enable(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(ENV_VAR, "1")
        m = _make_order_manager(tmp_path)
        m.enable()  # refused silently
        assert not m.is_enabled
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp0021b",
                    signal=_SIG,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()

    @pytest.mark.asyncio
    async def test_submit_order_after_disable_raises(self, tmp_path):
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp0021c",
                    signal=_SIG,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()


# ===========================================================================
# Part 4 — Cross-cutting invariants
# ===========================================================================


class TestCrossCuttingInvariants:
    def test_never_add_live_to_paper_statuses(self):
        """Guard against accidental widening of paper-trade statuses."""
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}

    def test_both_gates_use_same_env_var_name(self):
        assert ENV_VAR in inspect.getsource(BetExecutor.enable)
        assert ENV_VAR in inspect.getsource(OrderManager.enable)

    def test_both_gates_case_insensitive_lower_comparison(self):
        for fn in (BetExecutor.enable, OrderManager.enable):
            src = inspect.getsource(fn)
            assert ".lower()" in src

    def test_both_gates_return_false_not_none_on_refusal(self, monkeypatch,
                                                          tmp_path):
        monkeypatch.setenv(ENV_VAR, "1")
        assert BetExecutor().enable() is False
        assert _make_order_manager(tmp_path).enable() is False

    @pytest.mark.parametrize("component", [BetExecutor, OrderManager])
    def test_component_defaults_to_disabled(self, component, tmp_path):
        if component is OrderManager:
            obj = _make_order_manager(tmp_path)
        else:
            obj = component()
        assert obj.is_enabled is False

    def test_no_automatic_arming_at_import_time(self, monkeypatch):
        """Importing the modules with a clean env must not arm anything."""
        importlib.reload(importlib.import_module("tools.bet_executor"))
        assert BetExecutor()._enabled is False

    def test_truthy_set_matches_documented_contract(self):
        """The documented truthy set is exactly {1,true,yes} case-insensitively."""
        expected = {"1", "true", "yes"}
        for fn in (BetExecutor.enable, OrderManager.enable):
            src = inspect.getsource(fn)
            assert '("1", "true", "yes")' in src.replace("'", '"'), (
                f"{fn}: truthy tuple missing"
            )

    def test_generate_paper_trade_signal_not_widened_to_live(self):
        """The paper-signal generator must not special-case status 'live'."""
        from tools.backtest import BacktestEngine

        src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
        assert "=='live'" not in src.replace(" ", "")
        assert 'status == "live"' not in src
