"""Cycle health: get_status exposes last_cycle_ok / last_cycle_phase_failures.

A cycle in which any phase failed must NOT report as healthy, even though
the failure is non-fatal and the loop continues. Tested against a stub loop
(ledger + status-dict construction only — no ResearchLoop import of a hung
path beyond the module AST, matching test_loop_phase_errors.py).
"""

import ast
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous.
if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = types.ModuleType("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

import tools.autonomous as auto
from tools.loop.phase_ledger import PhaseFailureLedger


def _cycle_health_fields(loop) -> dict:
    """Reproduce the status dict's cycle-health fields without running
    the real get_status (which pulls in work queues, claude stats, etc.)."""
    return {
        "last_cycle_ok": loop._last_cycle_ok(),
        "last_cycle_phase_failures": loop._last_cycle_phase_failures(),
    }


class _StubLoop:
    """Just the state _last_cycle_ok/_last_cycle_phase_failures touch."""

    def __init__(self):
        self._cycles = 0
        self._phase_failures_ledger = PhaseFailureLedger()

    _last_cycle_ok = auto.ResearchLoop._last_cycle_ok
    _last_cycle_phase_failures = auto.ResearchLoop._last_cycle_phase_failures


class TestLastCycleHealth:
    def test_two_failures_this_cycle_not_ok(self):
        loop = _StubLoop()
        loop._cycles = 7
        loop._phase_failures_ledger.record(cycle=7, phase="backtest", kind="timeout")
        loop._phase_failures_ledger.record(cycle=7, phase="evaluate", kind="exception",
                                           exc=ValueError("boom"))
        fields = _cycle_health_fields(loop)
        assert fields["last_cycle_ok"] is False
        assert fields["last_cycle_phase_failures"] == 2

    def test_zero_failures_ok(self):
        loop = _StubLoop()
        loop._cycles = 3
        fields = _cycle_health_fields(loop)
        assert fields["last_cycle_ok"] is True
        assert fields["last_cycle_phase_failures"] == 0

    def test_no_cycles_yet_ok(self):
        loop = _StubLoop()
        assert loop._last_cycle_ok() is True
        assert loop._last_cycle_phase_failures() == 0

    def test_failure_in_older_cycle_is_still_ok(self):
        # Failure happened in cycle 4; we're now in cycle 5 with no new failure.
        loop = _StubLoop()
        loop._cycles = 5
        loop._phase_failures_ledger.record(cycle=4, phase="collect_data", kind="timeout")
        assert loop._last_cycle_ok() is True
        # Count is scoped to the current cycle, not the latest failing one.
        assert loop._last_cycle_phase_failures() == 0

    def test_count_scoped_to_latest_failing_cycle(self):
        loop = _StubLoop()
        loop._cycles = 9
        loop._phase_failures_ledger.record(cycle=8, phase="a", kind="timeout")
        loop._phase_failures_ledger.record(cycle=8, phase="b", kind="timeout")
        loop._phase_failures_ledger.record(cycle=9, phase="c", kind="exception",
                                           exc=ValueError("x"))
        assert loop._last_cycle_phase_failures() == 1
        assert loop._last_cycle_ok() is False

    def test_ledger_cap_does_not_break_health(self):
        loop = _StubLoop()
        loop._cycles = 60
        for i in range(51):
            loop._phase_failures_ledger.record(cycle=i + 10, phase=f"p{i}", kind="timeout")
        loop._phase_failures_ledger.record(cycle=60, phase="late", kind="timeout")
        assert loop._last_cycle_ok() is False
        assert loop._last_cycle_phase_failures() == 2


class TestGetStatusExposesCycleHealth:
    def test_get_status_source_includes_keys(self):
        tree = ast.parse(open(auto.__file__).read())
        found = {"last_cycle_ok": False, "last_cycle_phase_failures": False}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_status":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        if sub.value in found:
                            found[sub.value] = True
        assert all(found.values()), f"get_status missing keys: {found}"
