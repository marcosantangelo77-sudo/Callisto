"""Tests for scripts/demote_stale_live_hypotheses.py — the one-time LIVE
cascade migration.

Coverage:
  * Synthetic 5 LIVE hypotheses (3 would-fail, 2 would-pass).
  * Dry-run produces a correct verdict table with no DB mutation.
  * ``--live --yes`` actually mutates: status → paused, demotion_reason
    stored in notes JSON, hypothesis_stats row written with
    stage='live_demoted', wiki article created.
  * Rollback restores pre-cascade state.
  * The ``--live`` flag without ``--yes`` exits non-zero without mutating.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiosqlite
import pytest

# Make sure the repo root is on sys.path so ``scripts.demote_...`` imports.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from scripts import demote_stale_live_hypotheses as cascade  # noqa: E402
from tools.hypothesis import HypothesisManager  # noqa: E402


# ─── fixtures ───────────────────────────────────────────────────────


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
    """Minimal schema — no CHECK constraint on status so we can freely
    insert 'live' / 'paused' rows without running the real migrations.
    """
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
            hypothesis_id TEXT,
            completed_at DATETIME,
            signals_generated INTEGER DEFAULT 0
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
    await db.execute(
        """CREATE TABLE wiki_articles (
            topic TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            related_topics TEXT NOT NULL DEFAULT '[]',
            source_sessions TEXT NOT NULL DEFAULT '[]',
            source_entries TEXT NOT NULL DEFAULT '[]',
            domain TEXT NOT NULL DEFAULT 'GENERAL',
            confidence REAL NOT NULL DEFAULT 0.5,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            compile_count INTEGER NOT NULL DEFAULT 1,
            content_hash TEXT NOT NULL DEFAULT ''
        )"""
    )
    await db.commit()


async def _seed_hypothesis(
    db: aiosqlite.Connection,
    hid: str,
    *,
    status: str = "live",
    overlap_events: list[str] | None = None,
    unique_events: list[str] | None = None,
    paper_trades: int = 0,
    promoted_days_ago: int = 30,
) -> None:
    """Insert a hypothesis plus its backtest signal events and optional
    paper trades."""
    promoted_at = (datetime.now(timezone.utc) - timedelta(days=promoted_days_ago)).isoformat()
    await db.execute(
        "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, "
        "market_type, model_config, status, promoted_at, promoted_by) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            hid, f"name_{hid}", "thesis", "baseball_mlb", "h2h",
            "{}", status, promoted_at, "seed",
        ),
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for evid in (overlap_events or []) + (unique_events or []):
        await db.execute(
            "INSERT INTO backtest_events "
            "(run_id, event_id, hypothesis_id, sport, market, side, book, "
            " book_odds_american, book_implied_prob, model_fair_prob, "
            " edge, ev_pct, signal_generated, actual_result, game_date, "
            " snapshot_time, model_factors) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "r1", evid, hid, "baseball_mlb", "h2h", "Home",
                "draftkings", -110, 0.524, 0.560, 0.036, 5.0, 1,
                "won", today, today + "T00:00:00+00:00",
                json.dumps({"snapshot_quality": "pre_commence"}),
            ),
        )
    for i in range(paper_trades):
        # Spread paper trades across two regimes so the regime-diversity gate
        # added in feat/regime-aware-sizing doesn't incorrectly demote the
        # "passing" seeded hypotheses. Use recent dates (timedelta-based) so
        # significance/window filters still include these trades: alternate
        # between baseball_mlb (currently in regular) and icehockey_nhl
        # (currently in playoffs) — two distinct regime buckets.
        base = datetime.now(timezone.utc) - timedelta(days=i)
        gd = base.strftime("%Y-%m-%d")
        # Even-indexed trades use the hypothesis's own sport; odd-indexed
        # trades are stamped as NHL so we hit a second (sport, phase) bucket.
        sport_i = "baseball_mlb" if i % 2 == 0 else "icehockey_nhl"
        await db.execute(
            "INSERT INTO paper_trades "
            "(trade_id, hypothesis_id, event_id, sport, market, side, book, "
            " signal_time, signal_odds_american, signal_implied_prob, "
            " model_fair_prob, edge, ev_pct, clv_implied, actual_result, "
            " game_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{hid}_pt{i}", hid, f"pt_ev_{hid}_{i}",
                sport_i, "h2h", "Home", "draftkings",
                datetime.now(timezone.utc).isoformat(), -110, 0.524,
                0.56, 0.036, 5.0, 0.53, "won", gd,
            ),
        )
    await db.commit()


async def _build_scenario(db_path: str) -> None:
    """5 synthetic LIVE hypotheses, constructed so exactly 3 fail and 2 pass
    when evaluated against the full paper→live gate via
    ``status_override='paper_trading'``:

      - hyp_passA, hyp_passB: independent event universes (no overlap),
        15 paper trades each → pass every gate.
      - hyp_failOverlapPair_{A,B}: NOT created here; instead we use a
        single overlap failure route — ``hyp_failPaperTrades`` overlaps
        passA directly but also fails paper_trade_sample.  That keeps
        exactly 3 fails + 2 passes.  Overlap-only failure is covered by
        ``hyp_failOverlap`` below, which overlaps a non-pass source so the
        passes stay clean.
      - hyp_failOverlap: 100% of its signals overlap with an orphan
        "source" row (``hyp_src_overlap``) that is NOT in the LIVE set;
        wait — the portfolio_overlap gate checks *existing LIVE* hyps,
        so to trigger overlap we MUST overlap a LIVE row.  We use an
        extra LIVE hyp ``hyp_srcLive`` as the overlap source that ALSO
        fails min_days (so it is rightfully demoted too, and the passes
        stay independent).
      - hyp_failPaperTrades: 0 paper trades.  Independent events.
      - hyp_failMinDays / hyp_srcLive: fresh promotion (1 day ago) and
        acts as the overlap-source for hyp_failOverlap.  Also fails
        min_days itself.

    End state: 2 pass, 3 fail.
    """
    async with aiosqlite.connect(db_path) as db:
        await _init_schema(db)
        # hyp_passA — 10 unique events, 15 paper trades
        await _seed_hypothesis(
            db, "hyp_passA",
            unique_events=[f"passA_ev_{i}" for i in range(10)],
            paper_trades=15,
            promoted_days_ago=30,
        )
        # hyp_passB — 10 unique events (different prefix), 15 paper trades
        await _seed_hypothesis(
            db, "hyp_passB",
            unique_events=[f"passB_ev_{i}" for i in range(10)],
            paper_trades=15,
            promoted_days_ago=30,
        )
        # hyp_failMinDays — fresh promotion (overlap-source) — serves two roles:
        # (1) its min_days gate fails; (2) supplies overlap events for failOverlap.
        await _seed_hypothesis(
            db, "hyp_failMinDays",
            unique_events=[f"md_ev_{i}" for i in range(10)],
            paper_trades=15,
            promoted_days_ago=1,
        )
        # hyp_failOverlap — signals fully overlap failMinDays
        await _seed_hypothesis(
            db, "hyp_failOverlap",
            overlap_events=[f"md_ev_{i}" for i in range(10)],
            paper_trades=15,
            promoted_days_ago=30,
        )
        # hyp_failPaperTrades — independent events, 0 paper trades
        await _seed_hypothesis(
            db, "hyp_failPaperTrades",
            unique_events=[f"pt0_ev_{i}" for i in range(10)],
            paper_trades=0,
            promoted_days_ago=30,
        )


# ─── tests ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_reports_correctly(temp_db_path):
    await _build_scenario(temp_db_path)

    mgr = HypothesisManager(db_path=temp_db_path)
    await mgr.initialize()
    try:
        live = await mgr.list_hypotheses(status="live")
        verdicts = []
        for h in live:
            verdicts.append(await cascade.evaluate_live_row(mgr, h))
    finally:
        await mgr.close()

    by_id = {v["hypothesis_id"]: v for v in verdicts}

    # Failing rows MUST be flagged would_demote
    assert by_id["hyp_failOverlap"]["would_demote"] is True
    assert "portfolio_correlation" in by_id["hyp_failOverlap"]["categories"]

    assert by_id["hyp_failPaperTrades"]["would_demote"] is True
    assert "paper_trade_sample" in by_id["hyp_failPaperTrades"]["categories"]

    assert by_id["hyp_failMinDays"]["would_demote"] is True
    assert "min_days" in by_id["hyp_failMinDays"]["categories"]


@pytest.mark.asyncio
async def test_live_mutation_applies(temp_db_path):
    await _build_scenario(temp_db_path)

    mgr = HypothesisManager(db_path=temp_db_path)
    await mgr.initialize()
    try:
        # IMPORTANT: evaluate ALL verdicts first, THEN mutate.  The
        # portfolio_correlation gate is directional and state-sensitive;
        # demoting a row mid-loop changes the LIVE set for subsequent
        # evaluations (a row may stop failing overlap when its partner
        # is already paused).  ``main_async`` is built the same way.
        live = await mgr.list_hypotheses(status="live")
        verdicts: list[dict] = []
        for h in live:
            verdicts.append(await cascade.evaluate_live_row(mgr, h))
        for v in verdicts:
            if v["would_demote"]:
                await cascade.apply_demotion(mgr, v)

        # Post-state verification
        cur = await mgr._db.execute(
            "SELECT hypothesis_id, status, notes FROM hypotheses "
            "WHERE hypothesis_id LIKE 'hyp_fail%'"
        )
        rows = await cur.fetchall()
        assert len(rows) == 3
        for hid, status, notes in rows:
            assert status == "paused", f"{hid} not demoted (status={status})"
            payload = json.loads(notes)
            assert payload["legacy_grandfather"] is False
            assert payload["demotion_reason"]
            assert payload["previous_status"] == "live"

        # hypothesis_stats must have stage='live_demoted' rows
        cur = await mgr._db.execute(
            "SELECT hypothesis_id FROM hypothesis_stats "
            "WHERE stage = 'live_demoted'"
        )
        demoted_stats = {r[0] for r in await cur.fetchall()}
        assert demoted_stats == {
            "hyp_failOverlap", "hyp_failPaperTrades", "hyp_failMinDays",
        }

        # Wiki articles present
        cur = await mgr._db.execute(
            "SELECT topic FROM wiki_articles WHERE topic LIKE 'live_cascade_demotion_%'"
        )
        topics = [r[0] for r in await cur.fetchall()]
        assert len(topics) == 3

        # The 2 pass rows must stay LIVE
        cur = await mgr._db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'"
        )
        still_live = {r[0] for r in await cur.fetchall()}
        assert still_live == {"hyp_passA", "hyp_passB"}, (
            f"Expected only passA/passB LIVE; got {still_live}"
        )
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_rollback_restores_prior_state(temp_db_path):
    await _build_scenario(temp_db_path)

    mgr = HypothesisManager(db_path=temp_db_path)
    await mgr.initialize()
    try:
        live_before = await mgr.list_hypotheses(status="live")
        assert len(live_before) == 5

        # Snapshot + apply demotions (evaluate first, then mutate)
        backup = await cascade.snapshot_pre_cascade(mgr, Path(temp_db_path).parent)
        verdicts = []
        for h in live_before:
            verdicts.append(await cascade.evaluate_live_row(mgr, h))
        for v in verdicts:
            if v["would_demote"]:
                await cascade.apply_demotion(mgr, v)

        live_after = await mgr.list_hypotheses(status="live")
        paused = await mgr.list_hypotheses(status="paused")
        assert len(live_after) == 2
        assert len(paused) == 3

        # Rollback
        result = await cascade.rollback_from_backup(mgr, backup)
        assert result.get("restored") == 3
        live_restored = await mgr.list_hypotheses(status="live")
        assert len(live_restored) == 5
    finally:
        await mgr.close()
        try:
            os.remove(str(backup))
        except OSError:
            pass


@pytest.mark.asyncio
async def test_live_without_yes_refuses(temp_db_path, capsys):
    """The top-level CLI must refuse ``--live`` without ``--yes`` and exit 2,
    leaving no mutation."""
    await _build_scenario(temp_db_path)

    import argparse
    ns = argparse.Namespace(
        live=True, yes=False, limit=None, reason_filter=None,
        db=temp_db_path, backup_dir=str(Path(temp_db_path).parent),
        rollback=False, backup=None, no_telegram=True,
    )
    code = await cascade.main_async(ns)
    assert code == 2

    # DB must be untouched — still 5 LIVE.
    async with aiosqlite.connect(temp_db_path) as db:
        cur = await db.execute("SELECT COUNT(*) FROM hypotheses WHERE status='live'")
        row = await cur.fetchone()
        assert row[0] == 5
