"""Tests for the tools.arb package split.

The original tools/arbitrage_scanner.py was a single ~1100-line module; it is
now split into tools/arb/{models,prices,stakes,scanner,synthetic,orchestrator,
persistence,backtest}.py with arbitrage_scanner.py kept as a re-exporting
facade. These tests verify:

1. The facade re-exports the full public + private surface.
2. Each submodule is importable and functional on its own.
3. End-to-end behavior is preserved (scan → persist → summary).
4. Safety invariants: nothing touches live execution; paper/research only.
"""

from __future__ import annotations

import importlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

NOW = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
FRESH = NOW.isoformat()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _game(legs: list[dict], market: str = "h2h", gid: str = "g1") -> dict:
    """Build an odds-api style game dict from flat legs.

    Each leg: {book, name, price, point(optional), ts(optional)}.
    """
    books: dict[str, dict] = {}
    for leg in legs:
        bm = books.setdefault(leg["book"], {"key": leg["book"].lower(),
                                            "title": leg["book"],
                                            "markets": {}})
        mkt = bm["markets"].setdefault(market, {"key": market, "outcomes": []})
        o = {"name": leg["name"], "price": leg["price"]}
        if "point" in leg:
            o["point"] = leg["point"]
        if "ts" in leg:
            o["fetched_at"] = leg["ts"]
        mkt["outcomes"].append(o)
    for bm in books.values():
        bm["markets"] = list(bm["markets"].values())
    return {
        "id": gid,
        "sport_key": "test_nba",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": list(books.values()),
    }


# ---------------------------------------------------------------------------
# Facade re-exports
# ---------------------------------------------------------------------------
FACADE_NAMES = [
    # public API
    "scan_pure_arb", "scan_dutch_book", "scan_cross_market_synthetic",
    "full_arbitrage_scan", "persist_opportunity", "backtest_arbs",
    "ArbLeg", "ArbOpportunity",
    "DEFAULT_BUDGET", "DEFAULT_EPSILON", "DEFAULT_STALE_SECONDS",
    "MIN_EFFECTIVE_BUDGET_PCT", "MIN_PROFIT_PCT", "MAX_PROFIT_PCT",
    "MAX_IMPLIED_DIVERGENCE",
    # private helpers other modules/tests reach for
    "_parse_ts", "_age_seconds", "_extract_line_ts",
    "_collect_best_prices", "_collect_point_groups", "_best_at",
    "_compute_stakes", "_build_arb_from_pair", "_scan_spread_arbs",
]


@pytest.mark.parametrize("name", FACADE_NAMES)
def test_facade_reexports(name):
    import tools.arbitrage_scanner as facade
    assert hasattr(facade, name), f"facade missing {name}"
    assert getattr(facade, name) is not None


def test_facade_matches_package_identity():
    """Facade symbols ARE the package symbols (no copies)."""
    import tools.arbitrage_scanner as facade
    import tools.arb as pkg
    for name in FACADE_NAMES:
        assert getattr(facade, name) is getattr(pkg, name)


def test_submodules_importable():
    for mod in ("models", "prices", "stakes", "scanner", "synthetic",
                "orchestrator", "persistence", "backtest"):
        m = importlib.import_module(f"tools.arb.{mod}")
        assert m is not None


def test_models_dataclasses():
    from tools.arb.models import ArbLeg, ArbOpportunity
    leg = ArbLeg(
        bookmaker="B1", bookmaker_canonical="b1", outcome="Home",
        american_odds=105, decimal_odds=2.05, implied_prob=1 / 2.05,
        stake=100.0,
    )
    opp = ArbOpportunity(
        game_id="g", game="Away @ Home", sport="nba", market_type="h2h",
        thesis_tag="arb", total_implied=0.97, profit_pct=0.03,
        expected_profit=30.0, budget_requested=1000.0, effective_budget=1000.0,
        legs=[leg],
    )
    d = opp.to_dict()
    assert d["legs"][0]["outcome"] == "Home"
    assert round(leg.stake * leg.decimal_odds, 2) == 205.0


# ---------------------------------------------------------------------------
# prices module
# ---------------------------------------------------------------------------
def test_prices_collect_best_and_ts_helpers():
    from tools.arb.prices import (
        _age_seconds, _best_at, _collect_best_prices, _extract_line_ts, _parse_ts,
    )
    g = _game([
        {"book": "A", "name": "Home", "price": 105, "ts": FRESH},
        {"book": "B", "name": "Home", "price": 120, "ts": FRESH},
        {"book": "B", "name": "Away", "price": 105, "ts": FRESH},
    ])
    best = _collect_best_prices(g, "h2h")
    assert best["Home"]["american"] == 120          # highest decimal wins
    assert best["Home"]["bookmaker_canonical"]

    assert _parse_ts(None) is None
    assert _parse_ts("not-a-date") is None
    dt = _parse_ts("2026-04-22T12:00:00Z")
    assert dt is not None and dt.tzinfo is not None
    age = _age_seconds(FRESH, NOW)
    assert age == pytest.approx(0.0)
    assert _extract_line_ts({"fetched_at": FRESH}, {}) == FRESH

    spread_game = _game([
        {"book": "A", "name": "Home", "price": -110, "point": 2.5},
        {"book": "B", "name": "Home", "price": 100, "point": 2.5},
    ], market="spreads")
    got = _best_at(spread_game, "spreads", "Home", 2.5)
    assert got is not None and got["american"] == 100
    assert _best_at(spread_game, "spreads", "Home", 9.5) is None


def test_prices_point_groups():
    from tools.arb.prices import _collect_point_groups
    g = _game([
        {"book": "A", "name": "Home", "price": 105, "point": 2.5},
        {"book": "B", "name": "Away", "price": 105, "point": 3.5},
    ], market="totals")
    groups = _collect_point_groups(g, "totals")
    assert set(groups.keys()) == {2.5, 3.5}
    h2h_groups = _collect_point_groups(g, "h2h")
    assert list(h2h_groups.keys()) == [None]


# ---------------------------------------------------------------------------
# stakes module
# ---------------------------------------------------------------------------
def test_stakes_equal_payout_and_caps():
    from tools.arb.stakes import _compute_stakes
    from tools.arb.models import ArbLeg

    def mkleg(canonical, implied):
        return ArbLeg(bookmaker=canonical, bookmaker_canonical=canonical,
                      outcome="", american_odds=0,
                      decimal_odds=(1.0 / implied) if implied else 1e9,
                      implied_prob=implied)

    legs = [mkleg("a", 1 / 2.05), mkleg("b", 1 / 2.05)]
    eff, limited = _compute_stakes(legs, budget=1000.0)
    assert not limited
    assert eff == pytest.approx(1000.0)
    payouts = [round(l.stake * l.decimal_odds, 2) for l in legs]
    assert abs(payouts[0] - payouts[1]) < 0.01

    zero_legs = [mkleg("a", 0.0), mkleg("b", 0.0)]
    eff0, _ = _compute_stakes(zero_legs, budget=1000.0)
    assert eff0 == 0.0


# ---------------------------------------------------------------------------
# scanner module end-to-end (via package imports)
# ---------------------------------------------------------------------------
def test_scanner_finds_h2h_arb():
    from tools.arb.scanner import scan_pure_arb
    g = _game([
        {"book": "BookA", "name": "Home", "price": 110, "ts": FRESH},
        {"book": "BookB", "name": "Away", "price": 110, "ts": FRESH},
    ])
    arbs = scan_pure_arb(g, "h2h", now=NOW)
    assert len(arbs) == 1
    a = arbs[0]
    assert a.thesis_tag == "arb"
    assert len(a.legs) == 2
    assert a.profit_pct > 0
    assert {l.bookmaker for l in a.legs} == {"BookA", "BookB"}
    # paper-only markers intact
    assert all(l.stake >= 0 for l in a.legs)


def test_scanner_rejects_same_book_binary():
    from tools.arb.scanner import scan_pure_arb
    g = _game([
        {"book": "OnlyOne", "name": "Home", "price": 110, "ts": FRESH},
        {"book": "OnlyOne", "name": "Away", "price": 110, "ts": FRESH},
    ])
    assert scan_pure_arb(g, "h2h", now=NOW) == []


def test_dutch_book_tags_three_way():
    from tools.arb.scanner import scan_dutch_book
    # 3-way soccer market with sum(implied) ~0.96
    g = _game([
        {"book": "A", "name": "Home", "price": -180, "ts": FRESH},  # 0.643
        {"book": "B", "name": "Draw", "price": 550, "ts": FRESH},   # 0.154
        {"book": "C", "name": "Away", "price": 700, "ts": FRESH},   # 0.125
    ])
    arbs = scan_dutch_book(g, "h2h", now=NOW)
    assert len(arbs) == 1
    assert arbs[0].thesis_tag == "dutch"
    assert len(arbs[0].legs) == 3
    assert "dutch" in arbs[0].notes


def test_spread_pairing_via_package():
    from tools.arb.scanner import scan_pure_arb
    g = _game([
        {"book": "A", "name": "Home", "price": 105, "point": 3.5, "ts": FRESH},
        {"book": "B", "name": "Away", "price": 105, "point": -3.5, "ts": FRESH},
    ], market="spreads")
    arbs = scan_pure_arb(g, "spreads", now=NOW)
    assert len(arbs) == 1
    pts = sorted(l.point for l in arbs[0].legs)
    assert pts == [-3.5, 3.5]


# ---------------------------------------------------------------------------
# synthetic module
# ---------------------------------------------------------------------------
def test_synthetic_team_totals_vs_game_total():
    from tools.arb.synthetic import scan_cross_market_synthetic

    def tt_outcome(team, pt, price):
        return {"name": "Over", "description": team, "point": pt,
                "price": price, "fetched_at": FRESH}

    game = {
        "id": "syn-1", "sport_key": "nba",
        "home_team": "Lakers", "away_team": "Celtics",
        "bookmakers": [
            {"key": "book_a", "title": "BookA", "markets": [
                {"key": "team_totals", "outcomes": [
                    tt_outcome("Lakers", 110.5, 150),
                    tt_outcome("Celtics", 108.5, 150),
                ]},
            ]},
            {"key": "book_b", "title": "BookB", "markets": [
                # Game total whose Over implied (~0.85) far exceeds the
                # team-total pair implied (~0.80) -> synthetic gap.
                {"key": "totals", "outcomes": [
                    {"name": "Over", "point": 219.0, "price": -570,
                     "fetched_at": FRESH},
                    {"name": "Under", "point": 219.0, "price": 420,
                     "fetched_at": FRESH},
                ]},
            ]},
        ],
    }
    arbs = scan_cross_market_synthetic(game, now=NOW)
    assert len(arbs) >= 1
    a = arbs[0]
    assert a.thesis_tag == "synthetic_arb"
    assert a.market_type.startswith("synthetic:")
    assert len(a.legs) == 2


def test_synthetic_absent_returns_empty():
    from tools.arb.synthetic import scan_cross_market_synthetic
    g = _game([
        {"book": "A", "name": "Home", "price": 110, "ts": FRESH},
        {"book": "B", "name": "Away", "price": 110, "ts": FRESH},
    ])
    assert scan_cross_market_synthetic(g, now=NOW) == []


# ---------------------------------------------------------------------------
# orchestrator module
# ---------------------------------------------------------------------------
def test_full_scan_summary_shape():
    from tools.arb.orchestrator import full_arbitrage_scan
    g = _game([
        {"book": "A", "name": "Home", "price": 110, "ts": FRESH},
        {"book": "B", "name": "Away", "price": 110, "ts": FRESH},
    ])
    res = full_arbitrage_scan({"games": [g], "sport": "nba"}, now=NOW)
    assert res["summary"]["pure_count"] == 1
    assert res["summary"]["dutch_count"] == 0
    assert res["game_count"] == 1
    assert isinstance(res["pure_arbs"], list)
    assert set(res["summary"]["params"]) == {
        "epsilon", "stale_seconds", "budget"}


# ---------------------------------------------------------------------------
# persistence module
# ---------------------------------------------------------------------------
@pytest.fixture()
def ev_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT, sport TEXT, game_id TEXT, team TEXT,
            market TEXT, bookmaker TEXT, american_odds INTEGER,
            implied_probability REAL, estimated_true_prob REAL, edge REAL,
            expected_value REAL, kelly_fraction REAL, status TEXT,
            source TEXT, thesis_tag TEXT DEFAULT NULL, expires_at TEXT
        )
    """)
    yield conn
    conn.close()


def _mk_opp():
    from tools.arb.models import ArbLeg, ArbOpportunity
    legs = [
        ArbLeg(bookmaker="BookA", bookmaker_canonical="booka", outcome="Home",
               american_odds=110, decimal_odds=2.10, implied_prob=1 / 2.10,
               stake=487.8),
        ArbLeg(bookmaker="BookB", bookmaker_canonical="bookb", outcome="Away",
               american_odds=110, decimal_odds=2.10, implied_prob=1 / 2.10,
               stake=487.8),
    ]
    return ArbOpportunity(
        game_id="g1", game="Away @ Home", sport="nba", market_type="h2h",
        thesis_tag="arb", total_implied=0.952381, profit_pct=0.05,
        expected_profit=50.0, budget_requested=1000.0, effective_budget=1000.0,
        legs=legs, detected_at=FRESH,
        expires_at=datetime(2026, 4, 22, 12, 1, 0, tzinfo=timezone.utc).isoformat(),
    )


def test_persist_one_row_per_leg(ev_db):
    from tools.arb.persistence import persist_opportunity
    ids = persist_opportunity(ev_db, _mk_opp())
    assert len(ids) == 2
    rows = ev_db.execute(
        "SELECT source, status, thesis_tag FROM ev_opportunities"
    ).fetchall()
    assert all(r[0] == "arbitrage" and r[1] == "open" for r in rows)
    assert rows[0][2] == "arb"


def test_persist_adds_missing_columns(ev_db):
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE ev_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            detected_at TEXT, sport TEXT, game_id TEXT, team TEXT,
            market TEXT, bookmaker TEXT, american_odds INTEGER,
            implied_probability REAL, estimated_true_prob REAL, edge REAL,
            expected_value REAL, kelly_fraction REAL, status TEXT, source TEXT
        )
    """)
    from tools.arb.persistence import persist_opportunity
    ids = persist_opportunity(conn, _mk_opp())  # adds thesis_tag/expires_at
    assert len(ids) == 2
    cols = {r[1] for r in conn.execute("PRAGMA table_info(ev_opportunities)")}
    assert {"thesis_tag", "expires_at"} <= cols
    conn.close()


# ---------------------------------------------------------------------------
# backtest module
# ---------------------------------------------------------------------------
def test_backtest_over_snapshots(tmp_path):
    from tools.arb.backtest import backtest_arbs
    db = str(tmp_path / "snap.db")
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE odds_snapshots (
        id INTEGER PRIMARY KEY, sport TEXT, timestamp TEXT, snapshot_json TEXT
    )""")
    g = _game([
        {"book": "A", "name": "Home", "price": 110},
        {"book": "B", "name": "Away", "price": 110},
    ])
    snap = {"games": [g], "sport": "nba"}
    snap_ts = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    conn.execute(
        "INSERT INTO odds_snapshots (sport, timestamp, snapshot_json) VALUES (?,?,?)",
        ("nba", snap_ts, __import__("json").dumps(snap)),
    )
    conn.commit()
    res = backtest_arbs(db, days=30, limit_snapshots=None)
    assert res["total_snapshots_scanned"] == 1
    assert res["snapshots_with_arb"] == 1
    assert res["total_arb_instances"] >= 1
    assert res["profit_pct_max"] > 0
    conn.close()


# ---------------------------------------------------------------------------
# Split hygiene / safety invariants
# ---------------------------------------------------------------------------
def test_no_live_status_in_split():
    """Paper/research only: no 'live' signal statuses may creep in."""
    import pathlib
    arb_dir = pathlib.Path(__file__).resolve().parents[1] / "tools" / "arb"
    for f in arb_dir.glob("*.py"):
        text = f.read_text()
        assert '"live"' not in text and "'live'" not in text, \
            f"{f.name} must not reference live status"


def test_line_counts_actually_split():
    """The facade should be dramatically smaller than the original monolith."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    facade_lines = (root / "tools" / "arbitrage_scanner.py").read_text().count("\n")
    arb_lines = sum(
        p.read_text().count("\n") for p in (root / "tools" / "arb").glob("*.py")
    )
    assert facade_lines < 200
    assert arb_lines > 800
