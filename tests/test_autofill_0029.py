"""autofill characterization #0029 — LOCAL_ONLY money kill switch.

Characterizes the ``CALLISTO_LOCAL_ONLY`` nuclear kill switch as it guards
the two money-arming entry points:

* ``tools.bet_executor.BetExecutor.enable``
* ``tools.order_manager.OrderManager.enable``

Contract under characterization
-------------------------------
1. Both ``enable()`` methods check the environment variable BEFORE setting
   ``_enabled = True`` — a truthy value must leave the component disabled.
2. Truthy values are exactly ``"1"``, ``"true"``, ``"yes"`` (case-insensitive,
   compared against ``str.lower()``); everything else falls through and arms.
3. Refusal is silent-with-respect-to-state: no exception is raised, the return
   value is ``False``, and callers that ignore the return value still end up
   with a disabled component (fail-closed).
4. The default (variable unset) remains armable — this is an opt-out switch,
   not a default kill.
5. Repeated / interleaved enable/disable cycles behave deterministically.

Safety rules honored by this module:
- No live betting is ever armed here. Tests never set CALLISTO_LOCAL_ONLY to
  make real money moves; they only exercise in-process state flags.
- BetExecutor instances are constructed WITHOUT ``initialize()`` — no browser,
  no DB, no network. Only enable/disable/is_enabled/status-free surface.
- OrderManager instances use a throwaway sqlite file under tmp_path.
"""

from __future__ import annotations

import os
import re
import inspect
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

# Values the production gate treats as TRUTHY (after .lower()).
TRUTHY_VALUES = ["1", "true", "yes"]

# Case permutations that must still be refused (gate lowercases the env).
TRUTHY_CASE_VARIANTS = [
    "1", "TRUE", "True", "tRuE",
    "YES", "yes", "Yes", "yEs",
]

# Values that are NOT in ("1", "true", "yes") and therefore fall through —
# these characterize current behavior (strict membership, not bool()-style).
FALL_THROUGH_VALUES = ["0", "false", "no", "", " ", "off", "enabled=1", "01", "y"]

ENV_VAR = "CALLISTO_LOCAL_ONLY"


class MockSender:
    """No-op async telegram sender for OrderManager."""

    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, msg: str):
        self.calls.append(msg)


def _make_manager(tmp_path) -> OrderManager:
    return OrderManager(
        db_path=str(tmp_path / f"om_{abs(hash(tmp_path)) % 10**8}.db"),
        telegram_sender=MockSender(),
    )


def _make_executor() -> BetExecutor:
    # Deliberately NOT initialized: no browser, no DB connection.
    return BetExecutor()


def _clear_env(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


# ===========================================================================
# Part 1 — BetExecutor.enable() refuses under CALLISTO_LOCAL_ONLY
# ===========================================================================


class TestBetExecutorDefaultArmable:
    """The kill switch is opt-in: unset env keeps historical arming behavior."""

    def test_default_env_allows_enable(self, monkeypatch):
        _clear_env(monkeypatch)
        ex = _make_executor()
        assert ex.is_enabled is False  # fail-closed constructor
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_disable_after_enable(self, monkeypatch):
        _clear_env(monkeypatch)
        ex = _make_executor()
        assert ex.enable() is True
        ex.disable()
        assert ex.is_enabled is False

    def test_constructor_never_self_arms(self, monkeypatch):
        _clear_env(monkeypatch)
        ex = _make_executor()
        assert ex.is_enabled is False
        assert ex._enabled is False


class TestBetExecutorLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_canonical_truthy_refuses(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        ex = _make_executor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_CASE_VARIANTS)
    def test_case_variants_refuse(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        ex = _make_executor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_refusal_is_silent_no_exception(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "true")
        ex = _make_executor()
        # Callers that ignore the boolean still get a disabled executor.
        ex.enable()
        assert ex.is_enabled is False

    def test_repeated_refusal_stays_disabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "1")
        ex = _make_executor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_refusal_before_state_mutation(self, monkeypatch, caplog):
        """Refusal must happen BEFORE any attempt to set _enabled True."""
        monkeypatch.setenv(ENV_VAR, "1")
        ex = _make_executor()
        before = getattr(ex, "_enabled")
        with caplog.at_level("WARNING"):
            result = ex.enable()
        assert result is False
        assert getattr(ex, "_enabled") == before == False  # noqa: E712

    def test_warning_logged_on_refusal(self, monkeypatch, caplog):
        monkeypatch.setenv(ENV_VAR, "1")
        ex = _make_executor()
        with caplog.at_level("WARNING"):
            ex.enable()
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "CALLISTO_LOCAL_ONLY" in joined

    def test_env_set_after_construction_still_blocks(self, monkeypatch):
        """The gate reads the env at enable() time, not construction time."""
        _clear_env(monkeypatch)
        ex = _make_executor()
        assert ex.is_enabled is False
        monkeypatch.setenv(ENV_VAR, "1")
        assert ex.enable() is False
        assert ex.is_enabled is False


@pytest.mark.parametrize("value", FALL_THROUGH_VALUES)
def test_betexec_non_gate_values_fall_through(monkeypatch, value):
    """Characterization: only exact '1'/'true'/'yes' (case-insensitive) block."""
    monkeypatch.setenv(ENV_VAR, value)
    ex = _make_executor()
    assert ex.enable() is True
    assert ex.is_enabled is True


# ===========================================================================
# Part 2 — OrderManager.enable() refuses under CALLISTO_LOCAL_ONLY
# ===========================================================================


class TestOrderManagerDefaultArmable:
    def test_default_env_allows_enable(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        m = _make_manager(tmp_path)
        assert m.is_enabled is False
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    def test_disable_cycle(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        m = _make_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False


class TestOrderManagerLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_canonical_truthy_refuses(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        m = _make_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is False
        if result is not None:
            assert result is False

    @pytest.mark.parametrize("value", TRUTHY_CASE_VARIANTS)
    def test_case_variants_refuse(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        m = _make_manager(tmp_path)
        m.enable()
        assert m.is_enabled is False

    def test_repeated_refusal_stays_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "yes")
        m = _make_manager(tmp_path)
        for _ in range(5):
            m.enable()
        assert m.is_enabled is False

    def test_warning_logged_on_refusal(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv(ENV_VAR, "1")
        m = _make_manager(tmp_path)
        with caplog.at_level("WARNING"):
            m.enable()
        joined = "\n".join(r.getMessage() for r in caplog.records)
        assert "CALLISTO_LOCAL_ONLY" in joined

    def test_no_telegram_dispatch_on_refused_enable(self, tmp_path, monkeypatch):
        """enable() refusal happens before any approval flow could start."""
        monkeypatch.setenv(ENV_VAR, "1")
        sender = MockSender()
        m = OrderManager(db_path=str(tmp_path / "om_tg.db"), telegram_sender=sender)
        m.enable()
        assert sender.calls == []

    def test_env_set_after_construction_still_blocks(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        m = _make_manager(tmp_path)
        monkeypatch.setenv(ENV_VAR, "true")
        assert m.enable() is False
        assert m.is_enabled is False


@pytest.mark.parametrize("value", FALL_THROUGH_VALUES)
def test_ordermgr_non_gate_values_fall_through(tmp_path, monkeypatch, value):
    monkeypatch.setenv(ENV_VAR, value)
    m = _make_manager(tmp_path)
    result = m.enable()
    assert m.is_enabled is True
    if result is not None:
        assert result is True


# ===========================================================================
# Part 3 — Interleaved cycles & cross-component symmetry
# ===========================================================================


class TestCyclesAndSymmetry:
    def test_betexec_disable_then_local_only_enable_refused(self, monkeypatch):
        _clear_env(monkeypatch)
        ex = _make_executor()
        ex.enable()
        ex.disable()
        monkeypatch.setenv(ENV_VAR, "1")
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_ordermgr_disable_then_local_only_enable_refused(
        self, tmp_path, monkeypatch
    ):
        _clear_env(monkeypatch)
        m = _make_manager(tmp_path)
        m.enable()
        m.disable()
        monkeypatch.setenv(ENV_VAR, "yes")
        assert m.enable() is False
        assert m.is_enabled is False

    def test_both_components_block_under_same_env(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv(ENV_VAR, "true")
        ex = _make_executor()
        m = _make_manager(tmp_path)
        assert ex.enable() is False
        assert m.enable() is False
        assert ex.is_enabled is False
        assert m.is_enabled is False

    def test_patch_dict_environment_isolation(self, tmp_path):
        """Same contract holds via patch.dict instead of monkeypatch."""
        before = os.environ.get(ENV_VAR)
        with patch.dict(os.environ, {ENV_VAR: "1"}):
            ex = _make_executor()
            m = _make_manager(tmp_path)
            assert ex.enable() is False
            assert m.enable() is False
            assert not ex.is_enabled
            assert not m.is_enabled
        # patch.dict restores the prior value (including a leaked "1" from
        # another test module that set the env without monkeypatch).
        assert os.environ.get(ENV_VAR) == before


# ===========================================================================
# Part 4 — Source-level characterization of the gate ordering
# ===========================================================================


class TestSourceGateOrdering:
    """The guard must lexically precede ``self._enabled = True``."""

    def _source(self, cls, method: str) -> str:
        return inspect.getsource(getattr(cls, method))

    def test_betexec_guard_precedes_arm(self):
        src = self._source(BetExecutor, "enable")
        guard_pos = src.find('os.getenv("CALLISTO_LOCAL_ONLY"')
        arm_pos = src.find("self._enabled = True")
        assert guard_pos != -1, "BetExecutor.enable lost its LOCAL_ONLY guard"
        assert arm_pos != -1
        assert guard_pos < arm_pos

    def test_ordermgr_guard_precedes_arm(self):
        src = self._source(OrderManager, "enable")
        guard_pos = src.find('os.getenv("CALLISTO_LOCAL_ONLY"')
        arm_pos = src.find("self._enabled = True")
        assert guard_pos != -1, "OrderManager.enable lost its LOCAL_ONLY guard"
        assert arm_pos != -1
        assert guard_pos < arm_pos

    def test_betexec_returns_false_on_guard_branch(self):
        src = self._source(BetExecutor, "enable")
        guard = src[src.find("if os.getenv"):src.find("self._enabled = True")]
        assert "return False" in guard

    def test_ordermgr_returns_false_on_guard_branch(self):
        src = self._source(OrderManager, "enable")
        guard = src[src.find("if os.getenv"):src.find("self._enabled = True")]
        assert "return False" in guard

    @pytest.mark.parametrize("cls", [BetExecutor, OrderManager])
    def test_gate_membership_exact(self, cls):
        """The gate compares against exactly ('1', 'true', 'yes') lowercased."""
        src = inspect.getsource(cls.enable)
        match = re.search(r'\(\s*"1"\s*,\s*"true"\s*,\s*"yes"\s*\)', src)
        assert match is not None, (
            f"{cls.__name__}.enable gate tuple changed — "
            "this characterization expects ('1','true','yes')"
        )

    @pytest.mark.parametrize("cls", [BetExecutor, OrderManager])
    def test_lower_normalization_present(self, cls):
        src = inspect.getsource(cls.enable)
        assert '.lower()' in src, (
            f"{cls.__name__}.enable no longer lowercases the env value"
        )


# ===========================================================================
# Part 5 — Fail-closed posture around the disabled state
# ===========================================================================


class TestFailClosedPosture:
    def test_ordermgr_submit_refuses_while_local_only(self, tmp_path, monkeypatch):
        """Even if some caller thinks it enabled the manager, orders refuse."""
        monkeypatch.setenv(ENV_VAR, "1")

        import asyncio

        async def scenario():
            m = _make_manager(tmp_path)
            m.enable()  # refused, stays disabled
            assert not m.is_enabled
            await m.initialize()
            try:
                with pytest.raises(RuntimeError, match="disabled"):
                    await m.submit_order(
                        hypothesis_id="hyp_0029",
                        signal={"signal_id": "sig_0029", "sport": "baseball_mlb"},
                        stake_units=1.0,
                        stake_dollars=100.0,
                    )
            finally:
                await m.close()

        asyncio.run(scenario())

    def test_betexec_preflight_refuses_while_disabled(self, monkeypatch):
        """preflight_check fails closed on a disabled executor, before any
        browser/bankroll work — the disabled gate is the FIRST check."""
        monkeypatch.setenv(ENV_VAR, "1")

        import asyncio

        async def scenario():
            ex = _make_executor()
            ex.enable()  # refused
            assert not ex.is_enabled
            ok, reason = await ex.preflight_check(
                sport="baseball_mlb", odds=-150, edge=0.5, stake=1.0,
            )
            assert ok is False
            assert "disabled" in reason.lower()

        asyncio.run(scenario())

    def test_betexec_preflight_source_orders_disabled_gate_first(self):
        """Characterization: the _enabled check is the first safety check in
        preflight_check — nothing (edge, bankroll) is evaluated before it."""
        src = inspect.getsource(BetExecutor.preflight_check)
        enabled_pos = src.find("if not self._enabled")
        assert enabled_pos != -1
        body = src[:enabled_pos]
        assert "get_bankroll" not in body
        assert "MIN_EDGE" not in body

    def test_shutdown_resets_enabled_flag(self, monkeypatch):
        """shutdown() forces _enabled False regardless of prior state."""
        _clear_env(monkeypatch)

        import asyncio

        async def scenario():
            ex = _make_executor()
            ex.enable()
            assert ex.is_enabled is True
            await ex.shutdown()
            assert ex.is_enabled is False

        asyncio.run(scenario())


# ===========================================================================
# Part 6 — Live-betting safety invariants (regression tripwires)
# ===========================================================================


class TestLiveBettingInvariants:
    """Guard rails required by the task: nothing here may arm live betting."""

    def test_paper_trade_statuses_exclude_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        statuses = _PAPER_TRADE_SIGNAL_STATUSES
        assert statuses is not None, "_PAPER_TRADE_SIGNAL_STATUSES disappeared"
        assert "live" not in statuses
        assert set(statuses) == {"paper_trading"}

    @pytest.mark.parametrize("bad_status", ["live", "LIVE", "Live", "", None])
    def test_reject_non_paper_blocks_live(self, bad_status):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper(bad_status) is True

    def test_reject_non_paper_allows_paper_trading_only(self):
        from tools.signals.paper import allowed_paper_statuses, reject_non_paper

        assert reject_non_paper("paper_trading") is False
        assert allowed_paper_statuses() == frozenset({"paper_trading"})
