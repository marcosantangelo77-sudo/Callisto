"""Phase-failure recording: _record_phase_failure ledger + get_status exposure.

Failures must be recorded (not just logged) so a "healthy-looking" loop
cannot silently swallow phase errors. Cap at 50, oldest dropped.
"""

import ast
import asyncio
import os
import sys
import time
import types

import pytest

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


class _LedgerOnlyLoop:
    """Minimal stand-in with just the state _record_phase_failure touches."""

    def __init__(self):
        self._cycles = 7
        self._phase_failures = []
        self._PHASE_FAILURES_MAX = 50

    _record_phase_failure = auto.ResearchLoop._record_phase_failure


class TestRecordPhaseFailure:
    def test_append_shape(self):
        loop = _LedgerOnlyLoop()
        try:
            raise ValueError("boom")
        except ValueError as e:
            exc = e
        loop._record_phase_failure("backtest", "exception", exc)

        assert len(loop._phase_failures) == 1
        entry = loop._phase_failures[0]
        assert entry["cycle"] == 7
        assert entry["phase"] == "backtest"
        assert entry["kind"] == "exception"
        assert "boom" in entry["error"]
        assert len(entry["error"]) <= 300
        assert isinstance(entry["ts"], float)
        assert abs(entry["ts"] - time.time()) < 5

    def test_timeout_entry(self):
        loop = _LedgerOnlyLoop()
        loop._record_phase_failure("evaluate", "timeout")
        entry = loop._phase_failures[0]
        assert entry["kind"] == "timeout"
        assert entry["error"] == "timeout"

    def test_error_truncated_to_300(self):
        loop = _LedgerOnlyLoop()
        exc = ValueError("x" * 5000)
        loop._record_phase_failure("paper_trade", "exception", exc)
        assert len(loop._phase_failures[0]["error"]) <= 300

    def test_cap_50_drops_oldest(self):
        loop = _LedgerOnlyLoop()
        for i in range(51):
            loop._record_phase_failure(f"phase_{i}", "timeout")
            loop._cycles = i
        assert len(loop._phase_failures) == 50
        assert loop._phase_failures[0]["phase"] == "phase_1"  # oldest dropped
        assert loop._phase_failures[-1]["phase"] == "phase_50"


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

    def test_last_ten_semantics_on_instance(self):
        loop = _LedgerOnlyLoop()
        for i in range(20):
            loop._record_phase_failure(f"p{i}", "timeout")
        last10 = list(loop._phase_failures)[-10:]
        assert [e["phase"] for e in last10] == [f"p{i}" for i in range(10, 20)]
