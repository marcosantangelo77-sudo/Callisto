"""Tests for regime-diversity promotion gate (feat/regime-aware-sizing, 2026-04-22).

The gate rejects a hypothesis whose ALL resolved paper trades fall in one
(sport, season_phase) regime. The idea: a hypothesis that only has evidence
from MLB-regular hasn't proven it generalizes to playoffs / other sports /
other phases — block promotion until the sample spans ≥2 regimes.

Covers:
  - 30 signals all from one regime → blocked with `single_regime_sample`
  - Signals spanning 2+ regimes → regime gate PASSES (other gates may still
    block — we only assert this specific gate's verdict).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

os.environ.setdefault("CALLISTO_REGIME_DIVERSITY_GATE", "1")

from tools.hypothesis import HypothesisManager  # noqa: E402


async def _setup_db() -> aiosqlite.Connection:
    """Minimal schema — same shape as test_promotion_gates.py."""
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE hypotheses (
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
        )"""
    )
    await db.execute(
        """CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            event_id TEXT NOT NULL,
            hypothesis_id TEXT NOT NULL,
            sport TEXT,
            player TEXT,
            market TEXT,
            line REAL,
            side TEXT,
            book TEXT,
            book_odds_american INTEGER,
            book_implied_prob REAL,
            model_fair_prob REAL,
            model_factors TEXT,
            edge REAL,
            ev_pct REAL,
            kelly_fraction REAL,
            signal_generated BOOLEAN DEFAULT FALSE,
            actual_result TEXT,
            actual_stat REAL,
            closing_odds INTEGER,
            closing_implied REAL,
            clv_implied REAL,
            game_date DATE,
            snapshot_time DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL,
            event_id TEXT,
            sport TEXT,
            player TEXT,
            market TEXT,
            line REAL,
            side TEXT,
            book TEXT,
            signal_time DATETIME,
            signal_odds_american INTEGER,
            signal_implied_prob REAL,
            model_fair_prob REAL,
            edge REAL,
            ev_pct REAL,
            kelly_fraction REAL,
            recommended_stake REAL,
            closing_odds INTEGER,
            closing_implied REAL,
            clv_implied REAL,
            actual_result TEXT,
            actual_stat REAL,
            hypothetical_pnl REAL,
            game_date DATE,
            home_team TEXT,
            away_team TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        """CREATE TABLE hypothesis_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            computed_at DATETIME NOT NULL,
            total_n INTEGER,
            signals_n INTEGER,
            win INTEGER, loss INTEGER, push_ INTEGER,
            hit_rate REAL, avg_edge REAL, avg_ev REAL, avg_clv REAL,
            positive_clv_rate REAL, roi_pct REAL, sharpe REAL, max_drawdown REAL,
            p_value REAL, is_significant BOOLEAN,
            sortino REAL, brier_score REAL, information_coefficient REAL
        )"""
    )
    await db.commit()
    return db


async def _insert_hypothesis(db, hid: str, sport: str = "baseball_mlb") -> None:
    await db.execute(
        "INSERT INTO hypotheses "
        "(hypothesis_id, name, thesis, sport, market_type, model_config, "
        " status, promoted_at, promoted_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hid, f"test_{hid}", "Test thesis", sport, "h2h", "{}",
            "paper_trading",
            (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
            "test",
        ),
    )
    await db.commit()


async def _insert_paper_trade(
    db, hid: str, trade_id: str, sport: str, game_date: str,
    *, result: str = "won",
) -> None:
    await db.execute(
        "INSERT INTO paper_trades "
        "(trade_id, hypothesis_id, event_id, sport, market, side, book, "
        " signal_time, signal_odds_american, signal_implied_prob, "
        " model_fair_prob, edge, ev_pct, clv_implied, actual_result, "
        " hypothetical_pnl, game_date, home_team, away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trade_id, hid, f"E{trade_id}", sport, "h2h", "Home",
            "draftkings",
            f"{game_date}T12:00:00+00:00",
            -110, 0.524, 0.55, 0.036, 5.0, 0.540, result,
            90.0 if result == "won" else -100.0,
            game_date, "Home", "Away",
        ),
    )
    await db.commit()


async def _make_mgr(db) -> HypothesisManager:
    mgr = HypothesisManager(db_path=":memory:")
    mgr._db = db
    return mgr


@pytest.mark.asyncio
async def test_all_signals_one_regime_blocks_promotion():
    """30 MLB paper trades all in regular season → single_regime_sample block."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = "h-single"
        await _insert_hypothesis(db, hid, sport="baseball_mlb")
        # All 30 trades on MLB regular-season dates (2026-06-01 is MLB regular).
        for i in range(30):
            await _insert_paper_trade(
                db, hid, f"pt{i}",
                sport="baseball_mlb",
                game_date="2026-06-01",
            )

        readiness = await mgr.check_promotion_readiness(hid)
        reasons = " ".join(readiness.get("checks", []))
        assert "single_regime_sample" in reasons, (
            f"Expected single_regime_sample in checks; got: {reasons}"
        )
        assert readiness["ready"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_signals_across_two_regimes_passes_regime_gate():
    """15 MLB regular + 15 MLB playoffs → regime gate PASSES. Other gates may
    still block, but we only assert regime gate's verdict here."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = "h-multi"
        await _insert_hypothesis(db, hid, sport="baseball_mlb")
        # 15 regular-season trades (2026-06-01 is regular)
        for i in range(15):
            await _insert_paper_trade(
                db, hid, f"ptr{i}",
                sport="baseball_mlb",
                game_date="2026-06-01",
            )
        # 15 playoff trades (2026-10-10 is MLB playoffs)
        for i in range(15):
            await _insert_paper_trade(
                db, hid, f"ptp{i}",
                sport="baseball_mlb",
                game_date="2026-10-10",
            )

        readiness = await mgr.check_promotion_readiness(hid)
        reasons = " ".join(readiness.get("checks", []))
        # Regime gate must NOT fire single_regime_sample
        assert "single_regime_sample" not in reasons, (
            f"Regime gate should pass with 2 regimes; got: {reasons}"
        )
        # Should emit a PASS: regime_diversity line
        assert "regime_diversity" in reasons, (
            f"Expected regime_diversity PASS line; got: {reasons}"
        )


    finally:
        await db.close()


@pytest.mark.asyncio
async def test_env_toggle_disables_regime_diversity_gate(monkeypatch):
    """CALLISTO_REGIME_DIVERSITY_GATE=0 → gate never fires."""
    monkeypatch.setenv("CALLISTO_REGIME_DIVERSITY_GATE", "0")
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = "h-disabled"
        await _insert_hypothesis(db, hid, sport="baseball_mlb")
        for i in range(30):
            await _insert_paper_trade(
                db, hid, f"pt{i}",
                sport="baseball_mlb",
                game_date="2026-06-01",
            )

        readiness = await mgr.check_promotion_readiness(hid)
        reasons = " ".join(readiness.get("checks", []))
        assert "single_regime_sample" not in reasons, (
            f"Gate should be disabled by env; got: {reasons}"
        )
    finally:
        await db.close()
