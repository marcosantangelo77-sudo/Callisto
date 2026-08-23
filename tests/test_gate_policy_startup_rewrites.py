"""Gate policy: the two startup/cycle routines that rewrite evidence or
un-reject hypotheses must be no-ops without CALLISTO_ALLOW_THRESHOLD_MIGRATION.

Covers:
  - ResearchLoop._phase_refresh_signals  (rewrites backtest_events.signal_generated
    to match current thresholds — audit mechanism #2; its old justification,
    "deep work lowered a threshold", is dead because the apply step refuses
    lowerings outright)
  - ResearchLoop._requeue_stale_signal_rejections (rejected -> backtesting with
    no opt-in, while its sibling requeues are gated)

Behavioral: runs the REAL coroutines against an in-memory SQLite DB.
No network, no Claude.
"""

import asyncio
import os
import sys
import types

import aiosqlite
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# polars stub before importing tools.autonomous (see
# test_tier1_loop_autonomous_gate_policy.py for the rationale).
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

FLAG = "CALLISTO_ALLOW_THRESHOLD_MIGRATION"


async def _make_db(tmp_path):
    """Schema matching what the two routines read/write."""
    db = await aiosqlite.connect(str(tmp_path / "gate.db"))
    await db.executescript(
        """
        CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            edge_threshold REAL,
            promoted_by TEXT
        );
        CREATE TABLE hypothesis_stats (
            hypothesis_id TEXT PRIMARY KEY,
            information_coefficient REAL,
            brier_score REAL,
            signals_n INTEGER,
            total_n INTEGER
        );
        CREATE TABLE backtest_runs (
            run_id TEXT PRIMARY KEY,
            hypothesis_id TEXT,
            signals_generated INTEGER DEFAULT 0
        );
        CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY,
            run_id TEXT,
            hypothesis_id TEXT,
            event_id TEXT,
            edge REAL,
            signal_generated INTEGER DEFAULT 0
        );
        """
    )
    # h1: threshold 0.02; two events at edge 0.03 recorded signal_generated=0
    # (i.e. exactly the state refresh_signals exists to rewrite).
    await db.execute(
        "INSERT INTO hypotheses VALUES ('h1', 'hyp one', 'backtesting', 0.02, '')"
    )
    await db.execute(
        "INSERT INTO backtest_runs VALUES ('r1', 'h1', 0)"
    )
    await db.executemany(
        "INSERT INTO backtest_events (run_id, hypothesis_id, event_id, edge, "
        "signal_generated) VALUES (?, ?, ?, ?, 0)",
        [("r1", "h1", "e1", 0.03), ("r1", "h1", "e2", 0.03)],
    )
    # h2: rejected with '0 signals' but actually has 2 signals in events —
    # exactly the state requeue_stale_signal_rejections exists to reverse.
    await db.execute(
        "INSERT INTO hypotheses VALUES ('h2', 'hyp two', 'rejected', 0.005, "
        "'auto:rejected — 0 signals')"
    )
    await db.execute(
        "INSERT INTO backtest_events (run_id, hypothesis_id, event_id, edge, "
        "signal_generated) VALUES ('r2', 'h2', 'e3', 0.03, 1)"
    )
    await db.commit()
    return db


class _StubManager:
    def __init__(self, db):
        self._db = db

    async def update_status(self, hid, status, reason=""):
        await self._db.execute(
            "UPDATE hypotheses SET status = ? WHERE hypothesis_id = ?", (status, hid)
        )
        await self._db.commit()


class _StubBacktest:
    db_path = None  # set per-test


def _loop_with(db):
    """A ResearchLoop-shaped object without running __init__."""
    loop = auto.ResearchLoop.__new__(auto.ResearchLoop)
    loop.hypothesis_manager = _StubManager(db)
    loop.backtest_engine = _StubBacktest()
    return loop


class TestRefreshSignalsGated:
    @pytest.fixture()
    def db(self, tmp_path):
        return asyncio.run(_make_db(tmp_path))

    def test_no_flag_leaves_evidence_alone(self, db, tmp_path):
        os.environ.pop(FLAG, None)
        asyncio.run(_refresh(db, tmp_path))
        assert asyncio.run(_signals(db)) == [0, 0]

    def test_flag_authorizes_rewrite(self, db, tmp_path):
        os.environ[FLAG] = "1"
        try:
            asyncio.run(_refresh(db, tmp_path))
            assert sorted(asyncio.run(_signals(db))) == [1, 1]
        finally:
            os.environ.pop(FLAG, None)


async def _refresh(db, tmp_path):
    loop = _loop_with(db)
    loop.backtest_engine.db_path = str(tmp_path / "gate.db")
    await loop._phase_refresh_signals()


async def _signals(db):
    cur = await db.execute(
        "SELECT signal_generated FROM backtest_events WHERE hypothesis_id='h1' "
        "ORDER BY id"
    )
    return [r[0] for r in await cur.fetchall()]


class TestRequeueStaleGated:
    @pytest.fixture()
    def db(self, tmp_path):
        return asyncio.run(_make_db(tmp_path))

    def test_no_flag_does_not_unreject(self, db):
        os.environ.pop(FLAG, None)
        asyncio.run(_requeue(db))
        assert asyncio.run(_status(db, "h2")) == "rejected"

    def test_flag_authorizes_unreject(self, db):
        os.environ[FLAG] = "1"
        try:
            asyncio.run(_requeue(db))
            assert asyncio.run(_status(db, "h2")) == "backtesting"
        finally:
            os.environ.pop(FLAG, None)


async def _requeue(db):
    loop = _loop_with(db)
    await loop._requeue_stale_signal_rejections()


async def _status(db, hid):
    cur = await db.execute(
        "SELECT status FROM hypotheses WHERE hypothesis_id = ?", (hid,)
    )
    return (await cur.fetchone())[0]
