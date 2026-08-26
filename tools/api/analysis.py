"""Analysis endpoint handler bodies (moved from api.py)."""

from __future__ import annotations

import os

from fastapi import HTTPException


async def futures_efficiency_endpoint(
    current_odds: int = -200,
    record_wins: int = 30,
    record_losses: int = 20,
    games_played: int = 50,
    season_length: int = 82,
    sport: str = "basketball_nba",
):
    """Analyze if a futures bet is efficiently priced given current trajectory."""
    from tools.market_psychology import futures_efficiency

    return futures_efficiency(
        current_odds=current_odds,
        record_wins=record_wins,
        record_losses=record_losses,
        games_played=games_played,
        season_length=season_length,
        sport=sport,
    )


async def half_market_endpoint(
    full_game_total: float = 220.0,
    half_total: float = 110.0,
    sport: str = "basketball_nba",
    half: str = "first",
):
    """Analyze half/quarter market efficiency vs full-game projections."""
    from tools.market_psychology import half_market_adjustment

    return half_market_adjustment(
        full_game_total=full_game_total,
        half_total=half_total,
        sport=sport,
        half=half,
    )


async def cross_tabulate_endpoint(sport: str, min_sample: int = 20):
    """Multi-factor interaction analysis — discovers which factor combos produce edges."""
    from tools.temporal_analysis import load_game_results, cross_tabulate

    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    df = load_game_results(db_path, sport=sport)
    if df.height == 0:
        raise HTTPException(status_code=503, detail=f"No game results for {sport}")
    return cross_tabulate(df, min_sample=min_sample).to_dicts()
