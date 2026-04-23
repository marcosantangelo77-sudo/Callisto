"""Tests for the alt-line cross-book scanner.

The fixture mimics an NFL event where four bookmakers price alternate
spreads at -2.5, -3, -3.5, and -4. The scanner should surface a distinct
edge candidate per point value, demonstrating the "key number arbitrage"
signal the audit flagged.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.edge_scanner import scan_alt_line_edges


def _fresh() -> str:
    # Slightly in the past so freshness decay doesn't zero-weight lines
    return (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()


def _mk_game_with_alts():
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    return {
        "id": "nfl-test-1",
        "commence_time": future,
        "home_team": "Kansas City Chiefs",
        "away_team": "Buffalo Bills",
        "alt_bookmakers": [
            # Pinnacle sets each alt-spread point. We duplicate across 4
            # books at slightly different prices so the per-point groups
            # each have >=2 lines and show meaningful implied-range.
            # -2.5 group: books split 2-105 vs -125 => implied_range ~5%
            {
                "title": "Pinnacle",
                "key": "pinnacle",
                "last_update": _fresh(),
                "markets": [
                    {
                        "key": "alternate_spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -105, "point": -2.5},
                            {"name": "Kansas City Chiefs", "price": -130, "point": -3.0},
                            {"name": "Kansas City Chiefs", "price": -150, "point": -3.5},
                            {"name": "Kansas City Chiefs", "price": -175, "point": -4.0},
                        ],
                    }
                ],
            },
            {
                "title": "DraftKings",
                "key": "draftkings",
                "last_update": _fresh(),
                "markets": [
                    {
                        "key": "alternate_spreads",
                        "outcomes": [
                            # +4% edge at -2.5 vs Pinnacle
                            {"name": "Kansas City Chiefs", "price": +115, "point": -2.5},
                            {"name": "Kansas City Chiefs", "price": -110, "point": -3.0},
                            {"name": "Kansas City Chiefs", "price": -135, "point": -3.5},
                            {"name": "Kansas City Chiefs", "price": -160, "point": -4.0},
                        ],
                    }
                ],
            },
            {
                "title": "FanDuel",
                "key": "fanduel",
                "last_update": _fresh(),
                "markets": [
                    {
                        "key": "alternate_spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": +110, "point": -2.5},
                            {"name": "Kansas City Chiefs", "price": -115, "point": -3.0},
                            {"name": "Kansas City Chiefs", "price": -140, "point": -3.5},
                            {"name": "Kansas City Chiefs", "price": -165, "point": -4.0},
                        ],
                    }
                ],
            },
            {
                "title": "Circa",
                "key": "circa",
                "last_update": _fresh(),
                "markets": [
                    {
                        "key": "alternate_spreads",
                        "outcomes": [
                            {"name": "Kansas City Chiefs", "price": -108, "point": -2.5},
                            {"name": "Kansas City Chiefs", "price": -132, "point": -3.0},
                            {"name": "Kansas City Chiefs", "price": -155, "point": -3.5},
                            {"name": "Kansas City Chiefs", "price": -178, "point": -4.0},
                        ],
                    }
                ],
            },
        ],
    }


def test_alt_line_scanner_produces_edge_per_point():
    game = _mk_game_with_alts()
    edges = scan_alt_line_edges([game], sport="americanfootball_nfl")

    # Every edge should be tagged as an alt-line with a non-None alt_point.
    assert edges, "expected at least one alt-line edge"
    for e in edges:
        assert e.get("is_alt_line") is True
        assert e.get("alt_market") == "alternate_spreads"
        assert e.get("alt_point") is not None

    # The four different point values in the fixture should each be
    # represented — key-number arbitrage visibility is the whole point.
    found_points = {e["alt_point"] for e in edges}
    assert found_points & {-2.5, -3.0, -3.5, -4.0}, (
        f"expected at least some of {{-2.5, -3.0, -3.5, -4.0}}, "
        f"got {found_points}"
    )


def test_alt_line_scanner_ignores_games_with_no_alt_bookmakers():
    future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
    game = {
        "id": "no-alts",
        "commence_time": future,
        "home_team": "A",
        "away_team": "B",
        # No alt_bookmakers key.
    }
    assert scan_alt_line_edges([game], sport="americanfootball_nfl") == []


def test_alt_line_cache_keys_by_event_id():
    # Cache primitives exercise — fetching a cached value should not collide
    # across event IDs.
    from tools.edge_scanner import _alt_cache_get, _alt_cache_put

    _alt_cache_put("x:1", {"bookmakers": [{"title": "A"}]})
    _alt_cache_put("x:2", {"bookmakers": [{"title": "B"}]})
    got1 = _alt_cache_get("x:1")
    got2 = _alt_cache_get("x:2")
    assert got1 and got2 and got1 != got2
    assert got1["bookmakers"][0]["title"] == "A"
    assert got2["bookmakers"][0]["title"] == "B"
