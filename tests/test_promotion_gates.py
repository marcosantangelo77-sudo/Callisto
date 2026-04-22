"""Tests for tightened promotion gates (audit 2026-04-21).

Covers:
  * min_days enforcement in check_promotion_readiness
  * min_paper_trades requirement for paper_trading → live
  * CLV gate with env-overridable threshold
  * Removal of the backtest-only escape hatch
  * review_live_hypotheses demotion path

These tests build a minimal in-memory sqlite DB so the SQL itself (CAS, stage
filters, time-in-stage math) is verified, not just Python control flow.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest

from tools.hypothesis import HypothesisManager, PROMOTION_GATES


# ─── fixtures ────────────────────────────────────────────────────────


async def _setup_db() -> aiosqlite.Connection:
    """Minimal in-memory DB with the tables HypothesisManager touches.

    Mirrors schema.py but without the CHECK constraint on status so tests can
    insert 'live' / 'paused' rows freely and the migration path is separately
    validated in schema tests.
    """
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


async def _make_mgr(db) -> HypothesisManager:
    mgr = HypothesisManager(db_path=":memory:")
    mgr._db = db
    return mgr


async def _insert_hypothesis(db, **kw) -> str:
    """Insert a hypothesis row with defaults; override via kw."""
    defaults = dict(
        hypothesis_id="h-paper",
        name="test_hypothesis",
        thesis="Test thesis",
        sport="baseball_mlb",
        market_type="h2h",
        model_config="{}",
        edge_threshold=0.03,
        status="paper_trading",
        min_sample_size=50,
        significance_level=0.05,
        promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
        promoted_by="test",
    )
    defaults.update(kw)
    cols = ", ".join(defaults.keys())
    qs = ", ".join("?" for _ in defaults)
    await db.execute(
        f"INSERT INTO hypotheses ({cols}) VALUES ({qs})",
        tuple(defaults.values()),
    )
    await db.commit()
    return defaults["hypothesis_id"]


async def _insert_paper_trade(db, hid, trade_id, *, result="won",
                              odds=-110, edge=0.04, clv_implied=0.520,
                              signal_implied=0.524, days_ago=1) -> None:
    game_date = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    await db.execute(
        "INSERT INTO paper_trades "
        "(trade_id, hypothesis_id, event_id, sport, market, side, book, "
        " signal_time, signal_odds_american, signal_implied_prob, "
        " model_fair_prob, edge, ev_pct, clv_implied, actual_result, "
        " hypothetical_pnl, game_date, home_team, away_team) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            trade_id, hid, f"E{trade_id}", "baseball_mlb", "h2h", "Home",
            "draftkings",
            (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
            odds, signal_implied, 0.55, edge, 5.0, clv_implied, result,
            90.0 if result == "won" else -100.0,
            game_date, "Home", "Away",
        ),
    )
    await db.commit()


# ─── tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_definitions_require_hard_paper_trade_checks():
    """Regression lock: the paper→live gate MUST include min_paper_trades,
    min_days, and a non-zero min_clv_rate. These were all disabled before
    2026-04-21."""
    gate = PROMOTION_GATES["paper_trading→live"]
    assert gate.get("min_paper_trades") and gate["min_paper_trades"] >= 5, (
        "min_paper_trades missing or too low — backtest-only escape hatch"
    )
    assert gate.get("min_days") and gate["min_days"] >= 1
    assert gate.get("min_clv_rate") is not None and gate["min_clv_rate"] > 0, (
        "CLV gate disabled; audit finding #3 regressed"
    )


@pytest.mark.asyncio
async def test_paper_to_live_insufficient_days_rejects():
    """Hypothesis promoted to paper_trading 1 day ago — should NOT pass even
    with perfect stats."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(
            db,
            hypothesis_id="h-fresh",
            promoted_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        )
        # Seed 15 winning paper trades
        for i in range(15):
            await _insert_paper_trade(db, hid, f"pt{i}", result="won")

        readiness = await mgr.check_promotion_readiness(hid)
        assert readiness["ready"] is False
        reasons = " ".join(readiness.get("checks", []))
        assert "insufficient_time_in_stage" in reasons
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paper_to_live_insufficient_paper_trades_rejects():
    """15 days in paper, valid p-value, but only 3 resolved paper trades →
    REJECT. Without this gate, backtest-only evidence could still sneak a
    promotion through."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(db, hypothesis_id="h-thin")
        for i in range(3):
            await _insert_paper_trade(db, hid, f"pt{i}", result="won")
        # Seed 100 backtest signals to make sure backtest evidence alone can't
        # carry promotion.
        for i in range(100):
            await db.execute(
                "INSERT INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, market, side, book, "
                " book_odds_american, book_implied_prob, model_fair_prob, "
                " edge, ev_pct, signal_generated, actual_result, game_date, "
                " snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "r1", f"E{i}", hid, "baseball_mlb", "h2h", "Home",
                    "draftkings", -110, 0.524, 0.560, 0.036, 5.0, 1,
                    "won" if i % 2 == 0 else "lost",
                    "2025-12-01", "2025-12-01T00:00:00+00:00",
                ),
            )
        await db.commit()

        readiness = await mgr.check_promotion_readiness(hid)
        assert readiness["ready"] is False
        reasons = " ".join(readiness.get("checks", []))
        assert "paper_trade_sample_insufficient" in reasons


    finally:
        await db.close()


@pytest.mark.asyncio
async def test_paper_to_live_negative_clv_rejects():
    """All trades have negative CLV → positive_clv_rate = 0, below the gate
    floor → REJECT. This covers the 'CLV went negative' guard."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(db, hypothesis_id="h-badclv")
        # Every trade has signal_implied > clv_implied ⇒ CLV negative.
        for i in range(15):
            await _insert_paper_trade(
                db, hid, f"pt{i}", result="won",
                signal_implied=0.60, clv_implied=0.55,
            )

        readiness = await mgr.check_promotion_readiness(hid)
        reasons = " ".join(readiness.get("checks", []))
        # Either CLV gate or paper_trade gate should block; surface whichever.
        assert readiness["ready"] is False
        assert "CLV" in reasons or "paper_trade_sample_insufficient" in reasons


    finally:
        await db.close()


@pytest.mark.asyncio
async def test_auto_promote_zero_paper_trades_no_backtest_escape():
    """The audit's core fix: 0 resolved paper trades + 100 winning backtest
    signals must NOT promote to LIVE. Pre-fix, `_use_backtest_evidence` made
    this path end in 'promoted'."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(db, hypothesis_id="h-noperformance")
        # Zero paper trades, 50 winning backtest signals.
        for i in range(50):
            await db.execute(
                "INSERT INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, market, side, book, "
                " book_odds_american, book_implied_prob, model_fair_prob, "
                " edge, ev_pct, signal_generated, actual_result, game_date, "
                " snapshot_time) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "r1", f"E{i}", hid, "baseball_mlb", "h2h", "Home",
                    "draftkings", -110, 0.524, 0.620, 0.096, 9.5, 1,
                    "won", "2025-12-01", "2025-12-01T00:00:00+00:00",
                ),
            )
        await db.commit()

        result = await mgr.auto_promote(hid)
        assert result["action"] != "promoted", (
            f"Backtest escape hatch re-opened: {result}"
        )
        assert "paper_trade_sample_insufficient" in result.get("reason", "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_live_demotes_losing_hypothesis():
    """20 recent losses → hit_rate 0% → review_live_hypotheses demotes to
    'paused'."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(
            db, hypothesis_id="h-losing", status="live",
            promoted_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        for i in range(20):
            await _insert_paper_trade(db, hid, f"loss{i}", result="lost")

        results = await mgr.review_live_hypotheses(window_days=60)
        assert len(results) == 1
        outcome = results[0]
        assert outcome["demoted"] is True
        assert outcome["decision"] == "demoted_to_paused"
        # Confirm the status actually flipped in the DB.
        cur = await db.execute(
            "SELECT status FROM hypotheses WHERE hypothesis_id = ?", (hid,)
        )
        status = (await cur.fetchone())[0]
        assert status == "paused"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_live_holds_healthy_hypothesis():
    """16W-4L hypothesis with healthy CLV stays LIVE."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(
            db, hypothesis_id="h-winner", status="live",
            promoted_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        # 16 wins + 4 losses = 80% hit rate. Positive CLV (clv_implied low,
        # signal_implied high → CLV = clv - signal = negative, so flip numbers).
        for i in range(16):
            await _insert_paper_trade(
                db, hid, f"w{i}", result="won",
                signal_implied=0.50, clv_implied=0.55,  # CLV = +0.05
            )
        for i in range(4):
            await _insert_paper_trade(
                db, hid, f"l{i}", result="lost",
                signal_implied=0.50, clv_implied=0.55,
            )

        results = await mgr.review_live_hypotheses(window_days=60)
        assert len(results) == 1
        outcome = results[0]
        assert outcome["demoted"] is False
        assert outcome["decision"] == "hold_healthy"
        cur = await db.execute(
            "SELECT status FROM hypotheses WHERE hypothesis_id = ?", (hid,)
        )
        assert (await cur.fetchone())[0] == "live"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_live_insufficient_sample_holds():
    """Only 3 recent losses — below min_resolved; must NOT demote, to avoid
    thrashing freshly-promoted hypotheses on noise."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(
            db, hypothesis_id="h-thin-live", status="live",
            promoted_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        )
        for i in range(3):
            await _insert_paper_trade(db, hid, f"l{i}", result="lost")

        results = await mgr.review_live_hypotheses(window_days=60)
        assert results[0]["decision"] == "hold_insufficient_sample"
        assert results[0]["demoted"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_env_override_min_paper_trades(monkeypatch):
    """Setting CALLISTO_MIN_PAPER_TRADES=3 should tighten the effective gate
    on next import/reload. This verifies the env-var plumbing."""
    # Re-import the module fresh under the override. Because PROMOTION_GATES
    # is module-level we use importlib.reload to pick up the new env var.
    import importlib
    import tools.hypothesis as hyp_mod

    monkeypatch.setenv("CALLISTO_MIN_PAPER_TRADES", "3")
    monkeypatch.setenv("CALLISTO_MIN_CLV_RATE", "0.02")
    monkeypatch.setenv("CALLISTO_MIN_DAYS_PAPER", "1")
    importlib.reload(hyp_mod)
    try:
        assert hyp_mod.PROMOTION_GATES["paper_trading→live"]["min_paper_trades"] == 3
        assert hyp_mod.PROMOTION_GATES["paper_trading→live"]["min_clv_rate"] == pytest.approx(0.02)
        assert hyp_mod.PROMOTION_GATES["paper_trading→live"]["min_days"] == 1
    finally:
        # Reset env and reload baseline module state for downstream tests.
        monkeypatch.delenv("CALLISTO_MIN_PAPER_TRADES", raising=False)
        monkeypatch.delenv("CALLISTO_MIN_CLV_RATE", raising=False)
        monkeypatch.delenv("CALLISTO_MIN_DAYS_PAPER", raising=False)
        importlib.reload(hyp_mod)
