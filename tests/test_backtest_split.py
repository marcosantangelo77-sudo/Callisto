"""Characterization pins for slice 2 of the backtest god-module split.

The filter parsers, context-factor registries, context matching, schedule
context builder, bet resolution, and team-name matching now live in
tools/backtest_io.py. tools/backtest.py keeps thin delegating wrappers so
the BacktestEngine API is unchanged.
"""

import inspect

import tools.backtest_io as backtest_io
from tools.backtest import BacktestEngine


# ---------------------------------------------------------------------------
# Canonical definitions live in tools/backtest_io.py
# ---------------------------------------------------------------------------


def test_context_factor_registries_live_in_backtest_io():
    src = inspect.getsource(backtest_io)
    assert "UNFILTERABLE_CONTEXT_FACTORS = {" in src
    assert "FILTERABLE_CONTEXT_FACTORS = {" in src
    assert "_CONTEXT_KEYWORD_MAP = {" in src


def test_parser_functions_live_in_backtest_io():
    for fn in (
        "_parse_hypothesis_filters",
        "has_structured_filters",
        "_infer_context_needs",
        "compute_context_coverage",
        "_game_matches_context_filter",
        "_needs_context_filter",
        "matches_hypothesis_conditions",
        "build_schedule_context",
        "resolve_line",
        "_team_matches",
        "_normalize_team",
    ):
        assert hasattr(backtest_io, fn), f"{fn} missing from tools.backtest_io"


def test_no_large_filter_blocks_remain_in_backtest():
    src = inspect.getsource(__import__("tools.backtest", fromlist=["x"]))
    # The keyword map / unfilterable registry bodies must not be re-defined here.
    assert "UNFILTERABLE_CONTEXT_FACTORS = {" not in src
    assert "_CONTEXT_KEYWORD_MAP = {" not in src
    assert 'alias_map[alias] = canonical' not in src


# ---------------------------------------------------------------------------
# Delegators keep the historical BacktestEngine API working
# ---------------------------------------------------------------------------


def test_engine_delegates_team_matching():
    assert BacktestEngine._team_matches("LA Lakers", "Los Angeles Lakers")
    assert not BacktestEngine._team_matches("Boston Celtics", "LA Lakers")


def test_engine_delegates_filter_parsing():
    filters = BacktestEngine._parse_hypothesis_filters(
        "home underdogs of 3-7 points cover",
        {"market_type": "spreads"},
        "mlb_home_underdog_ats",
    )
    assert filters["home_away_filter"] == "home"
    assert filters["dog_fav_filter"] == "underdog"
    assert filters["spread_range"] == (3.0, 7.0)


def test_engine_class_attrs_alias_io_tables():
    assert BacktestEngine.UNFILTERABLE_CONTEXT_FACTORS is (
        backtest_io.UNFILTERABLE_CONTEXT_FACTORS
    )
    assert BacktestEngine.FILTERABLE_CONTEXT_FACTORS is (
        backtest_io.FILTERABLE_CONTEXT_FACTORS
    )


def test_engine_delegates_line_resolution():
    engine = BacktestEngine.__new__(BacktestEngine)
    assert engine._resolve_line(
        "spreads", "Home Team", -3.5, 110, 105, "Home Team", "Away Team"
    ) == "won"
    assert engine._resolve_line("totals", "Under", 210.5, 100, 105, "H", "A") == "won"


def test_engine_delegates_context_checks():
    assert BacktestEngine.has_structured_filters({"line_filters": {"side": "Over"}})
    assert not BacktestEngine.has_structured_filters({})
    assert BacktestEngine.compute_context_coverage(
        {"context_factors": ["days_rest", "weather"]}
    ) == 0.5
    assert BacktestEngine._infer_context_needs("dome games are cold", "") == [
        "venue_type"
    ]
    assert not BacktestEngine._needs_context_filter("plain totals over", "", {})
    assert not BacktestEngine._game_matches_context_filter(
        {}, "x", "y", {"game_filters": {"require_b2b": True}}
    )
