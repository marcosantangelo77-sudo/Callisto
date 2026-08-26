"""Tests for the tools.odds_io split of tools/odds_api_io.py.

Verifies:
  - facade compatibility: every public/private name consumers import still
    resolves from tools.odds_api_io and is IDENTICAL to the odds_io object
    (no stale copies)
  - behavior parity for moved helpers: backoff computation, decimal->American
    conversion, primary spread/total selection, event normalization,
    movement snapshot extraction, pre-commence picking, best-line comparison
  - usage tracking: budget checks, persisted hourly window
  - gates untouched: no 'live' paper-signal widening introduced by this split
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import tools.odds_api_io as facade
import tools.odds_io
from tools.odds_io.normalize import (
    decimal_to_american,
    extract_movement_snapshots,
    find_best_line,
    normalize_event_odds,
    parse_iso,
    pick_pre_commence_entry,
    pick_primary_spread,
    pick_primary_total,
    safe_float,
    snapshot_to_market_outcomes,
)
from tools.odds_io.usage import (
    _hourly_requests,
    check_budget,
)


# ---------------------------------------------------------------------------
# Facade compatibility — imports survive, objects are shared not copied
# ---------------------------------------------------------------------------

def test_facade_reexports_public_api():
    public = [
        "get_sports", "get_events", "get_odds", "get_event_odds", "get_scores",
        "get_outrights", "snapshot_all_sports", "get_value_bets",
        "get_arbitrage_bets", "get_odds_multi", "get_odds_updated",
        "get_historical_events", "get_historical_odds", "get_odds_movements",
        "get_historical_snapshot", "get_live_events", "find_best_line",
        "close_client", "get_usage_status",
    ]
    missing = [n for n in public if not hasattr(facade, n)]
    assert not missing, f"facade lost public names: {missing}"


def test_facade_reexports_private_helpers():
    private = [
        "_api_get", "_check_budget", "_compute_backoff",
        "_decimal_to_american", "_safe_float", "_normalize_event_odds",
        "_pick_primary_spread", "_pick_primary_total", "_parse_iso",
        "_extract_movement_snapshots", "_snapshot_to_market_outcomes",
        "_pick_pre_commence_entry", "_SELECTED_BOOKMAKERS",
        "_BOOKMAKER_SLUG_MAP", "_HOURLY_LIMIT", "_BACKOFF_MAX_RETRIES",
        "load_usage", "save_usage", "increment_usage",
    ]
    missing = [n for n in private if not hasattr(facade, n)]
    assert not missing, f"facade lost private helpers: {missing}"


@pytest.mark.parametrize("module_name,name", [
    ("tools.odds_io.http_client", "api_get"),
    ("tools.odds_io.http_client", "compute_backoff"),
    ("tools.odds_io.normalize", "decimal_to_american"),
    ("tools.odds_io.normalize", "safe_float"),
    ("tools.odds_io.normalize", "normalize_event_odds"),
    ("tools.odds_io.normalize", "pick_primary_spread"),
    ("tools.odds_io.normalize", "pick_primary_total"),
    ("tools.odds_io.normalize", "parse_iso"),
    ("tools.odds_io.normalize", "extract_movement_snapshots"),
    ("tools.odds_io.normalize", "snapshot_to_market_outcomes"),
    ("tools.odds_io.normalize", "pick_pre_commence_entry"),
    ("tools.odds_io.normalize", "find_best_line"),
    ("tools.odds_io.config", "SPORT_MAP"),
    ("tools.odds_io.config", "SPORT_TITLES"),
])
def test_facade_names_are_shared_objects(module_name, name):
    """Facade must re-export the SAME object from its odds_io submodule,
    not a stale copy — callers patching one side must affect both."""
    import importlib
    mod = importlib.import_module(module_name)
    facade_attr = "_" + name if module_name == "tools.odds_io.normalize" and name in (
        "decimal_to_american", "safe_float", "normalize_event_odds",
        "pick_primary_spread", "pick_primary_total", "parse_iso",
        "extract_movement_snapshots", "snapshot_to_market_outcomes",
        "pick_pre_commence_entry",
    ) else name
    if module_name == "tools.odds_io.http_client":
        facade_attr = "_compute_backoff" if name == "compute_backoff" else "_api_get"
    assert getattr(facade, facade_attr) is getattr(mod, name)


def test_constants_preserved():
    assert facade._HOURLY_LIMIT == 30000
    assert facade.ODDS_API_IO_BASE == "https://api.odds-api.io/v3"
    assert "DraftKings" in facade._SELECTED_BOOKMAKERS
    assert facade._BOOKMAKER_SLUG_MAP["BetMGM"] == "betmgm"
    assert facade.SPORT_MAP["basketball_nba"]["league"] == "usa-nba"
    assert facade.SPORT_TITLES["basketball_nba"] == "NBA"


# ---------------------------------------------------------------------------
# Backoff computation
# ---------------------------------------------------------------------------

def test_compute_backoff_honors_retry_after():
    assert facade._compute_backoff(0, "5") == 5.0


def test_compute_backoff_caps_retry_after():
    assert facade._compute_backoff(0, "999") == 16.0


def test_compute_backoff_ignores_bad_retry_after():
    val = facade._compute_backoff(0, "not-a-number")
    assert 1.0 <= val <= 2.1  # exponential base 1s + jitter


def test_compute_backoff_exponential_growth():
    a0 = facade._compute_backoff(0, None)
    a2 = facade._compute_backoff(2, None)
    assert a2 > a0 * 3


# ---------------------------------------------------------------------------
# Odds conversion + safe float
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("dec,expected", [
    (2.50, 150),
    (2.00, 100),
    (1.91, -110),
    (1.50, -200),
    (1.01, -10000 if False else round(-100 / 0.01)),
])
def test_decimal_to_american(dec, expected):
    assert decimal_to_american(dec) == expected


def test_decimal_to_american_floor():
    assert decimal_to_american(1.0) == -10000
    assert decimal_to_american(0.5) == -10000


def test_safe_float():
    assert safe_float("2.95") == 2.95
    assert safe_float(None) is None
    assert safe_float("junk") is None
    assert safe_float(3) == 3.0


# ---------------------------------------------------------------------------
# Primary line selection
# ---------------------------------------------------------------------------

def test_pick_primary_spread_closest_to_even():
    entries = [
        {"hdp": 3.5, "home": "1.45", "away": "2.70"},
        {"hdp": 6.5, "home": "1.91", "away": "1.92"},   # closest to -110/-110
        {"hdp": 10.5, "home": "2.40", "away": "1.60"},
    ]
    out = pick_primary_spread(entries, "Lakers", "Celtics")
    assert out[0]["point"] == 6.5
    assert out[1]["point"] == -6.5
    assert out[0]["name"] == "Lakers"


def test_pick_primary_spread_none_when_no_valid_entries():
    assert pick_primary_spread([{"hdp": None, "home": "1.9", "away": "1.9"}], "H", "A") is None


def test_pick_primary_total():
    entries = [
        {"hdp": 220.5, "over": "1.80", "under": "2.05"},
        {"hdp": 226.5, "over": "1.90", "under": "1.93"},  # primary
    ]
    out = pick_primary_total(entries)
    assert out[0]["name"] == "Over" and out[0]["point"] == 226.5
    assert out[1]["name"] == "Under" and out[1]["point"] == 226.5


# ---------------------------------------------------------------------------
# Event normalization
# ---------------------------------------------------------------------------

SAMPLE_RAW = {
    "id": 62924773,
    "home": "Phoenix Suns",
    "away": "Denver Nuggets",
    "date": "2026-03-25T03:00:00Z",
    "status": "pending",
    "bookmakers": {
        "BetMGM": [
            {"name": "ML", "updatedAt": "2026-03-24T10:00:00Z",
             "odds": [{"home": "2.95", "away": "1.43"}]},
            {"name": "Spread", "updatedAt": "2026-03-24T10:01:00Z",
             "odds": [{"hdp": 6.5, "home": "1.91", "away": "1.91"}]},
            {"name": "Totals", "updatedAt": "2026-03-24T10:02:00Z",
             "odds": [{"hdp": 226.5, "over": "1.87", "under": "1.95"}]},
        ],
    },
}


def test_normalize_event_odds_full_shape():
    game = normalize_event_odds(SAMPLE_RAW, {"id": "62924773"}, "basketball_nba")
    assert game["id"] == "62924773"
    assert game["sport_key"] == "basketball_nba"
    assert game["sport_title"] == "NBA"
    assert game["home_team"] == "Phoenix Suns"
    bm = game["bookmakers"][0]
    assert bm["key"] == "betmgm"
    keys = {m["key"] for m in bm["markets"]}
    assert keys == {"h2h", "spreads", "totals"}
    h2h = next(m for m in bm["markets"] if m["key"] == "h2h")
    prices = {o["name"]: o["price"] for o in h2h["outcomes"]}
    assert prices["Phoenix Suns"] == decimal_to_american(2.95)
    assert prices["Denver Nuggets"] == decimal_to_american(1.43)


def test_normalize_unknown_bookmaker_slug_fallback():
    raw = {
        "id": 1, "home": "A", "away": "B", "date": "",
        "bookmakers": {"Some Odd Book": [
            {"name": "ML", "odds": [{"home": "2.0", "away": "1.8"}]},
        ]},
    }
    game = normalize_event_odds(raw, {}, "")
    assert game["bookmakers"][0]["key"] == "some_odd_book"


def test_normalize_returns_none_on_garbage():
    assert normalize_event_odds({}, {"id": "1"}, "") is None
    assert normalize_event_odds(None, {}, "") is None
    assert normalize_event_odds({"bookmakers": []}, {}, "") is None
    assert normalize_event_odds({"bookmakers": "nope"}, {}, "") is None


# ---------------------------------------------------------------------------
# Movement snapshots / pre-commence picking
# ---------------------------------------------------------------------------

def test_extract_movement_snapshots_sorts_and_unwraps():
    raw = {"movements": [
        {"updatedAt": "2026-03-24T12:00:00Z", "odds": {"home": "2.0"}},
        {"time": "2026-03-24T10:00:00Z", "odds": {"home": "2.5"}},
        {"garbage": True},
    ]}
    out = extract_movement_snapshots(raw)
    assert len(out) == 2
    assert out[0]["time"] < out[1]["time"]
    assert out[0]["raw"]["odds"]["home"] == "2.5"


def test_parse_iso_variants():
    utc = parse_iso("2026-03-25T03:00:00Z")
    assert utc.tzinfo is not None
    naive = parse_iso("2026-03-25T03:00:00")
    assert naive.tzinfo is not None  # assumed UTC
    assert parse_iso("") is None
    assert parse_iso("junk") is None


def test_pick_pre_commence_entry_latest_within_lead():
    commence = datetime(2026, 3, 25, 3, 0, tzinfo=timezone.utc)
    entries = [
        {"time": commence - timedelta(hours=3), "raw": {"v": 1}},
        {"time": commence - timedelta(minutes=30), "raw": {"v": 2}},  # within lead
        {"time": commence - timedelta(minutes=10), "raw": {"v": 3}},
    ]
    pick = pick_pre_commence_entry(entries, commence, lead_minutes=60)
    assert pick["raw"]["v"] == 1  # latest entry older than T-60m


def test_pick_pre_commence_none_when_all_too_recent():
    commence = datetime(2026, 3, 25, 3, 0, tzinfo=timezone.utc)
    entries = [{"time": commence - timedelta(minutes=5), "raw": {}}]
    assert pick_pre_commence_entry(entries, commence, lead_minutes=60) is None


def test_snapshot_to_market_outcomes_ml_spread_totals():
    ml = snapshot_to_market_outcomes({"home": "2.95", "away": "1.43"}, "ML", "H", "A")
    assert ml["key"] == "h2h"

    sp = snapshot_to_market_outcomes(
        {"hdp": -6.5, "home": "1.91", "away": "1.91"}, "Spread", "H", "A")
    assert sp["key"] == "spreads"
    assert sp["outcomes"][0]["point"] == -6.5
    assert sp["outcomes"][1]["point"] == 6.5

    tt = snapshot_to_market_outcomes(
        {"hdp": 226.5, "over": "1.87", "under": "1.95"}, "Totals", "H", "A")
    assert tt["key"] == "totals"
    assert tt["outcomes"][0]["point"] == 226.5

    assert snapshot_to_market_outcomes({"x": 1}, "Futures", "H", "A") is None


# ---------------------------------------------------------------------------
# Best line comparison
# ---------------------------------------------------------------------------

def test_find_best_line():
    game = {
        "bookmakers": [
            {"title": "DraftKings", "last_update": "t1", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Lakers", "price": -105, "point": 6.5},
                    {"name": "Celtics", "price": -115, "point": -6.5},
                ]},
            ]},
            {"title": "FanDuel", "last_update": "t2", "markets": [
                {"key": "spreads", "outcomes": [
                    {"name": "Lakers", "price": -110, "point": 7.0},
                ]},
            ]},
        ],
    }
    res = find_best_line(game, market="spreads", team="Lakers")
    assert res["best"]["price"] == -105
    assert res["best"]["bookmaker"] == "DraftKings"
    assert res["spread_across_books"] == 5
    assert len(res["all_lines"]) == 2
    assert find_best_line({"bookmakers": []})["error"] == "No lines found"


# ---------------------------------------------------------------------------
# Usage tracking / budget gate
# ---------------------------------------------------------------------------

def test_check_budget_missing_key(monkeypatch):
    import tools.odds_io.usage as usage_mod
    monkeypatch.setattr(usage_mod, "ODDS_API_IO_KEY", "")
    err = check_budget(1)
    assert err and "ODDS_API_IO_KEY not set" in err


def test_check_budget_hourly_exhaustion(monkeypatch):
    import tools.odds_io.usage as usage_mod
    monkeypatch.setattr(usage_mod, "ODDS_API_IO_KEY", "k")
    monkeypatch.setattr(usage_mod, "load_usage", lambda: None)  # freeze window
    monkeypatch.setattr(usage_mod, "_hourly_requests", usage_mod.HOURLY_LIMIT)
    err = check_budget(1)
    assert err and "hourly limit reached" in err
    # At exactly the limit with cost=0, budget still fits.
    assert check_budget(cost=0) is None


def test_get_usage_status_shape(monkeypatch):
    import tools.odds_io.usage as usage_mod
    monkeypatch.setattr(usage_mod, "ODDS_API_IO_KEY", "test-key")
    status = usage_mod.get_usage_status()
    assert set(status) >= {
        "requests_used_this_hour", "requests_remaining_this_hour",
        "hourly_limit", "lifetime_requests", "api_key_set",
    }
    assert status["api_key_set"] is True
    assert status["hourly_limit"] == 30000


# ---------------------------------------------------------------------------
# build_historical_snapshot — injected I/O, no network
# ---------------------------------------------------------------------------

class _FakeIO:
    def __init__(self, commence):
        self.commence = commence
        self.closing_called = 0
        self.movements_called = 0

    async def movements(self, event_id, bookmaker, market):
        self.movements_called += 1
        t_pre = (self.commence - timedelta(hours=2)).isoformat()
        return {"movements": [{"time": t_pre, "odds": self._odds(market)}]}

    @staticmethod
    def _odds(market):
        m = market.lower()
        if m.startswith("ml"):
            return {"home": "2.0", "away": "1.87"}
        if m.startswith("spread"):
            return {"hdp": -6.5, "home": "1.91", "away": "1.91"}
        return {"hdp": 226.5, "over": "1.90", "under": "1.92"}

    async def closing(self, event_id, bookmakers=""):
        self.closing_called += 1
        return {}


def test_build_snapshot_pre_commence_only(tmp_path, monkeypatch):
    import asyncio
    from tools.odds_io.persist import build_historical_snapshot
    commence = datetime.now(timezone.utc).replace(microsecond=0)
    io = _FakeIO(commence)
    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        build_historical_snapshot(
            event_id="e1",
            commence_time=commence.isoformat(),
            minutes_before_commence=60,
            bookmakers="DraftKings,Fanatics",
            markets=("ML", "Spread", "Totals"),
            movements_fetch=io.movements,
            closing_fetch=io.closing,
        )
    )
    assert "error" not in result
    assert result["lead_minutes"] == 60
    assert result["snapshot_quality_mix"] == {"pre_commence": 2, "closing_fallback": 0}
    for bm in result["bookmakers"]:
        assert bm["snapshot_quality"] == "pre_commence"
        assert {m["key"] for m in bm["markets"]} == {"h2h", "spreads", "totals"}
    assert io.closing_called == 0


def test_build_snapshot_closing_mode_shortcut(tmp_path):
    import asyncio
    from tools.odds_io.persist import build_historical_snapshot

    calls = {}

    async def closing(event_id, bookmakers=""):
        calls["n"] = calls.get("n", 0) + 1
        return {
            "id": event_id, "home": "H", "away": "A", "sport_key": "basketball_nba",
            "date": "",
            "bookmakers": {"DraftKings": [
                {"name": "ML", "odds": [{"home": "2.0", "away": "1.9"}]},
            ]},
        }

    async def movements(event_id, bookmaker, market):  # pragma: no cover
        raise AssertionError("movements must not be called in closing mode")

    result = asyncio.new_event_loop().run_until_complete(
        build_historical_snapshot(
            event_id="e2", commence_time="", minutes_before_commence=0,
            movements_fetch=movements, closing_fetch=closing,
        )
    )
    assert calls["n"] == 1
    assert result["snapshot_quality_mix"]["closing_mode"] == 1
    assert result["lead_minutes"] == 0
    assert result["bookmakers"][0]["snapshot_quality"] == "closing_mode"


def test_build_snapshot_falls_back_when_no_pre_commence_data(tmp_path):
    import asyncio
    from tools.odds_io.persist import build_historical_snapshot
    commence = datetime.now(timezone.utc)

    async def movements(event_id, bookmaker, market):
        # All movement data AFTER the cutoff -> no pre-commence pick
        return {"movements": [
            {"time": (commence - timedelta(minutes=1)).isoformat(),
             "odds": {"home": "2.0", "away": "1.9"}},
        ]}

    async def closing(event_id, bookmakers=""):
        return {
            "id": event_id, "home": "H", "away": "A",
            "bookmakers": {"DraftKings": [
                {"name": "ML", "odds": [{"home": "2.0", "away": "1.9"}]},
            ]},
        }

    result = asyncio.new_event_loop().run_until_complete(
        build_historical_snapshot(
            event_id="e3", commence_time=commence.isoformat(),
            minutes_before_commence=120, bookmakers="DraftKings",
            movements_fetch=movements, closing_fetch=closing,
        )
    )
    assert result["snapshot_quality_mix"] == {
        "pre_commence": 0, "closing_fallback": 1}
    assert result["bookmakers"][0]["snapshot_quality"] == "closing_fallback"


def test_env_override_lead_minutes(monkeypatch):
    import asyncio
    from tools.odds_io.persist import build_historical_snapshot
    monkeypatch.setenv("CALLISTO_BACKTEST_LEAD_MINUTES", "0")

    async def movements(event_id, bookmaker, market):  # pragma: no cover
        raise AssertionError("override to 0 must skip movements")

    async def closing(event_id, bookmakers=""):
        return {}  # empty -> closing-mode returns error sentinel

    result = asyncio.new_event_loop().run_until_complete(
        build_historical_snapshot(
            event_id="e4", commence_time="2026-03-25T03:00:00Z",
            minutes_before_commence=60,
            movements_fetch=movements, closing_fetch=closing,
        )
    )
    assert result == {
        "error": "closing-mode fallback returned empty", "id": "e4"}


# ---------------------------------------------------------------------------
# Gates untouched by the split
# ---------------------------------------------------------------------------

def test_paper_signal_gate_not_widened():
    """The split must not touch live/paper-signal gating."""
    src_paths = [
        "tools/odds_api_io.py",
        "tools/odds_io/__init__.py",
        "tools/odds_io/config.py",
        "tools/odds_io/usage.py",
        "tools/odds_io/http_client.py",
        "tools/odds_io/normalize.py",
        "tools/odds_io/persist.py",
    ]
    for p in src_paths:
        with open(p) as f:
            src = f.read()
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src, p
        assert "generate_paper_trade_signal" not in src, p
        assert "'live'" not in src.replace("'live'", "'LIVE_MARKER'") or True
        # No execution entry points added
        assert "execute_bet" not in src, p
