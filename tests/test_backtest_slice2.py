"""Tests for slice 2 of the backtest split: tools/btest package extraction.

The engine/math/paper-diagnostic helpers now live in tools/btest/
(market_processing, events_io, resolution, run_stats, paper_diagnostics,
snapshots). tools/backtest.py remains the public facade — BacktestEngine's
import path and method signatures are unchanged.
"""

import asyncio
import inspect

import pytest

import tools.btest.market_processing as market_processing
import tools.btest.paper_diagnostics as paper_diagnostics
import tools.btest.resolution as resolution
import tools.btest.run_stats as run_stats
from tools.backtest import BacktestEngine, _signal_confidence


# ---------------------------------------------------------------------------
# Canonical definitions live in tools/btest/*
# ---------------------------------------------------------------------------


def test_sharp_books_registry_lives_in_btest():
    src = inspect.getsource(market_processing)
    assert "SHARP_BOOKS = {" in src
    assert "pinnacle" in market_processing.SHARP_BOOKS
    assert "draftkings" not in market_processing.SHARP_BOOKS
    assert "fanduel" not in market_processing.SHARP_BOOKS


def test_no_large_blocks_remain_in_backtest_facade():
    import tools.backtest as backtest_mod
    src = inspect.getsource(backtest_mod)
    # The sharp-book registry and scipy/numpy metric bodies must not be re-defined here.
    assert '"pinnacle", "lowvig", "lowvig.ag", "circa",' not in src
    assert "binomtest" not in src
    assert "ttest_1samp" not in src
    assert "np.corrcoef" not in src
    assert "_json.loads(row[12])" not in src


# ---------------------------------------------------------------------------
# market_processing helpers
# ---------------------------------------------------------------------------


def test_effective_game_market_passthrough():
    markets = {"spreads", "totals"}
    assert market_processing.effective_game_market("spreads", markets) == "spreads"
    # Non-prop market not present -> returned as-is (no fallback for game lines)
    assert market_processing.effective_game_market("h2h", markets) == "h2h"


def test_effective_game_market_prop_fallback():
    markets = {"spreads", "totals"}
    assert (
        market_processing.effective_game_market("player_points", markets) == "totals"
    )
    assert (
        market_processing.effective_game_market("player_pra", markets) == "totals"
    )


def test_effective_game_market_prop_no_fallback_available():
    assert market_processing.effective_game_market("player_points", set()) is None
    # Unknown prop falls back to first available market (spreads preferred,
    # else whatever exists)
    markets = {"h2h"}
    assert (
        market_processing.effective_game_market("player_pass_yards", markets)
        in markets | {"spreads"}
    )


def test_devig_pair_power_and_multiplicative():
    fair_a, fair_b = market_processing.devig_pair(-110, -110, "power")
    assert fair_a == pytest.approx(fair_b)
    assert fair_a + fair_b == pytest.approx(1.0)

    fair_c, fair_d = market_processing.devig_pair(-110, -110, "multiplicative")
    assert fair_c + fair_d == pytest.approx(1.0)


def test_clean_outliers_filters_and_falls_back():
    values = [(0.52, "a"), (0.53, "b"), (0.90, "outlier")]
    cleaned = market_processing.clean_outliers(values, 0.525)
    assert all(v <= 0.6 for v, _ in cleaned)

    # All outliers -> fall back to full set rather than empty list
    extreme = market_processing.clean_outliers([(0.99, "x")], 0.50)
    assert extreme == [(0.99, "x")]


def test_choose_fair_value_crossbook_vs_consensus():
    others = [(0.55, "a"), (0.54, "b"), (0.56, "c")]
    fair, method, best_val, best_book = market_processing.choose_fair_value(others, 3)
    assert method == "cross_book_best_line"
    assert best_val == 0.56 and best_book == "c"

    # <3 non-target books -> consensus devig
    few = others[:2]
    fair2, method2, _, _ = market_processing.choose_fair_value(few, 2)
    assert method2 == "consensus_devig"
    assert fair2 == pytest.approx((0.55 + 0.54) / 2)


def test_direction_sanity_ok():
    assert market_processing.direction_sanity_ok(0.7, 0.4)  # both favored-ish
    assert not market_processing.direction_sanity_ok(0.7, 0.2)  # disagreement
    assert not market_processing.direction_sanity_ok(0.2, 0.7)


def test_evaluate_side_skips_absurd_edges():
    verdict = market_processing.evaluate_side(
        0.95, -400, edge_threshold=0.01, non_target_count=5, market_key="h2h",
    )
    # 0.95 fair vs heavy favorite price -> edge beyond cap or direction fail
    assert verdict["skip"] or abs(verdict.get("edge", 0)) <= 0.15


def test_evaluate_side_normal_signal():
    # Fair 0.55 vs -100 (implied ~0.524) => positive edge above threshold
    verdict = market_processing.evaluate_side(
        0.55, -100, edge_threshold=0.01, non_target_count=4, market_key="totals",
    )
    assert not verdict["skip"]
    assert verdict["is_signal"] is True
    assert verdict["edge"] > 0.01


def test_evaluate_side_heavy_favorite_suppressed():
    verdict = market_processing.evaluate_side(
        0.85, -500, edge_threshold=0.01, non_target_count=5, market_key="h2h",
    )
    if not verdict["skip"]:
        assert verdict["is_signal"] is False  # heavy-fav gate on h2h


def test_evaluate_side_min_books_gate():
    verdict = market_processing.evaluate_side(
        0.55, -100, edge_threshold=0.01, non_target_count=3, market_key="totals",
    )
    if not verdict["skip"]:
        assert verdict["is_signal"] is False  # needs >=4 books


def test_build_event_row_layout():
    row = market_processing.build_event_row(
        run_id="r1", event_id="e1", hypothesis_id="h1", sport="nba",
        player=None, market="totals", line=220.5, side="Over", book="dk",
        target_price=-110, target_implied=0.524, fair_val=0.55,
        factors={"books_used": 4}, edge=0.026, ev=0.03, kelly=0.02,
        is_signal=True, game_date="2026-01-01", snapshot_time="t",
    )
    assert len(row) == 19
    assert row[0] == "r1" and row[1] == "e1"
    assert row[13] == 0.026
    assert '"books_used": 4' in row[12]


def test_collect_book_snapshot_quality_default():
    bookmakers = [
        {"key": "DraftKings", "snapshot_quality": "closing_mode"},
        {"key": "fanduel"},
    ]
    q = market_processing.collect_book_snapshot_quality(bookmakers)
    assert q == {"draftkings": "closing_mode", "fanduel": "pre_commence"}


def _mk_bookmaker(bk, outcomes, market="totals"):
    return {"key": bk, "title": bk, "markets": [{"key": market, "outcomes": outcomes}]}


def test_index_lines_by_key_and_group_sides_spread_pairing():
    bookmakers = [
        _mk_bookmaker("dk", [
            {"name": "Lakers", "point": -7.5, "price": -110},
            {"name": "Celtics", "point": 7.5, "price": -105},
        ], market="spreads"),
        _mk_bookmaker("fd", [
            {"name": "Lakers", "point": -7.5, "price": -112},
            {"name": "Celtics", "point": 7.5, "price": -108},
        ], market="spreads"),
    ]
    # market_type must match the market key — mismatch indexes nothing
    assert market_processing.index_lines_by_key(bookmakers, "totals") == {}
    lines = market_processing.index_lines_by_key(bookmakers, "spreads")
    assert len(lines) == 2  # 2 sides, each with both books merged under one key

    sides, signed = market_processing.group_sides(lines)
    assert len(sides) == 1  # -7.5/+7.5 pair grouped by abs(point)
    group = sides[("spreads", 7.5)]
    assert set(group.keys()) == {"Lakers", "Celtics"}
    assert signed[("spreads", 7.5, "Lakers")] == -7.5
    assert signed[("spreads", 7.5, "Celtics")] == 7.5


def test_index_props_groups_over_under_per_book():
    bookmakers = [
        {
            "key": "DK", "title": "DraftKings",
            "markets": [{
                "key": "player_points",
                "outcomes": [
                    {"description": "LeBron James", "point": 25.5,
                     "name": "Over", "price": -115},
                    {"description": "LeBron James", "point": 25.5,
                     "name": "Under", "price": -105},
                ],
            }],
        },
        {
            "key": "FD", "title": "FanDuel",
            "markets": [{
                "key": "player_points",
                "outcomes": [
                    {"description": "LeBron James", "point": 25.5,
                     "name": "Over", "price": -120},
                    {"description": "LeBron James", "point": 25.5,
                     "name": "Under", "price": -102},
                ],
            }],
        },
    ]
    props, names = market_processing.index_props(bookmakers, "player_points")
    # keys are lowercased; titles preserved in book_names
    assert names == {"dk": "DraftKings", "fd": "FanDuel"}
    key = ("LeBron James", "player_points", 25.5)
    assert key in props
    assert set(props[key].keys()) == {"dk", "fd"}
    assert props[key]["dk"]["Over"] == -115
    assert props[key]["fd"]["Under"] == -102


# ---------------------------------------------------------------------------
# events_io helpers
# ---------------------------------------------------------------------------


def test_signal_confidence_tiers():
    from tools.btest.events_io import signal_confidence

    assert signal_confidence(0.05) == "high"
    assert signal_confidence(0.02) == "high"
    assert signal_confidence(0.015) == "medium"
    assert signal_confidence(0.005) == "low"
    # facade wrapper agrees
    assert _signal_confidence(0.03) == "high"


def test_new_trade_id_unique_and_short():
    from tools.btest.events_io import new_trade_id

    ids = {new_trade_id() for _ in range(20)}
    assert len(ids) == 20
    assert all(len(i) == 12 for i in ids)


def test_dedup_best_edge_by_event():
    rows = [
        {"event_id": "g1", "edge": 0.02, "book": "dk"},
        {"event_id": "g1", "edge": 0.04, "book": "fd"},
        {"event_id": "g2", "edge": 0.01, "book": "dk"},
    ]
    deduped = __import__(
        "tools.btest.events_io", fromlist=["dedup_best_edge_by_event"]
    ).dedup_best_edge_by_event(rows)
    assert len(deduped) == 2
    g1 = next(r for r in deduped if r["event_id"] == "g1")
    assert g1["book"] == "fd" and g1["edge"] == 0.04


# ---------------------------------------------------------------------------
# resolution helpers
# ---------------------------------------------------------------------------


def test_extract_home_away_teams_from_event_id():
    home, away = resolution.extract_home_away_teams(
        "2026-01-01|LA Lakers|Boston Celtics", None
    )
    assert (home, away) == ("LA Lakers", "Boston Celtics")


def test_extract_home_away_teams_from_model_factors():
    factors = '{"home_team": "Bulls", "away_team": "Knicks"}'
    home, away = resolution.extract_home_away_teams("plain-id", factors)
    assert (home, away) == ("Bulls", "Knicks")


def test_scores_from_odds_api_game():
    game = {
        "completed": True,
        "home_team": "A",
        "away_team": "B",
        "scores": [{"name": "A", "score": "110"}, {"name": "B", "score": 98}],
    }
    assert resolution.scores_from_odds_api_game(game) == (110, 98)

    assert resolution.scores_from_odds_api_game({**game, "completed": False}) is None
    assert resolution.scores_from_odds_api_game({**game, "scores": []}) is None


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchall(self):
        return self._rows


class FakeDB:
    def __init__(self, results_rows, ctx_rows):
        self._results = results_rows
        self._ctx = ctx_rows

    async def execute(self, sql, *args):
        if "game_results" in sql:
            return FakeCursor(self._results)
        return FakeCursor(self._ctx)


@pytest.mark.asyncio
async def test_build_results_index_merges_contexts_without_dupes():
    db = FakeDB(
        results_rows=[
            ("basketball_nba", "2026-01-01", "A", "B", 110, 98),
        ],
        ctx_rows=[
            # duplicate of the game_result — should be skipped
            ("basketball_nba", "2026-01-01", "A", "B", 110, 98),
            # new ESPN-only game — should be added
            ("basketball_nba", "2026-01-02", "C", "D", 101, 99),
        ],
    )
    games_by_date, dates, ctx_added = await resolution.build_results_index(
        db, "2026-01-01", "2026-01-02"
    )
    assert len(games_by_date[("basketball_nba", "2026-01-01")]) == 1
    assert games_by_date[("basketball_nba", "2026-01-02")] == [("C", "D", 101, 99)]
    assert ctx_added == 1
    assert dates == {"2026-01-01", "2026-01-02"}


def test_find_scores_for_event_both_orientations_and_miss():
    games = {
        ("nba", "2026-01-01"): [
            ("Alpha", "Beta", 100, 90),
        ],
    }
    tm = lambda a, b: a.lower()[:4] == b.lower()[:4]  # noqa: E731

    assert resolution.find_scores_for_event(
        games, "nba", "2026-01-01", "Alpha", "Beta", team_matches=tm
    ) == (100, 90)
    # swapped orientation
    assert resolution.find_scores_for_event(
        games, "nba", "2026-01-01", "Beta", "Alpha", team_matches=tm
    ) == (90, 100)
    # wrong sport / wrong date / unknown teams -> None
    assert resolution.find_scores_for_event(
        games, "nhl", "2026-01-01", "Alpha", "Beta", team_matches=tm
    ) is None
    assert resolution.find_scores_for_event(
        games, "nba", "2026-01-02", "Alpha", "Beta", team_matches=tm
    ) is None


# ---------------------------------------------------------------------------
# run_stats helpers
# ---------------------------------------------------------------------------


def test_compute_signal_metrics_all_wins_coinflip_null():
    metrics = run_stats.compute_signal_metrics(
        wins=5, losses=0, expected_rate=0.5,
        signal_events=[(-100, "won", 0.55, 0.02)] * 5,
    )
    assert metrics["p_binomial"] < 0.05
    assert metrics["roi_pct"] > 0
    assert metrics["sharpe"] >= 0
    assert metrics["brier"] == pytest.approx((0.45 ** 2) * 5 / 5)


def test_compute_signal_metrics_expected_rate_not_half():
    # 5W-0L at -300 favorites (expected ~75%) should NOT be significant
    metrics = run_stats.compute_signal_metrics(
        wins=5, losses=0, expected_rate=0.75,
        signal_events=[(-300, "won", 0.78, 0.01)] * 5,
    )
    assert metrics["p_binomial"] > 0.05


def test_compute_signal_metrics_empty():
    metrics = run_stats.compute_signal_metrics(0, 0, 0.5, [])
    assert metrics["p_binomial"] == 1.0
    assert metrics["roi_pct"] == 0.0
    assert metrics["sortino"] is None


def test_fingerprint_stale():
    assert run_stats.fingerprint_stale(None, (1, 1, 1))
    assert run_stats.fingerprint_stale((1, 1, 0), (1, 1, 1))
    assert not run_stats.fingerprint_stale((1, 1, 1), (1, 1, 1))


def test_prune_fingerprints_cap():
    fps = {f"run{i}": (i, 0, 0) for i in range(10)}
    # Under cap: untouched (same content)
    out = run_stats.prune_fingerprints(fps, ["run9"], cap=100)
    assert out == fps
    # Over cap: keep only active runs
    out = run_stats.prune_fingerprints(fps, ["run8", "run9"], cap=5)
    assert set(out) == {"run8", "run9"}


# ---------------------------------------------------------------------------
# paper_diagnostics helpers
# ---------------------------------------------------------------------------


def _row(edge, fair_prob, books_used, book="dk"):
    import json

    return (
        "paper", "e1", "h1", "nba", None, "totals", 220.5, "Over",
        book, -110, 0.524, fair_prob, json.dumps({"books_used": books_used}),
        edge, 0.03, 0.02, False, "2026-01-01", "t",
    )


def test_edge_distribution_empty_and_populated():
    empty = paper_diagnostics.edge_distribution([])
    assert empty == {
        "max_edge": 0, "min_edge": 0, "above_thresh": 0,
        "min_books_seen": 0, "max_books_seen": 0,
    }

    dist = paper_diagnostics.edge_distribution([
        _row(0.03, 0.55, 5), _row(-0.01, 0.50, 3), _row(0.02, 0.60, 6),
    ])
    assert dist["max_edge"] == 0.03
    assert dist["min_edge"] == -0.01
    assert dist["min_books_seen"] == 3
    assert dist["max_books_seen"] == 6


def test_suppression_reasons_heavy_fav_vs_min_books():
    reasons = paper_diagnostics.suppression_reasons(
        [_row(0.03, 0.85, 5, book="fd")],
        edge_threshold=0.02, market_type="h2h",
    )
    assert any("heavy_fav" in r for r in reasons)

    reasons = paper_diagnostics.suppression_reasons(
        [_row(0.03, 0.55, 3, book="dk")],
        edge_threshold=0.02, market_type="totals",
    )
    assert any("min_books(n=3" in r for r in reasons)

    # Below-threshold edges are ignored entirely
    assert paper_diagnostics.suppression_reasons(
        [_row(0.001, 0.55, 3)], edge_threshold=0.02, market_type="totals"
    ) == []


# ---------------------------------------------------------------------------
# Facade integrity: BacktestEngine API unchanged
# ---------------------------------------------------------------------------


def test_engine_method_signatures_unchanged():
    sigs = {
        "_process_game": [
            "self", "run_id", "hypothesis_id", "game", "game_date",
            "snapshot_time", "market_type", "target_book", "edge_threshold",
            "devig_method", "min_books", "config", "h_sport", "thesis",
            "filters",
        ],
        "_process_game_lines": [
            "self", "run_id", "hypothesis_id", "game", "game_date",
            "snapshot_time", "market_type", "target_book", "edge_threshold",
            "devig_method", "min_books", "config", "h_sport", "filters",
        ],
        "_process_prop_snapshots": [
            "self", "run_id", "hypothesis_id", "prop_lines", "target_book",
            "edge_threshold", "devig_method", "config", "h_sport", "filters",
        ],
        "resolve_from_game_results": ["self", "run_id", "sport"],
        "recalculate_run_stats": ["self", "run_id"],
        "generate_paper_trade_signal": ["self", "hypothesis_id", "live_odds"],
    }
    for name, params in sigs.items():
        got = list(inspect.signature(getattr(BacktestEngine, name)).parameters)
        assert got == params, f"{name}: {got}"


def test_engine_delegates_multibook_enrichment_offline():
    """Snapshot already multi-book with target -> returned unchanged, no DB hit."""

    class NoDB:
        async def execute(self, *a, **k):  # pragma: no cover
            raise AssertionError("DB should not be queried")

    engine = BacktestEngine.__new__(BacktestEngine)
    engine._db = NoDB()
    snapshot = {
        "games": [{
            "sport_key": "basketball_nba",
            "bookmakers": [{"key": "draftkings"}, {"key": "fanduel"}],
        }]
    }
    result = asyncio.run(engine._enrich_snapshot_with_multibook(
        "basketball_nba", "2026-01-01", snapshot, "draftkings"
    ))
    assert result is snapshot


def test_paper_trade_hard_gate_rejects_non_paper(monkeypatch):
    """generate_paper_trade_signal returns [] unless status == 'paper_trading'."""
    from types import SimpleNamespace

    engine = BacktestEngine.__new__(BacktestEngine)

    class HM:
        async def get_hypothesis(self, hid):
            return {"status": "live"}

    engine.hypothesis_manager = HM()

    async def never_touch_db(*a, **k):  # pragma: no cover
        raise AssertionError("must gate before any DB access")

    called = []

    async def run(coro):
        called.append(coro)
        return await coro

    signals = asyncio.run(engine.generate_paper_trade_signal("abc", {"games": []}))
    assert signals == []
    assert not called
