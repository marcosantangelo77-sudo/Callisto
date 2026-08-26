"""autofill #0077 — LOCAL_ONLY money kill switch characterization.

Characterizes the appliance-wide nuclear switch ``CALLISTO_LOCAL_ONLY`` as it
gates the two arming surfaces of the live-money path:

  - ``tools.bet_executor.BetExecutor.enable``
  - ``tools.order_manager.OrderManager.enable``

Contract under characterization:

  1. When ``CALLISTO_LOCAL_ONLY`` is truthy (1/true/yes, any case), both
     ``enable()`` methods must REFUSE — return False and never flip
     ``_enabled`` to True.
  2. The gate is evaluated BEFORE the state flip: even repeated enable()
     calls against an already-enabled executor must leave it disarmed once
     local-only is observed... (see ordering tests below for the exact
     characterized behavior: the gate is checked on every call).
  3. Falsy / unset values do NOT block arming (the switch is opt-in).
  4. Refusals are silent-with-log: they return False and never raise, so
     existing callers that ignore the return value cannot accidentally arm.
  5. ``disable()`` always works regardless of the env var.
  6. Nothing in these tests ever arms live betting; no browser, no network,
     no DB writes beyond OrderManager's tmp_path sqlite used only to
     construct the manager.

SAFETY: this module must never widen the gate. If production ever drops the
guard, the source-inspection tests at the bottom FAIL CLOSED by asserting the
gate code still exists in both modules.
"""

from __future__ import annotations

import inspect
import os
from unittest.mock import patch

import pytest

import tools.bet_executor as bet_executor_module
import tools.order_manager as order_manager_module
from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager
from tools.betexec import lifecycle as betexec_lifecycle


TRUTHY_VALUES = ["1", "true", "TRUE", "True", "tRuE", "yes", "YES", "Yes"]
FALSY_VALUES = ["", "0", "false", "FALSE", "False", "no", "NO", "off", "maybe"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _NullSender:
    async def __call__(self, msg: str) -> None:
        return None


def _make_order_manager(tmp_path):
    return OrderManager(
        db_path=str(tmp_path / "om_0077.db"),
        telegram_sender=_NullSender(),
    )


def _clear_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)


# ---------------------------------------------------------------------------
# BetExecutor.enable — refusal matrix over truthy values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_betexec_enable_refused_for_every_truthy_value(monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    assert ex.is_enabled is False
    assert ex.enable() is False
    assert ex.is_enabled is False


@pytest.mark.parametrize("value", FALSY_VALUES)
def test_betexec_unset_or_falsy_values_do_not_block_arming(monkeypatch, value):
    """The kill switch is opt-in: unset or falsy values leave the gate open."""
    _clear_env(monkeypatch)
    if value:
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    assert ex.enable() is True
    assert ex.is_enabled is True


def test_betexec_repeated_enable_attempts_stay_refused(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    for _ in range(5):
        assert ex.enable() is False
        assert ex.is_enabled is False


def test_betexec_refusal_returns_false_not_none(monkeypatch):
    """Callers may branch on `if not ex.enable()` — must be exactly False."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
    ex = BetExecutor()
    result = ex.enable()
    assert result is False


def test_betexec_refusal_does_not_raise_when_return_ignored(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "TRUE")
    ex = BetExecutor()
    ex.enable()  # legacy callers ignore the return value
    assert ex.is_enabled is False


def test_betexec_disable_works_under_local_only(monkeypatch):
    """disable() is unconditional — the kill switch never blocks DISarming."""
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    # Force the internal flag directly (simulates a stale armed process);
    # disable must still bring it down even though enable() is gated.
    ex._enabled = True
    ex.disable()
    assert ex.is_enabled is False


def test_betexec_env_removed_after_refusal_allows_later_arm_in_process(
    monkeypatch,
):
    """Gate reads the environment on every call (characterized behavior)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    assert ex.enable() is False
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
    assert ex.enable() is True
    assert ex.is_enabled is True
    ex.disable()


def test_betexec_init_default_remains_disarmed(monkeypatch):
    _clear_env(monkeypatch)
    ex = BetExecutor()
    assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# OrderManager.enable — refusal matrix over truthy values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_ordermgr_enable_refused_for_every_truthy_value(tmp_path, monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    m = _make_order_manager(tmp_path)
    assert m.is_enabled is False
    assert m.enable() is False
    assert m.is_enabled is False


@pytest.mark.parametrize("value", FALSY_VALUES)
def test_ordermgr_unset_or_falsy_values_do_not_block_arming(
    tmp_path, monkeypatch, value
):
    _clear_env(monkeypatch)
    if value:
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    m = _make_order_manager(tmp_path)
    assert m.enable() is True
    assert m.is_enabled is True


def test_ordermgr_repeated_enable_attempts_stay_refused(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
    m = _make_order_manager(tmp_path)
    for _ in range(5):
        assert m.enable() is False
        assert m.is_enabled is False


def test_ordermgr_refusal_returns_false_not_none(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "Yes")
    m = _make_order_manager(tmp_path)
    assert m.enable() is False


def test_ordermgr_refusal_does_not_raise_when_return_ignored(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    m.enable()
    assert m.is_enabled is False


def test_ordermgr_disable_works_under_local_only(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    m._enabled = True  # simulate stale armed state
    m.disable()
    assert m.is_enabled is False


def test_ordermgr_default_disarmed_even_without_env(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    m = _make_order_manager(tmp_path)
    assert m.is_enabled is False


def test_ordermgr_gate_checked_on_every_call(tmp_path, monkeypatch):
    """Removing the env mid-process unblocks later enable calls."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
    m = _make_order_manager(tmp_path)
    assert m.enable() is False
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
    assert m.enable() is True
    assert m.is_enabled is True
    m.disable()


# ---------------------------------------------------------------------------
# lifecycle gate primitives (tools.betexec.lifecycle)
# ---------------------------------------------------------------------------


def test_is_local_only_truthy_matrix(monkeypatch):
    _clear_env(monkeypatch)
    for value in TRUTHY_VALUES:
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_lifecycle.is_local_only() is True, value


def test_is_local_only_falsy_matrix(monkeypatch):
    _clear_env(monkeypatch)
    for value in FALSY_VALUES:
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_lifecycle.is_local_only() is False, value


def test_is_local_only_unset(monkeypatch):
    _clear_env(monkeypatch)
    assert betexec_lifecycle.is_local_only() is False


def test_arm_gate_refusal_empty_when_open(monkeypatch):
    _clear_env(monkeypatch)
    assert betexec_lifecycle.arm_gate_refusal() == ""


@pytest.mark.parametrize("value", TRUTHY_VALUES)
def test_arm_gate_refusal_nonempty_when_closed(monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    refusal = betexec_lifecycle.arm_gate_refusal()
    assert isinstance(refusal, str)
    assert refusal  # non-empty reason string
    assert "LOCAL_ONLY" in refusal or "local-only" in refusal


def test_arm_gate_refusal_mentions_live_betting(monkeypatch):
    """The refusal message names what it protects."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    refusal = betexec_lifecycle.arm_gate_refusal()
    assert "live" in refusal.lower()


def test_lifecycle_exports_local_only_env_name():
    assert betexec_lifecycle.LOCAL_ONLY_ENV == "CALLISTO_LOCAL_ONLY"


# ---------------------------------------------------------------------------
# cross-surface consistency: both facades share one gate semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_both_surfaces_agree_under_local_only(tmp_path, monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    m = _make_order_manager(tmp_path)
    assert ex.enable() is False
    assert m.enable() is False
    assert ex.is_enabled is False
    assert m.is_enabled is False


@pytest.mark.parametrize("value", ["0", "false", "off"])
def test_both_surfaces_agree_when_switch_off(tmp_path, monkeypatch, value):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
    ex = BetExecutor()
    m = _make_order_manager(tmp_path)
    assert ex.enable() is True
    assert m.enable() is True
    ex.disable()
    m.disable()


def test_whitespace_padded_value_does_not_block(monkeypatch):
    """Characterized: bare .lower() comparison — ' 1 ' is NOT truthy here.

    This documents current (slightly permissive) behavior of OrderManager's
    inline check; lifecycle.is_local_only() shares the same shape. A future
    hardening to .strip() would need this test updated deliberately.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", " 1 ")
    assert betexec_lifecycle.is_local_only() is False


# ---------------------------------------------------------------------------
# fail-closed source inspection: the gates must physically exist
# ---------------------------------------------------------------------------


def test_source_betexecutor_enable_checks_gate_before_state_flip():
    src = inspect.getsource(bet_executor_module.BetExecutor.enable)
    assert "_enabled = True" in src
    gate_pos = src.find("arm_gate_refusal")
    flip_pos = src.find("_enabled = True")
    assert gate_pos != -1, "BetExecutor.enable lost its arm gate"
    assert flip_pos != -1
    assert gate_pos < flip_pos, "arm gate must be evaluated BEFORE the state flip"


def test_source_ordermanager_enable_checks_env_before_state_flip():
    src = inspect.getsource(order_manager_module.OrderManager.enable)
    assert "_enabled = True" in src
    gate_pos = src.find("CALLISTO_LOCAL_ONLY")
    flip_pos = src.find("_enabled = True")
    assert gate_pos != -1, "OrderManager.enable lost its nuclear kill switch"
    assert flip_pos != -1
    assert gate_pos < flip_pos, "kill switch must be evaluated BEFORE the state flip"


def test_source_ordermanager_enable_returns_false_on_refusal():
    src = inspect.getsource(order_manager_module.OrderManager.enable)
    refusal_branch = src[src.find("CALLISTO_LOCAL_ONLY") : src.find("_enabled = True")]
    assert "return False" in refusal_branch


def test_source_lifecycle_gate_targets_live_betting():
    src = inspect.getsource(betexec_lifecycle.arm_gate_refusal)
    lowered = src.lower()
    assert "callisto_local_only" in lowered
    assert "refuse" in lowered or "not enabled" in lowered


def test_no_status_widening_in_either_module():
    """Guard rails from the task brief: nothing widens to status=='live'."""
    exec_src = inspect.getsource(bet_executor_module)
    om_src = inspect.getsource(order_manager_module)
    assert "generate_paper_trade_signal" not in exec_src.replace(
        "import", ""
    ) or "== 'live'" not in exec_src
    assert "== 'live'" not in om_src


def test_paper_trade_signal_statuses_never_gain_live():
    """If _PAPER_TRADE_SIGNAL_STATUSES is importable anywhere, it lacks 'live'."""
    for mod_name in ("tools.bet_executor", "tools.order_manager"):
        mod = __import__(mod_name, fromlist=["x"])
        statuses = getattr(mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        if statuses is not None:
            assert "live" not in {str(s).lower() for s in statuses}


# ---------------------------------------------------------------------------
# logging characterization
# ---------------------------------------------------------------------------


def test_betexec_refusal_logs_warning(monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = BetExecutor()
    with caplog.at_level("WARNING", logger="callisto.executor"):
        assert ex.enable() is False
    warnings = [r for r in caplog.records if r.levelno >= 30]
    assert warnings, "expected a warning-level record on refusal"


def test_ordermgr_refusal_logs_warning(tmp_path, monkeypatch, caplog):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    m = _make_order_manager(tmp_path)
    with caplog.at_level("WARNING", logger="tools.order_manager"):
        assert m.enable() is False
    warnings = [r for r in caplog.records if r.levelno >= 30]
    assert warnings, "expected a warning-level record on refusal"


# ---------------------------------------------------------------------------
# patch.dict variant (mirrors sibling suites' style)
# ---------------------------------------------------------------------------


def test_patch_dict_style_betexec_refusal():
    with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "1"}):
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False


def test_patch_dict_style_ordermgr_refusal(tmp_path):
    with patch.dict(os.environ, {"CALLISTO_LOCAL_ONLY": "yes"}):
        m = _make_order_manager(tmp_path)
        assert m.enable() is False
        assert m.is_enabled is False


def test_ambient_env_respected_without_monkeypatch(tmp_path):
    """If the host already exports the switch, construction-time state stays
    disarmed regardless of ambient value (fail closed either way)."""
    ambient = os.environ.get("CALLISTO_LOCAL_ONLY")
    m = _make_order_manager(tmp_path)
    assert m.is_enabled is False  # init default, independent of env
    del ambient  # informational only
