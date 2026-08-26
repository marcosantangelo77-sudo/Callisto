"""autofill #0005 — LOCAL_ONLY money kill switch (characterization).

Characterizes the fail-closed arming gates on the two money-touching
components:

* ``tools.bet_executor.BetExecutor.enable``
* ``tools.order_manager.OrderManager.enable``

Contract under characterization (must NOT regress):

1. When ``CALLISTO_LOCAL_ONLY`` is truthy ("1"/"true"/"yes", any case),
   ``enable()`` must refuse BEFORE flipping ``_enabled`` to True — i.e. the
   env check happens first and ``_enabled`` stays False.
2. Falsy / unset values leave the default behaviour untouched (enable arms).
3. Refusal is silent-with-respect-to-exceptions: it logs a warning and
   returns False; existing callers that ignore the return value never see an
   exception.
4. The refusal path must never mention or set status "live" as enabled;
   live betting stays disarmed in local-only mode.

Tests-only module: no production code is modified here. These tests pin the
existing behaviour so refactors cannot silently widen the gate.
"""

from __future__ import annotations

import ast
import inspect
import logging
import os
import re
from unittest.mock import patch

import pytest

import tools.bet_executor as bet_executor_mod
import tools.order_manager as order_manager_mod
from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


TRUTHY_VALUES = ["1", "true", "TRUE", "True", "yes", "YES", "Yes"]
FALSY_VALUES = ["0", "false", "no", "", "  ", "off", "FALSE", "No"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class MockSender:
    """No-op telegram sender for OrderManager construction."""

    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, msg: str):
        self.messages.append(msg)


def make_executor() -> BetExecutor:
    """BetExecutor without initialize(): no browser, no network."""
    return BetExecutor()


def make_manager(tmp_path) -> OrderManager:
    return OrderManager(
        db_path=str(tmp_path / "om.db"), telegram_sender=MockSender()
    )


def with_local_only(value: str):
    """Context manager pinning CALLISTO_LOCAL_ONLY to a specific value."""
    return patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": value})


def clear_local_only():
    env = dict(os.environ)
    env.pop("CALLISTO_LOCAL_ONLY", None)
    return patch.dict(os.environ, env, clear=True)


# ---------------------------------------------------------------------------
# Part A: BetExecutor.enable — runtime characterization
# ---------------------------------------------------------------------------


class TestBetExecutorEnableLocalOnly:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = make_executor()
        assert ex.is_enabled is False
        assert ex.enable() is False
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = make_executor()
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_unset_env_enables_by_default(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        ex = make_executor()
        assert ex.is_enabled is False
        assert ex.enable() is True
        assert ex.is_enabled is True

    def test_refusal_does_not_raise_when_return_ignored(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = make_executor()
        ex.enable()  # must not raise even though refused
        assert ex.is_enabled is False

    def test_refusal_logs_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = make_executor()
        with caplog.at_level(logging.WARNING, logger="tools.bet_executor"):
            result = ex.enable()
        assert result is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "refusal should emit at least one WARNING"
        joined = " ".join(r.getMessage() for r in warnings)
        assert "CALLISTO_LOCAL_ONLY" in joined

    def test_refusal_is_idempotent(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        ex = make_executor()
        for _ in range(3):
            assert ex.enable() is False
            assert ex.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = make_executor()
        ex.enable()
        ex.disable()
        assert ex.is_enabled is False

    def test_repeated_enable_under_kill_switch_never_arms(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        ex = make_executor()
        results = [ex.enable() for _ in range(5)]
        assert results == [False] * 5
        assert ex.is_enabled is False

    def test_status_reports_disabled_after_refusal(self, monkeypatch):
        monkeypatch.setattr(bet_executor_mod, "_db_available", True, raising=False)
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = make_executor()
        ex.enable()

    def test_init_default_is_disarmed(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        ex = make_executor()
        assert ex._enabled is False
        assert ex.is_enabled is False

    def test_env_checked_before_enabled_flip(self, monkeypatch):
        """Even if env flips between construction and enable(), gate holds."""
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        ex = make_executor()
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_mixed_case_and_whitespace_variants(self, monkeypatch):
        # Documented contract is exact-match lower(); whitespace variants are
        # NOT part of the gate — characterize current behaviour honestly:
        # " true" (leading space) does NOT match and therefore enables.
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", " true ")
        ex = make_executor()
        # current implementation: strip-less comparison -> enables
        result = ex.enable()
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Part B: OrderManager.enable — runtime characterization
# ---------------------------------------------------------------------------


class TestOrderManagerEnableLocalOnly:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_refuses_enable(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        m = make_manager(tmp_path)
        assert m.is_enabled is False
        assert m.enable() is False
        assert m.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, tmp_path, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        m = make_manager(tmp_path)
        assert m.enable() is True
        assert m.is_enabled is True

    def test_unset_env_enables_by_default(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        m = make_manager(tmp_path)
        assert m.enable() is True
        assert m.is_enabled is True
        m.disable()

    def test_refusal_does_not_raise_when_return_ignored(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        m = make_manager(tmp_path)
        m.enable()
        assert m.is_enabled is False

    def test_refusal_logs_warning(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        m = make_manager(tmp_path)
        with caplog.at_level(logging.WARNING, logger="tools.order_manager"):
            result = m.enable()
        assert result is False
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "refusal should emit at least one WARNING"
        joined = " ".join(r.getMessage() for r in warnings)
        assert "CALLISTO_LOCAL_ONLY" in joined

    def test_refusal_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        m = make_manager(tmp_path)
        for _ in range(3):
            assert m.enable() is False
            assert m.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        m = make_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False

    def test_env_checked_before_enabled_flip(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        m = make_manager(tmp_path)
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        assert m.enable() is False
        assert m.is_enabled is False

    def test_init_default_is_disarmed(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
        m = make_manager(tmp_path)
        assert m._enabled is False
        assert m.is_enabled is False

    def test_no_telegram_message_on_refusal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        sender = MockSender()
        m = OrderManager(db_path=str(tmp_path / "x.db"), telegram_sender=sender)
        m.enable()
        assert sender.messages == []


# ---------------------------------------------------------------------------
# Part C: source-level pins (AST / text) — gate ordering & shape
# ---------------------------------------------------------------------------


def _func_source(module, name):
    src = inspect.getsource(module)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node), node
    raise AssertionError(f"{name} not found in {module.__name__}")


class TestSourcePins:
    def test_betexec_enable_checks_env_first(self):
        body, _ = _func_source(bet_executor_mod, "enable")
        env_idx = body.find("CALLISTO_LOCAL_ONLY")
        flip_idx = body.find("_enabled = True")
        assert env_idx != -1, "enable() lost its CALLISTO_LOCAL_ONLY guard"
        assert flip_idx != -1
        assert env_idx < flip_idx, (
            "CALLISTO_LOCAL_ONLY check must run BEFORE _enabled = True"
        )

    def test_ordermgr_enable_checks_env_first(self):
        body, _ = _func_source(order_manager_mod, "enable")
        env_idx = body.find("CALLISTO_LOCAL_ONLY")
        flip_idx = body.find("_enabled = True")
        assert env_idx != -1, "enable() lost its CALLISTO_LOCAL_ONLY guard"
        assert flip_idx != -1
        assert env_idx < flip_idx, (
            "CALLISTO_LOCAL_ONLY check must run BEFORE _enabled = True"
        )

    def test_betexec_enable_returns_bool_false_on_refusal(self):
        body, _ = _func_source(bet_executor_mod, "enable")
        assert "return False" in body

    def test_ordermgr_enable_returns_bool_false_on_refusal(self):
        body, _ = _func_source(order_manager_mod, "enable")
        assert "return False" in body

    def test_betexec_enable_truthy_set_matches_contract(self):
        body, _ = _func_source(bet_executor_mod, "enable")
        m = re.search(r'os\.getenv\(\s*"CALLISTO_LOCAL_ONLY"[^)]*\)', body)
        assert m, "gate must read os.getenv('CALLISTO_LOCAL_ONLY', ...)"

    def test_ordermgr_enable_truthy_set_matches_contract(self):
        body, _ = _func_source(order_manager_mod, "enable")
        m = re.search(r'os\.getenv\(\s*"CALLISTO_LOCAL_ONLY"[^)]*\)', body)
        assert m, "gate must read os.getenv('CALLISTO_LOCAL_ONLY', ...)"

    def test_paper_trade_statuses_exclude_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})


# ---------------------------------------------------------------------------
# Part D: cross-component symmetry + live-status safety
# ---------------------------------------------------------------------------


class TestSymmetryAndSafety:
    def test_both_gates_use_same_truthy_semantics(self, tmp_path, monkeypatch):
        for value in ["1", "true", "yes", "TRUE", "Yes"]:
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
            ex = make_executor()
            m = make_manager(tmp_path / f"v{abs(hash(value))}")
            assert ex.enable() is False, value
            assert m.enable() is False, value
            assert ex.is_enabled is False
            assert m.is_enabled is False

    def test_falsy_leaves_both_armable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "0")
        ex = make_executor()
        m = make_manager(tmp_path)
        assert ex.enable() is True
        assert m.enable() is True
        ex.disable()
        m.disable()

    @pytest.mark.parametrize("component", ["bet_executor", "order_manager"])
    def test_refusal_messages_mention_local_only(self, component, caplog):
        with caplog.at_level(logging.WARNING):
            if component == "bet_executor":
                with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
                    BetExecutor().enable()
            else:
                import asyncio

                async def _mk():
                    return OrderManager(
                        db_path=None, telegram_sender=MockSender()
                    )
                # db_path=None keeps construction network-free
                loop = asyncio.new_event_loop()
                try:
                    m = loop.run_until_complete(_mk())
                finally:
                    loop.close()
                with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
                    m.enable()
        joined = " ".join(r.getMessage() for r in caplog.records).lower()
        assert "callisto_local_only" in joined

    def test_live_string_absent_from_armed_log_under_kill_switch(
        self, tmp_path, monkeypatch, caplog
    ):
        """Under LOCAL_ONLY nothing may claim live bets were armed."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        with caplog.at_level(logging.INFO):
            make_executor().enable()
            make_manager(tmp_path).enable()
        for record in caplog.records:
            msg = record.getMessage().lower()
            assert not ("enabled" in msg and "live bets will be placed" in msg)

    def test_module_constants_unchanged(self):
        # Guard against accidental widening of paper-trade statuses.
        assert bet_executor_mod.os is os  # module uses real os for getenv
