"""Slice 8: LineMonitor lock wrapper + collaborator init into tools.lines.

Contract:
- ``process_snapshot`` (lock + ``_in_flight_db``) lives in
  tools.lines.process_snapshot. Facade ``_process_snapshot`` is a one-line
  delegate. Inner dispatch still goes through ``monitor._process_snapshot_inner``
  so instance overrides work.
- ``init_state`` now also wires ``_kl_tracker`` and ``_evaluator``;
  ``LineMonitor.__init__`` only forwards to ``init_state``.
- LineMonitor import path and method names are unchanged.
- Paper-signal statuses stay paper_trading-only. No live betting.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LM = ROOT / "tools" / "line_monitor.py"
CORE = ROOT / "tools" / "lines" / "core.py"
PROC = ROOT / "tools" / "lines" / "process_snapshot.py"
PAPER = ROOT / "tools" / "signals" / "paper.py"


def _async_defs(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
    }


def test_locked_process_snapshot_lives_in_process_snapshot_module():
    proc = _async_defs(PROC)
    assert "process_snapshot" in proc
    assert "process_snapshot_inner" in proc
    src = PROC.read_text(encoding="utf-8")
    assert "_snapshot_lock" in src
    assert "_in_flight_db" in src
    assert "monitor._process_snapshot_inner" in src


def test_facade_process_snapshot_is_thin_delegate():
    tree = ast.parse(LM.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "LineMonitor"
    )
    fn = next(
        n for n in cls.body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_process_snapshot"
    )
    src = ast.unparse(fn)
    assert "_process_snapshot_locked" in src
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    body_src = "\n".join(ast.unparse(s) for s in body)
    assert "_snapshot_lock" not in body_src
    assert "_in_flight_db" not in body_src


def test_init_state_wires_kl_tracker():
    src = CORE.read_text(encoding="utf-8")
    assert "KLDivergenceTracker" in src
    assert "_kl_tracker" in src
    assert "_evaluator" in src
    # Late import inside init_state — must not leak onto the core module
    # public namespace (test_linemon_slice7 allowlist).
    assert "from tools.lines.movement import KLDivergenceTracker" in src
    tree = ast.parse(LM.read_text(encoding="utf-8"))
    cls = next(
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "LineMonitor"
    )
    init = next(
        n for n in cls.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    init_src = ast.unparse(init)
    assert "init_state" in init_src
    assert "KLDivergenceTracker" not in init_src


def test_line_monitor_import_path_stable():
    from tools.line_monitor import LineMonitor

    assert LineMonitor.__module__ == "tools.line_monitor"
    m = LineMonitor(db_path=":memory:")
    assert m._evaluator is None
    assert m._kl_tracker is not None
    assert m._kl_tracker.db_path == ":memory:"


def test_lock_wrapper_sets_in_flight_and_clears():
    from tools.lines.process_snapshot import process_snapshot

    class Mon:
        def __init__(self):
            self._snapshot_lock = asyncio.Lock()
            self._in_flight_db = False
            self.calls = []

        async def _process_snapshot_inner(self, sport, snap):
            self.calls.append((sport, snap, self._in_flight_db))

    m = Mon()

    async def run():
        await process_snapshot(m, "nba", {"games": [1]})

    asyncio.run(run())
    assert m.calls == [("nba", {"games": [1]}, True)]
    assert m._in_flight_db is False


def test_lock_wrapper_clears_in_flight_on_inner_error():
    from tools.lines.process_snapshot import process_snapshot

    class Mon:
        def __init__(self):
            self._snapshot_lock = asyncio.Lock()
            self._in_flight_db = False

        async def _process_snapshot_inner(self, sport, snap):
            raise RuntimeError("boom")

    m = Mon()

    async def run():
        try:
            await process_snapshot(m, "nba", {})
        except RuntimeError:
            pass

    asyncio.run(run())
    assert m._in_flight_db is False


def test_process_snapshot_modules_do_not_arm_live():
    for path in (LM, CORE, PROC):
        src = path.read_text(encoding="utf-8")
        assert "generate_paper_trade_signal" not in src
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src


def test_paper_signal_still_paper_trading_only():
    src = PAPER.read_text(encoding="utf-8")
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src
    tree = ast.parse(src)
    assigned = None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES":
                    assigned = node.value
    assert assigned is not None
    dump = ast.dump(assigned)
    assert "paper_trading" in dump
    assert "live" not in dump
