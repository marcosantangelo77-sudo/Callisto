"""R2 characterization tests — pin CURRENT anti-thrash behaviour.

These pin the existing inline machinery in ResearchLoop._check_progress
(tools/autonomous.py:7837) BEFORE any change, so drift is detectable.
They exercise the real method on a duck-typed loop instance (no DB, no
network, no Claude) by stubbing the two DB reads with a fake cursor.

Current pinned behaviour:
  * checks only every PROGRESS_CHECK_INTERVAL (10) cycles
  * progress = any new promotions OR new total_signals since last snapshot
  * 3 consecutive no-progress checks → _spinning_detected = True AND
    _run_spinning_diagnosis() awaited (every subsequent check too — that
    re-fire is current behaviour; the pure replacement fixes it and its
    tests live in test_build_r2_loop_quality.py)
  * DB failure → sentinel zeros, never an exception
"""

from __future__ import annotations

import asyncio

import pytest

import tools.autonomous as aut


class _FakeCursor:
    def __init__(self, value):
        self._value = value

    async def fetchone(self):
        return (self._value,)


class _FakeDB:
    """Returns configured counts for the two COUNT queries."""

    def __init__(self, signals=0, backtesting=0, fail=False):
        self.signals = signals
        self.backtesting = backtesting
        self.fail = fail

    async def execute(self, query, *a, **k):
        if self.fail:
            raise RuntimeError("db down")
        if "signal_generated = 1" in query:
            return _FakeCursor(self.signals)
        return _FakeCursor(self.backtesting)


def _make_loop(signals=0, db_fail=False):
    """Build a ResearchLoop without touching its heavy collaborators."""
    loop = object.__new__(aut.ResearchLoop)
    loop._cycles = 0
    loop._promotions = 0
    loop._rejections = 0
    loop._backtests_run = 0
    loop._hypotheses_generated = 0
    loop._claude_escalations = 0
    loop._progress_window = []
    loop._spinning_detected = False
    loop._consecutive_no_progress = 0
    loop._last_progress_check = 0
    hm = type("HM", (), {})()
    hm._db = _FakeDB(signals=signals, fail=db_fail)
    loop.hypothesis_manager = hm
    loop._diagnosis_calls = 0

    async def _fake_diag():
        loop._diagnosis_calls += 1

    loop._run_spinning_diagnosis = _fake_diag
    return loop


# ── interval gating ───────────────────────────────────────────────────


def test_check_progress_skips_non_multiple_cycles():
    loop = _make_loop()
    loop._cycles = 7
    asyncio.run(loop._check_progress())
    assert loop._progress_window == []  # nothing snapshotted


def test_check_progress_snapshots_every_10th_cycle():
    loop = _make_loop(signals=5)
    for c in (10, 20):
        loop._cycles = c
        asyncio.run(loop._check_progress())
    assert len(loop._progress_window) == 2
    assert loop._progress_window[0]["cycle"] == 10
    assert loop._progress_window[1]["total_signals"] == 5


def test_first_snapshot_alone_does_not_judge():
    loop = _make_loop()
    loop._cycles = 10
    asyncio.run(loop._check_progress())
    assert not loop._spinning_detected
    assert loop._consecutive_no_progress == 0


# ── progress detection ────────────────────────────────────────────────


def test_new_promotions_count_as_progress():
    loop = _make_loop()
    loop._cycles = 10
    asyncio.run(loop._check_progress())
    loop._cycles = 20
    loop._promotions = 2  # +2 promotions, signals flat
    asyncio.run(loop._check_progress())
    assert loop._consecutive_no_progress == 0
    assert not loop._spinning_detected


def test_new_signals_count_as_progress():
    loop = _make_loop(signals=0)
    loop._cycles = 10
    asyncio.run(loop._check_progress())
    loop.hypothesis_manager._db.signals = 4
    loop._cycles = 20
    asyncio.run(loop._check_progress())
    assert loop._consecutive_no_progress == 0


# ── spinning detection ────────────────────────────────────────────────


def test_three_stagnant_checks_trigger_spinning_and_diagnosis():
    loop = _make_loop()
    for c in (10, 20, 30, 40):
        loop._cycles = c
        asyncio.run(loop._check_progress())
    # snapshots at 10,20,30,40 → three comparisons → third one spins
    assert loop._consecutive_no_progress == 3
    assert loop._spinning_detected is True
    assert loop._diagnosis_calls >= 1


def test_recovery_clears_spinning():
    loop = _make_loop()
    for c in (10, 20, 30, 40):  # spin at 40
        loop._cycles = c
        asyncio.run(loop._check_progress())
    assert loop._spinning_detected
    loop._promotions = 5  # recovery
    loop._cycles = 50
    asyncio.run(loop._check_progress())
    assert not loop._spinning_detected
    assert loop._consecutive_no_progress == 0


# ── resilience ────────────────────────────────────────────────────────
# NOTE: R2 changed DB-failure handling from "sentinel zeros" to "sentinel -1
# = unknown". The pinned behaviour that still holds: no exception escapes,
# and a failed read never counts as positive or negative progress on its own.


def test_db_failure_degrades_to_unknown_sentinel_without_raising():
    loop = _make_loop(db_fail=True)
    loop._cycles = 10
    asyncio.run(loop._check_progress())  # must not raise
    assert loop._progress_window[-1]["total_signals"] == -1  # unknown, not zero
    loop._cycles = 20
    asyncio.run(loop._check_progress())
    # unknown-vs-unknown is NOT progress and NOT evidence of spinning by itself;
    # it still counts toward the streak (flat) — pinned.
    assert loop._consecutive_no_progress == 1
    assert not loop._spinning_detected


# ── window cap (pinned: last-5 retention) ─────────────────────────────


def test_progress_window_capped_at_five():
    loop = _make_loop()
    for i in range(1, 8):
        loop._cycles = i * 10
        asyncio.run(loop._check_progress())
    assert len(loop._progress_window) == 5
