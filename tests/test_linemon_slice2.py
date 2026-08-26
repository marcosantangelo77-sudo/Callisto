"""Tests for tools/lines/snapshot_ops (line_monitor slice-2 extraction).

Covers the persistence helpers extracted from tools/line_monitor.py:
- insert_snapshot_record — odds_snapshots row write
- cache_snapshot_for_backtest — historical_odds_cache upsert + book counting
- store_market_microstructure — HHI/entropy rows, error swallowing
- record_line_movement — DB row + alert-sink append/cap
- capture_closing_lines — CLV window filtering + source normalization
- normalize_close_source / default_closing_window

Uses a real in-memory aiosqlite database so SQL is exercised end-to-end.
"""

import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

import sys
sys.path.insert(0, ".")

import aiosqlite

from tools.lines.snapshot_ops import (
    cache_snapshot_for_backtest,
    capture_closing_lines,
    default_closing_window,
    insert_snapshot_record,
    normalize_close_source,
    record_line_movement,
    store_market_microstructure,
)

from tools.lines.fallback_cascade import collect_free_sources, merge_free_sources

NOW = "2026-08-26T12:00:00+00:00"


# ── Fixtures / helpers ───────────────────────────────────────────────────────


async def _make_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    await db.execute(
        """CREATE TABLE odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            game_count INTEGER DEFAULT 0,
            credits_remaining INTEGER,
            fetched_at TEXT,
            source TEXT DEFAULT 'interval')"""
    )
    await db.execute(
        """CREATE TABLE historical_odds_cache (
            sport TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            event_id TEXT,
            market_type TEXT,
            response_json TEXT NOT NULL,
            credits_cost INTEGER DEFAULT 0,
            fetched_at TEXT)"""
    )
    await db.execute(
        """CREATE TABLE line_movements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            team TEXT,
            market TEXT,
            bookmaker TEXT,
            old_price INTEGER,
            new_price INTEGER,
            price_movement INTEGER,
            old_point REAL,
            new_point REAL,
            point_movement REAL,
            direction TEXT)"""
    )
    await db.execute(
        """CREATE TABLE market_microstructure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            game_id TEXT,
            market_type TEXT,
            timestamp TEXT,
            hhi_overall REAL,
            entropy_overall REAL,
            num_books INTEGER)"""
    )
    await db.commit()
    return db


def run(coro):
    return asyncio.run(coro)


class FakeClv:
    def __init__(self):
        self.rows = []

    async def record_closing_line(self, **kwargs):
        self.rows.append(kwargs)


def _snapshot(sport="basketball_nba", game_count=1):
    return {
        "sport": sport,
        "game_count": game_count,
        "games": [{
            "id": "g1",
            "home_team": "A", "away_team": "B",
            "bookmakers": [
                {"key": "dk", "title": "DK", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "A", "price": -110}]}]},
                {"key": "fd", "title": "FD", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "A", "price": -108}]}]},
                {"key": "pin", "title": "Pinnacle", "markets": [
                    {"key": "h2h", "outcomes": [{"name": "A", "price": -105}]}]},
            ],
        }],
    }


# ── insert_snapshot_record ──────────────────────────────────────────────────


class TestInsertSnapshotRecord:
    def test_inserts_row(self):
        async def main():
            db = await _make_db()
            snap = _snapshot()
            await insert_snapshot_record(
                db, sport="basketball_nba", snapshot=snap, now_iso=NOW,
                game_count=1, credits_remaining=42, ingest_source="ws",
            )
            cur = await db.execute("SELECT * FROM odds_snapshots")
            row = dict(zip([d[0] for d in cur.description], (await cur.fetchone())))
            await db.close()
            return row

        row = run(main())
        assert row["sport"] == "basketball_nba"
        assert row["timestamp"] == NOW == row["fetched_at"]
        assert row["game_count"] == 1
        assert row["credits_remaining"] == 42
        assert row["source"] == "ws"
        import json as _json
        assert _json.loads(row["snapshot_json"])["game_count"] == 1


# ── cache_snapshot_for_backtest ─────────────────────────────────────────────


class TestCacheSnapshotForBacktest:
    def test_caches_and_counts_books(self):
        async def main():
            db = await _make_db()
            books = await cache_snapshot_for_backtest(
                db, sport="basketball_nba", snapshot=_snapshot(), now_iso=NOW)
            cur = await db.execute("SELECT * FROM historical_odds_cache")
            row = dict(zip([d[0] for d in cur.description], (await cur.fetchone())))
            await db.close()
            return books, row

        books, row = run(main())
        assert books == 3  # three bookmakers in the fixture game
        assert row["sport"] == "basketball_nba"
        assert row["market_type"] == "h2h,spreads,totals"
        assert row["credits_cost"] == 0
        assert row["event_id"] is None

    def test_zero_game_count_skips_write(self):
        async def main():
            db = await _make_db()
            snap = _snapshot()
            snap["game_count"] = 0
            await cache_snapshot_for_backtest(
                db, sport="nba", snapshot=snap, now_iso=NOW)
            cur = await db.execute("SELECT COUNT(*) FROM historical_odds_cache")
            n = (await cur.fetchone())[0]
            await db.close()
            return n

        assert run(main()) == 0

    def test_stamps_missing_sport_key(self):
        async def main():
            db = await _make_db()
            snap = _snapshot()
            snap["games"][0].pop("sport_key", None)
            await cache_snapshot_for_backtest(db, sport="nba", snapshot=snap, now_iso=NOW)
            assert snap["games"][0]["sport_key"] == "nba"
            await db.close()

        run(main())


# ── store_market_microstructure ─────────────────────────────────────────────


class TestStoreMicrostructure:
    REPORT = {
        "cross_book_h2h": [{"game_id": "g1", "hhi": 0.31, "entropy": 0.99, "num_bookmakers": 3}],
        "cross_book_spreads": [{"game_id": "g1", "hhi": None, "entropy": None}],
        "cross_book_totals": [{"game_id": "g2", "entropy": 0.88, "num_bookmakers": 5}],
    }

    def test_stores_only_metrics_present(self):
        async def main():
            db = await _make_db()
            stored = await store_market_microstructure(
                db, sport="nba", edge_report=self.REPORT, now_iso=NOW)
            cur = await db.execute("SELECT market_type, hhi_overall, entropy_overall FROM market_microstructure")
            rows = [dict(zip(["market_type", "hhi", "entropy"], r)) for r in await cur.fetchall()]
            await db.close()
            return stored, sorted(rows, key=lambda r: r["market_type"])

        stored, rows = run(main())
        assert stored == 2
        assert rows[0] == {"market_type": "h2h", "hhi": 0.31, "entropy": 0.99}
        assert rows[1] == {"market_type": "totals", "hhi": None, "entropy": 0.88}

    def test_db_failure_is_swallowed(self):
        # No tables → every execute raises; helper must log-and-return 0.
        async def main():
            db = await aiosqlite.connect(":memory:")
            try:
                return await store_market_microstructure(
                    db, sport="nba", edge_report=self.REPORT, now_iso=NOW)
            finally:
                await db.close()

        assert run(main()) == 0


# ── record_line_movement ────────────────────────────────────────────────────


MOVEMENT = {
    "team": "Alpha", "market": "h2h", "bookmaker": "Mover",
    "old_price": 100, "new_price": 150, "price_movement": 50,
    "direction": "up", "point_movement": 0,
}


class TestRecordLineMovement:
    def test_inserts_row_and_appends_alert(self):
        async def main():
            db = await _make_db()
            alerts = deque(maxlen=100)
            await record_line_movement(db, alerts, sport="nba", movement=dict(MOVEMENT))
            cur = await db.execute("SELECT * FROM line_movements")
            row = dict(zip([d[0] for d in cur.description], (await cur.fetchone())))
            await db.close()
            return row, list(alerts)

        row, alerts = run(main())
        assert row["sport"] == "nba"
        assert row["direction"] == "up"
        assert row["price_movement"] == 50
        assert len(alerts) == 1
        assert alerts[0]["team"] == "Alpha" and alerts[0]["detected_at"]

    def test_alert_cap_trims(self):
        async def main():
            db = await _make_db()
            alerts = []
            await record_line_movement(db, alerts, sport="nba", movement=dict(MOVEMENT))
            alerts.extend({"i": i} for i in range(150))
            await record_line_movement(db, alerts, sport="nba", movement=dict(MOVEMENT))
            return len(alerts), alerts[-1]["team"]

        n, last_team = run(main())
        assert n == 100
        assert last_team == "Alpha"


# ── capture_closing_lines ───────────────────────────────────────────────────


def _closing_snapshot(minutes_until_start: float):
    start = datetime.now(timezone.utc) + timedelta(minutes=minutes_until_start)
    return {"games": [{
        "id": "evt-9",
        "commence_time": start.isoformat().replace("+00:00", "Z"),
        "home_team": "A", "away_team": "B",
        "bookmakers": [
            {"title": "Pinnacle", "markets": [
                {"key": "h2h", "outcomes": [
                    {"name": "A", "price": -120},
                    {"name": "B", "price": 105},
                    {"name": "Draw", "price": None},  # no price → skipped
                ]},
                {"key": "totals", "outcomes": [
                    {"name": "Over", "price": -105, "point": 220.5},
                ]},
            ]},
        ],
    }]}


class TestCaptureClosingLines:
    def test_records_lines_inside_window(self):
        clv = FakeClv()
        count = run(capture_closing_lines(
            clv, sport="nba", snapshot=_closing_snapshot(30),
            closing_window_seconds=3600))
        assert count == 3
        srcs = {r["source"] for r in clv.rows}
        assert srcs == {"pinnacle"}
        assert all(r["event_id"] == "evt-9" and r["sport"] == "nba" for r in clv.rows)
        over = [r for r in clv.rows if r["market"] == "totals"][0]
        assert over["closing_odds"] == -105 and over["closing_point"] == 220.5

    def test_skips_games_already_started(self):
        clv = FakeClv()
        count = run(capture_closing_lines(
            clv, sport="nba", snapshot=_closing_snapshot(-10),
            closing_window_seconds=3600))
        assert count == 0 and clv.rows == []

    def test_skips_games_outside_window(self):
        clv = FakeClv()
        count = run(capture_closing_lines(
            clv, sport="nba", snapshot=_closing_snapshot(120),
            closing_window_seconds=3600))
        assert count == 0

    def test_record_failures_are_swallowed(self):
        class Failing:
            async def record_closing_line(self, **kwargs):
                raise RuntimeError("db down")

        count = run(capture_closing_lines(
            Failing(), sport="nba", snapshot=_closing_snapshot(30),
            closing_window_seconds=3600))
        assert count == 0

    def test_bad_commence_time_ignored(self):
        clv = FakeClv()
        snap = {"games": [{"id": "x", "commence_time": "not-a-date", "bookmakers": []}]}
        assert run(capture_closing_lines(clv, sport="nba", snapshot=snap, closing_window_seconds=3600)) == 0


# ── small pure helpers ──────────────────────────────────────────────────────


class TestHelpers:
    def test_normalize_close_source(self):
        assert normalize_close_source("Betfair Exchange") == "betfair_exchange"
        assert normalize_close_source("BetOnline.ag") == "betonline.ag"
        assert normalize_close_source(None) == ""
        assert normalize_close_source("") == ""

    def test_default_closing_window(self):
        assert default_closing_window(900) == 3600      # at least 1hr
        assert default_closing_window(3600) == 3900     # interval + 300 buffer


# ── Fallback cascade ────────────────────────────────────────────────────────


def _payload(source_key, price=-110, game_count=1):
    return {
        "sport": "basketball_nba",
        "game_count": game_count,
        "source": source_key,
        "games": [{
            "id": f"g-{source_key}",
            "home_team": "Lakers", "away_team": "Celtics",
            "bookmakers": [{"key": source_key, "title": source_key,
                            "markets": [{"key": "h2h", "outcomes": [
                                {"name": "Lakers", "price": price}]}]}],
        }],
    }


def _scraper(key, *, error=False, count=1, raise_exc=False):
    async def _fn(sport):
        if raise_exc:
            raise RuntimeError("network down")
        if error:
            return {"error": "boom"}
        return _payload(key, game_count=count)
    return _fn


class TestCollectFreeSources:
    def test_primary_io_source_collected(self):
        async def main():
            return await collect_free_sources(
                "basketball_nba",
                odds_api_io_get_odds=_scraper("io"),
                odds_api_io_usage=lambda: {"api_key_set": True,
                                           "requests_remaining_this_hour": 100},
            )

        scraped = run(main())
        assert "odds_api_io" in scraped
        assert scraped["odds_api_io"]["game_count"] == 1

    def test_no_api_key_skips_io(self):
        async def main():
            return await collect_free_sources(
                "basketball_nba",
                odds_api_io_get_odds=_scraper("io"),
                odds_api_io_usage=lambda: {"api_key_set": False},
            )

        assert "odds_api_io" not in run(main())

    def test_zero_quota_skips_io(self):
        async def main():
            return await collect_free_sources(
                "basketball_nba",
                odds_api_io_get_odds=_scraper("io"),
                odds_api_io_usage=lambda: {"api_key_set": True,
                                           "requests_remaining_this_hour": 0},
            )

        assert "odds_api_io" not in run(main())

    def test_scraper_errors_and_exceptions_isolated(self):
        # Every supplementary scraper fails — result must be empty dict,
        # never an exception.
        import tools.lines.fallback_cascade as fc

        async def main():
            orig_dk = fc.scrape_dk_odds
            orig_an = fc.scrape_action_network
            orig_fd = fc.scrape_fd_odds
            try:
                fc.scrape_dk_odds = _scraper("dk", error=True)
                fc.scrape_action_network = _scraper("an", raise_exc=True)
                fc.scrape_fd_odds = _scraper("fd", error=True)
                return await collect_free_sources(
                    "basketball_nba",
                    odds_api_io_get_odds=_scraper("io", error=True),
                    odds_api_io_usage=lambda: {"api_key_set": True,
                                               "requests_remaining_this_hour": 5},
                )
            finally:
                fc.scrape_dk_odds = orig_dk
                fc.scrape_action_network = orig_an
                fc.scrape_fd_odds = orig_fd

        assert run(main()) == {}

    def test_successful_scrapers_all_present(self):
        import tools.lines.fallback_cascade as fc

        async def main():
            orig = (fc.scrape_dk_odds, fc.scrape_action_network, fc.scrape_fd_odds)
            try:
                fc.scrape_dk_odds = _scraper("dk")
                fc.scrape_action_network = _scraper("action_network")
                fc.scrape_fd_odds = _scraper("fd")
                return await collect_free_sources(
                    "basketball_nba",
                    odds_api_io_get_odds=_scraper("odds_api_io"),
                    odds_api_io_usage=lambda: {"api_key_set": True,
                                               "requests_remaining": 10},
                )
            finally:
                fc.scrape_dk_odds, fc.scrape_action_network, fc.scrape_fd_odds = orig

        scraped = run(main())
        assert set(scraped) == {"odds_api_io", "dk", "action_network", "fd"}


class TestMergeFreeSources:
    def test_merges_books_and_tags_source(self):
        scraped = {
            "dk": _payload("dk"),
            "fd": _payload("fd"),
        }
        merged = merge_free_sources(scraped, "basketball_nba")
        books = {b["key"] for b in merged["games"][0]["bookmakers"]}
        assert books == {"dk", "fd"}
        assert merged["source"].startswith("free_cascade_")
        assert "dk" in merged["source"] and "fd" in merged["source"]
        assert merged["game_count"] == 1

    def test_single_source_passthrough_tagged(self):
        merged = merge_free_sources({"dk": _payload("dk")}, "nba")
        assert merged["source"] == "free_cascade_dk"

