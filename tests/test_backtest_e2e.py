"""
End-to-end integration test for the backtest -> resolve -> evaluate pipeline.

Validates:
  1. Schema setup in an in-memory SQLite database
  2. Hypothesis creation (NBA spreads, cross-book edge detection)
  3. Historical odds cache with multi-book data (DraftKings target, FanDuel + BetMGM comparison)
  4. BacktestEngine.run_backtest() produces backtest_events with signals
  5. resolve_from_game_results() matches events to game_results and resolves them
  6. Resolution correctness: won/lost/push matches what the scores dictate
  7. evaluate_significance() produces a non-empty report with real numbers
  8. Paper trade resolution via DataCollector.resolve_game_level_outcomes()

The key invariant: team names match between odds data and game_results,
the resolution logic is correct, and the full pipeline produces real numbers.
"""

import json
import os
import tempfile
import uuid

import aiosqlite
import pytest
import pytest_asyncio

from tools.schema import SCHEMA_SQL
from tools.backtest import BacktestEngine
from tools.historical_odds import HistoricalOddsFetcher
from tools.hypothesis import HypothesisManager
from tools.data_collector import DataCollector


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def db_path(tmp_path):
    """Create a temp SQLite database with the full Callisto schema."""
    path = str(tmp_path / "test_backtest.db")
    async with aiosqlite.connect(path) as db:
        await db.executescript(SCHEMA_SQL)
        # Also create the odds_snapshots table (normally created by line_monitor)
        # so _enrich_snapshot_with_multibook doesn't crash.
        # closing_lines is normally created by CLVTracker.initialize(); data_collector
        # queries it directly during paper-trade resolution so we need it too.
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS odds_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sport TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                game_count INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS closing_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL,
                sport TEXT,
                captured_at TEXT NOT NULL,
                source TEXT DEFAULT 'pinnacle',
                market TEXT,
                team TEXT,
                closing_odds INTEGER,
                closing_point REAL,
                closing_implied REAL
            );
            CREATE INDEX IF NOT EXISTS idx_closing_event ON closing_lines(event_id, market);
        """)
        await db.commit()
    return path


@pytest_asyncio.fixture
async def hypothesis_manager(db_path):
    hm = HypothesisManager(db_path=db_path)
    await hm.initialize()
    yield hm
    await hm.close()


@pytest_asyncio.fixture
async def historical_fetcher(db_path):
    hf = HistoricalOddsFetcher(db_path=db_path)
    await hf.initialize()
    yield hf
    await hf.close()


@pytest_asyncio.fixture
async def backtest_engine(hypothesis_manager, historical_fetcher, db_path):
    engine = BacktestEngine(
        hypothesis_manager=hypothesis_manager,
        historical_fetcher=historical_fetcher,
        db_path=db_path,
    )
    await engine.initialize()
    yield engine
    await engine.close()


@pytest_asyncio.fixture
async def data_collector(db_path):
    dc = DataCollector(db_path=db_path)
    await dc.initialize()
    yield dc
    await dc.close()


# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

SPORT = "basketball_nba"
GAME_DATE = "2025-12-15"
HOME_TEAM = "Los Angeles Lakers"
AWAY_TEAM = "Boston Celtics"
HOME_SCORE = 110
AWAY_SCORE = 105


def build_odds_api_snapshot(
    home=HOME_TEAM,
    away=AWAY_TEAM,
    game_date=GAME_DATE,
):
    """
    Build a realistic historical odds API response with multi-book data.

    The key to generating cross-book edges: comparison books (FanDuel, BetMGM,
    Caesars, PointsBet) price the home team MORE aggressively than DraftKings.
    When devigged, these comparison books imply ~54% on the home side, but DK
    offers -110 (only 52.4% implied). That gap is the edge: fair value says 54%
    but you can buy it at 52.4%.

    DraftKings (target): Home -3.5 at -110 / Away +3.5 at -110  (standard vig)
    FanDuel (comparison): Home -3.5 at -130 / Away +3.5 at +110  (prices home heavier)
    BetMGM (comparison):  Home -3.5 at -125 / Away +3.5 at +105  (prices home heavier)
    Caesars (comparison): Home -3.5 at -128 / Away +3.5 at +108  (prices home heavier)
    PointsBet (comparison):Home -3.5 at -122 / Away +3.5 at +102 (prices home heavier)

    Devigged consensus fair prob for home ~54%, creating +3% EV on DK's -110 line.

    NOTE: The backtest engine enforces MIN_BOOKS_FOR_SIGNAL = 4 (non-target books).
    Therefore we include 5 total books (1 target + 4 comparison) so signals can fire.

    Also includes totals and h2h with similar cross-book divergence. For h2h
    we keep the home fair prob below 80% (heavy_fav cutoff) so signals still
    fire — achieved with prices around -150/+130.
    """
    event_id = f"nba-{game_date}-{home.replace(' ', '_')}-{away.replace(' ', '_')}"
    return {
        "sport": SPORT,
        "date": game_date,
        "timestamp": f"{game_date}T12:00:00Z",
        "games": [
            {
                "id": event_id,
                "sport_key": SPORT,
                "commence_time": f"{game_date}T00:00:00Z",
                "home_team": home,
                "away_team": away,
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home, "price": -110, "point": -3.5},
                                    {"name": away, "price": -110, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 220.5},
                                    {"name": "Under", "price": -110, "point": 220.5},
                                ],
                            },
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": -150},
                                    {"name": away, "price": 130},
                                ],
                            },
                        ],
                    },
                    {
                        "key": "fanduel",
                        "title": "FanDuel",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home, "price": -130, "point": -3.5},
                                    {"name": away, "price": 110, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -130, "point": 220.5},
                                    {"name": "Under", "price": 110, "point": 220.5},
                                ],
                            },
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": -180},
                                    {"name": away, "price": 155},
                                ],
                            },
                        ],
                    },
                    {
                        "key": "betmgm",
                        "title": "BetMGM",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home, "price": -125, "point": -3.5},
                                    {"name": away, "price": 105, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -125, "point": 220.5},
                                    {"name": "Under", "price": 105, "point": 220.5},
                                ],
                            },
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": -175},
                                    {"name": away, "price": 150},
                                ],
                            },
                        ],
                    },
                    {
                        "key": "caesars",
                        "title": "Caesars",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home, "price": -128, "point": -3.5},
                                    {"name": away, "price": 108, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -128, "point": 220.5},
                                    {"name": "Under", "price": 108, "point": 220.5},
                                ],
                            },
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": -178},
                                    {"name": away, "price": 152},
                                ],
                            },
                        ],
                    },
                    {
                        "key": "pointsbetus",
                        "title": "PointsBet",
                        "markets": [
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": home, "price": -122, "point": -3.5},
                                    {"name": away, "price": 102, "point": 3.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -122, "point": 220.5},
                                    {"name": "Under", "price": 102, "point": 220.5},
                                ],
                            },
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": home, "price": -172},
                                    {"name": away, "price": 147},
                                ],
                            },
                        ],
                    },
                ],
            },
        ],
        "game_count": 1,
    }


async def insert_game_result(db_path, sport=SPORT, game_date=GAME_DATE,
                              home=HOME_TEAM, away=AWAY_TEAM,
                              home_score=HOME_SCORE, away_score=AWAY_SCORE):
    """Insert a known game result into game_results table."""
    total = home_score + away_score
    margin = home_score - away_score
    winner = home if margin > 0 else away if margin < 0 else "push"
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO game_results "
            "(sport, game_date, home_team, away_team, home_score, away_score, "
            "total_score, spread_result, winner) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sport, game_date, home, away, home_score, away_score,
             total, margin, winner),
        )
        await db.commit()


async def insert_cached_odds(db_path, snapshot, sport=SPORT, game_date=GAME_DATE):
    """Insert the snapshot into historical_odds_cache so the fetcher finds it.

    Writes BOTH the legacy key ('h2h,spreads,totals') and the new
    lookahead-tagged key ('h2h,spreads,totals|lead=60') so tests work
    under the no-lookahead cache-keying introduced 2026-04-22.
    """
    async with aiosqlite.connect(db_path) as db:
        for market_key in ("h2h,spreads,totals", "h2h,spreads,totals|lead=60"):
            await db.execute(
                "INSERT OR REPLACE INTO historical_odds_cache "
                "(sport, snapshot_date, event_id, market_type, response_json, credits_cost) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (sport, game_date, None, market_key,
                 json.dumps(snapshot), 0),
            )
        await db.commit()


async def create_test_hypothesis(
    hypothesis_manager,
    market_type="spreads",
    edge_threshold=-1.0,
    name_suffix="",
):
    """
    Create a test NBA cross-book hypothesis.

    Uses edge_threshold=-1.0 by default so that ALL events are treated as signals.
    This is intentional for integration testing: we want to validate the full
    pipeline mechanics (create events -> resolve -> evaluate), not the edge
    detection thresholds (those are unit-tested elsewhere in test_devig.py).

    For h2h and totals markets, the backtest engine enforces side_filter_required
    (FWER audit, 2026-04-22) to prevent both-sides double-counting. These e2e
    tests intentionally exercise BOTH sides, so we mark the test hypotheses
    with legacy=True to grandfather them past that gate.
    """
    model_config = {
        "target_book": "draftkings",
        "devig_method": "power",
        "consensus_min_books": 1,
        # legacy=True forces deterministic `best_edge` signal collapse (by
        # max edge per event_id) instead of random_row. Without this, tests
        # that depend on which side of a spread gets picked are flaky because
        # random_row chooses non-deterministically (though seeded per hid).
        # It also grandfathers binary-both-sides tests (h2h/totals) past the
        # FWER side_filter gate so both sides can be evaluated together.
        "legacy": True,
    }

    hid = await hypothesis_manager.create_hypothesis(
        name=f"NBA Cross-Book {market_type.title()} Edges (Test){name_suffix}",
        thesis=f"Cross-book devigging reveals mispriced NBA {market_type} on DraftKings",
        sport=SPORT,
        market_type=market_type,
        model_config=model_config,
        edge_threshold=edge_threshold,
        min_sample_size=1,     # Low for testing
        significance_level=0.10,
    )
    return hid


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestBacktestEndToEnd:
    """Full pipeline: backtest -> resolve -> evaluate."""

    @pytest.mark.asyncio
    async def test_full_pipeline(self, backtest_engine, hypothesis_manager, db_path):
        """
        The main e2e test. Seeds data, runs the full backtest pipeline,
        and verifies every stage produces correct output.

        Uses edge_threshold=-1.0 so both sides of every spread are signals.

        `_get_backtest_signals` collapses multiple book-level rows with the
        same (event_id, side) into a single signal (one per unique bet).
        We seed TWO distinct games so sample_size is >= 2 and
        evaluate_significance() can produce a full statistical report.
        """
        # ── Setup ──
        hid = await create_test_hypothesis(hypothesis_manager, market_type="spreads")

        # Signals are collapsed to one row per unique event_id. To guarantee
        # at least 1 win AND 1 loss in the significance report, we seed two
        # games where the home team (Lakers in G1, Warriors in G2) is also
        # the collapsed side: Game 1 home covers, Game 2 home fails to cover.
        # Game 1: Lakers host Celtics, Lakers win 110-105 (home -3.5 covers)
        snapshot1 = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot1)
        await insert_game_result(db_path)

        # Game 2: Warriors host Knicks, Warriors lose 100-115 (home -3.5 loses)
        game2_date = "2025-12-16"
        home2, away2 = "Golden State Warriors", "New York Knicks"
        snapshot2 = build_odds_api_snapshot(home=home2, away=away2, game_date=game2_date)
        await insert_cached_odds(db_path, snapshot2, game_date=game2_date)
        await insert_game_result(
            db_path, game_date=game2_date, home=home2, away=away2,
            home_score=100, away_score=115,
        )
        snapshot = snapshot1  # legacy local reference used below for assertions

        # ── Run backtest ──
        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=game2_date,
            credit_budget=0,  # 0 budget = use only cached data
        )

        assert "error" not in result, f"Backtest returned error: {result.get('error')}"
        assert result["hypothesis_id"] == hid
        assert result["total_events"] > 0, "No events were processed"

        run_id = result["run_id"]

        # ── Verify backtest_events were created ──
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            total_events = row[0]
            assert total_events >= 2, (
                f"Expected at least 2 events (both spread sides), got {total_events}"
            )

            # ── Verify all events are signals (edge_threshold=-1.0) ──
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE run_id = ? AND signal_generated = 1",
                (run_id,),
            )
            row = await cursor.fetchone()
            signal_count = row[0]
            assert signal_count >= 2, (
                f"Expected at least 2 signals (both spread sides with threshold=-1), "
                f"got {signal_count} out of {total_events} events."
            )

            # ── Verify resolution happened (actual_result IS NOT NULL) ──
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE run_id = ? AND actual_result IS NOT NULL",
                (run_id,),
            )
            row = await cursor.fetchone()
            resolved_count = row[0]
            assert resolved_count >= 2, (
                f"Expected at least 2 resolved events, got {resolved_count}. "
                "Team name matching between odds data and game_results likely failed."
            )

            # ── Verify resolution correctness for spreads ──
            # Lakers won by 5 (110-105), spread was -3.5 for Lakers.
            # Lakers -3.5: margin=5, adjusted=5+(-3.5)=1.5 > 0 -> WON
            # Celtics +3.5: margin=-5, adjusted=-5+3.5=-1.5 < 0 -> LOST
            cursor = await db.execute(
                "SELECT side, line, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'spreads' AND actual_result IS NOT NULL",
                (run_id,),
            )
            spread_rows = await cursor.fetchall()
            assert len(spread_rows) >= 2, "Expected at least 2 resolved spread events"

            # Note: the backtest engine stores the SIGNED point as `line`
            # (home = -3.5, away = +3.5). Resolution logic then uses the
            # signed line directly:
            #   home: margin + line  ->  5 + (-3.5) = 1.5 > 0 -> WON
            #   away: -margin + line -> -5 + 3.5    = -1.5 < 0 -> LOST
            lakers_found = False
            celtics_found = False
            for side, line, actual_result in spread_rows:
                if side == HOME_TEAM and line == -3.5:
                    assert actual_result == "won", (
                        f"Lakers -3.5 should be WON (margin=5, adjusted=5+(-3.5)=1.5), "
                        f"got {actual_result}"
                    )
                    lakers_found = True
                elif side == AWAY_TEAM and line == 3.5:
                    assert actual_result == "lost", (
                        f"Celtics +3.5 should be LOST (adjusted=-5+3.5=-1.5), "
                        f"got {actual_result}"
                    )
                    celtics_found = True
            assert lakers_found, "No Lakers spread event found"
            assert celtics_found, "No Celtics spread event found"

        # ── Verify evaluate_significance() produced a real report ──
        sig_report = result.get("significance", {})
        assert sig_report.get("sample_size", 0) >= 2, (
            f"Significance report needs >= 2 resolved events, got {sig_report.get('sample_size')}"
        )
        # Should have results with wins/losses since events were resolved
        results_section = sig_report.get("results", {})
        sig_section = sig_report.get("significance", {})
        edge_section = sig_report.get("edge_metrics", {})

        # We have 1 win (Lakers covers) and 1 loss (Celtics doesn't cover)
        assert results_section.get("wins", 0) >= 1, "Expected at least 1 win"
        assert results_section.get("losses", 0) >= 1, "Expected at least 1 loss"

        # Hit rate, p-values, etc. should be present and numeric
        assert sig_report.get("stage") == "backtest"
        assert isinstance(sig_section.get("p_value_binomial"), (int, float))
        assert isinstance(sig_section.get("p_value_ttest"), (int, float))
        assert isinstance(sig_section.get("z_score"), (int, float))
        assert isinstance(edge_section.get("avg_edge"), (int, float))
        assert isinstance(edge_section.get("avg_ev"), (int, float))
        assert isinstance(edge_section.get("roi_pct"), (int, float))

    @pytest.mark.asyncio
    async def test_totals_resolution(self, backtest_engine, hypothesis_manager, db_path):
        """
        Verify totals market resolution: Over/Under against actual total score.
        Total score = 215 (110+105), line = 220.5.
        Over 220.5: 215 < 220.5 -> LOST
        Under 220.5: 215 < 220.5 -> WON
        """
        hid = await create_test_hypothesis(hypothesis_manager, market_type="totals")
        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        await insert_game_result(db_path)

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT side, line, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'totals' AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) >= 2, "Expected at least 2 resolved totals events"

            for side, line, actual_result in rows:
                if side.lower() == "over" and line == 220.5:
                    assert actual_result == "lost", (
                        f"Over 220.5 should be LOST (total=215), got {actual_result}"
                    )
                elif side.lower() == "under" and line == 220.5:
                    assert actual_result == "won", (
                        f"Under 220.5 should be WON (total=215), got {actual_result}"
                    )

    @pytest.mark.asyncio
    async def test_h2h_resolution(self, backtest_engine, hypothesis_manager, db_path):
        """
        Verify h2h (moneyline) resolution: home team wins.
        Lakers 110 > Celtics 105, so Lakers ML is WON, Celtics ML is LOST.
        """
        hid = await create_test_hypothesis(hypothesis_manager, market_type="h2h")
        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        await insert_game_result(db_path)

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT side, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'h2h' AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) >= 2, "Expected at least 2 resolved h2h events"

            for side, actual_result in rows:
                if side == HOME_TEAM:
                    assert actual_result == "won", (
                        f"Lakers h2h should be WON (110>105), got {actual_result}"
                    )
                elif side == AWAY_TEAM:
                    assert actual_result == "lost", (
                        f"Celtics h2h should be LOST (110>105), got {actual_result}"
                    )

    @pytest.mark.asyncio
    async def test_spreads_push(self, backtest_engine, hypothesis_manager, db_path):
        """
        When the margin exactly matches the spread, BOTH sides should push.

        The backtest engine stores the SIGNED point as the `line` column
        (home = -5.0, away = +5.0). Resolution formula is:
          home:  margin + line  ->  5 + (-5) = 0  => push
          away: -margin + line  -> -5 +  5  = 0  => push
        """
        hid = await create_test_hypothesis(hypothesis_manager, market_type="spreads")

        # Build snapshot with integer spreads that can push
        snapshot = build_odds_api_snapshot()
        for game in snapshot["games"]:
            for bm in game["bookmakers"]:
                for mkt in bm["markets"]:
                    if mkt["key"] == "spreads":
                        for outcome in mkt["outcomes"]:
                            if outcome["name"] == HOME_TEAM:
                                outcome["point"] = -5.0  # Exactly the margin
                            else:
                                outcome["point"] = 5.0

        await insert_cached_odds(db_path, snapshot)
        # Score: 110-105 = margin 5.
        await insert_game_result(db_path)

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT side, line, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'spreads' AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) >= 2, "Expected at least 2 resolved spread events"

            for side, line, actual_result in rows:
                if side == AWAY_TEAM:
                    # Away +5, margin +5: -margin + line = -5 + 5 = 0 => push
                    assert actual_result == "push", (
                        f"{AWAY_TEAM} with line={line} should be PUSH "
                        f"(formula: -margin + line = -5 + 5 = 0), got {actual_result}"
                    )
                elif side == HOME_TEAM:
                    # Home -5, margin +5: margin + line = 5 + (-5) = 0 => push
                    assert actual_result == "push", (
                        f"{HOME_TEAM} with line={line} should be PUSH "
                        f"(formula: margin + line = 5 + (-5) = 0), got {actual_result}"
                    )

    @pytest.mark.asyncio
    async def test_away_team_wins_spreads(self, backtest_engine, hypothesis_manager, db_path):
        """When away team wins by 15, spread resolution flips (Lakers -3.5 loses)."""
        hid = await create_test_hypothesis(hypothesis_manager, market_type="spreads")

        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        # Away team wins: Celtics 115, Lakers 100
        await insert_game_result(
            db_path, home_score=100, away_score=115,
        )

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result

        async with aiosqlite.connect(db_path) as db:
            # spreads: Lakers -3.5, margin = -15, adjusted = -15 + (-3.5) = -18.5 -> lost
            #          Celtics +3.5, margin = +15, adjusted = 15 + 3.5 = 18.5 -> won
            cursor = await db.execute(
                "SELECT side, line, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'spreads' AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) >= 2, "Expected at least 2 resolved spread events"
            for side, line, actual_result in rows:
                if side == HOME_TEAM:
                    assert actual_result == "lost"
                elif side == AWAY_TEAM:
                    assert actual_result == "won"

    @pytest.mark.asyncio
    async def test_away_team_wins_h2h(self, backtest_engine, hypothesis_manager, db_path):
        """When away team wins, h2h resolution should reflect that."""
        hid = await create_test_hypothesis(hypothesis_manager, market_type="h2h")

        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        # Away team wins: Celtics 115, Lakers 100
        await insert_game_result(
            db_path, home_score=100, away_score=115,
        )

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT side, actual_result FROM backtest_events "
                "WHERE run_id = ? AND market = 'h2h' AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            rows = await cursor.fetchall()
            assert len(rows) >= 2, "Expected at least 2 resolved h2h events"
            for side, actual_result in rows:
                if side == HOME_TEAM:
                    assert actual_result == "lost"
                elif side == AWAY_TEAM:
                    assert actual_result == "won"

    @pytest.mark.asyncio
    async def test_no_resolution_without_game_results(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """Without game_results rows, events should remain unresolved."""
        hid = await create_test_hypothesis(hypothesis_manager)
        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        # Deliberately do NOT insert game results

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert result["total_events"] > 0

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE run_id = ? AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row[0] == 0, "Events should be unresolved without game_results"

    @pytest.mark.asyncio
    async def test_backtest_run_record(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """Verify the backtest_runs table is populated with correct metadata.

        Uses TWO games so signals collapse to >= 2 unique (event_id) rows —
        evaluate_significance requires `resolved >= 2` before it populates the
        run's actual_win/actual_loss columns.
        """
        hid = await create_test_hypothesis(hypothesis_manager)

        # Game 1: Lakers vs Celtics on GAME_DATE, Lakers win 110-105
        snapshot1 = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot1)
        await insert_game_result(db_path)

        # Game 2: Warriors vs Knicks on GAME_DATE+1, Warriors win 120-110
        game2_date = "2025-12-16"
        home2, away2 = "Golden State Warriors", "New York Knicks"
        snapshot2 = build_odds_api_snapshot(home=home2, away=away2, game_date=game2_date)
        await insert_cached_odds(db_path, snapshot2, game_date=game2_date)
        await insert_game_result(
            db_path, game_date=game2_date, home=home2, away=away2,
            home_score=120, away_score=110,
        )

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=game2_date,
            credit_budget=0,
        )

        run_id = result["run_id"]

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT * FROM backtest_runs WHERE run_id = ?", (run_id,),
            )
            row = await cursor.fetchone()
            assert row is not None, "backtest_runs row not created"
            cols = [d[0] for d in cursor.description]
            run = dict(zip(cols, row))

            assert run["hypothesis_id"] == hid
            assert run["date_range_start"] == GAME_DATE
            assert run["date_range_end"] == game2_date
            assert run["total_events"] > 0
            assert run["completed_at"] is not None
            # Should have wins/losses after resolution
            assert (run["actual_win"] or 0) + (run["actual_loss"] or 0) > 0

    @pytest.mark.asyncio
    async def test_model_factors_stored(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """Verify model_factors JSON contains expected metadata."""
        hid = await create_test_hypothesis(hypothesis_manager)
        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        await insert_game_result(db_path)

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT model_factors FROM backtest_events WHERE run_id = ? LIMIT 1",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row is not None
            factors = json.loads(row[0])
            assert "home_team" in factors
            assert "away_team" in factors
            assert factors["home_team"] == HOME_TEAM
            assert factors["away_team"] == AWAY_TEAM
            assert "devig_method" in factors
            assert "books_used" in factors
            assert factors["target_excluded"] is True


class TestPaperTradeResolution:
    """Paper trade resolution via DataCollector.resolve_game_level_outcomes()."""

    @pytest.mark.asyncio
    async def test_resolve_spread_paper_trade(self, data_collector, db_path):
        """
        Insert a paper trade for Lakers -3.5 spread, insert the game result
        (Lakers win by 5), and verify resolve_game_level_outcomes() resolves
        the trade correctly as 'won'.
        """
        hid = "test-hypo-001"
        trade_id = "test-trade-001"

        # Insert the hypothesis (needed for FK, though not enforced in SQLite by default)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO hypotheses "
                "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                "edge_threshold, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, "Test Hypo", "Test", SPORT, "spreads", "{}", 0.01, "paper_trading"),
            )
            await db.commit()

        # Insert paper trade: Lakers -3.5 on DraftKings
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO paper_trades "
                "(trade_id, hypothesis_id, sport, market, line, side, book, "
                "signal_time, signal_odds_american, signal_implied_prob, "
                "model_fair_prob, edge, ev_pct, game_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hid, SPORT, "spreads", -3.5, HOME_TEAM, "draftkings",
                    "2025-12-15T12:00:00Z", -110, 0.524,
                    0.55, 0.026, 0.026, GAME_DATE,
                ),
            )
            await db.commit()

        # Insert game result: Lakers 110, Celtics 105 (margin=5, covers -3.5)
        await insert_game_result(db_path)

        # Resolve
        summary = await data_collector.resolve_game_level_outcomes(SPORT, GAME_DATE)

        assert summary["total_pending"] == 1
        assert summary["resolved"] == 1
        assert summary["unmatched"] == 0

        # Verify actual_result was set correctly
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT actual_result FROM paper_trades WHERE trade_id = ?",
                (trade_id,),
            )
            row = await cursor.fetchone()
            assert row[0] == "won", (
                f"Lakers -3.5 paper trade should be WON (margin=5), got {row[0]}"
            )

    @pytest.mark.asyncio
    async def test_resolve_totals_paper_trade(self, data_collector, db_path):
        """
        Resolve a totals paper trade (Over 220.5 when total=215 -> lost).

        For totals, the side field is 'Over'/'Under' (not a team name), so
        resolve_game_level_outcomes needs the event_id + game_contexts join
        to match the paper trade to the correct game.
        """
        hid = "test-hypo-002"
        trade_id = "test-trade-002"
        event_id = f"nba-{GAME_DATE}-test"

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO hypotheses "
                "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                "edge_threshold, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, "Totals Hypo", "Test", SPORT, "totals", "{}", 0.01, "paper_trading"),
            )
            # Insert game_contexts so the event_id join works
            await db.execute(
                "INSERT INTO game_contexts "
                "(sport, event_id, game_date, home_team, away_team, context_json) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (SPORT, event_id, GAME_DATE, HOME_TEAM, AWAY_TEAM, "{}"),
            )
            await db.execute(
                "INSERT INTO paper_trades "
                "(trade_id, hypothesis_id, event_id, sport, market, line, side, book, "
                "signal_time, signal_odds_american, signal_implied_prob, "
                "model_fair_prob, edge, ev_pct, game_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hid, event_id, SPORT, "totals", 220.5, "Over", "draftkings",
                    "2025-12-15T12:00:00Z", -110, 0.524,
                    0.55, 0.026, 0.026, GAME_DATE,
                ),
            )
            await db.commit()

        # Total = 215 < 220.5, so Over loses
        await insert_game_result(db_path)

        summary = await data_collector.resolve_game_level_outcomes(SPORT, GAME_DATE)
        assert summary["resolved"] == 1

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT actual_result FROM paper_trades WHERE trade_id = ?",
                (trade_id,),
            )
            row = await cursor.fetchone()
            assert row[0] == "lost", (
                f"Over 220.5 paper trade should be LOST (total=215), got {row[0]}"
            )

    @pytest.mark.asyncio
    async def test_resolve_h2h_paper_trade(self, data_collector, db_path):
        """Resolve a moneyline paper trade (Lakers ML when Lakers win -> won)."""
        hid = "test-hypo-003"
        trade_id = "test-trade-003"

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO hypotheses "
                "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                "edge_threshold, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, "H2H Hypo", "Test", SPORT, "h2h", "{}", 0.01, "paper_trading"),
            )
            await db.execute(
                "INSERT INTO paper_trades "
                "(trade_id, hypothesis_id, sport, market, line, side, book, "
                "signal_time, signal_odds_american, signal_implied_prob, "
                "model_fair_prob, edge, ev_pct, game_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hid, SPORT, "h2h", None, HOME_TEAM, "draftkings",
                    "2025-12-15T12:00:00Z", -160, 0.615,
                    0.65, 0.035, 0.035, GAME_DATE,
                ),
            )
            await db.commit()

        await insert_game_result(db_path)

        summary = await data_collector.resolve_game_level_outcomes(SPORT, GAME_DATE)
        assert summary["resolved"] == 1

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT actual_result FROM paper_trades WHERE trade_id = ?",
                (trade_id,),
            )
            row = await cursor.fetchone()
            assert row[0] == "won", (
                f"Lakers h2h paper trade should be WON (110>105), got {row[0]}"
            )

    @pytest.mark.asyncio
    async def test_already_resolved_not_touched(self, data_collector, db_path):
        """Paper trades that already have actual_result should not be re-resolved."""
        hid = "test-hypo-004"
        trade_id = "test-trade-004"

        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT INTO hypotheses "
                "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                "edge_threshold, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (hid, "Already Resolved", "Test", SPORT, "spreads", "{}", 0.01, "paper_trading"),
            )
            await db.execute(
                "INSERT INTO paper_trades "
                "(trade_id, hypothesis_id, sport, market, line, side, book, "
                "signal_time, signal_odds_american, signal_implied_prob, "
                "model_fair_prob, edge, ev_pct, game_date, actual_result) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_id, hid, SPORT, "spreads", -3.5, HOME_TEAM, "draftkings",
                    "2025-12-15T12:00:00Z", -110, 0.524,
                    0.55, 0.026, 0.026, GAME_DATE, "won",
                ),
            )
            await db.commit()

        await insert_game_result(db_path)

        summary = await data_collector.resolve_game_level_outcomes(SPORT, GAME_DATE)
        # Should find 0 pending because the trade is already resolved
        assert summary["total_pending"] == 0
        assert summary["resolved"] == 0


class TestResolutionLogicUnit:
    """Unit tests for BacktestEngine._resolve_line() in isolation."""

    def setup_method(self):
        """Create a bare engine instance for testing _resolve_line."""
        # _resolve_line is a pure function that doesn't need DB access
        self.engine = BacktestEngine.__new__(BacktestEngine)

    def test_spreads_won(self):
        result = self.engine._resolve_line(
            "spreads", HOME_TEAM, -3.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "won"  # margin=5, adjusted=5+(-3.5)=1.5>0

    def test_spreads_lost(self):
        result = self.engine._resolve_line(
            "spreads", HOME_TEAM, -7.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "lost"  # margin=5, adjusted=5+(-7.5)=-2.5<0

    def test_spreads_push(self):
        result = self.engine._resolve_line(
            "spreads", HOME_TEAM, -5.0, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "push"  # margin=5, adjusted=5+(-5)=0

    def test_spreads_away_team(self):
        result = self.engine._resolve_line(
            "spreads", AWAY_TEAM, 3.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "lost"  # margin=-5, adjusted=-5+3.5=-1.5<0

    def test_totals_over_won(self):
        result = self.engine._resolve_line(
            "totals", "Over", 210.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "won"  # total=215>210.5

    def test_totals_over_lost(self):
        result = self.engine._resolve_line(
            "totals", "Over", 220.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "lost"  # total=215<220.5

    def test_totals_under_won(self):
        result = self.engine._resolve_line(
            "totals", "Under", 220.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "won"  # total=215<220.5

    def test_totals_under_lost(self):
        result = self.engine._resolve_line(
            "totals", "Under", 210.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "lost"  # total=215>210.5

    def test_totals_push(self):
        result = self.engine._resolve_line(
            "totals", "Over", 215.0, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "push"  # total=215==215

    def test_h2h_home_wins(self):
        result = self.engine._resolve_line(
            "h2h", HOME_TEAM, None, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "won"

    def test_h2h_away_wins(self):
        result = self.engine._resolve_line(
            "h2h", AWAY_TEAM, None, 100, 115, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "won"

    def test_h2h_home_loses(self):
        result = self.engine._resolve_line(
            "h2h", HOME_TEAM, None, 100, 115, HOME_TEAM, AWAY_TEAM,
        )
        assert result == "lost"

    def test_unknown_market_returns_none(self):
        result = self.engine._resolve_line(
            "player_points", "Over", 25.5, 110, 105, HOME_TEAM, AWAY_TEAM,
        )
        assert result is None


class TestMultiGameBacktest:
    """Test backtest with multiple games on the same date."""

    @pytest.mark.asyncio
    async def test_two_games_same_date(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """Two games on the same date should both be processed and resolved."""
        hid = await create_test_hypothesis(hypothesis_manager)

        # Build snapshot with two games
        game1_home, game1_away = "Golden State Warriors", "Miami Heat"
        game2_home, game2_away = "Denver Nuggets", "Phoenix Suns"

        snapshot1 = build_odds_api_snapshot(home=game1_home, away=game1_away)
        snapshot2 = build_odds_api_snapshot(home=game2_home, away=game2_away)

        # Merge games into one snapshot
        merged = snapshot1.copy()
        merged["games"] = snapshot1["games"] + snapshot2["games"]
        merged["game_count"] = 2

        await insert_cached_odds(db_path, merged)

        # Insert results for both games
        await insert_game_result(
            db_path, home=game1_home, away=game1_away,
            home_score=120, away_score=112,
        )
        await insert_game_result(
            db_path, home=game2_home, away=game2_away,
            home_score=99, away_score=108,
        )

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )

        assert "error" not in result
        # Should have events from both games
        assert result["total_events"] >= 4  # At least 2 sides x 2 games for spreads

        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT event_id) FROM backtest_events WHERE run_id = ?",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row[0] == 2, f"Expected 2 distinct events, got {row[0]}"

            # Check resolution count - both games should resolve
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_events "
                "WHERE run_id = ? AND actual_result IS NOT NULL",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row[0] >= 4, (
                f"Expected at least 4 resolved events (2 sides x 2 games), got {row[0]}"
            )


class TestDateRangeSafety:
    """Verify end_date is capped at yesterday inside run_backtest()."""

    @pytest.mark.asyncio
    async def test_future_end_date_capped(self, backtest_engine, hypothesis_manager, db_path):
        """run_backtest with end_date=today should cap it to yesterday."""
        from datetime import datetime as dt, timezone as tz, timedelta
        today = dt.now(tz.utc).strftime("%Y-%m-%d")
        yesterday = str(dt.now(tz.utc).date() - timedelta(days=1))

        hid = await create_test_hypothesis(hypothesis_manager)
        # Seed data for yesterday so we have something to process
        snapshot = build_odds_api_snapshot(game_date=yesterday)
        await insert_cached_odds(db_path, snapshot, game_date=yesterday)
        await insert_game_result(db_path, game_date=yesterday)

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=yesterday,
            end_date=today,  # Should get capped to yesterday
            credit_budget=0,
        )

        assert "error" not in result, f"Unexpected error: {result.get('error')}"
        # Verify the run's date_range_end is yesterday, not today
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT date_range_end FROM backtest_runs WHERE run_id = ?",
                (result["run_id"],),
            )
            row = await cursor.fetchone()
            assert row[0] == yesterday, (
                f"date_range_end should be capped to {yesterday}, got {row[0]}"
            )

    @pytest.mark.asyncio
    async def test_start_after_end_returns_error(self, backtest_engine, hypothesis_manager, db_path):
        """If start_date > capped end_date, should return error (not crash)."""
        from datetime import datetime as dt, timezone as tz
        today = dt.now(tz.utc).strftime("%Y-%m-%d")

        hid = await create_test_hypothesis(hypothesis_manager)
        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=today,  # start=today, end=today -> capped to yesterday -> start>end
            end_date=today,
            credit_budget=0,
        )
        assert "error" in result


class TestRecalculateRunStats:
    """Test that run stats are recalculated after deferred resolution."""

    @pytest.mark.asyncio
    async def test_deferred_resolution_updates_run(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """
        Simulate the broken pipeline scenario:
        1. Run backtest WITHOUT game_results (events created, unresolved)
        2. Verify run has null stats
        3. Insert game_results
        4. Call resolve_from_game_results()
        5. Verify run stats are now populated
        """
        hid = await create_test_hypothesis(hypothesis_manager)
        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        # Do NOT insert game_results yet

        result = await backtest_engine.run_backtest(
            hypothesis_id=hid,
            start_date=GAME_DATE,
            end_date=GAME_DATE,
            credit_budget=0,
        )
        run_id = result["run_id"]
        assert result["total_events"] > 0

        # Verify run has null/zero stats (the broken state)
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT actual_win, actual_loss, hit_rate FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            # Should be 0/0/null since no events resolved
            assert row[0] == 0 or row[0] is None
            assert row[1] == 0 or row[1] is None

        # Now insert game_results (simulating scores arriving later)
        await insert_game_result(db_path)

        # Run deferred resolution
        resolution = await backtest_engine.resolve_from_game_results(run_id=run_id, sport=SPORT)
        assert resolution["resolved"] > 0, "Expected some events to resolve"

        # Verify run stats are now populated
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT actual_win, actual_loss, hit_rate FROM backtest_runs WHERE run_id = ?",
                (run_id,),
            )
            row = await cursor.fetchone()
            wins, losses, hit_rate = row
            assert (wins + losses) > 0, f"Expected wins+losses > 0, got {wins}+{losses}"
            assert hit_rate is not None, "hit_rate should be populated after resolution"
            assert 0 <= hit_rate <= 1, f"hit_rate should be between 0 and 1, got {hit_rate}"

    @pytest.mark.asyncio
    async def test_global_resolution_recalculates_all_stale_runs(
        self, backtest_engine, hypothesis_manager, db_path,
    ):
        """
        Multiple broken runs should all get recalculated when
        resolve_from_game_results() is called without run_id.
        """
        # Create two hypotheses and run backtests without results
        hid1 = await create_test_hypothesis(hypothesis_manager, name_suffix=" A")
        hid2 = await create_test_hypothesis(hypothesis_manager, market_type="h2h", name_suffix=" B")

        snapshot = build_odds_api_snapshot()
        await insert_cached_odds(db_path, snapshot)
        # No game_results yet

        r1 = await backtest_engine.run_backtest(
            hypothesis_id=hid1, start_date=GAME_DATE, end_date=GAME_DATE, credit_budget=0,
        )
        r2 = await backtest_engine.run_backtest(
            hypothesis_id=hid2, start_date=GAME_DATE, end_date=GAME_DATE, credit_budget=0,
        )

        assert r1["total_events"] > 0
        assert r2["total_events"] > 0

        # Both runs should be in broken state
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_runs "
                "WHERE completed_at IS NOT NULL AND total_events > 0 "
                "AND (actual_win = 0 AND actual_loss = 0 AND hit_rate IS NULL)",
            )
            broken = (await cursor.fetchone())[0]
            assert broken == 2, f"Expected 2 broken runs, got {broken}"

        # Now insert results and resolve globally
        await insert_game_result(db_path)
        resolution = await backtest_engine.resolve_from_game_results()
        assert resolution["resolved"] > 0

        # Both runs should now have stats
        async with aiosqlite.connect(db_path) as db:
            cursor = await db.execute(
                "SELECT COUNT(*) FROM backtest_runs "
                "WHERE completed_at IS NOT NULL AND total_events > 0 "
                "AND (actual_win = 0 AND actual_loss = 0 AND hit_rate IS NULL)",
            )
            still_broken = (await cursor.fetchone())[0]
            assert still_broken == 0, (
                f"Expected 0 broken runs after resolution, got {still_broken}"
            )


class TestFuzzyResolution:
    """Test fuzzy team name matching in resolution and _resolve_line."""

    def setup_method(self):
        self.engine = BacktestEngine.__new__(BacktestEngine)

    def test_resolve_line_fuzzy_h2h(self):
        """h2h resolution should work with fuzzy team names."""
        # Side from Odds API uses full name, game_results may use abbreviation
        result = self.engine._resolve_line(
            "h2h", "Los Angeles Lakers", None, 110, 105,
            "LA Lakers", "Boston Celtics",
        )
        assert result == "won", f"Fuzzy h2h home should resolve to 'won', got {result}"

    def test_resolve_line_fuzzy_away(self):
        result = self.engine._resolve_line(
            "h2h", "Boston Celtics", None, 110, 105,
            "LA Lakers", "Boston Celtics",
        )
        assert result == "lost", f"Fuzzy h2h away should resolve to 'lost', got {result}"

    def test_resolve_line_fuzzy_spreads(self):
        """Spreads with fuzzy team names should still identify home/away correctly."""
        result = self.engine._resolve_line(
            "spreads", "Los Angeles Lakers", -3.5, 110, 105,
            "LA Lakers", "Boston Celtics",
        )
        assert result == "won", f"Fuzzy spreads home should resolve to 'won', got {result}"

    def test_resolve_line_mascot_match(self):
        """Mascot-only matching (last word) should work."""
        result = self.engine._resolve_line(
            "h2h", "Golden State Warriors", None, 110, 105,
            "GS Warriors", "Miami Heat",
        )
        assert result == "won", f"Mascot match should resolve to 'won', got {result}"

    def test_team_matches_exact(self):
        assert BacktestEngine._team_matches("Boston Celtics", "Boston Celtics")

    def test_team_matches_normalized(self):
        assert BacktestEngine._team_matches("Los Angeles Lakers", "LA Lakers")

    def test_team_matches_mascot(self):
        assert BacktestEngine._team_matches("Golden State Warriors", "GS Warriors")

    def test_team_matches_substring(self):
        assert BacktestEngine._team_matches("Athletics", "Oakland Athletics")

    def test_team_no_match(self):
        assert not BacktestEngine._team_matches("Boston Celtics", "LA Lakers")
