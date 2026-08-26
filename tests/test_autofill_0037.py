"""autofill #0037 — LOCAL_ONLY money kill switch characterization.

Characterizes the contract that ``CALLISTO_LOCAL_ONLY`` (the appliance-wide
nuclear switch) must block arming of BOTH money-touching components:

  * ``tools.bet_executor.BetExecutor.enable``
  * ``tools.order_manager.OrderManager.enable``

The invariant under test: when CALLISTO_LOCAL_ONLY is truthy, ``enable()``
returns False and leaves ``_enabled``/``is_enabled`` False — the flag is
checked BEFORE ``_enabled = True`` is ever assigned, so no window exists in
which a live bet could be placed.

Safety rails asserted here (fail-closed, never arm live):
  * default construction is disabled for both classes
  * truthy spellings ("1", "true", "yes", any case) refuse to enable
  * falsy / absent env still permits explicit arming (documented behavior)
  * refusal never raises even when the return value is ignored
  * disable() after a refused enable() keeps the component disarmed
  * re-enable attempts under LOCAL_ONLY stay refused
  * submit paths stay blocked while LOCAL_ONLY is set
  * paper-trade signal statuses are untouched by these tests — this module
    never adds "live" to _PAPER_TRADE_SIGNAL_STATUSES and never widens
    generate_paper_trade_signal to status=='live'.

Tests-only module: no production gate is modified.
"""

import asyncio
import os
from unittest.mock import patch

import pytest

import tools.bet_executor as bet_executor_mod
import tools.order_manager as order_manager_mod
from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Environment spellings
# ---------------------------------------------------------------------------

TRUTHY_VALUES = [
    "1",
    "true",
    "True",
    "TRUE",
    "yes",
    "Yes",
    "YES",
]

FALSY_VALUES = [
    "",
    "0",
    "false",
    "False",
    "no",
    "No",
    "off",
    "disabled",
]


class MockSender:
    """Minimal stand-in for the Telegram sender OrderManager accepts."""

    def __init__(self):
        self.sent = []

    async def __call__(self, msg):
        self.sent.append(msg)


def _make_executor():
    """BetExecutor without initialize(): no browser, no DB, no network."""
    return BetExecutor()


def _make_manager(tmp_path):
    return OrderManager(
        db_path=str(tmp_path / f"om_{id(tmp_path)}.db"),
        telegram_sender=MockSender(),
    )


def _clear_local_only():
    os.environ.pop("CALLISTO_LOCAL_ONLY", None)


def _set_local_only(value="1"):
    os.environ["CALLISTO_LOCAL_ONLY"] = value


# ---------------------------------------------------------------------------
# BetExecutor: default state & basic lifecycle (LOCAL_ONLY unset)
# ---------------------------------------------------------------------------


class TestBetExecutorDefaults:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    def test_default_construction_is_disabled(self):
        ex = _make_executor()
        assert ex.is_enabled is False
        assert ex._enabled is False

    def test_enable_without_local_only_arms(self):
        ex = _make_executor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_disable_disarms_after_enable(self):
        ex = _make_executor()
        ex.enable()
        ex.disable()
        assert ex.is_enabled is False

    def test_double_enable_is_idempotent(self):
        ex = _make_executor()
        assert ex.enable() is True
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_disable_when_already_disabled_is_safe(self):
        ex = _make_executor()
        ex.disable()
        assert ex.is_enabled is False

    def test_falsy_env_still_permits_arm(self):
        for value in FALSY_VALUES:
            _set_local_only(value)
            try:
                ex = _make_executor()
                assert ex.enable() is True, f"env={value!r} should not block"
                assert ex.is_enabled is True
            finally:
                _clear_local_only()


# ---------------------------------------------------------------------------
# BetExecutor: LOCAL_ONLY refusal matrix
# ---------------------------------------------------------------------------


class TestBetExecutorLocalOnlyRefusal:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, value):
        _set_local_only(value)
        ex = _make_executor()
        assert ex.enable() is False
        assert ex.is_enabled is False
        assert ex._enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refusal_leaves_no_enabled_window(self, value):
        """_enabled must never be assigned True during a refused enable()."""
        _set_local_only(value)
        ex = _make_executor()
        observed = []

        original_setattr = BetExecutor.__setattr__

        def spy(obj, name, val):
            if name == "_enabled":
                observed.append(val)
            return original_setattr(obj, name, val)

        with patch.object(BetExecutor, "__setattr__", spy):
            result = ex.enable()

        assert result is False
        assert True not in observed, (
            f"_enabled was assigned True under LOCAL_ONLY: {observed}"
        )

    def test_refusal_does_not_raise_when_return_ignored(self):
        _set_local_only("1")
        ex = _make_executor()
        ex.enable()  # callers may ignore the bool; refusal stays silent
        assert ex.is_enabled is False

    def test_repeated_enable_attempts_stay_refused(self):
        _set_local_only("1")
        ex = _make_executor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_refusal_survives_disable_round_trip(self):
        _set_local_only("1")
        ex = _make_executor()
        ex.enable()
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_clearing_env_allows_later_arm(self):
        """Documented flip side: once LOCAL_ONLY is cleared, arm succeeds."""
        _set_local_only("1")
        ex = _make_executor()
        assert ex.enable() is False
        _clear_local_only()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_whitespace_value_is_not_truthy(self):
        """' ' fails .lower() membership — characterized as permissive."""
        _set_local_only(" ")
        ex = _make_executor()
        assert ex.enable() is True

    def test_status_payload_reports_disabled_under_local_only(self):
        _set_local_only("1")
        ex = _make_executor()
        ex.enable()
        assert ex._enabled is False


# ---------------------------------------------------------------------------
# OrderManager: default state & basic lifecycle (LOCAL_ONLY unset)
# ---------------------------------------------------------------------------


class TestOrderManagerDefaults:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    def test_default_construction_is_disabled(self, tmp_path):
        m = _make_manager(tmp_path)
        assert m.is_enabled is False
        assert m._enabled is False

    def test_enable_without_local_only_arms(self, tmp_path):
        m = _make_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    def test_disable_disarms_after_enable(self, tmp_path):
        m = _make_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False

    def test_double_enable_is_idempotent(self, tmp_path):
        m = _make_manager(tmp_path)
        first = m.enable()
        second = m.enable()
        assert m.is_enabled is True
        if first is not None and second is not None:
            assert first is True
            assert second is True

    def test_falsy_env_still_permits_arm(self, tmp_path):
        for value in FALSY_VALUES:
            _set_local_only(value)
            try:
                m = _make_manager(tmp_path)
                assert m.enable() is not False or m.is_enabled is True
                assert m.is_enabled is True
            finally:
                _clear_local_only()


# ---------------------------------------------------------------------------
# OrderManager: LOCAL_ONLY refusal matrix
# ---------------------------------------------------------------------------


class TestOrderManagerLocalOnlyRefusal:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, tmp_path, value):
        _set_local_only(value)
        m = _make_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is False
        assert m._enabled is False
        if result is not None:
            assert result is False

    def test_refusal_does_not_raise_when_return_ignored(self, tmp_path):
        _set_local_only("1")
        m = _make_manager(tmp_path)
        m.enable()  # legacy callers ignore the return value
        assert m.is_enabled is False

    def test_repeated_enable_attempts_stay_refused(self, tmp_path):
        _set_local_only("1")
        m = _make_manager(tmp_path)
        for _ in range(5):
            m.enable()
        assert m.is_enabled is False

    def test_refusal_survives_disable_round_trip(self, tmp_path):
        _set_local_only("1")
        m = _make_manager(tmp_path)
        m.enable()
        m.disable()
        m.enable()
        assert m.is_enabled is False

    def test_clearing_env_allows_later_arm(self, tmp_path):
        _set_local_only("true")
        m = _make_manager(tmp_path)
        m.enable()
        assert m.is_enabled is False
        _clear_local_only()
        m.enable()
        assert m.is_enabled is True

    def test_both_components_refuse_together(self, tmp_path):
        """The nuclear switch blocks BOTH money gates simultaneously."""
        _set_local_only("1")
        ex = _make_executor()
        m = _make_manager(tmp_path)
        ex.enable()
        m.enable()
        assert ex.is_enabled is False
        assert m.is_enabled is False


# ---------------------------------------------------------------------------
# Cross-component invariants
# ---------------------------------------------------------------------------


class TestCrossComponentInvariants:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    def test_modules_read_env_at_call_time_not_import_time(self):
        """Flipping the env after import must still take effect."""
        _set_local_only("1")
        ex = _make_executor()
        assert ex.enable() is False
        _clear_local_only()
        ex2 = _make_executor()
        assert ex2.enable() is True

    def test_gates_exist_on_both_classes(self):
        assert callable(BetExecutor.enable)
        assert callable(OrderManager.enable)

    def test_source_checks_env_before_assignment_bet_executor(self):
        import inspect

        src = inspect.getsource(BetExecutor.enable)
        env_check_pos = src.find('os.getenv("CALLISTO_LOCAL_ONLY"')
        assign_pos = src.find("self._enabled = True")
        assert env_check_pos != -1, "gate check missing from BetExecutor.enable"
        assert assign_pos != -1
        assert env_check_pos < assign_pos, (
            "BetExecutor.enable must check CALLISTO_LOCAL_ONLY "
            "BEFORE assigning _enabled = True"
        )

    def test_source_checks_env_before_assignment_order_manager(self):
        import inspect

        src = inspect.getsource(OrderManager.enable)
        env_check_pos = src.find('os.getenv("CALLISTO_LOCAL_ONLY"')
        assign_pos = src.find("self._enabled = True")
        assert env_check_pos != -1, "gate check missing from OrderManager.enable"
        assert assign_pos != -1
        assert env_check_pos < assign_pos, (
            "OrderManager.enable must check CALLISTO_LOCAL_ONLY "
            "BEFORE assigning _enabled = True"
        )


# ---------------------------------------------------------------------------
# Submission-path fail-closed behavior under LOCAL_ONLY
# ---------------------------------------------------------------------------


class TestSubmissionPathsStayBlocked:
    def setup_method(self):
        _clear_local_only()

    def teardown_method(self):
        _clear_local_only()

    @pytest.mark.asyncio
    async def test_order_manager_submit_refused_under_local_only(self, tmp_path):
        sig = {"signal_id": "sig_0037", "sport": "baseball_mlb"}
        _set_local_only("1")
        m = _make_manager(tmp_path)
        m.enable()
        assert not m.is_enabled
        await m.initialize()
        try:
            with pytest.raises(RuntimeError):
                await m.submit_order(
                    hypothesis_id="hyp_0037",
                    signal=sig,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()

    @pytest.mark.asyncio
    async def test_submit_refused_after_enable_was_refused(self, tmp_path):
        """Full chain under LOCAL_ONLY: refused enable() -> blocked submit.

        Characterized guard shape: submit_order checks ``_enabled`` only;
        the LOCAL_ONLY enforcement point is enable(), which is why the gate
        must run BEFORE assigning ``_enabled = True``.
        """
        sig = {"signal_id": "sig_chain", "sport": "baseball_mlb"}
        _set_local_only("1")
        m = _make_manager(tmp_path)
        assert m.enable() is False
        await m.initialize()
        try:
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp_chain",
                    signal=sig,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()


# ---------------------------------------------------------------------------
# Safety-rail sanity: paper-trade statuses untouched
# ---------------------------------------------------------------------------


class TestPaperTradeRailsUntouched:
    """The paper-signal hard gate stays paper-only; this test module never
    widens it and asserts "live" is absent."""

    def test_statuses_do_not_include_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_only_paper_trading_allowed(self):
        from tools.signals.paper import allowed_paper_statuses

        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_reject_non_paper_flags_live(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("live") is True
        assert reject_non_paper("paper_trading") is False
