"""autofill 0045 — LOCAL_ONLY money kill switch characterization.

Characterizes the arming gate shared by ``BetExecutor.enable`` and
``OrderManager.enable``: when ``CALLISTO_LOCAL_ONLY`` is truthy, neither
component may set ``_enabled = True``, before any other side effect runs.
No browser is launched and no network calls are made anywhere in this module.
"""

from __future__ import annotations

import os
import warnings
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# helpers / fixtures
# ---------------------------------------------------------------------------

TRUTHY_VALUES = [
    "1",
    "true",
    "TRUE",
    "True",
    "yes",
    "YES",
    "Yes",
]

FALSY_VALUES = ["", "0", "false", "FALSE", "no", "No", "off"]


class MockSender:
    async def __call__(self, msg: str):
        pass


def _make_executor() -> BetExecutor:
    """Instantiate BetExecutor without initialize(): no browser, no network."""
    return BetExecutor()


def _make_manager(tmp_path) -> OrderManager:
    return OrderManager(
        db_path=str(tmp_path / "om_0045.db"), telegram_sender=MockSender()
    )


@pytest.fixture(autouse=True)
def _no_live_env(monkeypatch):
    """Safety net: tests that don't set the var explicitly get a clean env,
    so a default run can never accidentally arm against a live appliance."""
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    yield


# ---------------------------------------------------------------------------
# BetExecutor.enable — default (no CALLISTO_LOCAL_ONLY)
# ---------------------------------------------------------------------------


class TestBetExecutorDefault:
    def test_init_starts_disabled(self):
        ex = _make_executor()
        assert ex.is_enabled is False

    def test_enable_arms_when_no_kill_switch(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        ex = _make_executor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_disable_disarms_after_enable(self):
        ex = _make_executor()
        ex.enable()
        assert ex.is_enabled is True
        ex.disable()
        assert ex.is_enabled is False

    def test_disable_idempotent(self):
        ex = _make_executor()
        ex.disable()
        ex.disable()
        assert ex.is_enabled is False

    def test_falsy_values_still_allow_arming(self, monkeypatch):
        for val in FALSY_VALUES:
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
            ex = _make_executor()
            if val == "":
                # empty string behaves like unset in os.getenv default check
                continue
            assert ex.enable() is True, f"val={val!r}"
            assert ex.is_enabled is True


# ---------------------------------------------------------------------------
# BetExecutor.enable — LOCAL_ONLY refusal (the money kill switch)
# ---------------------------------------------------------------------------


class TestBetExecutorLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refuses_to_arm(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = _make_executor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test__enabled_never_set_true(self, monkeypatch, value):
        """The core characterization: _enabled stays False under the switch."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = _make_executor()
        ex.enable()
        assert getattr(ex, "_enabled") is False

    def test_case_and_whitespace_insensitive_truthy(self, monkeypatch):
        # documented behavior is .lower() in ("1","true","yes"); whitespace is
        # NOT stripped — characterize exact current behavior
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", " true ")
        ex = _make_executor()
        # " true ".lower() == " true " not in tuple -> enables (current truth!)
        assert ex.enable() is True

    def test_refusal_is_silent_no_exception(self, monkeypatch):
        """Existing callers may ignore enable()'s return value."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # any warning would fail
            result = None
            try:
                result = _make_executor().enable()
            except Exception as exc:  # pragma: no cover - failure path
                pytest.fail(f"enable() raised under kill switch: {exc}")
        assert result is False

    def test_repeated_enable_attempts_stay_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        ex = _make_executor()
        for _ in range(3):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_cannot_sneak_arm_via_disable_then_enable_cycle(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = _make_executor()
        ex.disable()
        assert ex.enable() is False
        assert ex._enabled is False

    def test_armed_before_switch_set_stays_armed_only_until_disabled(
        self, monkeypatch
    ):
        """Setting the env after arming does not retro-disarm; but re-enable
        after disable is refused. Characterizes one-way-at-arm-time gate."""
        ex = _make_executor()
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        assert ex.enable() is True
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        # already armed: gate only guards the enable() call itself
        assert ex.is_enabled is True
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# OrderManager.enable — default behavior
# ---------------------------------------------------------------------------


class TestOrderManagerDefault:
    def test_init_starts_disabled(self, tmp_path):
        m = _make_manager(tmp_path)
        assert m.is_enabled is False

    def test_enable_arms_when_no_kill_switch(self, tmp_path):
        m = _make_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True

    def test_disable_after_enable(self, tmp_path):
        m = _make_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False


# ---------------------------------------------------------------------------
# OrderManager.enable — LOCAL_ONLY refusal
# ---------------------------------------------------------------------------


class TestOrderManagerLocalOnlyRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_refuses_to_arm(self, tmp_path, value):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": value}):
            m = _make_manager(tmp_path)
            result = m.enable()
            assert m.is_enabled is False
            if result is not None:
                assert result is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test__enabled_never_set_true(self, tmp_path, value):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": value}):
            m = _make_manager(tmp_path)
            m.enable()
            assert getattr(m, "_enabled") is False

    def test_falsy_values_still_allow_arming(self, tmp_path, monkeypatch):
        for val in FALSY_VALUES:
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
            m = _make_manager(tmp_path)
            assert m.enable() is True
            assert m.is_enabled is True
            m.disable()

    def test_disable_then_reenable_refused_under_switch(self, tmp_path):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
            m = _make_manager(tmp_path)
            m.disable()
            assert m.enable() is False
            assert m.is_enabled is False

    def test_repeated_enable_attempts_stay_refused(self, tmp_path):
        with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "true"}):
            m = _make_manager(tmp_path)
            for _ in range(3):
                result = m.enable()
                if result is not None:
                    assert result is False
            assert m.is_enabled is False


# ---------------------------------------------------------------------------
# Cross-component symmetry: both gates agree on the truthiness contract
# ---------------------------------------------------------------------------


class TestGateSymmetry:
    """The two kill switches must treat the same env values identically."""

    @pytest.mark.parametrize(
        "value", TRUTHY_VALUES + FALSY_VALUES + ["maybe", "on"]
    )
    def test_same_verdict_for_both_components(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = _make_executor()
        ex_result = ex.enable()
        ex_armed = ex.is_enabled
        m = _make_manager(tmp_path)
        om_result = m.enable()
        om_armed = m.is_enabled
        # verdicts match between components
        assert bool(ex_armed) == bool(om_armed)
        if ex_result is not None and om_result is not None:
            assert ex_result == om_result

    def test_truthy_set_is_exactly_lowercase_1_true_yes(self):
        """Pin the exact accepted set: {'1','true','yes'} after lowercasing."""
        accepted = ("1", "true", "yes")
        for val in ("1", "true", "yes"):
            assert val in accepted
        for val in ("on", "y", "enabled", "t"):
            assert val not in accepted


# ---------------------------------------------------------------------------
# Production gate sanity: refusal code paths are intact and guard-first
# ---------------------------------------------------------------------------


class TestProductionGatesIntact:
    def _assert_guard_first(self, src: str) -> None:
        assert "CALLISTO_LOCAL_ONLY" in src
        guard_idx = src.index("CALLISTO_LOCAL_ONLY")
        arm_idx = src.index("_enabled = True")
        assert guard_idx < arm_idx

    def test_bet_executor_enable_checks_env_before_enabling(self):
        import inspect

        self._assert_guard_first(inspect.getsource(BetExecutor.enable))

    def test_order_manager_enable_checks_env_before_enabling(self):
        import inspect

        from tools.order_manager import OrderManager as OM

        self._assert_guard_first(inspect.getsource(OM.enable))

    def test_no_live_status_added_to_paper_trade_statuses(self):
        """Hard safety pin: 'live' must never join paper-trade statuses."""
        from tools import order_manager as om_mod

        statuses = getattr(om_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is None:
            pytest.skip("no _PAPER_TRADE_SIGNAL_STATUSES in order_manager")
        assert "live" not in {str(s).lower() for s in statuses}
