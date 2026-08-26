"""Tests for the tools.edges split of tools/edge_scanner.py.

Guarantees:
  - Every historical public/private name stays importable from
    tools.edge_scanner (facade compatibility).
  - The split modules behave identically to the pre-split logic for
    freshness weighting, cross-book scanning, sharp-money detection,
    vig scanning, and in-progress filtering.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import tools.edge_scanner as es


# ---------------------------------------------------------------------------
# Facade: every name that existed before the split must still import.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    # freshness / consensus
    "_parse_line_timestamp", "_freshness_weight", "weighted_sharp_consensus",
    # filtering + sharp titles
    "_filter_in_progress_games", "get_sharp_titles_for_sport",
    "_refresh_granger_cache", "_granger_sharp_cache", "_GRANGER_CACHE_TTL",
    "_STATIC_SHARP_TITLES", "_PACE_SPORT_MAP", "_LOW_SCORING_SPORTS",
    # scans
    "scan_cross_book_edges", "detect_sharp_money", "scan_vig_edges",
    "scan_pace_model_total_edges", "full_edge_scan",
    "fetch_alt_lines_for_games", "scan_alt_line_edges",
    "apply_wiki_adjustments_to_edges",
    # internals other modules/tests reach into
    "_scan_line_group", "_simulation_validate_edges", "_compute_market_hold",
    "_scan_dead_number_steals",
    "_ALT_LINE_CACHE", "_ALT_LINE_TTL_S", "_alt_cache_get", "_alt_cache_put",
    "_ODDS_HALF_LIFE_S", "_DEFAULT_HALF_LIFE_S", "_DEBUG_WEIGHTS",
    "WIKI_EDGE_ADJUSTMENT_CAP", "SOFT_TITLES", "logger",
    # re-exported third-party helpers used by downstream code
    "calculate_implied_probability", "calculate_ev", "find_best_line",
    "compute_market_metrics", "canonicalize_book",
])
def test_facade_reexports(name):
    assert hasattr(es, name), f"tools.edge_scanner lost '{name}' after split"


def test_submodules_exist():
    import tools.edges.common
    import tools.edges.scanning
    import tools.edges.filters
    import tools.edges.wiki
    assert tools.edge_scanner.full_edge_scan is tools.edges.filters.full_edge_scan


# ---------------------------------------------------------------------------
# Freshness parsing / weighting
# ---------------------------------------------------------------------------

def test_parse_line_timestamp_prefers_fetched_at():
    line = {
        "fetched_at": "2026-04-04T12:00:00Z",
        "last_update": "2026-04-04T11:00:00Z",
    }
    dt = es._parse_line_timestamp(line)
    assert dt is not None
    assert dt.hour == 12 and dt.tzinfo is not None


def test_parse_line_timestamp_none_when_missing():
    assert es._parse_line_timestamp({}) is None
    assert es._parse_line_timestamp({"last_update": ""}) is None


def test_freshness_weight_unknown_age_is_one():
    assert es._freshness_weight({}) == 1.0


def test_freshness_weight_decays_with_age():
    now = datetime.now(timezone.utc)
    fresh = {"fetched_at": now.isoformat()}
    old = {"fetched_at": (now - timedelta(seconds=180)).isoformat()}
    w_fresh = es._freshness_weight(fresh, now=now)
    w_old = es._freshness_weight(old, now=now)
    assert w_fresh > w_old > 0.3  # one half-life ≈ e^-1 ≈ 0.37
    assert w_fresh == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# weighted_sharp_consensus
# ---------------------------------------------------------------------------

def test_consensus_empty_returns_none():
    consensus, rows = es.weighted_sharp_consensus([])
    assert consensus is None and rows == []


def test_consensus_equal_weights_mean():
    lines = [
        {"bookmaker": "pinnacle", "price": -110},
        {"bookmaker": "circa", "price": -110},
    ]
    consensus, rows = es.weighted_sharp_consensus(lines)
    implied = es.calculate_implied_probability(-110)
    assert consensus == pytest.approx(implied, abs=1e-6)
    assert len(rows) == 2
    assert all(r["weight"] == pytest.approx(1.0, abs=1e-3) for r in rows)


def test_consensus_stale_line_weighs_less():
    now = datetime.now(timezone.utc)
    lines = [
        {"bookmaker": "pinnacle", "price": -110,
         "fetched_at": now.isoformat()},
        {"bookmaker": "lowvig.ag", "price": -150,
         "fetched_at": (now - timedelta(hours=24)).isoformat()},
    ]
    consensus, rows = es.weighted_sharp_consensus(lines, now=now)
    implied = es.calculate_implied_probability(-110)
    # The ancient line contributes only epsilon — consensus ≈ fresh line.
    assert consensus == pytest.approx(implied, abs=1e-3)
    assert rows[1]["age_s"] > 80000


# ---------------------------------------------------------------------------
# In-progress game filter
# ---------------------------------------------------------------------------

def _game(gid="g1", commence=None):
    return {"id": gid, "home_team": "Home", "away_team": "Away",
            "commence_time": commence}


def test_filter_in_progress_drops_started_games():
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    games = [_game("past", past), _game("future", future), _game("nostamp")]
    out = es._filter_in_progress_games(games)
    assert [g["id"] for g in out] == ["future", "nostamp"]


def test_filter_in_progress_keeps_unparseable_to_be_safe():
    out = es._filter_in_progress_games([_game("bad", "not-a-date")])
    assert [g["id"] for g in out] == ["bad"]


# ---------------------------------------------------------------------------
# Cross-book scanning via scan_cross_book_edges
# ---------------------------------------------------------------------------

def _snapshot_game(lines, gid="g1", commence_future=True):
    commence = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).isoformat() if commence_future else datetime.now(timezone.utc).isoformat()
    return {
        "id": gid,
        "home_team": "Lakers",
        "away_team": "Celtics",
        "commence_time": commence,
        "bookmakers": [
            {"key": bk, "title": bk, "markets": [{
                "key": "spreads",
                "outcomes": [{"name": "Lakers", "price": price, "point": -3},
                             {"name": "Celtics", "price": -price - 5,
                              "point": 3}],
            }]} for bk, price in lines
        ],
    }


def test_scan_cross_book_flags_soft_book_value():
    # Sharp books at -105/-110; soft book (fanduel) at -140 on same point.
    game = _snapshot_game([
        ("pinnacle", -105),
        ("bookmaker.eu", -110),
        ("fanduel", -140),
    ])
    edges = es.scan_cross_book_edges([game], market="spreads", sport="basketball_nba")
    assert isinstance(edges, list)
    # fanduel's side should surface with a positive edge vs sharp consensus
    soft_edges = [se for e in edges for se in e.get("soft_book_edges", [])
                  if se["bookmaker"] == "fanduel"]
    assert soft_edges, "expected a fanduel soft-book edge"
    assert all(se["edge_vs_sharp"] > 0.02 for se in soft_edges)


def test_scan_cross_book_no_sharp_books_no_edges():
    game = _snapshot_game([("fanduel", -110), ("draftkings", -108)])
    edges = es.scan_cross_book_edges([game], market="spreads")
    assert edges == []


def test_scan_cross_book_filters_in_progress_games():
    game = _snapshot_game(
        [("pinnacle", -105), ("bookmaker.eu", -110), ("fanduel", -140)],
        commence_future=False,
    )
    assert es.scan_cross_book_edges([game], market="spreads") == []


# ---------------------------------------------------------------------------
# detect_sharp_money
# ---------------------------------------------------------------------------

def _snap(prices):
    return {"games": [{
        "id": "g1",
        "bookmakers": [
            {"key": k, "title": k, "markets": [{
                "key": "spreads",
                "outcomes": [{"name": "Lakers", "price": p, "point": -3}],
            }]} for k, p in prices.items()
        ],
    }]}


def test_detect_sharp_money_one_mover_two_stale():
    old = _snap({"pinnacle": -110, "fanduel": -110, "draftkings": -110})
    new = _snap({"pinnacle": -125, "fanduel": -110, "draftkings": -111})
    signals = es.detect_sharp_money(old, new)
    assert len(signals) == 1
    sig = signals[0]
    assert sig["signal"] == "SHARP_MOVE"
    assert sig["market"] == "spreads" and sig["team"] == "Lakers"
    moved = {m["bookmaker"] for m in sig["moved_books"]}
    assert moved == {"pinnacle"}
    stale = {s["bookmaker"] for s in sig["stale_books"]}
    assert stale == {"fanduel", "draftkings"}


def test_detect_sharp_money_all_move_no_signal():
    old = _snap({"pinnacle": -110, "fanduel": -110, "draftkings": -110})
    new = _snap({"pinnacle": -125, "fanduel": -124, "draftkings": -126})
    assert es.detect_sharp_money(old, new) == []


# ---------------------------------------------------------------------------
# Vig edges
# ---------------------------------------------------------------------------

def _vig_game(outcomes):
    return {
        "id": "g1", "home_team": "Home", "away_team": "Away",
        "commence_time": (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat(),
        "bookmakers": [{"key": "lowvig.ag", "title": "lowvig.ag", "markets": [
            {"key": "spreads", "outcomes": outcomes},
        ]}],
    }


def test_scan_vig_edges_flags_low_juice():
    game = _vig_game([
        {"name": "Home", "price": -105, "point": -3},
        {"name": "Away", "price": -105, "point": 3},
    ])
    edges = es.scan_vig_edges([game], market="spreads")
    assert len(edges) == 1
    assert edges[0]["edge_type"] == "LOW_VIG"
    assert edges[0]["vig_pct"] < 3.5


def test_scan_vig_edges_standard_vig_not_flagged():
    game = _vig_game([
        {"name": "Home", "price": -110, "point": -3},
        {"name": "Away", "price": -110, "point": 3},
    ])
    assert es.scan_vig_edges([game], market="spreads") == []


# ---------------------------------------------------------------------------
# full_edge_scan report shape
# ---------------------------------------------------------------------------

def test_full_edge_scan_empty_snapshot_error():
    report = es.full_edge_scan({"games": []})
    assert report["error"] == "No games in snapshot"


def test_full_edge_scan_report_keys():
    game = _snapshot_game([
        ("pinnacle", -105), ("bookmaker.eu", -110), ("fanduel", -140),
    ])
    report = es.full_edge_scan({"games": [game], "sport": "basketball_nba"})
    for key in ("cross_book_spreads", "cross_book_h2h", "cross_book_totals",
                "low_vig_spreads", "low_vig_h2h", "low_vig_totals",
                "pace_model_totals", "dead_number_steals",
                "alt_line_edges", "simulation_validated", "total_edges"):
        assert key in report, f"missing report key {key}"
    assert report["filtered_in_progress"] == 0


def test_full_edge_scan_counts_filtered_in_progress():
    game = _snapshot_game(
        [("pinnacle", -105), ("bookmaker.eu", -110), ("fanduel", -140)],
        commence_future=False,
    )
    report = es.full_edge_scan({"games": [game]})
    assert report["filtered_in_progress"] == 1
    assert "error" in report  # everything was filtered out


# ---------------------------------------------------------------------------
# Alt-line cache plumbing
# ---------------------------------------------------------------------------

def test_alt_cache_roundtrip_and_ttl_expiry(monkeypatch):
    import tools.edges.scanning as scanning
    cache = {}
    monkeypatch.setattr(scanning, "_ALT_LINE_CACHE", cache)
    es._alt_cache_put("k", {"v": 1})
    assert es._alt_cache_get("k") == {"v": 1}
    # Age the entry beyond TTL
    ts, data = next(iter(cache.values()))
    cache["k"] = (ts - es._ALT_LINE_TTL_S - 1, data)
    assert es._alt_cache_get("k") is None
    assert "k" not in cache


# ---------------------------------------------------------------------------
# Wiki adjustments (fail-open behaviour)
# ---------------------------------------------------------------------------

def test_wiki_adjustments_disabled_by_env(monkeypatch):
    monkeypatch.setenv("CALLISTO_WIKI_IN_LOOP", "0")
    edges = [{"team": "X", "market": "spreads"}]
    out = asyncio.run(es.apply_wiki_adjustments_to_edges(edges, "nba"))
    assert "wiki_confidence_delta" not in out[0]


def test_wiki_cap_constant_importable():
    assert 0 < es.WIKI_EDGE_ADJUSTMENT_CAP <= 1.0
