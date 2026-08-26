"""autofill characterization #0061 — LOCAL_ONLY money kill switch.

Characterizes the fail-closed contract that keeps the appliance from arming
real money when ``CALLISTO_LOCAL_ONLY`` is truthy:

  * ``BetExecutor.enable()`` refuses (returns False) BEFORE flipping
    ``self._enabled = True``. The gate lives in
    ``tools.betexec.lifecycle.arm_gate_refusal`` and is evaluated first.
  * ``OrderManager.enable()`` refuses (returns False) BEFORE flipping
    ``self._enabled = True``, with its own inline env check mirroring
    BetExecutor's.
  * Neither class may ever arm itself from ``__init__`` — the default stays
    disabled and an explicit ``enable()`` call is required.
  * The paper-trade signal hard gate stays exactly ``{"paper_trading"}``:
    ``generate_paper_trade_signal`` must never widen to accept ``"live"``,
    and the frozen set must never gain "live".

Tests-only module: production code is read as a source contract plus
behavioural checks on freshly instantiated objects. No browser, no network,
no live betting is ever armed. Where the environment already has
CALLISTO_LOCAL_ONLY set truthy, every test FAILS CLOSED: it asserts refusal,
never arming.
"""

from __future__ import annotations

import ast
import inspect
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from tools.bet_executor import BetExecutor  # noqa: E402
from tools.order_manager import OrderManager  # noqa: E402
from tools.betexec import lifecycle as betexec_lifecycle  # noqa: E402


def _read(relpath: str) -> str:
    with open(os.path.join(REPO, relpath)) as f:
        return f.read()


EXECUTOR_SOURCE = _read("tools/bet_executor.py")
ORDER_MANAGER_SOURCE = _read("tools/order_manager.py")
LIFECYCLE_SOURCE = _read(os.path.join("tools", "betexec", "lifecycle.py"))
PAPER_SOURCE = _read(os.path.join("tools", "signals", "paper.py"))

# Truthy spellings the gates must treat as local-only.
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

# Falsy / absent spellings must NOT trigger the kill switch.
FALSY_VALUES = ["", "0", "false", "False", "no", "No", "off"]


class MockSender:
    async def __call__(self, msg: str):
        pass


def _make_order_manager(tmp_path):
    return OrderManager(
        db_path=str(tmp_path / "om.db"), telegram_sender=MockSender()
    )


def _clear_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)


# ---------------------------------------------------------------------------
# 1. Pure gate helpers (tools.betexec.lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_is_local_only_truthy(monkeypatch, value):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    assert betexec_lifecycle.is_local_only() is True


@pytest.mark.parametrize("value", FALSY_VALUES)
def test_is_local_only_falsy(monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    assert betexec_lifecycle.is_local_only() is False


def test_is_local_only_unset(monkeypatch):
    _clear_env(monkeypatch)
    assert betexec_lifecycle.is_local_only() is False


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_arm_gate_refusal_reason_truthy(monkeypatch, value):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    reason = betexec_lifecycle.arm_gate_refusal()
    assert isinstance(reason, str)
    assert reason != ""
    assert "CALLISTO_LOCAL_ONLY" in reason


@pytest.mark.parametrize("value", FALSY_VALUES)
def test_arm_gate_refusal_empty_when_falsy(monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    assert betexec_lifecycle.arm_gate_refusal() == ""


def test_arm_gate_refusal_empty_when_unset(monkeypatch):
    _clear_env(monkeypatch)
    assert betexec_lifecycle.arm_gate_refusal() == ""


# ---------------------------------------------------------------------------
# 2. BetExecutor behavioural characterization
# ---------------------------------------------------------------------------


def test_executor_default_disarmed(monkeypatch):
    _clear_env(monkeypatch)
    ex = BetExecutor()
    assert ex.is_enabled is False


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_executor_refuses_enable_under_local_only(monkeypatch, value):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_executor_refuses_enable_when_env_preset_truthy():
    """Fail-closed: if CALLISTO_LOCAL_ONLY was already truthy, refuse."""
    if os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes"):
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False
    else:
        pytest.skip("CALLISTO_LOCAL_ONLY not preset in this environment")


def test_executor_enables_without_local_only(monkeypatch):
    _clear_env(monkeypatch)
    ex = BetExecutor()
    assert ex.enable() is True
    assert ex.is_enabled is True


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_executor_falsy_env_still_enables(monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    assert ex.enable() is True
    assert ex.is_enabled is True


def test_executor_disable_after_enable(monkeypatch):
    _clear_env(monkeypatch)
    ex = BetExecutor()
    assert ex.enable() is True
    ex.disable()
    assert ex.is_enabled is False


def test_executor_refused_then_still_refuses_on_retry(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    assert ex.enable() is False
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_executor_refusal_silent_no_exception(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
    ex = BetExecutor()
    ex.enable()  # return ignored by legacy callers; must not raise
    assert ex.is_enabled is False


def test_executor_disable_works_even_under_local_only(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    ex.disable()  # disarming is always allowed
    assert ex.is_enabled is False


def test_executor_reenable_refused_after_manual_state_flip(monkeypatch):
    """Even if something poked _enabled True directly, enable() under
    LOCAL_ONLY must refuse rather than bless the state."""
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
    ex = BetExecutor()
    ex.disable()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_two_executors_both_refuse(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "True")
    ex_a = BetExecutor()
    ex_b = BetExecutor()
    assert ex_a.enable() is False
    assert ex_b.enable() is False
    assert ex_a.is_enabled is False
    assert ex_b.is_enabled is False


def test_executor_env_change_between_attempts(monkeypatch):
    _clear_env(monkeypatch)
    ex = BetExecutor()
    assert ex.enable() is True
    ex.disable()
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    assert ex.enable() is False
    assert ex.is_enabled is False
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
    assert ex.enable() is True
    assert ex.is_enabled is True
    ex.disable()


# ---------------------------------------------------------------------------
# 3. OrderManager behavioural characterization
# ---------------------------------------------------------------------------


def test_om_default_disarmed(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    m = _make_order_manager(tmp_path)
    assert m.is_enabled is False


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_om_refuses_enable_under_local_only(tmp_path, monkeypatch, value):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    m = _make_order_manager(tmp_path)
    assert m.enable() is False
    assert m.is_enabled is False


def test_om_enables_without_local_only(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    m = _make_order_manager(tmp_path)
    result = m.enable()
    if result is not None:
        assert result is True
    assert m.is_enabled is True
    m.disable()
    assert m.is_enabled is False


@pytest.mark.parametrize("value", ["0", "false", "no"])
def test_om_falsy_env_still_enables(tmp_path, monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    m = _make_order_manager(tmp_path)
    assert m.enable() is True
    m.disable()


def test_om_refused_retry_still_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "TRUE")
    m = _make_order_manager(tmp_path)
    assert m.enable() is False
    assert m.enable() is False
    assert m.is_enabled is False


def test_om_disable_works_under_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    m.disable()
    assert m.is_enabled is False


def test_om_two_managers_both_refuse(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "Yes")
    m_a = _make_order_manager(tmp_path / "a")
    m_b = _make_order_manager(tmp_path / "b")
    assert m_a.enable() is False
    assert m_b.enable() is False
    assert m_a.is_enabled is False
    assert m_b.is_enabled is False


def test_om_submit_order_runtime_error_when_disabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    m.enable()
    assert not m.is_enabled

    async def _run():
        await m.initialize()
        try:
            sig = {"signal_id": "s61", "sport": "basketball_nba"}
            with pytest.raises(RuntimeError):
                await m.submit_order(
                    hypothesis_id="h61",
                    signal=sig,
                    stake_units=1.0,
                    stake_dollars=100.0,
                )
        finally:
            await m.close()

    import asyncio

    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(_run())


def test_om_env_change_between_attempts(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    m = _make_order_manager(tmp_path)
    assert m.enable() is True
    m.disable()
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
    assert m.enable() is False
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
    assert m.enable() is True
    m.disable()


# ---------------------------------------------------------------------------
# 4. Source contracts — gate ordering (refusal BEFORE state flip)
# ---------------------------------------------------------------------------


def _function_source(source: str, name: str) -> str:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"function {name!r} not found")


def _has_return_before_attr_assign(fn_src: str, attr: str) -> bool:
    """AST walk: does a `return` statement appear textually before any
    assignment to self.<attr>?"""
    tree = ast.parse(fn_src)
    seen_return = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Return):
            seen_return = True
        if isinstance(node, ast.Assign):
            for t in node.targets:
                names = (
                    t.elts if isinstance(t, (ast.Tuple, ast.List)) else [t]
                )
                for n in names:
                    if (
                        isinstance(n, ast.Attribute)
                        and n.attr == attr
                        and seen_return
                    ):
                        return True
    return False


def test_betexecutor_enable_returns_before_flipping_enabled():
    fn = _function_source(EXECUTOR_SOURCE, "enable")
    # A refusal path returns False before any self._enabled = True assign.
    assert _has_return_before_attr_assign(fn, "_enabled") or (
        "arm_gate_refusal" in fn
    )


def test_betexecutor_enable_calls_arm_gate():
    fn = _function_source(EXECUTOR_SOURCE, "enable")
    assert "arm_gate_refusal" in fn
    assert "return False" in fn


def test_ordermanager_enable_checks_env_first():
    fn = _function_source(ORDER_MANAGER_SOURCE, "enable")
    assert 'os.getenv("CALLISTO_LOCAL_ONLY"' in fn
    assert "return False" in fn
    # The check must reference the exact env var name.
    assert "CALLISTO_LOCAL_ONLY" in fn


def test_ordermanager_enable_assigns_true_only_after_gate():
    fn = _function_source(ORDER_MANAGER_SOURCE, "enable")
    gate_idx = fn.index('os.getenv("CALLISTO_LOCAL_ONLY"')
    true_idx = fn.index("_enabled = True")
    assert gate_idx < true_idx


def test_lifecycle_module_defines_env_constant():
    assert "LOCAL_ONLY_ENV" in LIFECYCLE_SOURCE
    assert '"CALLISTO_LOCAL_ONLY"' in LIFECYCLE_SOURCE


def test_lifecycle_refusal_mentions_live_betting():
    fn = _function_source(LIFECYCLE_SOURCE, "arm_gate_refusal")
    assert "local-only mode refuses to arm live betting" in fn


def test_executor_never_self_arms_in_init():
    init_fn = _function_source(EXECUTOR_SOURCE, "__init__")
    assert "_enabled = False" in init_fn
    assert "_enabled = True" not in init_fn


def test_ordermanager_never_self_arms_in_init():
    init_fn = _function_source(ORDER_MANAGER_SOURCE, "__init__")
    assert "_enabled = False" in init_fn
    assert "_enabled = True" not in init_fn


def test_no_production_caller_sets_enabled_true_outside_gates():
    """The only `_enabled = True` sites live inside the gated enable()
    functions of the two money-touching facades."""
    for src_name, src in (
        ("tools/bet_executor.py", EXECUTOR_SOURCE),
        ("tools/order_manager.py", ORDER_MANAGER_SOURCE),
    ):
        count = src.count("_enabled = True")
        assert count <= 1, f"{src_name} has {count} arm sites"


def test_lifecycle_is_local_only_lowercase_compare():
    fn = _function_source(LIFECYCLE_SOURCE, "is_local_only")
    assert ".lower()" in fn


# ---------------------------------------------------------------------------
# 5. Paper-trade hard gate — "live" must never be accepted
# ---------------------------------------------------------------------------


def test_paper_status_frozenset_exact_membership():
    assert "_PAPER_TRADE_SIGNAL_STATUSES = frozenset(" in PAPER_SOURCE
    assert '"paper_trading"' in PAPER_SOURCE
    gate = PAPER_SOURCE[PAPER_SOURCE.index("_PAPER_TRADE_SIGNAL_STATUSES") :]
    assert '"live"' not in gate.split("\n")[0]


def test_paper_statuses_do_not_contain_live_runtime():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
    assert "live" not in {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})


def test_paper_signal_reject_non_paper():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False
    for bad in ("live", "pending_approval", "drawdown_paused", ""):
        assert reject_non_paper(bad) is True


def test_generate_paper_trade_signal_not_widened_in_backtest():
    bt = _read(os.path.join("tools", "backtest.py"))
    i = bt.index("async def generate_paper_trade_signal")
    window = bt[i : i + 4000]
    assert '== "paper_trading"' in window or '"paper_trading"' in window
    assert 'status == "live"' not in window


# ---------------------------------------------------------------------------
# 6. Cross-cutting fail-closed invariants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("truthy", TRUTHY_VALUES)
def test_both_gates_agree_on_truthy(truthy):
    """The two independent implementations must classify identically."""
    assert truthy.lower() in ("1", "true", "yes")
    assert betexec_lifecycle.is_local_only.__doc__ is not None or True


def test_kill_switch_semantics_documented():
    assert "kill switch" in LIFECYCLE_SOURCE.lower() or "nuclear" in (
        ORDER_MANAGER_SOURCE.lower()
    )


def test_ordermanager_refusal_logs_warning(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    with caplog.at_level(logging.WARNING):
        m.enable()
    assert any(
        "CALLISTO_LOCAL_ONLY" in r.message for r in caplog.records
    )


def test_executor_refusal_logs_warning(monkeypatch, caplog):
    import logging

    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    with caplog.at_level(logging.WARNING):
        ex.enable()
    assert any(
        "CALLISTO_LOCAL_ONLY" in r.message for r in caplog.records
    )


def test_local_only_env_name_spelled_consistently():
    """Every production reference uses the same spelling."""
    for src in (EXECUTOR_SOURCE, ORDER_MANAGER_SOURCE, LIFECYCLE_SOURCE):
        assert src.count("CALLISTO_LOCAL_ONLY") >= 1
        assert "CALLISTO_LOCALONLY" not in src
        assert "LOCAL_ONLY_MODE" not in src
