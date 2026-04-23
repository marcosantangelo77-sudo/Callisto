"""
prop_stat_map — Canonical translation from prop market keys to player_stats.stat_type.

Prop markets in `backtest_events.market` come in several flavours because
different odds providers name them differently:

  * Odds-API / DraftKings:  ``player_points``, ``player_rebounds``,
    ``player_threes``, ``pitcher_strikeouts``, ``batter_total_bases``,
    ``player_shots_on_goal``, ``player_pass_yds`` ...
  * Nash-DK scraper (normalised):  same as above (see
    tools.prop_scraper_free._DK_NASH_PROP_MAP).

The resolver needs to find the corresponding row in ``player_stats``,
which is keyed by (sport, player_name, stat_type, game_date). The canonical
``stat_type`` values written by the collectors today are:

  * NBA / NCAAB / NCAAW: points, rebounds, assists, threes, steals,
                         blocks, turnovers, points_rebounds_assists
  * MLB (statcast only): statcast_strikeouts, statcast_avg_velocity,
                         statcast_pitches, statcast_avg_exit_velocity
  * MLB (ESPN box, rare): passingYards-style camelCase if football path
                          — MLB box is currently NOT written by
                          _store_player_stats (category 'baseball' is
                          unhandled). Resolver tolerates this by also
                          looking at ``statcast_*`` fallbacks.

Unknown markets are returned as ``None`` so the caller can skip them
rather than mis-resolve.

This module is intentionally stat-type literal and side-free; split out
from prop_resolver so tests and the SGP scanner can share the same map.
"""
from __future__ import annotations

from typing import Optional

# ── MARKET → PRIMARY stat_type (the one collectors write today) ──
# Keys use lowercase market strings as they appear in ``backtest_events.market``.
# Values are the canonical ``stat_type`` to look up in ``player_stats``.
_MARKET_TO_STAT: dict[str, str] = {
    # NBA / NCAAB (single-stat)
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_steals": "steals",
    "player_blocks": "blocks",
    "player_turnovers": "turnovers",
    "player_points_rebounds_assists": "points_rebounds_assists",
    # NBA/NCAAB composites (not currently in _store_player_stats — fallback only)
    "player_points_rebounds": "points_rebounds",
    "player_points_assists": "points_assists",
    "player_rebounds_assists": "rebounds_assists",
    # MLB pitcher
    "pitcher_strikeouts": "strikeouts",
    "pitcher_outs": "outs_recorded",
    "pitcher_hits_allowed": "hits_allowed",
    "pitcher_earned_runs": "earned_runs",
    "pitcher_walks": "walks",
    # MLB batter
    "batter_hits": "hits",
    "batter_total_bases": "total_bases",
    "batter_rbis": "rbis",
    "batter_runs": "runs_scored",
    "batter_home_runs": "home_runs",
    "batter_stolen_bases": "stolen_bases",
    "batter_walks": "walks",
    "batter_strikeouts": "strikeouts_batter",
    # NHL skater
    "player_shots_on_goal": "shots_on_goal",
    "skater_shots_on_goal": "shots_on_goal",
    "player_goals": "goals",
    "player_points_nhl": "points_nhl",
    "player_assists_nhl": "assists_nhl",
    # NHL goalie
    "player_saves": "saves",
    "goalie_saves": "saves",
    # NFL
    "player_pass_yds": "passingYards",
    "player_pass_tds": "passingTouchdowns",
    "player_rush_yds": "rushingYards",
    "player_rush_tds": "rushingTouchdowns",
    "player_rec_yds": "receivingYards",
    "player_receptions": "receptions",
    "player_touchdowns": "touchdowns",
    "player_interceptions": "interceptions",
}

# Secondary lookups — if primary stat_type has no row, try these in order.
# Covers the live-DB reality that MLB writes ``statcast_strikeouts`` today,
# not ``strikeouts``.
_FALLBACK_STAT_TYPES: dict[str, tuple[str, ...]] = {
    "strikeouts": ("statcast_strikeouts", "K", "SO"),
    "outs_recorded": ("outs", "IP"),
    "shots_on_goal": ("SOG", "shots"),
    "total_bases": ("totalBases", "TB"),
    "hits": ("H",),
    "runs_scored": ("R", "runs"),
    "home_runs": ("HR",),
    "rbis": ("RBI",),
    "stolen_bases": ("SB",),
    "points_nhl": ("points",),
    "assists_nhl": ("assists",),
    "passingYards": ("passing_yards", "PYDS"),
    "rushingYards": ("rushing_yards", "RYDS"),
    "receivingYards": ("receiving_yards", "RECYDS"),
}


def market_to_stat_type(market: str) -> Optional[str]:
    """Return the primary ``player_stats.stat_type`` for a given market.

    Case-insensitive and whitespace-tolerant. Returns ``None`` for
    non-prop markets (h2h, spreads, totals, ...) or unknown prop keys.
    """
    if not market:
        return None
    key = market.strip().lower()
    if not any(key.startswith(p) for p in
               ("player_", "pitcher_", "batter_", "skater_", "goalie_")):
        return None
    return _MARKET_TO_STAT.get(key)


def fallback_stat_types(primary: str) -> tuple[str, ...]:
    """Return alternative stat_type names to probe if primary missed."""
    return _FALLBACK_STAT_TYPES.get(primary, ())


def is_prop_market(market: str) -> bool:
    """True if a market string represents a player prop."""
    if not market:
        return False
    key = market.strip().lower()
    return any(key.startswith(p) for p in
               ("player_", "pitcher_", "batter_", "skater_", "goalie_"))


__all__ = [
    "market_to_stat_type",
    "fallback_stat_types",
    "is_prop_market",
]
