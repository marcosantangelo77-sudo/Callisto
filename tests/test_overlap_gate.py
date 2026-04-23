"""Portfolio-correlation gate: reject paper→live promotion when the
candidate's signals overlap >CALLISTO_MAX_LIVE_OVERLAP_PCT of the same
event_ids as an existing LIVE hypothesis.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


async def _init_schema(db: aiosqlite.Connection) -> None:
    await db.execute(
        """CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT, thesis TEXT,
            sport TEXT, market_type TEXT,
            model_config TEXT, edge_threshold REAL DEFAULT 0.02,
            status TEXT DEFAULT 'draft',
            min_sample_size INTEGER DEFAULT 1000,
            significance_level REAL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME, promoted_by TEXT, notes TEXT
        )"""
    )
    await db.execute(
        """CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT, hypothesis_id TEXT,
            signal_generated BOOLEAN,
            game_date TEXT
        )"""
    )


@pytest.mark.asyncio
async def test_overlap_over_40pct_is_rejected(temp_db_path):
    """LIVE hyp A has 100 signals; candidate B has 50 signals, 25 overlap.
    Overlap = 25/50 = 50% > 40% cap → reject.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with aiosqlite.connect(temp_db_path) as db:
        await _init_schema(db)
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, "
            "market_type, model_config, status) VALUES "
            "('hyp_a_live', 'A', 't', 'mlb', 'h2h', '{}', 'live')"
        )
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, "
            "market_type, model_config, status) VALUES "
            "('hyp_b_cand', 'B', 't', 'mlb', 'h2h', '{}', 'paper_trading')"
        )
        # A: 100 signals on events ev_0..ev_99
        for i in range(100):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'hyp_a_live', 1, ?)",
                (f"ev_{i}", today),
            )
        # B: 50 signals — first 25 overlap with A (ev_0..ev_24), 25 new
        for i in range(25):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'hyp_b_cand', 1, ?)",
                (f"ev_{i}", today),
            )
        for i in range(25):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'hyp_b_cand', 1, ?)",
                (f"new_{i}", today),
            )
        await db.commit()

    # Run the overlap computation directly (monkey-patch DB path)
    from tools import hypothesis as H

    mgr = H.HypothesisManager(db_path=temp_db_path)
    await mgr.initialize()
    try:
        overlap = await mgr._compute_portfolio_overlap("hyp_b_cand")
        assert "hyp_a_live" in overlap
        pct = overlap["hyp_a_live"]
        # B has 50 distinct signal events; 25 overlap with A → 50%
        assert 0.45 <= pct <= 0.55, f"overlap={pct:.2%}"
        assert pct > H.MAX_LIVE_OVERLAP_PCT
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_overlap_under_cap_passes(temp_db_path):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    async with aiosqlite.connect(temp_db_path) as db:
        await _init_schema(db)
        await db.execute(
            "INSERT INTO hypotheses VALUES "
            "('a_live','A','t','mlb','h2h','{}',0.02,'live',1000,0.05,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL,NULL,NULL)"
        )
        await db.execute(
            "INSERT INTO hypotheses VALUES "
            "('b_cand','B','t','mlb','h2h','{}',0.02,'paper_trading',1000,0.05,"
            "CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,NULL,NULL,NULL)"
        )
        for i in range(100):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'a_live', 1, ?)",
                (f"ev_{i}", today),
            )
        # Only 10 overlap, 40 distinct
        for i in range(10):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'b_cand', 1, ?)",
                (f"ev_{i}", today),
            )
        for i in range(40):
            await db.execute(
                "INSERT INTO backtest_events (event_id, hypothesis_id, "
                "signal_generated, game_date) VALUES (?, 'b_cand', 1, ?)",
                (f"other_{i}", today),
            )
        await db.commit()

    from tools import hypothesis as H
    mgr = H.HypothesisManager(db_path=temp_db_path)
    await mgr.initialize()
    try:
        overlap = await mgr._compute_portfolio_overlap("b_cand")
        pct = overlap.get("a_live", 0.0)
        # 10 / 50 = 20% < 40%
        assert 0.15 <= pct <= 0.25
        assert pct < H.MAX_LIVE_OVERLAP_PCT
    finally:
        await mgr.close()
