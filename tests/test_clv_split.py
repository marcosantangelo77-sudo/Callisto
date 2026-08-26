"""Tests for the tools/clv split of tools/clv_tracker.py.

tools/clv_tracker.py is now a thin facade over the ``tools.clv`` package:

- ``tools/clv/constants.py``  — DB_PATH, vig table, reliable-close sources
- ``tools/clv/odds_math.py``  — pure helpers (devig, odds conversion, etc.)
- ``tools/clv/clv_log.py``    — CLVLogMixin (clv_log writers)
- ``tools/clv/reporting.py``  — CLVReportingMixin (reports/queries)
- ``tools/clv/tracker.py``    — CLVTracker core

These tests verify:
  1. Facade re-export parity (every legacy name still importable and
     identical to the canonical object in tools.clv).
  2. The mixins compose correctly on CLVTracker.
  3. Pure helpers behave identically through both import paths.
  4. End-to-end behavior is preserved: record → close line → resolve →
     clv_log row, paper-trade logging, sync idempotency, backfill,
     reports, bankroll, get_all_bets.
"""

import asyncio
import os
import tempfile

import aiosqlite
import pytest

from tools.clv_tracker import CLVTracker, _half_vig_devig

CLV_LOG_DDL = """
    CREATE TABLE clv_log (
        bet_id TEXT PRIMARY KEY,
        event TEXT, outcome TEXT, point REAL, book TEXT,
        our_odds_decimal REAL,
        pinnacle_close_fair_prob REAL,
        pinnacle_close_fair_decimal REAL,
        clv_cents REAL,
        clv_prob_bp REAL,
        actual_result TEXT, actual_pnl REAL,
        close_reliable INTEGER, logged_at TEXT,
        regime_phase_at_placement TEXT
    )
"""

PAPER_TRADES_DDL = """
    CREATE TABLE paper_trades (
        trade_id TEXT PRIMARY KEY,
        event_id TEXT,
        sport TEXT,
        market TEXT,
        side TEXT,
        line REAL,
        book TEXT,
        signal_odds_american INTEGER,
        signal_implied_prob REAL,
        closing_odds INTEGER,
        closing_implied REAL,
        clv_implied REAL,
        actual_result TEXT,
        hypothetical_pnl REAL
    )
"""


# ---------------------------------------------------------------------------
# Facade / package structure tests
# ---------------------------------------------------------------------------

def test_facade_reexports_tracker_class():
    from tools.clv import CLVTracker as PackageTracker
    from tools.clv_tracker import CLVTracker as FacadeTracker
    assert FacadeTracker is PackageTracker


def test_facade_reexports_private_helpers():
    import tools.clv as pkg
    import tools.clv.odds_math as om
    import tools.clv_tracker as facade

    for name in ("_half_vig_devig", "_american_to_decimal",
                 "_regime_stamp", "_interpret_clv"):
        facade_fn = getattr(facade, name)
        pkg_name = name.lstrip("_")
        pkg_fn = getattr(pkg, pkg_name)
        om_fn = getattr(om, pkg_name)
        assert facade_fn is pkg_fn is om_fn, name


def test_facade_reexports_constants():
    import tools.clv.constants as c
    import tools.clv_tracker as facade

    assert facade.DB_PATH == c.DB_PATH
    assert facade._BOOK_VIG_ESTIMATE is c.BOOK_VIG_ESTIMATE
    assert facade._RELIABLE_CLOSE_SOURCES is c.RELIABLE_CLOSE_SOURCES


def test_package_exports_all_names():
    import tools.clv as pkg
    expected = {
        "BOOK_VIG_ESTIMATE", "DB_PATH", "RELIABLE_CLOSE_SOURCES",
        "CLVLogMixin", "CLVReportingMixin", "CLVTracker",
        "american_to_decimal", "half_vig_devig", "interpret_clv",
        "regime_stamp",
    }
    missing = expected - set(pkg.__all__)
    assert not missing, f"missing exports: {missing}"


def test_mixin_composition():
    assert issubclass(CLVTracker, __import__(
        "tools.clv.clv_log", fromlist=["CLVLogMixin"]).CLVLogMixin)
    assert issubclass(CLVTracker, __import__(
        "tools.clv.reporting", fromlist=["CLVReportingMixin"]).CLVReportingMixin)


# ---------------------------------------------------------------------------
# Pure helper tests (through the facade, matching legacy behavior)
# ---------------------------------------------------------------------------

def test_half_vig_devig_basic():
    assert _half_vig_devig(0.5, 0.05) == pytest.approx(0.5 / 1.025)


@pytest.mark.parametrize("bad", [None, 0, -0.2])
def test_half_vig_devig_passthrough_on_bad_input(bad):
    assert _half_vig_devig(bad, 0.05) == bad


def test_half_vig_devig_bounded_and_type_error_safe():
    assert _half_vig_devig(1.5, 0.05) == 1.0
    assert _half_vig_devig("not-a-number", 0.05) == "not-a-number"


def test_american_to_decimal():
    from tools.clv_tracker import _american_to_decimal
    assert _american_to_decimal(+150) == pytest.approx(2.5)
    assert _american_to_decimal(-110) == pytest.approx(1.9090909)
    assert _american_to_decimal(None) is None
    assert _american_to_decimal(0) is None
    assert _american_to_decimal("junk") is None


def test_interpret_clv_tiers():
    from tools.clv_tracker import _interpret_clv
    assert "STRONG" in _interpret_clv(0.04)
    assert "POSITIVE" in _interpret_clv(0.02)
    assert "SLIGHT EDGE" in _interpret_clv(0.01)
    assert "BREAK EVEN" in _interpret_clv(0.0)
    assert "SLIGHT NEGATIVE" in _interpret_clv(-0.01)
    assert "NEGATIVE" in _interpret_clv(-0.02)


def test_regime_stamp_degrades_gracefully():
    from tools.clv_tracker import _regime_stamp
    assert _regime_stamp("") is None


# ---------------------------------------------------------------------------
# Async integration tests
# ---------------------------------------------------------------------------

async def _make_tracker(db_path: str) -> CLVTracker:
    tracker = CLVTracker(db_path=db_path)
    await tracker.initialize()
    async with aiosqlite.connect(db_path) as db:
        # clv_log and paper_trades are created by migrations in prod; create
        # them here so the split modules can be exercised end-to-end.
        await db.execute(CLV_LOG_DDL)
        await db.execute(PAPER_TRADES_DDL)
        await db.commit()
    return tracker


@pytest.mark.asyncio
async def test_full_flow_record_close_resolve_logs_clv():
    """Record bet → record closing line → resolve; clv_log row must appear
    with positive clv_prob_bp (we beat the close)."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            bet_id = await tracker.record_bet(
                sport="basketball_nba",
                game_description="Lakers @ Celtics",
                team="Boston Celtics",
                market="spreads",
                bookmaker="DraftKings",
                placement_odds=+150,
                placement_point=2.5,
                stake=100,
                event_id="evt-1",
            )

            await tracker.record_closing_line(
                event_id="evt-1",
                market="spreads",
                team="boston celtics",  # case-insensitive match required
                closing_odds=+130,
                source="Pinnacle",
                sport="basketball_nba",
            )

            summary = await tracker.resolve_bet(bet_id, "won", payout=250)
            assert summary["result"] == "won"

            cursor = await tracker._db.execute(
                "SELECT clv_prob_bp, close_reliable FROM clv_log WHERE bet_id = ?",
                (str(bet_id),),
            )
            row = await cursor.fetchone()
            assert row is not None
            clv_prob_bp, close_reliable = row
            assert clv_prob_bp is not None and clv_prob_bp > 0
            assert bool(close_reliable) is True

            # bets row updated with close + clv fields
            bets = await tracker.get_all_bets()
            assert len(bets) == 1
            assert bets[0]["closing_odds"] == 130
            assert bets[0]["clv_odds"] == 20  # +150 - +130

            report = await tracker.get_clv_report()
            assert report["total_bets"] == 1
            assert report["results"]["won"] == 1
            assert report["with_closing_line"] == 1
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_paper_trade_log_and_sync_idempotent():
    """log_paper_trade_clv writes pt-namespaced rows; the sync anti-join
    skips already-logged trades."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            trade = {
                "trade_id": "t1",
                "event_id": "evt-pt",
                "side": "Team X",
                "line": None,
                "book": "FanDuel",
                "signal_odds_american": +150,
                "signal_implied_prob": 0.4,
                "closing_odds": +130,
                "closing_implied": 0.435,
                "actual_result": "won",
                "hypothetical_pnl": 150,
                "sport": "basketball_nba",
            }
            written = await tracker.log_paper_trade_clv(trade)
            assert written is True

            cursor = await tracker._db.execute(
                "SELECT clv_prob_bp, close_reliable, book FROM clv_log "
                "WHERE bet_id = 'pt:t1'"
            )
            row = await cursor.fetchone()
            assert row is not None
            clv_prob_bp, close_reliable, book = row
            assert clv_prob_bp > 0
            assert bool(close_reliable) is True
            assert book == "fanduel"

            # Missing inputs → no write.
            assert await tracker.log_paper_trade_clv({"trade_id": "t2"}) is False
            assert await tracker.log_paper_trade_clv(
                {"trade_id": "t3", "actual_result": "won"}) is False
            assert await tracker.log_paper_trade_clv(
                {"actual_result": "won", "signal_implied_prob": 0.4}) is False

            # Sync finds nothing new (t1 already logged).
            assert await tracker.sync_paper_trades_to_clv_log() == 0
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_sync_backfills_unlogged_paper_trades():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO paper_trades (trade_id, side, book, "
                    "signal_odds_american, signal_implied_prob, closing_odds, "
                    "closing_implied, actual_result, hypothetical_pnl) "
                    "VALUES ('p9', 'Side A', 'BetMGM', -110, 0.524, -105, "
                    "0.512, 'lost', -100)"
                )
                await db.commit()

            written = await tracker.sync_paper_trades_to_clv_log()
            assert written == 1
            # Idempotent second pass.
            assert await tracker.sync_paper_trades_to_clv_log() == 0

            cursor = await tracker._db.execute(
                "SELECT clv_prob_bp FROM clv_log WHERE bet_id = 'pt:p9'"
            )
            row = await cursor.fetchone()
            assert row is not None and row[0] < 0  # worse price than close
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_backfill_clv_log_covers_bets_and_paper_trades():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            bet_id = await tracker.record_bet(
                sport="mlb", game_description="A @ B", team="B",
                market="moneyline", bookmaker="Pinnacle",
                placement_odds=-120, stake=100, event_id="evt-bf",
            )
            await tracker.resolve_bet(bet_id, "lost")

            async with aiosqlite.connect(db_path) as db:
                await db.execute(
                    "INSERT INTO paper_trades (trade_id, side, book, "
                    "signal_odds_american, signal_implied_prob, "
                    "actual_result) VALUES ('pb1', 'S', 'Circa', +120, "
                    "0.455, 'push')"
                )
                await db.commit()

            total = await tracker.backfill_clv_log()
            assert total == 2  # 1 real bet + 1 paper trade

            rows = await tracker._db.execute(
                "SELECT bet_id FROM clv_log ORDER BY bet_id"
            )
            ids = {r[0] for r in await rows.fetchall()}
            assert str(bet_id) in ids
            assert "pt:pb1" in ids

            # Second backfill is safe (INSERT OR REPLACE); the already-synced
            # paper trade is skipped by the anti-join, so only the real bet
            # re-writes — total drops to 1 but no row is lost.
            assert await tracker.backfill_clv_log() == 1
            cursor = await tracker._db.execute(
                "SELECT COUNT(*) FROM clv_log"
            )
            assert (await cursor.fetchone())[0] == 2
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_bankroll_history_and_initial_balance():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            await tracker.set_initial_bankroll(5000.0)
            history = await tracker.get_bankroll_history()
            assert len(history) == 1
            assert history[0]["balance"] == 5000.0
            assert history[0]["description"] == "Initial bankroll"
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_get_all_bets_filters():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            b1 = await tracker.record_bet(
                sport="nba", game_description="g1", team="T1", market="spreads",
                bookmaker="DK", placement_odds=-110,
            )
            await tracker.record_bet(
                sport="mlb", game_description="g2", team="T2", market="total",
                bookmaker="FD", placement_odds=+105,
            )
            nba = await tracker.get_all_bets(sport="nba")
            assert len(nba) == 1 and nba[0]["id"] == b1
            pending = await tracker.get_all_bets(result="pending", limit=10)
            assert len(pending) == 2
        finally:
            await tracker.close()


@pytest.mark.asyncio
async def test_resolve_missing_bet_returns_error():
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        tracker = await _make_tracker(db_path)
        try:
            result = await tracker.resolve_bet(9999, "won", payout=1)
            assert "error" in result
        finally:
            await tracker.close()
