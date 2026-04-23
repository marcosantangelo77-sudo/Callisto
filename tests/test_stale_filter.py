"""Stale-line filter tests — an arb with any leg older than threshold is rejected."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta

from tools.arbitrage_scanner import scan_pure_arb, DEFAULT_STALE_SECONDS


NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)


def _mkgame(fresh_leg_ts: str, stale_leg_ts: str) -> dict:
    return {
        "id": "g-stale",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": "pinnacle", "title": "Pinnacle",
                "last_update": fresh_leg_ts, "fetched_at": fresh_leg_ts,
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Home", "price": 120, "fetched_at": fresh_leg_ts},
                ]}],
            },
            {
                "key": "fanduel", "title": "FanDuel",
                "last_update": stale_leg_ts, "fetched_at": stale_leg_ts,
                "markets": [{"key": "h2h", "outcomes": [
                    {"name": "Away", "price": 115, "fetched_at": stale_leg_ts},
                ]}],
            },
        ],
    }


def test_fresh_arb_survives():
    ts_fresh = NOW.isoformat()
    game = _mkgame(ts_fresh, (NOW - timedelta(seconds=30)).isoformat())
    arbs = scan_pure_arb(game, "h2h", now=NOW)
    assert len(arbs) == 1


def test_one_stale_leg_rejects_arb():
    """A leg older than DEFAULT_STALE_SECONDS should kill the arb."""
    ts_fresh = NOW.isoformat()
    ts_stale = (NOW - timedelta(seconds=DEFAULT_STALE_SECONDS + 30)).isoformat()
    game = _mkgame(ts_fresh, ts_stale)
    # With default stale threshold it should be rejected.
    assert scan_pure_arb(game, "h2h", now=NOW) == []
    # With a widened stale_seconds the arb should be accepted again.
    accepted = scan_pure_arb(game, "h2h", now=NOW, stale_seconds=600.0)
    assert len(accepted) == 1


def test_both_legs_stale_also_rejected():
    ts_stale = (NOW - timedelta(seconds=300)).isoformat()
    game = _mkgame(ts_stale, ts_stale)
    assert scan_pure_arb(game, "h2h", now=NOW) == []


def test_missing_timestamp_rejected_by_default():
    """No fetched_at and allow_missing_ts=False → rejected as unverifiable."""
    game = {
        "id": "g", "home_team": "Home", "away_team": "Away",
        "bookmakers": [
            {"key": "a", "title": "A", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Home", "price": 120},
            ]}]},
            {"key": "b", "title": "B", "markets": [{"key": "h2h", "outcomes": [
                {"name": "Away", "price": 115},
            ]}]},
        ],
    }
    assert scan_pure_arb(game, "h2h", now=NOW) == []
    # Backtest mode flips allow_missing_ts=True → accepted.
    accepted = scan_pure_arb(game, "h2h", now=NOW, allow_missing_ts=True)
    assert len(accepted) == 1
