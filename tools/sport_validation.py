"""Sport / market allowlists for API input validation.

Centralizes the set of `sport` keys and market identifiers Callisto's
external APIs (odds-api.io, DraftKings, etc.) understand. Path parameters
that flow into upstream HTTP calls or DB queries must be validated against
these allowlists to defeat SSRF / injection / accidental enumeration.

Not a security-critical layer on its own (the main defences are parameterized
SQL + bound URLs) but a cheap way to turn garbage input into a clean 400.
"""

from __future__ import annotations

from typing import Iterable

# Canonical odds-api.io sport keys Callisto actively handles. Keep this list
# explicit — accept only what we actually use, not what the provider claims
# to support.
ALLOWED_SPORTS: frozenset[str] = frozenset({
    "baseball_mlb",
    "basketball_nba",
    "basketball_ncaab",
    "basketball_ncaaw",
    "basketball_wnba",
    "americanfootball_nfl",
    "americanfootball_ncaaf",
    "icehockey_nhl",
    "soccer_epl",
    "soccer_uefa_champs_league",
    "soccer_usa_mls",
    "mma_mixed_martial_arts",
    "boxing_boxing",
    "tennis_atp",
    "tennis_wta",
    "golf_pga",
    "golf_masters",
})

ALLOWED_MARKETS: frozenset[str] = frozenset({
    "h2h", "h2h_lay",
    "spreads", "spreads_lay",
    "totals", "totals_lay",
    "alternate_spreads", "alternate_totals",
    "team_totals", "alternate_team_totals",
    "player_points", "player_rebounds", "player_assists",
    "player_threes", "player_blocks", "player_steals",
    "player_points_rebounds", "player_points_assists",
    "player_rebounds_assists", "player_points_rebounds_assists",
    "player_home_runs", "player_hits", "player_total_bases",
    "player_runs_scored", "player_rbis", "player_strikeouts",
    "player_shots_on_goal", "player_goals", "player_power_play_points",
    "player_pass_yds", "player_rush_yds", "player_receptions",
    "player_receiving_yds", "player_tds",
})


def is_allowed_sport(sport: str | None) -> bool:
    return bool(sport) and sport in ALLOWED_SPORTS


def is_allowed_market(market: str | None) -> bool:
    return bool(market) and market in ALLOWED_MARKETS


def validate_sport(sport: str, *, extra_allowed: Iterable[str] = ()) -> str:
    """Raise ValueError if ``sport`` isn't in the allowlist. Returns it unchanged."""
    allowed = ALLOWED_SPORTS | frozenset(extra_allowed)
    if not sport or sport not in allowed:
        raise ValueError(
            f"Unsupported sport '{sport}'. Expected one of: "
            f"{sorted(allowed)[:8]}... ({len(allowed)} total)"
        )
    return sport


__all__ = [
    "ALLOWED_SPORTS",
    "ALLOWED_MARKETS",
    "is_allowed_sport",
    "is_allowed_market",
    "validate_sport",
]
