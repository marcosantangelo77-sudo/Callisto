"""autofill 0085 — LOCAL_ONLY money kill switch (characterization).

Characterizes the fail-closed arming behavior of the two money-touching
facade classes:

  * ``tools.bet_executor.BetExecutor.enable`` — refuses when
    ``CALLISTO_LOCAL_ONLY`` is truthy, BEFORE flipping ``_enabled`` True.
    The refusal reason lives in ``tools.betexec.lifecycle.arm_gate_refusal``.
  * ``tools.order_manager.OrderManager.enable`` — same nuclear switch,
    mirrored inline (case-insensitive ``1``/``true``/``yes``).

SAFETY: these tests never arm live betting. Every test that touches
arming either runs under LOCAL_ONLY (refusal expected) or arms and then
disables within the test. No browser is launched; no network calls are
made. ``BetExecutor`` is instantiated but never initialized.

Truthiness contract being characterized:
  truthy  -> {"1", "true", "yes"} case-insensitively
  falsy   -> everything else ("0", "false", "no", "", unset, whitespace)
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import subprocess
from unittest.mock import patch

import pytest

from tools.bet_executor import BetExecutor
from tools.betexec import lifecycle as betexec_lifecycle
from tools.betexec.lifecycle import (
    LOCAL_ONLY_ENV,
    arm_gate_refusal,
    is_local_only,
)
from tools.bet_executor import BetExecutor
from tools.order_manager import OrderManager


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

TRUTHY_VALUES = [
    "1",
    "true",
    "TRUE",
    "True",
    "tRue",
    "yes",
    "YES",
    "Yes",
    "yEs",
]

FALSY_VALUES = [
    "0",
    "false",
    "FALSE",
    "False",
    "no",
    "NO",
    "No",
    "",
    " ",
    "off",
    "maybe",
    "2",
    "-1",
    "01",
    "00",
    "tr ue",
]

ALL_ENV_KEYS = [LOCAL_ONLY_ENV]


class MockSender:
    """Telegram stand-in that records nothing and sends nowhere."""

    def __init__(self):
        self.sent = []

    async def __call__(self, msg: str):
        self.sent.append(msg)


@pytest.fixture
def clean_env(monkeypatch):
    """Remove every LOCAL_ONLY-ish key so tests start from a known state."""
    for key in ALL_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


@pytest.fixture
def local_only(clean_env):
    """Force the nuclear switch on."""
    clean_env.setenv(LOCAL_ONLY_ENV, "1")
    return True


def _make_order_manager(tmp_path) -> OrderManager:
    return OrderManager(
        db_path=str(tmp_path / "om_0085.db"),
        telegram_sender=MockSender(),
    )


# ---------------------------------------------------------------------------
# Part 1 — tools.betexec.lifecycle primitives
# ---------------------------------------------------------------------------


class TestIsLocalOnlyPrimitive:
    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_truthy_values(self, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        assert is_local_only() is True

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values(self, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        assert is_local_only() is False

    def test_unset_is_falsy(self, clean_env):
        assert is_local_only() is False

    def test_reads_environment_each_call(self, clean_env):
        # The gate must not cache: flip mid-process.
        clean_env.setenv(LOCAL_ONLY_ENV, "1")
        assert is_local_only() is True
        clean_env.setenv(LOCAL_ONLY_ENV, "0")
        assert is_local_only() is False

    def test_env_var_name_contract(self):
        # The appliance contract pins the variable name.
        assert LOCAL_ONLY_ENV == "CALLISTO_LOCAL_ONLY"


class TestArmGateRefusal:
    @pytest.mark.parametrize("value", ["1", "true", "yes"])
    def test_refusal_message_present_when_local_only(self, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        refusal = arm_gate_refusal()
        assert refusal
        assert "CALLISTO_LOCAL_ONLY" in refusal
        assert "local-only" in refusal.lower()

    @pytest.mark.parametrize("value", ["0", "false", ""])
    def test_empty_string_when_not_local_only(self, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        assert arm_gate_refusal() == ""

    def test_unset_means_no_refusal(self, clean_env):
        assert arm_gate_refusal() == ""

    def test_refusal_evaluated_before_state_flip_by_construction(self, clean_env):
        """Source-level characterization: enable() checks the gate first."""
        src = inspect.getsource(BetExecutor.enable)
        gate_pos = src.index("arm_gate_refusal")
        flip_pos = src.index("self._enabled = True")
        assert gate_pos < flip_pos


# ---------------------------------------------------------------------------
# Part 2 — BetExecutor.enable under LOCAL_ONLY
# ---------------------------------------------------------------------------


class TestBetExecutorLocalOnlyRefusal:
    def test_default_disabled_without_initialization(self):
        ex = BetExecutor()
        assert ex.is_enabled is False

    @pytest.mark.parametrize("value", TRUTHY_VALUES)
    def test_enable_returns_false(self, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_enable_never_flips_enabled_true_under_switch(self, local_only):
        ex = BetExecutor()
        ex.enable()
        ex.enable()
        ex.enable()
        assert ex._enabled is False
        assert ex.is_enabled is False

    def test_repeated_enable_attempts_stay_refused(self, local_only):
        ex = BetExecutor()
        for _ in range(5):
            assert ex.enable() is False
        assert ex.is_enabled is False

    def test_refusal_does_not_raise_for_callers_ignoring_return(self, local_only):
        ex = BetExecutor()
        ex.enable()  # legacy callers ignore the boolean
        assert ex.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(self, local_only):
        ex = BetExecutor()
        ex.enable()
        ex.disable()
        assert ex.is_enabled is False

    def test_falsy_values_do_not_block(self, clean_env):
        for value in ["0", "false", "no", ""]:
            clean_env.setenv(LOCAL_ONLY_ENV, value)
            ex = BetExecutor()
            try:
                assert ex.enable() is True
                assert ex.is_enabled is True
            finally:
                ex.disable()

    def test_unset_does_not_block_and_can_be_disabled(self, clean_env):
        ex = BetExecutor()
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()
        assert ex.is_enabled is False

    def test_flip_midflight_blocks_later_arm(self, clean_env):
        """Arming before the switch, refusing after it is set."""
        ex = BetExecutor()
        assert ex.enable() is True
        clean_env.setenv(LOCAL_ONLY_ENV, "1")
        ex.disable()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_caplog_warns_on_refusal(self, clean_env, caplog):
        clean_env.setenv(LOCAL_ONLY_ENV, "1")
        ex = BetExecutor()
        with caplog.at_level(logging.WARNING, logger="callisto.executor"):
            ex.enable()
        warning_texts = [r.getMessage() for r in caplog.records]
        assert any("CALLISTO_LOCAL_ONLY" in t for t in warning_texts)


class TestBetExecutorDisabledPathGuards:
    """Even without the env switch, an un-enabled executor must refuse money."""

    @pytest.mark.asyncio
    async def test_preflight_refused_when_never_enabled(self):
        ex = BetExecutor()
        ok, reason = await ex.preflight_check(
            sport="basketball_nba", odds=-110, edge=0.05, stake=1.0,
        )
        assert ok is False
        assert "disabled" in reason.lower()


# ---------------------------------------------------------------------------
# Part 3 — OrderManager.enable under LOCAL_ONLY
# ---------------------------------------------------------------------------


class TestOrderManagerLocalOnlyRefusal:
    def test_default_disabled_at_construction(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        assert m.is_enabled is False
        assert m._enabled is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "Yes"])
    def test_enable_returns_false(self, tmp_path, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        m = _make_order_manager(tmp_path)
        result = m.enable()
        if result is not None:
            assert result is False
        assert m.is_enabled is False

    def test_enable_never_flips_enabled_true_under_switch(self, tmp_path, local_only):
        m = _make_order_manager(tmp_path)
        for _ in range(4):
            m.enable()
        assert m._enabled is False

    def test_repeated_enable_attempts_stay_refused(self, tmp_path, local_only):
        m = _make_order_manager(tmp_path)
        results = [m.enable() for _ in range(6)]
        assert all(r is False for r in results)
        assert m.is_enabled is False

    def test_disable_after_refused_enable_stays_disabled(self, tmp_path, local_only):
        m = _make_order_manager(tmp_path)
        m.enable()
        m.disable()
        assert m.is_enabled is False

    @pytest.mark.parametrize("value", FALSY_VALUES)
    def test_falsy_values_do_not_block(self, tmp_path, clean_env, value):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        m = _make_order_manager(tmp_path)
        try:
            result = m.enable()
            assert m.is_enabled is True
            if result is not None:
                assert result is True
        finally:
            m.disable()

    def test_unset_enables_normally_then_disables(self, tmp_path, clean_env):
        m = _make_order_manager(tmp_path)
        result = m.enable()
        assert m.is_enabled is True
        if result is not None:
            assert result is True
        m.disable()
        assert m.is_enabled is False

    def test_caplog_warns_on_refusal(self, tmp_path, clean_env, caplog):
        clean_env.setenv(LOCAL_ONLY_ENV, "yes")
        m = _make_order_manager(tmp_path)
        with caplog.at_level(logging.WARNING, logger="tools"):
            with caplog.at_level(logging.WARNING):
                m.enable()
        warning_texts = [r.getMessage() for r in caplog.records]
        assert any("CALLISTO_LOCAL_ONLY" in t for t in warning_texts)

    def test_source_orders_check_before_state_flip(self):
        src = inspect.getsource(OrderManager.enable)
        check_pos = min(src.index("os.getenv"), src.index("getenv"))
        flip_pos = src.index("self._enabled = True")
        assert check_pos < flip_pos

    def test_truthy_set_matches_documented_contract(self):
        """The inline check accepts exactly {'1','true','yes'} case-insensitively."""
        src = inspect.getsource(OrderManager.enable)
        match = re.search(r'lower\(\)\s+in\s*\(([^)]*)\)', src)
        assert match, "expected tuple membership check on lowercased env"
        members = {m.strip().strip('"\'') for m in match.group(1).split(",") if m.strip()}
        assert members == {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Part 4 — end-to-end refusal of money movement while switch is on
# ---------------------------------------------------------------------------


class TestOrderSubmissionRefusedUnderLocalOnly:
    @pytest.mark.asyncio
    async def test_submit_order_raises_when_switch_on(self, tmp_path, clean_env):
        clean_env.setenv(LOCAL_ONLY_ENV, "1")
        m = _make_order_manager(tmp_path)
        assert m.enable() is False
        await m.initialize()
        try:
            sig = {"signal_id": "sig_0085_a", "sport": "baseball_mlb"}
            with pytest.raises(RuntimeError, match="disabled"):
                await m.submit_order(
                    hypothesis_id="hyp_0085_a", signal=sig,
                    stake_units=0.5, stake_dollars=50.0,
                )
        finally:
            await m.close()


class TestBetExecutorGateViaLifecycleModule:
    """BetExecutor delegates to betexec_lifecycle.arm_gate_refusal."""

    def test_facade_module_is_wired(self, local_only):
        assert hasattr(betexec_lifecycle, "arm_gate_refusal")
        refusal = betexec_lifecycle.arm_gate_refusal()
        assert "CALLISTO_LOCAL_ONLY" in refusal

    def test_patching_gate_to_refuse_blocks_arm(self, clean_env, monkeypatch):
        """If the shared gate refuses for any reason, enable() must refuse."""
        monkeypatch.setattr(
            "tools.betexec.lifecycle.arm_gate_refusal",
            lambda: "synthetic refusal",
        )
        ex = BetExecutor()
        assert ex.enable() is False
        assert ex.is_enabled is False

    def test_patching_gate_to_allow_arms_normally(self, clean_env, monkeypatch):
        clean_env.setenv(LOCAL_ONLY_ENV, "1")
        monkeypatch.setattr(
            "tools.betexec.lifecycle.arm_gate_refusal", lambda: ""
        )
        ex = BetExecutor()
        try:
            assert ex.enable() is True
            assert ex.is_enabled is True
        finally:
            ex.disable()


# ---------------------------------------------------------------------------
# Part 5 — production source hygiene (the gates were NOT weakened)
# ---------------------------------------------------------------------------


class TestProductionGateHygiene:
    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _read(self, relpath):
        with open(os.path.join(self.REPO_ROOT, relpath), encoding="utf-8") as fh:
            return fh.read()

    def test_betexecutor_enable_still_gates(self):
        src = self._read("tools/bet_executor.py")
        assert "arm_gate_refusal()" in src
        enable_src = src[src.index("def enable"):]
        assert "return False" in enable_src[:enable_src.index("def disable")]
        assert "self._enabled = False" in src  # default stays off

    def test_ordermanager_enable_still_gates(self):
        src = self._read("tools/order_manager.py")
        enable_src = src[src.index("def enable"):src.index("def disable")]
        assert "CALLISTO_LOCAL_ONLY" in enable_src
        assert 'in ("1", "true", "yes")' in enable_src

    def test_lifecycle_module_pins_env_name(self):
        src = self._read("tools/betexec/lifecycle.py")
        assert 'LOCAL_ONLY_ENV = "CALLISTO_LOCAL_ONLY"' in src
        assert '"live"' not in src.lower() or "statuses" not in src.lower()

    def test_no_live_status_added_to_paper_trade_statuses(self):
        """Hard safety invariant: paper-trade statuses never gain 'live'."""
        candidates = []
        for root, dirs, files in os.walk(os.path.join(self.REPO_ROOT, "tools")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    candidates.append(os.path.join(root, name))
        hits = []
        for path in candidates:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for match in re.finditer(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=", text):
                tail = text[match.start():match.start() + 400]
                status_match = re.search(r"\{([^}]*)\}", tail, re.S)
                if status_match:
                    statuses = {
                        s.strip().strip("\"'")
                        for s in status_match.group(1).split(",")
                        if s.strip()
                    }
                    hits.append((os.path.relpath(path, self.REPO_ROOT), statuses))
        assert hits, "expected to find _PAPER_TRADE_SIGNAL_STATUSES somewhere"
        for relpath, statuses in hits:
            assert "live" not in statuses, f"'live' crept into statuses in {relpath}"

    def test_generate_paper_trade_signal_not_widened_to_live(self):
        found = False
        for root, dirs, files in os.walk(os.path.join(self.REPO_ROOT, "tools")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                    if "def generate_paper_trade_signal" in text:
                        found = True
                        fn_src = text[text.index("def generate_paper_trade_signal"):]
                        body = fn_src[:4000]
                        assert "status == 'live'" not in body
                        assert 'status == "live"' not in body
        assert found, "generate_paper_trade_signal should exist in tools/"


# ---------------------------------------------------------------------------
# Part 6 — both facades behave identically (cross-check matrix)
# ---------------------------------------------------------------------------


class TestFacadeParityMatrix:
    @pytest.mark.parametrize("value,should_arm", [
        ("1", False), ("true", False), ("yes", False),
        ("0", True), ("false", True), ("no", True), ("", True),
    ])
    def test_both_facades_agree(self, tmp_path, clean_env, value, should_arm):
        clean_env.setenv(LOCAL_ONLY_ENV, value)
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        try:
            assert ex.enable() is should_arm
            om_result = om.enable()
            assert om.is_enabled is should_arm
            if om_result is not None:
                assert om_result is should_arm
        finally:
            ex.disable()
            om.disable()

    def test_unset_parity(self, tmp_path, clean_env):
        ex = BetExecutor()
        om = _make_order_manager(tmp_path)
        try:
            assert ex.enable() is True
            om.enable()
            assert ex.is_enabled and om.is_enabled
        finally:
            ex.disable()
            om.disable()


# ---------------------------------------------------------------------------
# Part 7 — repo-wide grep guard against accidental gate removal
# ---------------------------------------------------------------------------


class TestRepoGrepGuard:
    """Run ripgrep/grep over the repo to confirm the switch strings survive."""

    REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_grep_finds_local_only_in_production_sources(self):
        proc = subprocess.run(
            ["grep", "-rl", "CALLISTO_LOCAL_ONLY", "--include=*.py",
             os.path.join(self.REPO_ROOT, "tools")],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0
        files = {os.path.relpath(f, self.REPO_ROOT)
                 for f in proc.stdout.splitlines()}
        assert "tools/bet_executor.py" in files
        assert "tools/order_manager.py" in files
        assert "tools/betexec/lifecycle.py" in files

    def test_this_test_module_documents_the_switch(self):
        with open(__file__, encoding="utf-8") as fh:
            assert "CALLISTO_LOCAL_ONLY" in fh.read()
