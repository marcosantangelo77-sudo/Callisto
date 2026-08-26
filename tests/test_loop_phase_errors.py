"""Phase-failure recording: PhaseFailureLedger + _record_phase_failure + get_status.

Failures must be recorded (not just logged) so a "healthy-looking" loop
cannot silently swallow phase errors. Cap at 50, oldest dropped.
"""

import ast
import os
import sys
import time
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


class _LedgerOnlyLoop:
    """Minimal stand-in with just the state _record_phase_failure touches."""

    def __init__(self):
        self._cycles = 7
        self._phase_failures_ledger = PhaseFailureLedger()

    _record_phase_failure = auto.ResearchLoop._record_phase_failure


class TestLedgerUnit:
    def test_record_shape(self):
        ledger = PhaseFailureLedger()
        try:
            raise ValueError("boom")
        except ValueError as e:
            exc = e
        ledger.record(cycle=7, phase="backtest", kind="exception", exc=exc)

        assert ledger.count == 1
        entry = ledger.latest(10)[0]
        assert entry["cycle"] == 7
        assert entry["phase"] == "backtest"
        assert entry["kind"] == "exception"
        assert "boom" in entry["error"]
        assert len(entry["error"]) <= 300
        assert isinstance(entry["ts"], float)
        assert abs(entry["ts"] - time.time()) < 5

    def test_timeout_entry(self):
        ledger = PhaseFailureLedger()
        ledger.record(cycle=1, phase="evaluate", kind="timeout")
        entry = ledger.latest(10)[0]
        assert entry["kind"] == "timeout"
        assert entry["error"] == "timeout"

    def test_error_truncated_to_300(self):
        ledger = PhaseFailureLedger()
        ledger.record(cycle=1, phase="paper_trade", kind="exception",
                      exc=ValueError("x" * 5000))
        assert len(ledger.latest(10)[0]["error"]) <= 300

    def test_cap_50_drops_oldest(self):
        ledger = PhaseFailureLedger()
        for i in range(51):
            ledger.record(cycle=i, phase=f"phase_{i}", kind="timeout")
        assert ledger.count == 50
        entries = ledger.latest(50)
        assert entries[0]["phase"] == "phase_1"  # oldest dropped
        assert entries[-1]["phase"] == "phase_50"

    def test_latest_n_semantics(self):
        ledger = PhaseFailureLedger()
        for i in range(20):
            ledger.record(cycle=i, phase=f"p{i}", kind="timeout")
        last10 = ledger.latest(10)
        assert [e["phase"] for e in last10] == [f"p{i}" for i in range(10, 20)]
        assert ledger.latest(0) == []
        assert ledger.count == 20


class TestRecordPhaseFailureDelegation:
    def test_append_shape(self):
        loop = _LedgerOnlyLoop()
        try:
            raise ValueError("boom")
        except ValueError as e:
            exc = e
        loop._record_phase_failure("backtest", "exception", exc)

        assert loop._phase_failures_ledger.count == 1
        entry = loop._phase_failures_ledger.latest(10)[0]
        assert entry["cycle"] == 7
        assert entry["phase"] == "backtest"
        assert entry["kind"] == "exception"
        assert "boom" in entry["error"]

    def test_cap_via_delegation(self):
        loop = _LedgerOnlyLoop()
        for i in range(51):
            loop._cycles = i
            loop._record_phase_failure(f"phase_{i}", "timeout")
        assert loop._phase_failures_ledger.count == 50


class TestGetStatusExposesFailures:
    def test_get_status_source_includes_keys(self):
        src_file = auto.__file__
        tree = ast.parse(open(src_file).read())
        found = {"phase_failures": False, "phase_failure_count": False}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "get_status":
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                        if sub.value in found:
                            found[sub.value] = True
        assert all(found.values()), f"get_status missing keys: {found}"

    def test_researchloop_uses_ledger(self):
        src_file = auto.__file__
        tree = ast.parse(open(src_file).read())
        names = {
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and node.attr.startswith("_phase_failures")
        }
        assert "_phase_failures_ledger" in names
        assert "_phase_failures" not in names
