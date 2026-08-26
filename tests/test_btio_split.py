"""Characterization pins for slice 3 of the backtest_io split.

tools/backtest_io.py is now a facade: the context-factor registries remain
canonically defined there (characterization-pinned by tests/test_backtest_split.py),
while the heavy logic lives in the tools.btio package:

  - tools.btio.filters        — hypothesis filter parsing, coverage scoring
  - tools.btio.schedule       — DB-backed schedule-context computation
  - tools.btio.context_match  — game-vs-context matching (fail closed)
  - tools.btio.resolution     — bet resolution
  - tools.btio.teams          — team-name normalization/matching

The facade must re-export the full implementation surface so existing
callers (tools/backtest.py, tools/autonomous.py) are unaffected.
"""

import inspect

import tools.backtest_io as backtest_io
import tools.btio as btio
from tools.btio import context_match, filters, resolution, schedule, teams


# ---------------------------------------------------------------------------
# Facade re-exports every implementation name
# ---------------------------------------------------------------------------

RE_EXPORTS = [
    # registries
    "UNFILTERABLE_CONTEXT_FACTORS",
    "FILTERABLE_CONTEXT_FACTORS",
    "_CONTEXT_KEYWORD_MAP",
    # filters
    "has_structured_filters",
    "_infer_context_needs",
    "_parse_hypothesis_filters",
    "matches_hypothesis_conditions",
    "_log_unfilterable_context_factors",
    "compute_context_coverage",
    # schedule
    "build_schedule_context",
    # context match
    "_game_matches_context_filter",
    "_needs_context_filter",
    # resolution
    "resolve_line",
    # teams
    "_TEAM_ALIASES",
    "_build_alias_map",
    "_normalize_team",
    "_team_matches",
]


def test_facade_reexports_every_btio_name():
    for name in RE_EXPORTS:
        assert hasattr(backtest_io, name), f"{name} missing from tools.backtest_io"
        assert getattr(backtest_io, name) is getattr(btio, name), (
            f"backtest_io.{name} is not the same object as btio.{name}"
        )


def test_btio_package_exports_match_backtest_io():
    """Every public/private name the engine delegates to resolves via tools.btio."""
    for name in RE_EXPORTS:
        assert getattr(btio, name) is getattr(backtest_io, name), (
            f"btio.{name} does not resolve to the facade object"
        )


def test_no_logic_left_in_facade():
    src = inspect.getsource(backtest_io)
    # The facade keeps only the registry tables; all function bodies moved out.
    for body in (
        "thesis_words = re.split",
        "range_match = re.search",
        "bearish_name = False",
        "buffer_start = dt.strptime",
        "_any_filter_matched = False",
        "alias_map[alias] = canonical",
        'if side.lower() == "over":',
    ):
        assert body not in src, f"logic left in facade: {body}"


def test_registry_tables_are_canonical_in_facade():
    src = inspect.getsource(backtest_io)
    assert "UNFILTERABLE_CONTEXT_FACTORS = {" in src
    assert "FILTERABLE_CONTEXT_FACTORS = {" in src
    assert "_CONTEXT_KEYWORD_MAP = {" in src


# ---------------------------------------------------------------------------
# Line-filter parsing (tools.btio.filters)
# ---------------------------------------------------------------------------


def test_parse_structured_line_filters_win():
    cfg = {
        "line_filters": {
            "home_away": "away",
            "dog_fav": "favorite",
            "side": "Over",
            "spread_range": [7.5, 3.5],  # reversed on purpose — must be normalized
        }
    }
    f = filters._parse_hypothesis_filters("ignored thesis", cfg, "h1")
    assert f["home_away_filter"] == "away"
    assert f["dog_fav_filter"] == "favorite"
    assert f["side_filter"] == "Over"
    assert f["spread_range"] == (3.5, 7.5)


def test_parse_side_from_totals_thesis():
    f = filters._parse_hypothesis_filters(
        "unders hit when defenses rest", {"market_type": "totals"}, ""
    )
    assert f["side_filter"] == "Under"


def test_parse_side_not_confused_by_underdog():
    f = filters._parse_hypothesis_filters(
        "underdogs cover at home", {"market_type": "spreads"}, ""
    )
    assert "side_filter" not in f
    assert f["dog_fav_filter"] == "underdog"
    # "at home" alone is not a home-side filter — needs "home underdog/team/etc."
    assert "home_away_filter" not in f


def test_parse_home_away_from_thesis_and_name():
    f = filters._parse_hypothesis_filters(
        "home underdogs of 3-7 points cover", {"market_type": "spreads"}, ""
    )
    assert f["home_away_filter"] == "home"
    f2 = filters._parse_hypothesis_filters("", {}, "mlb_road_favorite_ml")
    assert f2["home_away_filter"] == "away"


def test_bearish_name_flips_dog_fav():
    bullish = filters._parse_hypothesis_filters(
        "favorites overpriced after travel", {}, "nba_favorite_overpriced_ml"
    )
    assert bullish["dog_fav_filter"] == "underdog"
    assert bullish["_bearish_flip"] is True


def test_spread_min_from_thesis():
    f = filters._parse_hypothesis_filters("favorites by 8+ points", {}, "")
    assert f["spread_min"] == 8.0


def test_matches_hypothesis_conditions_spread_range():
    flt = {"spread_range": (3.0, 7.0)}
    assert filters.matches_hypothesis_conditions(
        "Home", "spreads", 5.0, "Home", "Away", flt
    )
    assert not filters.matches_hypothesis_conditions(
        "Home", "spreads", 10.5, "Home", "Away", flt
    )
    assert not filters.matches_hypothesis_conditions(
        "Home", "spreads", -1.5, "Home", "Away", flt
    )


def test_matches_hypothesis_conditions_home_away():
    flt = {"home_away_filter": "away"}
    assert filters.matches_hypothesis_conditions(
        "LA Lakers", "h2h", None, "Boston Celtics", "Los Angeles Lakers", flt
    )
    assert not filters.matches_hypothesis_conditions(
        "Boston Celtics", "h2h", None, "Boston Celtics", "LA Lakers", flt
    )


def test_matches_hypothesis_conditions_dog_fav_via_fair_prob():
    dog = {"dog_fav_filter": "underdog"}
    fav = {"dog_fav_filter": "favorite"}
    assert filters.matches_hypothesis_conditions(
        "X", "h2h", None, "H", "A", dog, fair_prob=0.35
    )
    assert not filters.matches_hypothesis_conditions(
        "X", "h2h", None, "H", "A", dog, fair_prob=0.70
    )
    assert filters.matches_hypothesis_conditions(
        "X", "h2h", None, "H", "A", fav, fair_prob=0.70
    )


def test_context_coverage_whitelist_semantics():
    # Unknown factors do NOT count as filterable (whitelist, not blacklist).
    assert filters.compute_context_coverage({}) == 1.0
    assert filters.compute_context_coverage(
        {"context_factors": ["days_rest", "back_to_back", "weather"]}
    ) == pytest_approx(2 / 3)
    assert filters.compute_context_coverage(
        {"context_factors": ["season_week", "park_type"]}
    ) == 0.0


def pytest_approx(x):
    from pytest import approx

    return approx(x)


def test_log_unfilterable_returns_only_registered():
    out = filters._log_unfilterable_context_factors(
        "h1", {"context_factors": ["Weather", "days_rest"]}
    )
    assert out == ["Weather"]
    assert filters._log_unfilterable_context_factors("h2", {}) == []


# ---------------------------------------------------------------------------
# Game-vs-context matching (tools.btio.context_match) — fail closed
# ---------------------------------------------------------------------------


def _ctx(**over):
    base = {
        "home_days_rest": 3,
        "away_days_rest": 3,
        "home_b2b": False,
        "away_b2b": False,
        "home_road_streak": 0,
        "away_road_streak": 0,
        "home_games_in_4": 1,
        "away_games_in_4": 1,
        "is_revenge": False,
        "home_sandwich": False,
        "away_sandwich": False,
        "home_win_pct": 0.5,
        "away_win_pct": 0.5,
        "home_prev_margin": 0,
        "away_prev_margin": 0,
    }
    base.update(over)
    return base


def test_no_context_data_fails_closed():
    assert not context_match._game_matches_context_filter(
        {}, "anything", "", {}
    )


def test_structured_game_filters_require_revenge():
    gf = {"require_revenge": True}
    assert context_match._game_matches_context_filter(
        _ctx(is_revenge=True), "h", "", {"game_filters": gf}
    )
    assert not context_match._game_matches_context_filter(
        _ctx(is_revenge=False), "h", "", {"game_filters": gf}
    )


def test_structured_game_filters_conjunctive_same_team():
    gf = {"require_b2b": True, "max_rest_days": 1}
    # home satisfies both → pass
    assert context_match._game_matches_context_filter(
        _ctx(home_b2b=True, home_days_rest=1), "h", "", {"game_filters": dict(gf)}
    )
    # split across teams (home b2b but rested, away short rest no b2b) → fail
    assert not context_match._game_matches_context_filter(
        _ctx(home_b2b=True, home_days_rest=3, away_days_rest=1),
        "h", "", {"game_filters": dict(gf)},
    )


def test_structured_game_filters_min_rest_mismatch():
    gf = {"min_rest_mismatch": 2}
    assert context_match._game_matches_context_filter(
        _ctx(home_days_rest=4, away_days_rest=1), "h", "", {"game_filters": gf}
    )
    assert not context_match._game_matches_context_filter(
        _ctx(home_days_rest=3, away_days_rest=2), "h", "", {"game_filters": gf}
    )


def test_regex_fallback_requires_context_factors():
    # b2b keyword without context_factors → fail closed (identical-events guard)
    assert not context_match._game_matches_context_filter(
        _ctx(home_b2b=True), "nba_b2b_second_night", "", {}
    )


def test_regex_fallback_b2b_with_context_factor():
    assert context_match._game_matches_context_filter(
        _ctx(away_b2b=True), "h", "",
        {"context_factors": ["back_to_back"]},
    )
    assert not context_match._game_matches_context_filter(
        _ctx(), "h", "", {"context_factors": ["back_to_back"]}
    )


def test_regex_fallback_playoff_clinch_thresholds():
    cf = {"context_factors": ["playoff_standing"]}
    assert context_match._game_matches_context_filter(
        _ctx(home_win_pct=0.70, away_win_pct=0.40), "clinch_game", "", cf
    )
    # Neither team near clinching → fail
    assert not context_match._game_matches_context_filter(
        _ctx(home_win_pct=0.50, away_win_pct=0.45), "clinch_game", "", cf
    )


def test_needs_context_filter_detection():
    assert context_match._needs_context_filter(
        "x", "", {"game_filters": {"require_b2b": True}}
    )
    assert context_match._needs_context_filter(
        "x", "", {"context_factors": ["days_rest"]}
    )
    assert context_match._needs_context_filter("nba_short_rest_spot", "", {})
    assert context_match._needs_context_filter("", "teams on a road trip", {})
    assert not context_match._needs_context_filter("plain totals over", "", {})


# ---------------------------------------------------------------------------
# Schedule-context builder (tools.btio.schedule)
# ---------------------------------------------------------------------------


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    async def execute_fetchall(self, query, params):
        return self.rows


async def _collect_schedule(rows, **kwargs):
    return await schedule.build_schedule_context(_FakeDB(rows), "nba", **kwargs)


def test_build_schedule_context_rest_and_revenge():
    rows = [
        ("2026-01-01", "Lakers", "Celtics", 110, 100, 210, "home", "Lakers"),
        ("2026-01-02", "Lakers", "Knicks", 105, 108, 213, "away", "Knicks"),
        ("2026-01-05", "Celtics", "Lakers", 98, 101, 199, "away", "Lakers"),
    ]
    ctx = _run(_collect_schedule(rows, start_date="2026-01-02", end_date="2026-01-05"))
    assert set(ctx) == {
        ("2026-01-02", "Lakers", "Knicks"),
        ("2026-01-05", "Celtics", "Lakers"),
    }
    knicks = ctx[("2026-01-02", "Lakers", "Knicks")]
    assert knicks["home_days_rest"] == 1  # played 2026-01-01
    assert knicks["home_b2b"] is True
    lal = ctx[("2026-01-05", "Celtics", "Lakers")]
    assert lal["away_days_rest"] == 3  # last game 2026-01-02
    assert lal["is_revenge"] is True   # met within 30-day buffer
    assert 0.0 <= lal["away_win_pct"] <= 1.0


def test_build_schedule_context_empty_db():
    assert _run(_collect_schedule([], start_date="2026-01-01", end_date="2026-01-07")) == {}


def test_build_schedule_context_live_game_augmentation():
    rows = [
        ("2026-01-01", "Lakers", "Celtics", 110, 100, 210, "home", "Lakers"),
    ]
    live = [("2026-01-03", "Heat", "Bulls")]
    ctx = _run(schedule.build_schedule_context(
        _FakeDB(rows), "nba", "2026-01-01", "2026-01-05", live_games=live
    ))
    heat = ctx[("2026-01-03", "Heat", "Bulls")]
    assert heat["home_days_rest"] == 99  # no prior games
    assert heat["away_days_rest"] == 99
    assert heat["home_win_pct"] == 0.5


def _run(coro):
    import asyncio

    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Bet resolution (tools.btio.resolution)
# ---------------------------------------------------------------------------


def test_resolve_line_h2h_home_and_away():
    H, A = "Boston Celtics", "LA Lakers"
    assert resolution.resolve_line("h2h", H, None, 110, 105, H, A) == "won"
    assert resolution.resolve_line("h2h", A, None, 110, 105, H, A) == "lost"
    assert resolution.resolve_line("h2h", H, None, 100, 100, H, A) == "push"


def test_resolve_line_h2h_fuzzy_names():
    assert resolution.resolve_line(
        "h2h", "LA Lakers", None, 112, 100, "Los Angeles Lakers", "Boston Celtics"
    ) == "won"


def test_resolve_line_spreads():
    H, A = "Boston Celtics", "Denver Nuggets"
    # Home favored -3.5, wins by 5 → cover
    assert resolution.resolve_line("spreads", H, -3.5, 110, 105, H, A) == "won"
    # Home favored -3.5, wins by 2 → lose
    assert resolution.resolve_line("spreads", H, -3.5, 102, 100, H, A) == "lost"
    # Away +7, loses by 7 exactly → push
    assert resolution.resolve_line("spreads", A, 7.0, 107, 100, H, A) == "push"


def test_resolve_line_totals():
    assert resolution.resolve_line("totals", "Over", 210.5, 108, 105, "Celtics", "Nuggets") == "won"
    assert resolution.resolve_line("totals", "Over", 215.0, 108, 105, "Celtics", "Nuggets") == "lost"
    assert resolution.resolve_line("totals", "Under", 213.0, 108, 105, "Celtics", "Nuggets") == "push"


def test_resolve_line_unknown_market_returns_none():
    assert resolution.resolve_line("props", "Over", 1.5, 1, 0, "H", "A") is None


# ---------------------------------------------------------------------------
# Team-name matching (tools.btio.teams)
# ---------------------------------------------------------------------------


def test_normalize_alias_map_hits():
    assert teams._normalize_team("LA Dodgers") == "los angeles dodgers"
    assert teams._normalize_team("D-Backs") == "arizona diamondbacks"
    assert teams._normalize_team("Niners") == "san francisco 49ers"
    assert teams._normalize_team("T-Wolves") == "minnesota timberwolves"
    assert teams._normalize_team("") == ""


def test_normalize_city_abbreviation_fallback():
    # Unknown team with a known city gets abbreviated
    n = teams._normalize_team("New York Rovers")
    assert n.startswith("ny ")


def test_team_matches_exact_alias_and_mascot():
    assert teams._team_matches("Lakers", "Los Angeles Lakers")
    assert teams._team_matches("Boston Celtics", "BOS Celtics")
    assert not teams._team_matches("Lakers", "Celtics")


def test_team_matches_substring_athletics():
    assert teams._team_matches("Athletics", "Oakland Athletics")
    assert not teams._team_matches("", "Lakers")


def test_alias_map_is_cached_singleton():
    first = teams._TEAM_ALIASES
    teams._normalize_team("Lakers")  # ensure built
    assert teams._TEAM_ALIASES is first
    assert len(teams._TEAM_ALIASES) > 100


# ---------------------------------------------------------------------------
# Engine delegation still routes through the facade
# ---------------------------------------------------------------------------


def test_engine_delegation_paths_intact():
    from tools.backtest import BacktestEngine

    assert BacktestEngine._CONTEXT_KEYWORD_MAP is backtest_io._CONTEXT_KEYWORD_MAP
    assert BacktestEngine._TEAM_ALIASES is backtest_io._TEAM_ALIASES
    engine = BacktestEngine.__new__(BacktestEngine)
    assert engine._build_alias_map() == btio._build_alias_map()
    assert engine._normalize_team("LA Lakers") == btio._normalize_team("LA Lakers")
