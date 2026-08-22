"""B1 build tests — OutcomeResolver package.

Characterization + new-capability tests for the domain-general evidence
seam. Sports stays green: nothing here mutates existing pipeline behavior;
the betting resolver is a pure adapter over the same tables the existing
tests already pin.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from tools.resolvers import (
    BettingOutcomeResolver,
    EvidenceRecord,
    GenericPredictionResolver,
    InMemoryOutcomeResolver,
    ResolutionSummary,
    STAGE_SEMANTICS,
)
from tools.resolvers.base import (
    BETTING_OUTCOME_MAP,
    OUTCOME_INDETERMINATE,
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
)


# ── helpers ───────────────────────────────────────────────────────────


async def _sports_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY, hypothesis_id TEXT NOT NULL,
            event_id TEXT, sport TEXT, market TEXT, side TEXT,
            signal_odds_american INTEGER, signal_implied_prob REAL,
            model_fair_prob REAL, edge REAL, ev_pct REAL,
            clv_implied REAL, actual_result TEXT, hypothetical_pnl REAL,
            game_date DATE, home_team TEXT, away_team TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    # clv_log minimal shape: bet_id + canonical clv_prob_bp
    await db.execute(
        """CREATE TABLE clv_log (
            bet_id TEXT PRIMARY KEY, clv_prob_bp REAL,
            actual_result TEXT)"""
    )
    await db.execute(
        """CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT,
            hypothesis_id TEXT, book_odds_american INTEGER,
            book_implied_prob REAL, model_fair_prob REAL, edge REAL,
            ev_pct REAL, clv_implied REAL, actual_result TEXT,
            game_date DATE, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"""
    )
    return db


async def _seed_paper(db, hid="h1", n=4):
    for i in range(n):
        await db.execute(
            "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, "
            "signal_odds_american, signal_implied_prob, model_fair_prob, edge, "
            "ev_pct, clv_implied, actual_result, hypothetical_pnl, game_date, "
            "home_team, away_team) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"pt{i}", hid, f"E{i}", -110, 0.524, 0.56, 0.036, 5.0,
             0.53 if i % 2 else 0.50, "won" if i % 2 == 0 else "lost",
             90.0 if i % 2 == 0 else -100.0, "2026-08-01", "Home", "Away"),
        )
        # canonical devigged CLV in bp: positive on even trades
        await db.execute(
            "INSERT INTO clv_log (bet_id, clv_prob_bp, actual_result) "
            "VALUES (?, ?, ?)",
            (f"pt:pt{i}", 25.0 if i % 2 == 0 else -10.0, None),
        )
    await db.commit()


# ── vocabulary mapping ────────────────────────────────────────────────


def test_betting_outcome_map_is_total_over_lifecycle_vocabulary():
    assert BETTING_OUTCOME_MAP["won"] == OUTCOME_POSITIVE
    assert BETTING_OUTCOME_MAP["lost"] == OUTCOME_NEGATIVE
    assert BETTING_OUTCOME_MAP["push"] == OUTCOME_INDETERMINATE


def test_stage_semantics_generalise_without_renaming_storage():
    # Storage names unchanged (every reader in repo depends on them)...
    assert set(STAGE_SEMANTICS) == {
        "draft", "backtesting", "paper_trading", "live", "retired"
    }
    # ...but semantics are domain-general.
    assert STAGE_SEMANTICS["paper_trading"] == "preregistered_forward_testing"
    assert STAGE_SEMANTICS["live"] == "deployed_conclusion"


# ── EvidenceRecord ────────────────────────────────────────────────────


def test_evidence_record_binary_outcome_and_decided():
    won = EvidenceRecord(event_id="a", predicted_prob=0.6,
                         resolved_outcome=OUTCOME_POSITIVE)
    push = EvidenceRecord(event_id="b", predicted_prob=0.6,
                          resolved_outcome=OUTCOME_INDETERMINATE)
    assert won.binary_outcome == 1 and won.is_decided
    assert push.binary_outcome is None and not push.is_decided


def test_from_betting_row_adapts_full_row():
    row = {
        "event_id": "E9", "model_fair_prob": 0.58, "actual_result": "push",
        "book_odds_american": -110, "book_implied_prob": 0.524,
        "clv_prob_bp": 12.0, "game_date": "2026-08-01",
        "home_team": "Home", "away_team": "Away",
    }
    rec = EvidenceRecord.from_betting_row(row)
    assert rec.resolved_outcome == OUTCOME_INDETERMINATE
    assert rec.predicted_prob == 0.58
    assert rec.clv_prob_bp == 12.0
    assert "Home" in rec.context_key and "2026-08-01" in rec.context_key


# ── ResolutionSummary ─────────────────────────────────────────────────


def test_summary_counts_and_rates():
    recs = [
        EvidenceRecord("e1", 0.6, OUTCOME_POSITIVE, payoff=0.9, clv_prob_bp=30),
        EvidenceRecord("e2", 0.6, OUTCOME_NEGATIVE, payoff=-1.0, clv_prob_bp=-20),
        EvidenceRecord("e3", 0.6, OUTCOME_POSITIVE, payoff=0.9, clv_prob_bp=10),
        EvidenceRecord("e4", 0.6, OUTCOME_INDETERMINATE, payoff=0.0),
        EvidenceRecord("e5", 0.6, "unresolved"),
    ]
    s = ResolutionSummary.from_records(recs)
    assert (s.total, s.positive, s.negative, s.indeterminate, s.unresolved) == (5, 2, 1, 1, 1)
    assert s.hit_rate == pytest.approx(2 / 3)
    assert not s.fully_resolved
    assert s.avg_clv_prob_bp == pytest.approx((30 - 20 + 10) / 3)
    assert s.positive_clv_rate == pytest.approx(2 / 3)


# ── BettingOutcomeResolver ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_betting_resolver_yields_generalised_records_with_canonical_clv():
    db = await _sports_db()
    try:
        await _seed_paper(db)
        r = BettingOutcomeResolver(db)
        recs = [rec async for rec in r.iter_evidence("h1")]
        assert len(recs) == 4
        assert all(rec.source == "betting" for rec in recs)
        # Canonical devigged clv_log value wins over raw clv_implied delta.
        by_event = {rec.event_id: rec for rec in recs}
        assert by_event["E0"].clv_prob_bp == pytest.approx(25.0)
        assert by_event["E1"].clv_prob_bp == pytest.approx(-10.0)

        s = await r.summarize("h1")
        assert s.total == 4 and s.hit_rate == pytest.approx(0.5)
        assert s.avg_clv_prob_bp == pytest.approx((25 - 10 + 25 - 10) / 4)

        ok, n = await r.mean_clv_prob_bp("h1")
        assert ok == pytest.approx(7.5) and n == 4
        missing, n2 = await r.mean_clv_prob_bp("nope")
        assert missing is None and n2 == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_betting_resolver_backtest_stage_reads_backtest_events():
    db = await _sports_db()
    try:
        await db.execute(
            "INSERT INTO backtest_events (event_id, hypothesis_id, "
            "book_odds_american, model_fair_prob, actual_result) "
            "VALUES ('B1','hb',-110,0.55,'won')"
        )
        await db.commit()
        r = BettingOutcomeResolver(db)
        recs = [rec async for rec in r.iter_evidence("hb", stage="backtesting")]
        assert len(recs) == 1
        assert recs[0].resolved_outcome == OUTCOME_POSITIVE
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_betting_resolver_has_resolved_min_n():
    db = await _sports_db()
    try:
        await _seed_paper(db)
        r = BettingOutcomeResolver(db)
        assert await r.has_resolved("h1", min_n=3) is True
        assert await r.has_resolved("ghost") is False
    finally:
        await db.close()


# ── Generic resolver: non-sports claims enter the lifecycle ───────────


@pytest.mark.asyncio
async def test_generic_in_memory_resolver_scores_bitcoin_claim():
    """A Bitcoin hash-rate prediction must be scorable with zero sports
    vocabulary."""
    claim = InMemoryOutcomeResolver()
    # 60-day hash-rate-above-threshold forecast, preregistered, now resolved.
    claim.add(EvidenceRecord("btc-day-1", 0.7, OUTCOME_POSITIVE))
    claim.add(EvidenceRecord("btc-day-2", 0.7, OUTCOME_NEGATIVE))
    claim.add(EvidenceRecord("btc-day-3", 0.7, OUTCOME_POSITIVE))

    s = await claim.summarize("any-id")
    assert s.total == 3 and s.fully_resolved
    assert s.hit_rate == pytest.approx(2 / 3)
    assert await claim.has_resolved("any-id", min_n=2)


@pytest.mark.asyncio
async def test_generic_sqlite_resolver_tolerates_missing_tables():
    db = await aiosqlite.connect(":memory:")
    try:
        r = GenericPredictionResolver.Sqlite(db)
        recs = [x async for x in r.iter_evidence("h")]
        assert recs == []
        s = await r.summarize("h")
        assert s.total == 0 and not s.fully_resolved
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_generic_sqlite_resolver_reads_predictions_outcomes():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "claim_id TEXT, event_id TEXT, predicted_prob REAL, "
            "context_key TEXT, created_at DATETIME)"
        )
        await db.execute(
            "CREATE TABLE outcomes (prediction_id INTEGER, resolved_outcome TEXT, "
            "payoff REAL, resolved_at DATETIME)"
        )
        await db.execute(
            "INSERT INTO predictions (claim_id, event_id, predicted_prob, context_key) "
            "VALUES ('mat-science', 'exp-42', 0.15, 'low-base-rate-domain')"
        )
        await db.execute(
            "INSERT INTO predictions (claim_id, event_id, predicted_prob) "
            "VALUES ('mat-science', 'exp-43', 0.15)"
        )
        await db.execute(
            "INSERT INTO outcomes VALUES (1, 'confirmed', 4.0, '2026-08-02')"
        )
        # second prediction unresolved — no outcome row
        await db.commit()

        r = GenericPredictionResolver.Sqlite(db)
        recs = [x async for x in r.iter_evidence("mat-science")]
        assert len(recs) == 2
        hit = [x for x in recs if x.is_decided]
        # 15% base-rate claim that hit once: spectacular at its base rate,
        # ordinary under absolute floors — the base-rate item handles that.
        assert len(hit) == 1 and hit[0].binary_outcome == 1
        s = await r.summarize("mat-science")
        assert s.hit_rate == pytest.approx(1.0) and not s.fully_resolved
    finally:
        await db.close()
