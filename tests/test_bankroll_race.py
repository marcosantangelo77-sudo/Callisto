"""Test the BEGIN IMMEDIATE bankroll race fix.

feat/portfolio-kelly-live-loop (audit 2026-04-22).

The original _record_bet path read the bankroll and then wrote the new row in
two steps, so two concurrent calls could both read the same snapshot and both
debit the same balance (silently double-counting credits). BEGIN IMMEDIATE on
the read-modify-write sequence plus the asyncio bankroll lock ensures the
final balance equals starting - sum(stakes).
"""

import asyncio
import os
from datetime import datetime, timezone

import aiosqlite
import pytest


async def _mk_db(tmp_path):
    db_path = tmp_path / "race.db"
    db = await aiosqlite.connect(str(db_path))
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
        CREATE TABLE bets (
            bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT,
            sport TEXT,
            event_id TEXT,
            game_description TEXT,
            bet_type TEXT,
            team TEXT,
            market TEXT,
            bookmaker TEXT,
            placement_odds INTEGER,
            placement_point REAL,
            placement_implied_prob REAL,
            stake REAL,
            result TEXT,
            edge_at_placement REAL,
            kelly_at_placement REAL,
            notes TEXT,
            tags TEXT
        )
    """)
    await db.execute(
        "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, 10000.0, 0, 'seed')",
        (datetime.now(timezone.utc).isoformat(),),
    )
    await db.commit()
    return db


@pytest.mark.asyncio
async def test_concurrent_bankroll_debits_are_atomic(tmp_path):
    """Five concurrent _record_bet calls should leave bankroll = 10000 - sum(stakes)."""
    from tools.bet_executor import BetExecutor
    db = await _mk_db(tmp_path)
    executor = BetExecutor()
    executor._db = db
    try:
        stakes = [50.0, 75.0, 100.0, 125.0, 150.0]

        async def place(i, stake):
            return await executor._record_bet(
                sport="baseball_mlb",
                event_id=f"evt_{i}",
                game_description="Test Game",
                team=f"Team{i}",
                market="h2h",
                bookmaker="DraftKings",
                odds=-110,
                point=None,
                stake=stake,
                edge=0.04,
                fair_prob=0.55,
                hypothesis_id=f"hyp_{i}",
            )

        await asyncio.gather(*[place(i, s) for i, s in enumerate(stakes)])

        cursor = await db.execute(
            "SELECT balance FROM bankroll ORDER BY id DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        final = row[0]
        expected = 10000.0 - sum(stakes)
        assert abs(final - expected) < 0.01, (
            f"Final bankroll ${final:.2f} != expected ${expected:.2f} — race!"
        )

        # Also verify the ledger has N+1 rows (1 seed + N debits).
        cursor = await db.execute("SELECT COUNT(*) FROM bankroll")
        row = await cursor.fetchone()
        assert row[0] == len(stakes) + 1
    finally:
        await db.close()
