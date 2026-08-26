"""Tests for auto_promote gate policy: diagnose-only on high thresholds.

Regression lock for the "silent gate saw" bug: when a backtesting hypothesis
has events but 0 signals and the edge diagnostic says threshold_too_high,
auto_promote must NOT lower the threshold, retroactively flip
backtest_events.signal_generated, or sync backtest_runs.signals_generated.
It must log, hold, and leave the evidence untouched.

Schema pattern reused from tests/test_promotion_gates.py.
"""

from __future__ import annotations

import json

import pytest

from tools.hypothesis import HypothesisManager


# ─── fixtures (mirrors tests/test_promotion_gates.py) ────────────────


async def _setup_db():
    import aiosqlite

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
        """CREATE TABLE backtest_runs (
            run_id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL,
            sport TEXT,
            start_date DATE,
            end_date DATE,
            total_events INTEGER,
            signals_generated INTEGER,
            actual_win INTEGER,
            actual_loss INTEGER,
            hit_rate REAL,
            avg_edge REAL,
            avg_ev REAL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            hypothesis_id TEXT NOT NULL,
            event_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await db.execute(
        """CREATE TABLE hypothesis_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            hypothesis_id TEXT NOT NULL,
            stage TEXT NOT NULL,
            computed_at DATETIME NOT NULL
        )"""
    )
    await db.commit()
    return db


async def _make_mgr(db) -> HypothesisManager:
    mgr = HypothesisManager(db_path=":memory:")
    mgr._db = db
    return mgr


async def _insert_hypothesis(db, hid="h-thresh", status="backtesting",
                             edge_threshold=0.05, evaluate_cycles=2) -> str:
    model_config = json.dumps({"evaluate_cycles": evaluate_cycles})
    await db.execute(
        "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type,"
        " model_config, edge_threshold, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (hid, "test_hypothesis", "Test thesis", "baseball_mlb", "h2h",
         model_config, edge_threshold, status),
    )
    await db.commit()
    return hid


async def _insert_events(db, hid, edges=(0.02, 0.04)) -> None:
    for i, e in enumerate(edges):
        await db.execute(
            "INSERT INTO backtest_events (run_id, event_id, hypothesis_id,"
            " sport, market, edge, signal_generated) VALUES (?, ?, ?, ?, ?, ?, 0)",
            ("run-1", f"{hid}-ev-{i}", hid, "baseball_mlb", "h2h", e),
        )
    await db.commit()


# ─── tests ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_threshold_too_high_is_diagnose_only_stubbed_diag():
    """With threshold_too_high diagnosed, auto_promote must hold WITHOUT
    mutating the threshold or rewriting any evidence."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(db)
        await _insert_events(db, hid)

        stub = {
            "threshold_too_high": True,
            "recommended_threshold": 0.01,
            "current_threshold": 0.05,
            "max_edge": 0.04,
        }
        calls = []

        async def fake_diag(hid_):
            calls.append(hid_)
            return dict(stub)

        mgr._diagnose_edge_threshold = fake_diag

        result = await mgr.auto_promote(hid)

        assert calls == [hid], "diagnostic should be called once"
        assert result.get("action") == "held"
        assert result.get("reason") == "threshold_too_high"
        assert result["diagnosis"]["threshold_too_high"] is True

        cur = await db.execute(
            "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?", (hid,))
        assert (await cur.fetchone())[0] == 0.05, "threshold must not be lowered"

        cur = await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id = ?"
            " AND signal_generated != 0", (hid,))
        assert (await cur.fetchone())[0] == 0, "signal_generated must stay 0"

        cur = await db.execute(
            "SELECT signals_generated FROM backtest_runs WHERE run_id = 'run-1'")
        row = await cur.fetchone()
        assert row is None or row[0] in (None, 0), "runs.signals_generated must not be synced"

        cur = await db.execute(
            "SELECT status FROM hypotheses WHERE hypothesis_id = ?", (hid,))
        assert (await cur.fetchone())[0] == "backtesting"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_threshold_too_high_real_diagnostic():
    """Same behavior with the REAL _diagnose_edge_threshold on seeded edges:
    threshold 0.05 > max edge 0.04 → diagnose-only hold."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        hid = await _insert_hypothesis(db)
        await _insert_events(db, hid)

        result = await mgr.auto_promote(hid)

        assert result.get("action") == "held"
        assert result.get("reason") == "threshold_too_high"
        diag = result.get("diagnosis", {})
        assert diag.get("threshold_too_high") is True
        assert diag.get("max_edge") == 0.04

        cur = await db.execute(
            "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?", (hid,))
        assert (await cur.fetchone())[0] == 0.05

        cur = await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id = ?"
            " AND signal_generated != 0", (hid,))
        assert (await cur.fetchone())[0] == 0

        # evaluate_cycles incremented by 1 (the "we looked" counter), not reset to 0
        cur = await db.execute(
            "SELECT model_config FROM hypotheses WHERE hypothesis_id = ?", (hid,))
        mc = json.loads((await cur.fetchone())[0])
        assert mc.get("evaluate_cycles") == 3
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_not_too_high_does_not_break_reject_hold_paths():
    """When diagnosis says the threshold is fine but there are still 0 signals
    after many cycles, auto_promote continues down its normal (hold/reject)
    paths — none of which rewrite evidence here."""
    db = await _setup_db()
    try:
        mgr = await _make_mgr(db)
        # Low threshold (0.02): all seeded edges clear it... but we seed 0
        # signals anyway so _get_backtest_signals returns nothing; diagnosis
        # will report threshold_too_high=False because above_threshold > 0.
        hid = await _insert_hypothesis(db, hid="h-ok", edge_threshold=0.02,
                                       evaluate_cycles=15)
        await _insert_events(db, hid, edges=(0.03, 0.04))
        await db.execute(
            "UPDATE backtest_events SET signal_generated = 1,"
            " game_date = '2026-01-0' || (id + 1)"
            " WHERE hypothesis_id = ?", (hid,))

        result = await mgr.auto_promote(hid)

        # Not promoted via any threshold-rewrite path; evidence untouched.
        cur = await db.execute(
            "SELECT edge_threshold FROM hypotheses WHERE hypothesis_id = ?", (hid,))
        assert (await cur.fetchone())[0] == 0.02
        cur = await db.execute(
            "SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id = ?"
            " AND signal_generated != 1", (hid,))
        assert (await cur.fetchone())[0] == 0
        assert result.get("action") in ("held", "rejected"), result
    finally:
        await db.close()
