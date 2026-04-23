"""Tests for drawdown kill-switch.

feat/portfolio-kelly-live-loop (audit 2026-04-22).
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

os.environ.setdefault("CALLISTO_MAX_DRAWDOWN_PCT", "0.15")
os.environ.setdefault("CALLISTO_DRAWDOWN_WINDOW_DAYS", "30")


async def _mk_executor(tmp_path):
    from tools.bet_executor import BetExecutor
    db_path = tmp_path / "test.db"
    db = await aiosqlite.connect(str(db_path))
    # Minimal schema for the drawdown path.
    await db.execute("""
        CREATE TABLE bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            balance REAL NOT NULL,
            change REAL,
            bet_id INTEGER,
            description TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE bankroll_peak (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at DATETIME NOT NULL,
            balance REAL NOT NULL,
            note TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            thesis TEXT NOT NULL,
            sport TEXT NOT NULL,
            market_type TEXT NOT NULL,
            model_config TEXT NOT NULL,
            edge_threshold REAL NOT NULL DEFAULT 0.01,
            status TEXT NOT NULL DEFAULT 'draft',
            min_sample_size INTEGER NOT NULL DEFAULT 50,
            significance_level REAL NOT NULL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME,
            promoted_by TEXT,
            notes TEXT
        )
    """)
    await db.commit()
    executor = BetExecutor()
    executor._db = db
    executor._enabled = True
    return executor, db


async def _seed_bankroll(db, balances_and_offsets):
    """balances_and_offsets: [(balance, seconds_ago), ...]"""
    now = datetime.now(timezone.utc)
    for bal, offset in balances_and_offsets:
        ts = (now - timedelta(seconds=offset)).isoformat()
        await db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, ?, 0, 'seed')",
            (ts, bal),
        )
        await db.execute(
            "INSERT INTO bankroll_peak (observed_at, balance, note) VALUES (?, ?, 'seed')",
            (ts, bal),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_drawdown_trigger_flips_enabled_and_pauses(tmp_path):
    executor, db = await _mk_executor(tmp_path)
    try:
        await _seed_bankroll(
            db,
            [
                (10_000.0, 86400),   # yesterday: peak
                (8_000.0, 60),       # now: -20% drawdown (above 15% threshold)
            ],
        )
        # Insert 3 LIVE hyps.
        for i in range(3):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type, "
                "model_config, status) VALUES (?, ?, ?, 'baseball_mlb', 'h2h', '{}', 'live')",
                (f"hyp_{i}", f"name_{i}", "thesis"),
            )
        await db.commit()
        assert executor.is_enabled

        status = await executor.check_drawdown_and_kill()

        assert status["triggered"] is True
        assert status["drawdown_pct"] >= 0.15
        assert not executor.is_enabled, "Executor should be DISABLED after drawdown"
        assert len(status["paused_hypotheses"]) == 3

        # All hyps moved to drawdown_paused
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE status = 'drawdown_paused'"
        )
        row = await cursor.fetchone()
        assert row[0] == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_drawdown_recovery_does_not_autoresume(tmp_path):
    """Recovery to near-peak should NOT flip _enabled back on.

    Manual re-enable is required for safety.
    """
    executor, db = await _mk_executor(tmp_path)
    try:
        # Simulate full drawdown-then-recovery history.
        await _seed_bankroll(
            db,
            [
                (10_000.0, 172800),  # 2d ago: peak
                (8_000.0, 86400),    # 1d ago: drawdown fires
                (9_800.0, 60),       # now: recovered to within 2%
            ],
        )
        for i in range(2):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type, "
                "model_config, status) VALUES (?, ?, ?, 'baseball_mlb', 'h2h', '{}', 'drawdown_paused')",
                (f"hyp_{i}", f"name_{i}", "thesis"),
            )
        await db.commit()
        executor._enabled = False  # As if prior drawdown already fired.

        status = await executor.check_drawdown_and_kill()
        # Current 9800 < peak 10000 so drawdown_pct=2%, below threshold.
        assert status["triggered"] is False
        assert executor.is_enabled is False, (
            "Recovery MUST NOT auto-resume; manual re-enable required"
        )
        # drawdown_paused hyps stay drawdown_paused.
        cursor = await db.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE status = 'drawdown_paused'"
        )
        row = await cursor.fetchone()
        assert row[0] == 2
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_drawdown_below_threshold_is_noop(tmp_path):
    executor, db = await _mk_executor(tmp_path)
    try:
        await _seed_bankroll(
            db,
            [
                (10_000.0, 86400),
                (9_500.0, 60),  # 5% drawdown — under 15% threshold
            ],
        )
        status = await executor.check_drawdown_and_kill()
        assert status["triggered"] is False
        assert executor.is_enabled is True
    finally:
        await db.close()
