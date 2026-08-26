"""Backtest event resolution against game results.

Extracted from tools/backtest.py (slice 2). Mixed sync/async helpers used by
BacktestEngine.resolve_with_scores / resolve_from_game_results.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Optional

logger = logging.getLogger("callisto.backtest")


def extract_home_away_teams(
    event_id: str,
    model_factors_json: Optional[str],
) -> tuple[str, str]:
    """Pull home/away team names from event_id ("date|home|away") or factors."""
    home_team = ""
    away_team = ""

    if event_id and "|" in event_id:
        parts = event_id.split("|")
        if len(parts) >= 3:
            home_team = parts[1]
            away_team = parts[2]
    elif model_factors_json:
        try:
            factors = json.loads(model_factors_json)
            home_team = factors.get("home_team", "")
            away_team = factors.get("away_team", "")
        except (json.JSONDecodeError, TypeError):
            pass

    return home_team, away_team


def scores_from_odds_api_game(game: dict) -> Optional[tuple[int, int]]:
    """Extract (home_score, away_score) from a The Odds API scores entry.

    Returns None when the game is incomplete or scores are unusable.
    """
    if not game.get("completed"):
        return None
    scores = game.get("scores", [])
    if not scores or len(scores) < 2:
        return None

    home_score = None
    away_score = None
    for s in scores:
        if s.get("name") == game.get("home_team"):
            home_score = int(s.get("score", 0))
        elif s.get("name") == game.get("away_team"):
            away_score = int(s.get("score", 0))

    if home_score is None or away_score is None:
        return None
    return home_score, away_score


async def build_results_index(
    db,
    min_date: str,
    max_date: str,
) -> tuple[dict, set, int]:
    """Build a (sport, date) -> [(home, away, hscore, ascore)] lookup.

    Primary source: game_results table. Fallback: game_contexts (ESPN
    scores). Only loads the ±3-day window around [min_date, max_date] —
    loading all rows caused ~50 MB allocations that pymalloc never returned.

    Returns (games_by_date, dates_with_games, contexts_added).
    """
    games_by_date = defaultdict(list)
    seen: set = set()
    dates_with_games: set = set()

    result_cursor = await db.execute(
        "SELECT sport, game_date, home_team, away_team, home_score, away_score "
        "FROM game_results WHERE game_date >= date(?, '-3 day') "
        "AND game_date <= date(?, '+3 day')",
        (min_date, max_date),
    )
    result_rows = await result_cursor.fetchall()
    for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in result_rows:
        key = (r_sport, r_date, r_home, r_away)
        seen.add(key)
        games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
        dates_with_games.add(r_date)

    ctx_cursor = await db.execute(
        "SELECT sport, game_date, home_team, away_team, home_score, away_score "
        "FROM game_contexts WHERE home_score IS NOT NULL AND away_score IS NOT NULL "
        "AND game_date >= date(?, '-3 day') AND game_date <= date(?, '+3 day')",
        (min_date, max_date),
    )
    ctx_rows = await ctx_cursor.fetchall()
    ctx_added = 0
    for r_sport, r_date, r_home, r_away, r_hscore, r_ascore in ctx_rows:
        key = (r_sport, r_date, r_home, r_away)
        if key not in seen:
            seen.add(key)
            games_by_date[(r_sport, r_date)].append((r_home, r_away, r_hscore, r_ascore))
            dates_with_games.add(r_date)
            ctx_added += 1

    return dict(games_by_date), dates_with_games, ctx_added


def find_scores_for_event(
    games_by_date: dict,
    sport: str,
    game_date: str,
    home_team: str,
    away_team: str,
    team_matches,
) -> Optional[tuple[int, int]]:
    """Match an unresolved event to a game result.

    Exact-date match only: the old ±1 day fuzzy window occasionally matched
    bets to the wrong adjacent-day game now that local_game_date is
    canonical across tables. Sport-agnostic fallback was removed too — it
    matched MLB bets against NBA games, producing random W/L attribution.
    Tries both home/away orientations (data-source differences).
    """
    for candidate in games_by_date.get((sport, game_date), []):
        r_home, r_away, r_hscore, r_ascore = candidate
        if team_matches(home_team, r_home) and team_matches(away_team, r_away):
            return (r_hscore, r_ascore)
        if team_matches(home_team, r_away) and team_matches(away_team, r_home):
            return (r_ascore, r_hscore)
    return None


__all__ = [
    "extract_home_away_teams",
    "scores_from_odds_api_game",
    "build_results_index",
    "find_scores_for_event",
]
