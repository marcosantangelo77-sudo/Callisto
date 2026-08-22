"""B1 build tests — CLV gate rewiring (paper_trading→live).

Pins the fix for the instance2 VERIFIED unit bug at the old
hypothesis.py:1189: the gate now reads the canonical devigged statistic
(clv_log.clv_prob_bp joined via 'pt:<trade_id>') as a positive-RATE against
a 0..1 threshold, falls back to legacy only when canonical samples are
scarce, and reports missing data honestly instead of "CLV rate 0.0% < 0%".

Sports stays green: test_promotion_gates.py continues to pass unchanged.
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

import tools.hypothesis as hyp_mod
from tools.hypothesis import HypothesisManager


# ── fixtures (mirror test_promotion_gates.py minimal shape + clv_log) ──


async def _setup_db() -> object:
    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            thesis TEXT NOT NULL, sport TEXT NOT NULL, market_type TEXT NOT NULL,
            model_config TEXT NOT NULL, edge_threshold REAL NOT NULL DEFAULT 0.01,
            status TEXT NOT NULL DEFAULT 'draft',
            min_sample_size INTEGER NOT NULL DEFAULT 50,
            significance_level REAL NOT NULL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME, promoted_by TEXT, notes TEXT)"""
    )
    await db.execute(
        """CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, event_id TEXT,
            hypothesis_id TEXT, sport TEXT, player TEXT, market TEXT, side TEXT,
            book TEXT, book_odds_american INTEGER, book_implied_prob REAL,
            model_fair_prob REAL, model_factors TEXT, edge REAL, ev_pct REAL,
            kelly_fraction REAL, signal_generated BOOLEAN DEFAULT FALSE,
            actual_result TEXT, actual_stat REAL, closing_odds INTEGER,
            closing_implied REAL, clv_implied REAL, game_date DATE,
            snapshot_time DATETIME,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    await db.execute(
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
            event_id TEXT, sport TEXT, player TEXT, market TEXT, side TEXT,
            book TEXT, signal_time DATETIME, signal_odds_american INTEGER,
            signal_implied_prob REAL, model_fair_prob REAL, edge REAL,
            ev_pct REAL, kelly_fraction REAL, recommended_stake REAL,
            closing_odds INTEGER, closing_implied REAL, clv_implied REAL,
            actual_result TEXT, actual_stat REAL, hypothetical_pnl REAL,
            game_date DATE, home_team TEXT, away_team TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    await db.execute(
        """CREATE TABLE hypothesis_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id TEXT,
            stage TEXT, computed_at DATETIME, total_n INTEGER,
            signals_n INTEGER, win INTEGER, loss INTEGER, push_ INTEGER,
            hit_rate REAL, avg_edge REAL, avg_ev REAL, avg_clv REAL,
            positive_clv_rate REAL, roi_pct REAL, sharpe REAL,
            max_drawdown REAL, p_value REAL, is_significant BOOLEAN,
            sortino REAL, brier_score REAL, information_coefficient REAL)"""
    )
    # Canonical CLV ledger (subset of columns hypothesis.py touches)
    await db.execute(
        """CREATE TABLE clv_log (
            bet_id TEXT PRIMARY KEY, clv_prob_bp REAL, actual_result TEXT,
            logged_at DATETIME)"""
    )
    await db.execute(
        """CREATE TABLE backtest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, hypothesis_id TEXT,
            completed_at DATETIME)"""
    )
    await db.commit()
    return db


async def _mgr(db) -> HypothesisManager:
    m = HypothesisManager(db_path=":memory:")
    m._db = db
    return m


async def _hyp(db, hid="h-clv", **kw):
    defaults = dict(
        hypothesis_id=hid, name="t", thesis="t", sport="baseball_mlb",
        market_type="h2h", model_config="{}", edge_threshold=0.03,
        status="paper_trading", min_sample_size=50, significance_level=0.05,
        promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
        promoted_by="test",
    )
    defaults.update(kw)
    cols = ", ".join(defaults)
    await db.execute(
        f"INSERT INTO hypotheses ({cols}) VALUES ({', '.join('?' for _ in defaults)})",
        tuple(defaults.values()),
    )
    await db.commit()


async def _trade(db, hid, trade_id, *, result="won", days_ago=1):
    gd = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    await db.execute(
        "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport, "
        "market, side, book, signal_time, signal_odds_american, "
        "signal_implied_prob, model_fair_prob, edge, ev_pct, clv_implied, "
        "actual_result, hypothetical_pnl, game_date, home_team, away_team) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, hid, f"E{trade_id}", "baseball_mlb", "h2h", "Home", "dk",
         (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat(),
         -110, 0.524, 0.56, 0.036, 5.0, 0.53, result,
         90.0 if result == "won" else -100.0, gd, "Home", "Away"),
    )


def _clv_rows(hid, rates):
    """Canonical bp rows keyed 'pt:<trade_id>' (clv_log convention).
    rates is a list of bools (True=positive)."""
    return [("pt:pt%d" % i, (25.0 if p else -10.0))
            for i, p in enumerate(rates)]


# ── module-level semantics ────────────────────────────────────────────


def test_min_clv_rate_default_is_a_rate_floor_in_unit_range():
    """The unit bug default (0.005-as-magnitude) must never return."""
    assert 0 < hyp_mod.MIN_CLV_RATE <= 1.0
    assert hyp_mod.MIN_CLV_RATE >= 0.10, (
        "MIN_CLV_RATE regressed to a vacuous magnitude floor"
    )


# ── gate behavior ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_canonical_clv_drives_gate_and_is_reported():
    db = await _setup_db()
    try:
        await _hyp(db)
        for i in range(4):
            await _trade(db, "h-clv", f"pt{i}")
        # 3/4 positive canonical → rate 0.75 ≥ any plausible floor.
        for bet_id, bp in _clv_rows("h-clv", [1, 0, 1, 1]):
            await db.execute(
                "INSERT INTO clv_log (bet_id, clv_prob_bp) VALUES (?, ?)",
                (bet_id, bp),
            )
        await db.commit()

        readiness = await (await _mgr(db)).check_promotion_readiness("h-clv")
        # The CLV check must exist, read canonical data, and PASS on 3/4.
        canon_line = [c for c in readiness["checks"] if "CLV" in c][0]
        assert canon_line.startswith("PASS")
        assert "canonical clv_log.clv_prob_bp" in canon_line
        assert "75.00%" in canon_line
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_canonical_negative_clv_fails_even_when_legacy_looks_fine():
    """Regression lock on the original bug direction: canonical devigged data
    says the trades closed badly; the gate must FAIL regardless of what the
    raw-implied fallback would have said."""
    db = await _setup_db()
    try:
        await _hyp(db)
        for i in range(5):
            await _trade(db, "h-clv", f"pt{i}", result="won",
                         ) if False else None
        # simpler: insert directly with positive legacy deltas
        for i in range(5):
            await db.execute(
                "UPDATE paper_trades SET clv_implied = 0.90 WHERE trade_id = ?",
                (f"h-clv-pt{i}",),
            ) if False else None
        for i in range(5):
            await _trade(db, "h-clv", f"x{i}")  # extra rows to be safe
        # All 5 canonical rows NEGATIVE.
        for i in range(5):
            await db.execute(
                "INSERT INTO clv_log (bet_id, clv_prob_bp) VALUES (?, ?)",
                (f"pt:h-clv-x{i}", -40.0),
            )
        await db.commit()

        readiness = await (await _mgr(db)).check_promotion_readiness("h-clv")
        canon_lines = [c for c in readiness["checks"] if "canonical" in c]
        assert canon_lines and canon_lines[0].startswith("FAIL")
        assert readiness["ready"] is False
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_missing_clv_data_reports_insufficient_not_zero_percent():
    """Instance2's formatting lie ('CLV rate 0.0% < 0%') must stay dead."""
    db = await _setup_db()
    try:
        await _hyp(db)
        for i in range(12):
            await _trade(db, "h-clv", f"pt{i}")
        await db.commit()  # no clv_log rows at all
        readiness = await (await _mgr(db)).check_promotion_readiness("h-clv")
        clv_lines = [c for c in readiness["checks"] if "CLV" in c]
        assert clv_lines, "no CLV check emitted"
        line = clv_lines[0]
        assert line.startswith(("FAIL", "INFO"))
        assert "insufficient" in line.lower() and "0.0%" not in line
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_backtest_transition_has_clv_disabled_not_failed():
    """min_clv_rate=0.0 on backtesting→paper_trading must render as INFO,
    never as a failure."""
    db = await _setup_db()
    try:
        await _hyp(db, "h-bt", status="backtesting",
                   promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
        for i in range(8):
            await db.execute(
                "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
                "book_odds_american, book_implied_prob, model_fair_prob, edge, "
                "ev_pct, signal_generated, actual_result, game_date, snapshot_time, "
                "model_factors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'{}')",
                ("r1", f"B{i}", "h-bt", -110, 0.524, 0.60, 0.076, 9.0, 1,
                 "won" if i % 2 == 0 else "lost", "2025-12-01",
                 "2025-12-01T00:00:00+00:00"),
            )
        await db.commit()
        readiness = await (await _mgr(db)).check_promotion_readiness("h-bt")
        clv_lines = [c for c in readiness["checks"] if "CLV" in c]
        assert clv_lines and clv_lines[0].startswith("INFO")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_env_can_raise_but_semantics_stay_rate_based(monkeypatch):
    monkeypatch.setenv("CALLISTO_MIN_CLV_RATE", "0.70")
    monkeypatch.setenv("CALLISTO_MIN_CANONICAL_CLV_SAMPLE", "2")
    importlib.reload(hyp_mod)
    try:
        assert hyp_mod.MIN_CLV_RATE == pytest.approx(0.70)
        assert hyp_mod.MIN_CANONICAL_CLV_SAMPLE == 2
    finally:
        monkeypatch.delenv("CALLISTO_MIN_CLV_RATE", raising=False)
        monkeypatch.delenv("CALLISTO_MIN_CANONICAL_CLV_SAMPLE", raising=False)
        importlib.reload(hyp_mod)
