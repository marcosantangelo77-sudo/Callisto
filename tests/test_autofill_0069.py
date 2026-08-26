"""autofill #0069 — characterization of the LOCAL_ONLY money kill switch.

CALLISTO_LOCAL_ONLY is the appliance-wide nuclear switch.  When it is truthy
(``1`` / ``true`` / ``yes``, case-insensitive):

  * ``BetExecutor.enable()`` must refuse (return False) and must NOT flip
    ``_enabled`` to True — the gate is evaluated BEFORE any state change and
    lives in ``tools.betexec.lifecycle.arm_gate_refusal``.
  * ``OrderManager.enable()`` must refuse the same way with its own inline
    guard in ``tools/order_manager.py``.

These are characterization tests: they pin today's fail-closed behaviour so
refactors cannot silently widen the arming path.  No browser, no network,
no live betting is ever armed here — every test either leaves the switch on
(refusing) or works purely on an un-initialized executor/manager.
"""

import ast
import inspect
import os
import re
from pathlib import Path

import pytest

from tools import order_manager as om_module
from tools.bet_executor import BetExecutor
from tools.betexec import lifecycle as betexec_lifecycle
from tools.order_manager import OrderManager


REPO_ROOT = Path(__file__).resolve().parent.parent
BET_EXECUTOR_SRC = (REPO_ROOT / "tools" / "bet_executor.py").read_text()
ORDER_MANAGER_SRC = (REPO_ROOT / "tools" / "order_manager.py").read_text()
LIFECYCLE_SRC = (
    REPO_ROOT / "tools" / "betexec" / "lifecycle.py"
).read_text()

TRUTHY_VALUES = ["1", "true", "TRUE", "True", "yes", "YES", "Yes"]
FALSY_VALUES = ["", "0", "false", "False", "no", "NO", "off"]


@pytest.fixture(autouse=True)
def _clean_local_only_env(monkeypatch):
    """Every test starts from a deterministic switch state."""
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)


# ---------------------------------------------------------------------------
# Gate unit tests — tools.betexec.lifecycle
# ---------------------------------------------------------------------------


class TestArmGateRefusal:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_env_produces_refusal(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        refusal = betexec_lifecycle.arm_gate_refusal()
        assert isinstance(refusal, str)
        assert refusal != ""
        assert "LOCAL_ONLY" in refusal

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_env_allows_arm(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_lifecycle.arm_gate_refusal() == ""

    def test_unset_env_allows_arm(self):
        assert betexec_lifecycle.arm_gate_refusal() == ""

    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_is_local_only_true(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_lifecycle.is_local_only() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "maybe"])
    def test_is_local_only_false_for_other_strings(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        assert betexec_lifecycle.is_local_only() is False

    def test_is_local_only_false_when_unset(self):
        assert betexec_lifecycle.is_local_only() is False

    def test_refusal_message_names_the_switch(self):
        assert "CALLISTO_LOCAL_ONLY" in betexec_lifecycle.LOCAL_ONLY_ENV

    def test_refusal_mentions_live_betting_block(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        refusal = betexec_lifecycle.arm_gate_refusal()
        lowered = refusal.lower()
        assert "arm" in lowered or "live" in lowered

    def test_whitespace_value_is_not_truthy(self, monkeypatch):
        """The gate uses .lower() without .strip() — pin that ' 1 ' is NOT
        recognized (current behaviour; changing this is a behaviour change)."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", " 1 ")
        assert betexec_lifecycle.is_local_only() is False
        assert betexec_lifecycle.arm_gate_refusal() == ""


# ---------------------------------------------------------------------------
# BetExecutor.enable() behaviour
# ---------------------------------------------------------------------------


class TestBetExecutorEnableLocalOnly:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_enable_refused_under_local_only(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_enable_allowed_without_switch(self):
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, monkeypatch, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        assert ex.enable() is True
        ex.disable()

    def test_refusal_returns_false_not_exception(self, monkeypatch):
        """Callers may ignore enable()'s return; refusal stays silent."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        ex.enable()  # must not raise
        assert ex.is_enabled is False

    def test_disable_after_refusal_keeps_disabled(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex.is_enabled is False

    def test_clearing_switch_then_enabling_works(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        assert ex.enable() is False
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()

    def test_repeated_refusals_stay_refused(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        ex = BetExecutor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_flip_flop_switch_respected_each_call(self, monkeypatch):
        ex = BetExecutor()
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        assert ex.enable() is False
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
        assert ex.enable() is True
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_init_default_is_disabled_regardless_of_env(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        assert ex.is_enabled is False

    def test_init_default_disabled_with_ambient_off(self):
        ex = BetExecutor()
        assert ex.is_enabled is False

    def test_manual_state_flip_visible_but_gate_still_blocks_reenable(
        self, monkeypatch
    ):
        """If something else set _enabled=True, disabling then enabling under
        the switch must not re-arm."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        ex._enabled = True
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# OrderManager.enable() behaviour
# ---------------------------------------------------------------------------


def _make_order_manager(tmp_path) -> OrderManager:
    return OrderManager(db_path=str(tmp_path / "orders_test.db"))


class TestOrderManagerEnableLocalOnly:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_enable_refused_under_local_only(
        self, monkeypatch, tmp_path, value
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        om = _make_order_manager(tmp_path)
        assert om.enable() is False
        assert om.is_enabled is False

    def test_enable_allowed_without_switch(self, tmp_path):
        om = _make_order_manager(tmp_path)
        assert om.enable() is True
        assert om.is_enabled is True
        om.disable()
        assert om.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, monkeypatch, tmp_path, value):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        om = _make_order_manager(tmp_path)
        assert om.enable() is True
        om.disable()

    def test_refusal_silent_when_return_ignored(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "yes")
        om = _make_order_manager(tmp_path)
        om.enable()  # no exception
        assert om.is_enabled is False

    def test_clearing_switch_then_enabling_works(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "true")
        om = _make_order_manager(tmp_path)
        assert om.enable() is False
        monkeypatch.delenv("CALLISTO_LOCAL_ONLY")
        assert om.enable() is True
        om.disable()

    def test_repeated_refusals_stay_refused(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        om = _make_order_manager(tmp_path)
        for _ in range(5):
            assert om.enable() is False
        assert om.is_enabled is False

    def test_disable_after_refusal_keeps_disabled(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        om = _make_order_manager(tmp_path)
        om.enable()
        om.disable()
        assert om.is_enabled is False

    def test_case_insensitive_mixed_forms(self, monkeypatch, tmp_path):
        for value in ("tRuE", "yEs", "01"):
            monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
            om = _make_order_manager(tmp_path)
            if value == "01":
                # '01' is not in the recognized set — current behaviour.
                assert om.enable() is True
            else:
                assert om.enable() is False
            om.disable()


# ---------------------------------------------------------------------------
# Source-level pins: gate evaluated BEFORE the state flip
# ---------------------------------------------------------------------------


def _function_source(src: str, name: str) -> str:
    m = re.search(rf"    def {name}\(self\).*?(?=\n    def |\n\s*@\w|\Z)", src, re.S)
    assert m, f"could not locate def {name} in source"
    return m.group(0)


class TestSourcePinsGateBeforeStateFlip:
    def test_betexecutor_enable_checks_gate_first(self):
        body = _function_source(BET_EXECUTOR_SRC, "enable")
        gate_idx = body.find("arm_gate_refusal()")
        flip_idx = body.find("self._enabled = True")
        assert gate_idx != -1, "BetExecutor.enable lost its arm gate"
        assert flip_idx != -1, "BetExecutor.enable no longer sets _enabled"
        assert gate_idx < flip_idx, "gate must be checked BEFORE _enabled=True"

    def test_betexecutor_enable_returns_false_on_refusal(self):
        body = _function_source(BET_EXECUTOR_SRC, "enable")
        assert "return False" in body

    def test_betexecutor_has_no_unconditional_true_assignment(self):
        body = _function_source(BET_EXECUTOR_SRC, "enable")
        lines = body.splitlines()
        seen_gate = False
        for line in lines:
            if "arm_gate_refusal()" in line:
                seen_gate = True
            if "self._enabled = True" in line:
                assert seen_gate, "_enabled=True appears before the gate"

    def test_ordermanager_enable_inline_guard_before_flip(self):
        body = _function_source(ORDER_MANAGER_SRC, "enable")
        guard_idx = body.find('os.getenv("CALLISTO_LOCAL_ONLY"')
        flip_idx = body.find("self._enabled = True")
        assert guard_idx != -1, "OrderManager.enable lost its inline guard"
        assert flip_idx != -1
        assert guard_idx < flip_idx

    def test_ordermanager_guard_recognized_set(self):
        body = _function_source(ORDER_MANAGER_SRC, "enable")
        m = re.search(
            r'os\.getenv\("CALLISTO_LOCAL_ONLY"[^)]*\)\.lower\(\)\s*'
            r"in\s*\(([^)]*)\)",
            body,
        )
        assert m, "guard expression missing"
        expr = m.group(1)
        for token in ('"1"', '"true"', '"yes"'):
            assert token in expr, f"guard no longer recognizes {token}"

    def test_ordermanager_returns_false_on_refusal(self):
        body = _function_source(ORDER_MANAGER_SRC, "enable")
        assert "return False" in body

    def test_both_gates_use_same_recognized_values(self):
        """Lifecycle gate and inline OM guard agree on 1/true/yes."""
        lc = LIFECYCLE_SRC[LIFECYCLE_SRC.find("def is_local_only"):]
        for token in ('"1"', '"true"', '"yes"'):
            assert token in lc
        om_body = _function_source(ORDER_MANAGER_SRC, "enable")
        for token in ('"1"', '"true"', '"yes"'):
            assert token in om_body

    def test_ast_parse_both_modules_cleanly(self):
        for src in (BET_EXECUTOR_SRC, ORDER_MANAGER_SRC, LIFECYCLE_SRC):
            ast.parse(src)

    def test_lifecycle_defines_is_local_only_helper(self):
        assert "def is_local_only()" in LIFECYCLE_SRC
        assert "def arm_gate_refusal()" in LIFECYCLE_SRC

    def test_no_live_status_widening_in_lifecycle(self):
        """Guard against someone widening generate_paper_trade_signal or the
        paper-trade status set from inside the lifecycle module."""
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in LIFECYCLE_SRC
        assert "generate_paper_trade_signal" not in LIFECYCLE_SRC

    def test_enable_signatures_return_bool_annotation(self):
        assert "def enable(self) -> bool:" in BET_EXECUTOR_SRC
        assert "def enable(self) -> bool:" in ORDER_MANAGER_SRC


# ---------------------------------------------------------------------------
# Cross-component symmetry + runtime introspection
# ---------------------------------------------------------------------------


class TestCrossComponentSymmetry:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_both_components_refuse_identically(
        self, monkeypatch, tmp_path, value
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", value)
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        assert ex.enable() is False
        assert om.enable() is False
        assert ex.is_enabled is False
        assert om.is_enabled is False

    def test_both_components_arm_identically_when_off(self, tmp_path):
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        assert ex.enable() is True
        assert om.enable() is True
        ex.disable()
        om.disable()

    def test_gate_functions_are_runtime_callable(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        assert callable(betexec_lifecycle.arm_gate_refusal)
        assert callable(betexec_lifecycle.is_local_only)

    def test_enable_methods_are_plain_functions(self):
        assert callable(BetExecutor.enable)
        assert callable(OrderManager.enable)

    def test_source_files_exist_at_expected_paths(self):
        assert (REPO_ROOT / "tools" / "bet_executor.py").is_file()
        assert (REPO_ROOT / "tools" / "order_manager.py").is_file()
        assert (REPO_ROOT / "tools" / "betexec" / "lifecycle.py").is_file()


# ---------------------------------------------------------------------------
# Warning-log characterization
# ---------------------------------------------------------------------------


class TestRefusalLogging:
    def test_betexecutor_logs_warning_on_refusal(
        self, monkeypatch, caplog
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        ex = BetExecutor()
        import logging

        with caplog.at_level(logging.WARNING, logger="callisto.executor"):
            ex.enable()
        warnings = [
            r for r in caplog.records if r.levelno >= logging.WARNING
        ]
        assert any("LOCAL_ONLY" in r.getMessage() for r in warnings)

    def test_ordermanager_logs_warning_on_refusal(
        self, monkeypatch, tmp_path, caplog
    ):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        om = _make_order_manager(tmp_path)
        import logging

        om_logger = logging.getLogger(
            om_module.__name__ if hasattr(om_module, "__name__") else "tools.order_manager"
        )
        with caplog.at_level(logging.WARNING):
            om.enable()
        all_records = caplog.records
        named = [r for r in all_records if r.name == om_logger.name]
        assert any("LOCAL_ONLY" in r.getMessage() for r in named) or any(
            "OrderManager" in r.getMessage() and "refused" in r.getMessage()
            for r in all_records
        )

    def test_no_warning_logged_on_successful_enable(self, monkeypatch, tmp_path, caplog):
        import logging

        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        with caplog.at_level(logging.WARNING):
            assert ex.enable() is True
            assert om.enable() is True
        refusals = [
            r for r in caplog.records
            if "LOCAL_ONLY" in r.getMessage() and r.levelno >= logging.WARNING
        ]
        assert refusals == []
        ex.disable()
        om.disable()


# ---------------------------------------------------------------------------
# Env-var edge cases around the switch itself
# ---------------------------------------------------------------------------


class TestSwitchEdgeCases:
    def test_empty_string_means_off_for_both(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "")
        assert betexec_lifecycle.is_local_only() is False
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        assert ex.enable() is True
        assert om.enable() is True
        ex.disable()
        om.disable()

    def test_random_string_means_off(self, monkeypatch):
        """Current implementation treats unrecognized strings as off.
        Pinned here so any tightening to fail-closed is a deliberate,
        reviewed change."""
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "please-dont-bet")
        assert betexec_lifecycle.is_local_only() is False

    def test_uppercase_yes_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "YES")
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        assert ex.enable() is False
        assert om.enable() is False

    def test_environment_restored_between_tests_by_fixture(self):
        assert os.getenv("CALLISTO_LOCAL_ONLY") is None
