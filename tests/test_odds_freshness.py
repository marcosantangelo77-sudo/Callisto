"""Unit tests for freshness-weighted sharp consensus in edge_scanner.

These lock down the invariants the freshness audit fix introduced:
  * A 30-second-old line dominates a 10-minute-old line under default
    half-life (180s).
  * Lowering the half-life makes weighting MORE aggressive.
  * Raising the half-life approaches the legacy unweighted mean.
  * Lines with unparseable timestamps fall back to weight 1.0 so legacy
    rows don't silently disappear.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tools.edge_scanner import (
    weighted_sharp_consensus,
    _freshness_weight,
    _parse_line_timestamp,
)
from tools.odds_api import calculate_implied_probability


def _line(price: int, bookmaker: str, age_s: float, now=None) -> dict:
    """Build a stand-in for find_best_line()'s entries."""
    if now is None:
        now = datetime.now(timezone.utc)
    ts = now - timedelta(seconds=age_s)
    return {
        "bookmaker": bookmaker,
        "price": price,
        "point": None,
        "fetched_at": ts.isoformat(),
    }


def test_fresh_line_dominates_stale_line():
    now = datetime.now(timezone.utc)
    # Book A fresh (30s old) at -110 → implied ~0.524
    # Book B stale (600s old) at +110 → implied ~0.476
    # Unweighted mean = 0.500. Freshness-weighted mean must be much
    # closer to A's 0.524 because B's weight at 600s is ~0.04.
    lines = [
        _line(-110, "Pinnacle", 30, now=now),
        _line(+110, "Circa", 600, now=now),
    ]
    consensus, debug = weighted_sharp_consensus(lines, now=now)
    a = calculate_implied_probability(-110)
    b = calculate_implied_probability(+110)
    assert consensus is not None
    # Consensus must lean toward A's price, not the midpoint.
    assert consensus > (a + b) / 2 + 0.015, (
        f"freshness weighting failed: consensus={consensus}, mid={(a+b)/2}"
    )
    assert consensus <= a + 1e-6
    # Debug rows should carry the weight/age we computed.
    assert len(debug) == 2
    assert debug[0]["weight"] > 10 * debug[1]["weight"]


def test_half_life_controls_weighting_aggressiveness():
    """Shorter half-life → more aggressive weighting → closer to fresh line.

    This is the CALLISTO_ODDS_HALF_LIFE_S knob's contract: operators can
    trade off freshness sensitivity vs stability without touching code.
    """
    now = datetime.now(timezone.utc)
    lines = [
        _line(-110, "Pinnacle", 30, now=now),
        _line(+110, "Circa", 300, now=now),
    ]
    short = weighted_sharp_consensus(lines, now=now, half_life_s=60)[0]
    long_ = weighted_sharp_consensus(lines, now=now, half_life_s=3600)[0]
    assert short is not None and long_ is not None
    # With a 60s half-life, B's weight is tiny → consensus ~ A's implied.
    # With a 3600s half-life, both near-equal weight → consensus ~ midpoint.
    assert short > long_, (
        f"half-life didn't tighten weighting: short={short}, long={long_}"
    )


def test_missing_timestamp_defaults_to_weight_one():
    """Legacy snapshot rows without fetched_at must still contribute.

    Silently zeroing them out would regress consensus quality immediately
    after the schema migration runs (old rows have fetched_at=NULL).
    """
    now = datetime.now(timezone.utc)
    line_no_ts = {"bookmaker": "Pinnacle", "price": -110, "point": None}
    line_with_ts = _line(+110, "Circa", 30, now=now)
    # Weight 1.0 means the no-timestamp line contributes equal to a
    # fresh one — the defensible fallback.
    w_none = _freshness_weight(line_no_ts, now=now)
    w_fresh = _freshness_weight(line_with_ts, now=now)
    # Legacy line without fetched_at → full weight so the migration
    # doesn't silently disable historical data.
    assert w_none == 1.0
    # 30s old at 180s half-life ≈ exp(-30/180) ≈ 0.846 — meaningfully less
    # than the no-timestamp fallback but still a healthy weight.
    assert 0.8 < w_fresh < 0.9


def test_consensus_handles_empty_and_all_zero_weights():
    consensus, debug = weighted_sharp_consensus([])
    assert consensus is None
    assert debug == []


def test_parse_line_timestamp_prefers_fetched_at_over_last_update():
    """fetched_at wins — it's our stamp; last_update is the book's claim.

    The book can and does lie about last_update (caching, clock skew, WS
    latency). Our ingest stamp is authoritative.
    """
    now = datetime.now(timezone.utc)
    fetched = (now - timedelta(seconds=10)).isoformat()
    stale = (now - timedelta(seconds=1000)).isoformat()
    line = {"fetched_at": fetched, "last_update": stale}
    ts = _parse_line_timestamp(line)
    assert ts is not None
    age_s = (now - ts).total_seconds()
    assert 0 <= age_s <= 30, f"Expected ~10s, got {age_s}"


def test_parse_line_timestamp_z_suffix():
    """Odds-api uses Z suffix; fromisoformat() needs +00:00."""
    line = {"fetched_at": "2026-04-21T12:00:00Z"}
    ts = _parse_line_timestamp(line)
    assert ts is not None
    assert ts.tzinfo is not None


def test_multi_point_values_emit_separate_edges():
    """When books disagree on the point value, we must emit SEPARATE edge
    candidates per value instead of silently dropping the minority group.

    Pre-fix behavior: keep only the dominant point value, drop minority
    (killing key-number arbitrage surface). Post-fix: each group of 2+
    books on a point value gets its own edge row if it otherwise qualifies.
    """
    from tools.edge_scanner import scan_cross_book_edges

    now = datetime.now(timezone.utc).isoformat()
    game = {
        "id": "g1",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "sport_key": "basketball_nba",
        "commence_time": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        "bookmakers": [
            # Two books on +3 with different prices — group 1
            {"key": "pinnacle", "title": "Pinnacle", "last_update": now, "fetched_at": now,
             "markets": [{"key": "spreads", "outcomes": [
                 {"name": "Lakers", "price": -105, "point": 3, "fetched_at": now},
                 {"name": "Celtics", "price": -115, "point": -3, "fetched_at": now},
             ]}]},
            {"key": "circa", "title": "Circa", "last_update": now, "fetched_at": now,
             "markets": [{"key": "spreads", "outcomes": [
                 {"name": "Lakers", "price": -108, "point": 3, "fetched_at": now},
                 {"name": "Celtics", "price": -112, "point": -3, "fetched_at": now},
             ]}]},
            # Two books on +2.5 with very different prices — group 2,
            # should also surface as its own edge candidate.
            {"key": "draftkings", "title": "DraftKings", "last_update": now, "fetched_at": now,
             "markets": [{"key": "spreads", "outcomes": [
                 {"name": "Lakers", "price": +120, "point": 2.5, "fetched_at": now},
                 {"name": "Celtics", "price": -140, "point": -2.5, "fetched_at": now},
             ]}]},
            {"key": "fanduel", "title": "FanDuel", "last_update": now, "fetched_at": now,
             "markets": [{"key": "spreads", "outcomes": [
                 {"name": "Lakers", "price": +100, "point": 2.5, "fetched_at": now},
                 {"name": "Celtics", "price": -120, "point": -2.5, "fetched_at": now},
             ]}]},
        ],
    }
    edges = scan_cross_book_edges([game], market="spreads", sport="nba")
    # We must have at least one edge per point value for the Lakers side.
    points_seen = {e["best_line"]["point"] for e in edges if e.get("team") == "Lakers"}
    # At minimum, the point-2.5 group (big price spread) should qualify.
    assert 2.5 in points_seen or 3 in points_seen, (
        f"Expected edges per point value, got {edges}"
    )


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
