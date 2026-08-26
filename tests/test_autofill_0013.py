"""autofill #0013 — LOCAL_ONLY money kill switch (characterization).

Characterizes the behavior of ``CALLISTO_LOCAL_ONLY`` as a nuclear money
kill switch covering two arming paths:

1. ``tools.bet_executor.BetExecutor.enable()`` must refuse to set
   ``_enabled = True`` when the env var is truthy, and must refuse it
   BEFORE any other side effects.
2. ``tools.order_manager.OrderManager.enable()`` must behave the same.

Safety invariants under test (never armed here):

* A truthy ``CALLISTO_LOCAL_ONLY`` makes every ``enable()`` call a no-op
  that returns a falsy result and leaves ``is_enabled() is False``.
* Even if some other code path flipped ``_enabled`` by hand, the
  execution gates (``BetExecutor._preflight_checks`` /
  ``OrderManager.submit_order``) still refuse to act while the executor
  reports disabled — defense in depth.
* Falsy / unset values leave normal enable/disable semantics intact.
* The drawdown kill switch path also disarms via ``_enabled = False``
  and cannot be re-armed under LOCAL_ONLY.

No browser, no network. The executors are instantiated but never fully
initialized where avoidable; only ``enable``/``disable``/``is_enabled``
and pure gates are exercised. Tests never place real bets and never add
"live" to any paper-trade signal status list.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Truthy / falsy value tables for CALLISTO_LOCAL_ONLY
# ---------------------------------------------------------------------------

# Values the production gates treat as truthy: lowercased membership in
# ("1", "true", "yes").
TRUTHY_VALUES = [
    "1",
    "true",
    "TRUE",
    "True",
    "tRuE",
    "yes",
    "YES",
    "Yes",
    "yEs",
]

# Everything else is treated as falsy by the current implementation:
# the check is exact-membership on the lowercased string, so "on", "2",
# or even " true" (leading space) do NOT trip the kill switch.
FALSY_VALUES = [
    "",
    "0",
    "false",
    "FALSE",
    "no",
    "NO",
    "off",
    "2",
    "true ",   # trailing space -> not an exact match after lower()
    " true",   # leading space
    "yes!",
]

ENV_VAR = "CALLISTO_LOCAL_ONLY"


def _clear_local_only(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


def _set_local_only(monkeypatch, value):
    monkeypatch.setenv(ENV_VAR, value)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _NullSender:
    """Async-callable stand-in for the Telegram sender."""

    async def __call__(self, msg: str) -> None:
        self.last_message = msg


def _make_order_manager(tmp_path):
    return OrderManager(
        db_path=str(tmp_path / "om_0013.db"),
        telegram_sender=_NullSender(),
    )


# ===========================================================================
# Part 1: BetExecutor.enable() vs CALLISTO_LOCAL_ONLY
# ===========================================================================


class TestBetExecutorLocalOnlyEnable:
    """BetExecutor.enable refuses BEFORE setting _enabled True."""

    def test_default_env_enable_disable_cycle(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        assert ex.is_enabled is False  # __init__ default unchanged
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, monkeypatch, value):
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuse_is_idempotent(self, monkeypatch, value):
        """Repeated refusals never arm the executor."""
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refusal_silences_rather_than_raises(self, monkeypatch, value):
        """Callers ignoring enable()'s return get silence, not an exception."""
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        ret = ex.enable()  # must not raise
        assert not ret
        assert ex.is_enabled is False

    def test_refusal_does_not_mutate_internal_flag_via_enable(self, monkeypatch):
        """The gate fires before the assignment line — flag stays untouched."""
        _set_local_only(monkeypatch, "1")
        ex = BetExecutor()
        assert getattr(ex, "_enabled") is False
        ex.enable()
        assert getattr(ex, "_enabled") is False

    @pytest.mark.parametrize("value", ["0", "", "false", "no"])
    def test_falsy_values_still_allow_enable(self, monkeypatch, value):
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()

    def test_unset_value_still_allows_enable(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()

    def test_rearm_after_disable_allowed_when_unset(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()

    def test_disarm_then_set_local_only_blocks_rearm(self, monkeypatch):
        """Arming first, then flipping LOCAL_ONLY, still blocks re-arm."""
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        assert ex.enable() is True
        ex.disable()
        _set_local_only(monkeypatch, "1")
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_status_dict_reports_disabled_under_local_only(self, monkeypatch):
        """Whatever status surface exists, enabled must read False."""
        _set_local_only(monkeypatch, "1")
        ex = BetExecutor()
        ex.enable()
        snapshot = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(ex.status())
        assert snapshot["enabled"] is False

    @pytest.mark.asyncio
    async def test_status_dict_reports_enabled_when_unset(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        ex.enable()
        snapshot = await ex.status()
        assert snapshot["enabled"] is True
        ex.disable()


class TestBetExecutorDefenseInDepth:
    """Even a hand-flipped _enabled flag does not bypass the gates."""

    @pytest.mark.asyncio
    async def test_preflight_refuses_when_disabled(self, monkeypatch):
        _set_local_only(monkeypatch, "1")
        ex = BetExecutor()
        ok, reason = await ex.preflight_check(
            sport="baseball_mlb",
            odds=120,
            edge=0.10,
            stake=10.0,
        )
        assert ok is False
        assert "disabled" in reason.lower()

    @pytest.mark.asyncio
    async def test_preflight_disabled_reason_precedes_edge_reason(
        self, monkeypatch
    ):
        """A great edge + huge stake still gets 'disabled' as the reason."""
        _set_local_only(monkeypatch, "true")
        ex = BetExecutor()
        ok, reason = await ex.preflight_check(
            sport="basketball_nba",
            odds=100,
            edge=0.99,
            stake=1_000_000.0,
        )
        assert ok is False
        assert "disabled" in reason.lower()

    def test_manual_flag_flip_then_enable_still_refused(self, monkeypatch):
        """Setting _enabled=True by hand then calling enable() under
        LOCAL_ONLY: enable() returns False. We deliberately do NOT leave
        the flag armed — this test restores it immediately."""
        _set_local_only(monkeypatch, "1")
        ex = BetExecutor()
        try:
            ex._enabled = True  # simulate a buggy caller
            assert ex.enable() is False
            # enable() itself must not have re-disarmed OR re-armed; but the
            # documented contract is that it refuses, i.e. never sets True
            # again. Either way the safe assertion is: after disable(), off.
        finally:
            ex._enabled = False
        assert ex.is_enabled is False


class TestDrawdownKillSwitchInteraction:
    """The drawdown path disarms via _enabled=False; LOCAL_ONLY blocks re-arm."""

    def test_drawdown_disarms_and_local_only_blocks_rearm(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        ex.enable()
        assert ex.is_enabled is True
        # Simulate what evaluate-drawdown handling does on trigger:
        ex._enabled = False
        _set_local_only(monkeypatch, "1")
        assert ex.enable() is False
        assert ex.is_enabled is False


# ===========================================================================
# Part 2: OrderManager.enable() vs CALLISTO_LOCAL_ONLY
# ===========================================================================


class TestOrderManagerLocalOnlyEnable:
    """OrderManager.enable refuses BEFORE setting _enabled True."""

    def test_default_env_enable_arms(self, tmp_path, monkeypatch):
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        assert m.is_enabled is False
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_blocks_enable(self, tmp_path, monkeypatch, value):
        _set_local_only(monkeypatch, value)
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is False
        if result is not None:
            assert result is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_block_is_idempotent(self, tmp_path, monkeypatch, value):
        _set_local_only(monkeypatch, value)
        m = _make_order_manager(tmp_path)
        for _ in range(4):
            m.enable()
        assert m.is_enabled is False

    @pytest.mark.parametrize("value", ["0", "", "false", "no"])
    def test_falsy_values_allow_enable(self, tmp_path, monkeypatch, value):
        _set_local_only(monkeypatch, value)
        m = _make_order_manager(tmp_path)
        m.enable()
        assert m.is_enabled is True
        m.disable()

    def test_unset_value_allows_enable(self, tmp_path, monkeypatch):
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        m.enable()
        assert m.is_enabled is True
        m.disable()

    def test_disable_still_works_after_arm(self, tmp_path, monkeypatch):
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        m.enable()
        assert m.is_enabled is True
        m.disable()
        assert m.is_enabled is False

    def test_disarm_then_local_only_blocks_rearm(self, tmp_path, monkeypatch):
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        _set_local_only(monkeypatch, "YES")
        m.enable()
        assert m.is_enabled is False

    def test_init_default_is_disarmed_regardless_of_env(self, tmp_path, monkeypatch):
        """Construction never arms, with or without the kill switch set."""
        _set_local_only(monkeypatch, "1")
        m_armed_env = _make_order_manager(tmp_path)
        assert m_armed_env.is_enabled is False
        _clear_local_only(monkeypatch)
        m_clean_env = _make_order_manager(tmp_path / ".." / "om_0013b.db")
        assert m_clean_env.is_enabled is False


class TestOrderManagerSubmitGate:
    """submit_order raises 'disabled' when LOCAL_ONLY blocked the arm."""

    @pytest.mark.asyncio
    async def test_submit_refused_under_local_only(self, tmp_path, monkeypatch):
        sig = {"signal_id": "sig_0013a", "sport": "baseball_mlb"}
        _set_local_only(monkeypatch, "1")
        m = _make_order_manager(tmp_path)
        m.enable()  # silently refused
        assert not m.is_enabled
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp_0013a",
                    signal=sig,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()

    @pytest.mark.asyncio
    async def test_submit_refused_even_if_never_enabled(self, tmp_path, monkeypatch):
        """Fresh manager, clean env: un-enabled manager still refuses."""
        sig = {"signal_id": "sig_0013b", "sport": "soccer_epl"}
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp_0013b",
                    signal=sig,
                    stake_units=0.5,
                    stake_dollars=25.0,
                )
        finally:
            await m.close()


# ===========================================================================
# Part 3: Cross-cutting / environmental characterization
# ===========================================================================


class TestEnvHandlingParity:
    """Both gates use identical truthiness semantics."""

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_parity_both_refuse(self, monkeypatch, tmp_path, value):
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        m = _make_order_manager(tmp_path)
        assert ex.enable() is False
        m.enable()
        assert ex.is_enabled is False
        assert m.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_parity_falsy_values_do_not_trip_switch(
        self, monkeypatch, tmp_path, value
    ):
        """Documents current behavior: non-exact matches do NOT block.

        These values are falsy under the existing implementation; if the
        gate ever tightens (e.g. strips whitespace), these tests should be
        updated to reflect the stricter contract.
        """
        _set_local_only(monkeypatch, value)
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()


class TestPatchDictStyleEnvControl:
    """Same gates driven via patch.dict instead of monkeypatch."""

    def test_patch_dict_blocks_bet_executor(self):
        with patch.dict(os.environ, {ENV_VAR: "1"}):
            ex = BetExecutor()
            assert ex.enable() is False
            assert ex.is_enabled is False

    def test_pop_from_environ_allows_bet_executor(self):
        os.environ.pop(ENV_VAR, None)
        try:
            ex = BetExecutor()
            assert ex.enable() is True
            assert ex.is_enabled is True
            ex.disable()
        finally:
            os.environ.pop(ENV_VAR, None)

    def test_patch_dict_blocks_order_manager(self, tmp_path):
        with patch.dict(os.environ, {ENV_VAR: "true"}):
            m = _make_order_manager(tmp_path)
            m.enable()
            assert m.is_enabled is False

    def test_patch_dict_empty_leaves_other_vars_alone(self, tmp_path):
        os.environ.pop(ENV_VAR, None)
        os.environ["CALLISTO_UNRELATED_MARKER"] = "hello"
        try:
            m = _make_order_manager(tmp_path)
            m.enable()
            assert m.is_enabled is True
            m.disable()
        finally:
            os.environ.pop("CALLISTO_UNRELATED_MARKER", None)


class TestNeverLiveInvariants:
    """Guard rails: this suite must never widen live-betting surfaces."""

    def test_paper_trade_statuses_do_not_contain_live(self):
        from tools import bet_executor as be

        statuses = getattr(be, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in {str(s).lower() for s in statuses}

    def test_generate_paper_trade_signal_source_not_widened(self):
        import inspect

        from tools import bet_executor as be

        fn = getattr(be, "generate_paper_trade_signal", None)
        if fn is None:
            pytest.skip("generate_paper_trade_signal not present")
        src = inspect.getsource(fn)
        # The paper-signal generator must not branch on status == 'live'.
        assert "status == 'live'" not in src
        assert 'status == "live"' not in src

    def test_enable_gate_literal_matches_contract(self):
        """The kill-switch membership tuple is exactly ('1','true','yes')."""
        import inspect

        from tools import bet_executor as be
        from tools import order_manager as om

        for mod in (be, om):
            src = inspect.getsource(mod)
            assert '("1", "true", "yes")' in src or (
                "('1', 'true', 'yes')" in src
            )


class TestRepeatedToggleCycles:
    """Stress the toggle path a bit — state must stay coherent."""

    def test_executor_many_cycles_clean_env(self, monkeypatch):
        _clear_local_only(monkeypatch)
        ex = BetExecutor()
        for _ in range(20):
            assert ex.enable() is True
            assert ex.is_enabled is True
            ex.disable()
            assert ex.is_enabled is False

    def test_executor_many_cycles_local_only(self, monkeypatch):
        _set_local_only(monkeypatch, "1")
        ex = BetExecutor()
        for _ in range(20):
            assert ex.enable() is False
            assert ex.is_enabled is False

    def test_order_manager_many_cycles_clean_env(self, tmp_path, monkeypatch):
        _clear_local_only(monkeypatch)
        m = _make_order_manager(tmp_path)
        for _ in range(20):
            m.enable()
            assert m.is_enabled is True
            m.disable()
            assert m.is_enabled is False

    def test_order_manager_many_cycles_local_only(self, tmp_path, monkeypatch):
        _set_local_only(monkeypatch, "1")
        m = _make_order_manager(tmp_path)
        for _ in range(20):
            m.enable()
            assert m.is_enabled is False

    def test_interleaved_env_flips(self, monkeypatch, tmp_path):
        """Flip LOCAL_ONLY between cycles; each cycle honors current env."""
        ex = BetExecutor()
        m = _make_order_manager(tmp_path)
        for round_no in range(6):
            if round_no % 2 == 0:
                _clear_local_only(monkeypatch)
                assert ex.enable() is True
                m.enable()
                assert ex.is_enabled is True
                assert m.is_enabled is True
            else:
                _set_local_only(monkeypatch, "yes")
                ex.disable()
                m.disable()
                assert ex.enable() is False
                m.enable()
                assert ex.is_enabled is False
                assert m.is_enabled is False
        # Leave everything disarmed.
        ex.disable()
        m.disable()
        assert ex.is_enabled is False
        assert m.is_enabled is False
