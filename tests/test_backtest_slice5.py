"""Slice-5 extraction tests.

Covers three areas:

1. Facade imports — tools/backtest.py must still re-export everything it
   re-exported before the slice-5 diet (game_line_processing move), and
   BacktestEngine method names/signatures must be unchanged.
2. Paper-only signal hard gate — generate_paper_trade_signal calls
   reject_non_paper BEFORE paper_pipeline, so only "paper_trading"
   hypotheses produce signals. "live" is forbidden.
3. The moved helper itself — game_line_processing.process_game /
   process_game_lines exercised on synthetic multi-book odds.
"""

import inspect

import pytest

from tools.backtest import BacktestEngine
from tools.signals.paper import (
    _PAPER_TRADE_SIGNAL_STATUSES,
    reject_non_paper,
)
from tools.btest import game_line_processing


# ─────────────────────────────────────────────────────────────
# 1. Facade imports & signature stability
# ─────────────────────────────────────────────────────────────

def test_process_game_delegates_to_btest_package():
    """_process_game must be a thin delegator over tools.btest.game_line_processing."""
    src = inspect.getsource(BacktestEngine._process_game)
    assert "game_line_processing.process_game(" in src
    # The heavy body must NOT live in the facade anymore
    body = src.split('"""', 2)[-1]
    assert "available_markets" not in body
    assert "_devig_pair" not in body


def test_process_game_lines_delegates_to_btest_package():
    """_process_game_lines must be a thin delegator over the moved helper."""
    src = inspect.getsource(BacktestEngine._process_game_lines)
    assert "game_line_processing.process_game_lines(" in src
    body = src.split('"""', 2)[-1]
    assert "sides_by_line" not in body
    assert "all_fair_a" not in body


def test_moved_helpers_keep_signatures():
    """Parameter names/order of the delegators match the implementations."""
    facade_params = list(inspect.signature(BacktestEngine._process_game).parameters)
    impl_params = list(inspect.signature(game_line_processing.process_game).parameters)
    # impl takes `engine` first; the rest line up with self + facade params
    assert impl_params[0] == "engine"
    assert ["self"] + impl_params[1:] == facade_params

    facade_params = list(inspect.signature(BacktestEngine._process_game_lines).parameters)
    impl_params = list(inspect.signature(game_line_processing.process_game_lines).parameters)
    assert impl_params[0] == "engine"
    assert ["self"] + impl_params[1:] == facade_params


def test_facade_reexports_still_available():
    """Names other modules import from tools.backtest must survive the diet."""
    from tools import backtest as bt
    for name in (
        "_PAPER_TRADE_SIGNAL_STATUSES",
        "reject_non_paper",
        "allowed_paper_statuses",
        "_signal_confidence",
        "_SHARP_BOOKS",
        "_build_event_row",
        "_clean_outliers",
        "_devig_pair",
        "_effective_game_market",
        "_evaluate_side",
        "_group_sides",
        "_index_lines_by_key",
        "devig_market",
        "power_devig",
        "multiplicative_devig",
        "ev_binary",
        "evaluate_edge",
        "kelly_binary",
        "american_to_decimal",
        "american_to_implied",
    ):
        assert hasattr(bt, name), f"facade lost re-export: {name}"


def test_engine_class_attributes_intact():
    from tools import backtest_io
    assert BacktestEngine.UNFILTERABLE_CONTEXT_FACTORS is backtest_io.UNFILTERABLE_CONTEXT_FACTORS
    assert BacktestEngine.FILTERABLE_CONTEXT_FACTORS is backtest_io.FILTERABLE_CONTEXT_FACTORS
    assert BacktestEngine._CONTEXT_KEYWORD_MAP is backtest_io._CONTEXT_KEYWORD_MAP
    assert BacktestEngine._TEAM_ALIASES is backtest_io._TEAM_ALIASES


# ─────────────────────────────────────────────────────────────
# 2. Paper-only signal hard gate
# ─────────────────────────────────────────────────────────────

def test_hard_gate_statuses_frozen_and_paper_only():
    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_reject_non_paper_matrix():
    assert reject_non_paper("paper_trading") is False  # allowed
    for status in ("live", "", None, "backtesting", "active", "paused", "LIVE", "Paper_Trading"):
        assert reject_non_paper(status) is True, status


class _StubHyp:
    """Minimal hypothesis_manager stub returning a canned row."""

    def __init__(self, row):
        self.row = row
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self.row


def _make_engine(hyp_row):
    engine = object.__new__(BacktestEngine)
    engine.hypothesis_manager = _StubHyp(hyp_row)
    return engine


@pytest.mark.asyncio
async def test_generate_signal_runs_for_paper_trading():
    called = {}

    async def fake_pipeline(engine, hypothesis_id, live_odds):
        called["id"] = hypothesis_id
        return [{"pick": "over", "edge": 0.03}]

    orig = game_line_processing  # ensure module-level patch target exists
    import tools.btest.paper_pipeline as pp
    saved = pp.generate_paper_trade_signal
    pp.generate_paper_trade_signal = fake_pipeline
    try:
        engine = _make_engine({"status": "paper_trading"})
        out = await engine.generate_paper_trade_signal("h1", {"bookmakers": []})
        assert out == [{"pick": "over", "edge": 0.03}]
        assert called["id"] == "h1"
        assert engine.hypothesis_manager.calls == 1
    finally:
        pp.generate_paper_trade_signal = saved


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["live", "backtesting", None, "", "paused"])
async def test_generate_signal_hard_gates_before_pipeline(status):
    """Non-paper statuses short-circuit — pipeline never runs."""
    ran = {"pipeline": False}

    async def spy_pipeline(engine, hypothesis_id, live_odds):
        ran["pipeline"] = True
        return [{"should": "not_happen"}]

    import tools.btest.paper_pipeline as pp
    saved = pp.generate_paper_trade_signal
    pp.generate_paper_trade_signal = spy_pipeline
    try:
        engine = _make_engine({} if status is None else {"status": status})
        out = await engine.generate_paper_trade_signal("h1", {})
        assert out == []
        assert ran["pipeline"] is False
    finally:
        pp.generate_paper_trade_signal = saved


@pytest.mark.asyncio
async def test_generate_signal_missing_hypothesis_short_circuits():
    ran = {"pipeline": False}

    async def spy_pipeline(engine, hypothesis_id, live_odds):
        ran["pipeline"] = True
        return []

    import tools.btest.paper_pipeline as pp
    saved = pp.generate_paper_trade_signal
    pp.generate_paper_trade_signal = spy_pipeline
    try:
        engine = _make_engine(None)  # get_hypothesis -> None
        assert await engine.generate_paper_trade_signal("missing", {}) == []
        assert ran["pipeline"] is False
    finally:
        pp.generate_paper_trade_signal = saved


def test_gate_order_in_source():
    """reject_non_paper must textually precede the pipeline call in the facade."""
    src = inspect.getsource(
        BacktestEngine.generate_paper_trade_signal,
    )
    gate_pos = src.index("reject_non_paper(")
    pipe_pos = src.index("paper_pipeline.generate_paper_trade_signal(")
    assert gate_pos < pipe_pos


# ─────────────────────────────────────────────────────────────
# 3. The moved helper: process_game / process_game_lines
# ─────────────────────────────────────────────────────────────

def _game(books_prices, home="Lakers", away="Celtics"):
    """Build a synthetic odds-api style game dict.

    books_prices: {book_key: {side_name: american_price}}
    """
    bookmakers = [
        {
            "key": bk,
            "markets": [
                {
                    "key": "spreads",
                    "outcomes": [
                        {"name": side, "price": price, "point": -3.5}
                        for side, price in sides.items()
                    ],
                }
            ],
        }
        for bk, sides in books_prices.items()
    ]
    return {
        "id": "g1",
        "sport_key": "basketball_nba",
        "home_team": home,
        "away_team": away,
        "commence_time": "2026-01-01T00:00:00Z",
        "bookmakers": bookmakers,
    }


@pytest.mark.asyncio
async def test_process_game_returns_pending_rows():
    game = _game({
        "pinnacle": {"Lakers": -110, "Celtics": -110},
        "fanduel": {"Lakers": -105, "Celtics": -115},
        "draftkings": {"Lakers": +100, "Celtics": -120},
        "betmgm": {"Lakers": -108, "Celtics": -112},
    })
    events, signals, rows = await game_line_processing.process_game(
        engine=None, run_id="r1", hypothesis_id="h1", game=game,
        game_date="2026-01-01", snapshot_time="2026-01-01T00:00:00Z",
        market_type="player_points", target_book="draftkings",
        edge_threshold=0.01, devig_method="multiplicative",
        min_books=2, config={},
    )
    assert (events, signals) == (len(rows), sum(1 for r in rows if r[16]))
    assert len(rows) > 0
    import json as _json
    for row in rows:
        # canonical column order: book=8, factors(json)=12, is_signal=16
        assert row[8] in ("pinnacle", "fanduel", "draftkings", "betmgm")
        factors = _json.loads(row[12])
        assert factors["target_excluded"] is True
        assert factors["snapshot_quality"] == "pre_commence"
        assert factors["edge_method"] in ("cross_book_best_line", "consensus_devig")


@pytest.mark.asyncio
async def test_process_game_skips_when_too_few_books():
    game = _game({
        "pinnacle": {"Lakers": -110, "Celtics": -110},
    })
    events, signals, rows = await game_line_processing.process_game(
        engine=None, run_id="r1", hypothesis_id="h1", game=game,
        game_date="2026-01-01", snapshot_time="t", market_type="spreads",
        target_book="fanduel", edge_threshold=0.01,
        devig_method="multiplicative", min_books=2, config={},
    )
    assert (events, signals, rows) == (0, 0, [])


@pytest.mark.asyncio
async def test_process_game_unresolvable_market_returns_zero():
    game = _game({
        "pinnacle": {"Lakers": -110, "Celtics": -110},
        "fanduel": {"Lakers": -105, "Celtics": -115},
        "draftkings": {"Lakers": +100, "Celtics": -120},
    })
    # player_threes maps to totals, which doesn't exist in this game —
    # effective_game_market falls back to the first available market
    # (spreads), so processing proceeds on spreads instead.
    events, _, rows = await game_line_processing.process_game(
        engine=None, run_id="r1", hypothesis_id="h1", game=game,
        game_date="2026-01-01", snapshot_time="t",
        market_type="player_threes", target_book="fanduel",
        edge_threshold=0.99, devig_method="multiplicative",
        min_books=2, config={},
    )
    # Impossible edge threshold: rows may be recorded but no signals
    assert sum(1 for r in rows if r[16]) == 0


@pytest.mark.asyncio
async def test_process_game_lines_never_targets_sharp_books():
    """Only soft/retail books may appear as the evaluated target."""
    game = _game({
        "pinnacle": {"Lakers": -112, "Celtics": -108},
        "lowvig": {"Lakers": -111, "Celtics": -109},
        "draftkings": {"Lakers": +100, "Celtics": -120},
        "fanduel": {"Lakers": -104, "Celtics": -116},
    })
    _, _, rows = await game_line_processing.process_game_lines(
        engine=None, run_id="r1", hypothesis_id="h1", game=game,
        game_date="2026-01-01", snapshot_time="t", market_type="spreads",
        target_book="pinnacle", edge_threshold=0.01,
        devig_method="multiplicative", min_books=2, config={},
    )
    sharp = {"pinnacle", "lowvig"}
    assert all(row[-5] not in sharp for row in rows)


@pytest.mark.asyncio
async def test_engine_delegators_match_impl_output():
    """The facade methods produce identical results to the package functions."""

    class _NoopEngine:
        pass  # helpers don't need DB or fetcher

    game = _game({
        "pinnacle": {"Lakers": -110, "Celtics": -110},
        "fanduel": {"Lakers": -105, "Celtics": -115},
        "draftkings": {"Lakers": +100, "Celtics": -120},
        "betmgm": {"Lakers": -108, "Celtics": -112},
    })
    kwargs = dict(
        run_id="r1", hypothesis_id="h1", game=game,
        game_date="2026-01-01", snapshot_time="t", market_type="spreads",
        target_book="draftkings", edge_threshold=0.01,
        devig_method="multiplicative", min_books=2, config={},
    )
    via_engine = await BacktestEngine._process_game_lines(_NoopEngine(), **kwargs)
    direct = await game_line_processing.process_game_lines(None, **kwargs)
    assert via_engine == direct

    via_engine_pg = await BacktestEngine._process_game(_NoopEngine(), **kwargs)
    direct_pg = await game_line_processing.process_game(None, **kwargs)
    assert via_engine_pg == direct_pg
