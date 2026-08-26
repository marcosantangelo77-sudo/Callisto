"""Model route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singleton ``line_monitor`` via a
late ``from api import ...`` inside the function body to avoid a circular
import at module load time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException


def build_weather_dict(
    wind_mph: Optional[float] = None,
    wind_dir: str = "",
    temp_f: Optional[float] = None,
    humidity: Optional[float] = None,
    precipitation: str = "",
) -> Optional[dict]:
    """Assemble the weather dict from query params (None when nothing set)."""
    has_any = any(v is not None for v in [wind_mph, temp_f, humidity]) or bool(precipitation)
    if not has_any:
        return None
    weather_data: dict = {}
    if wind_mph is not None:
        weather_data["wind_speed_mph"] = wind_mph
    if wind_dir:
        weather_data["wind_direction"] = wind_dir
    if temp_f is not None:
        weather_data["temp_f"] = temp_f
    if humidity is not None:
        weather_data["humidity_pct"] = humidity
    if precipitation:
        weather_data["precipitation"] = precipitation
    return weather_data


def split_refs(refs: str) -> Optional[list]:
    """Comma-separated ref names -> list (None when empty)."""
    return [r.strip() for r in refs.split(",") if r.strip()] or None


# Maps odds-api sport keys to the injury model's sport codes.
MODEL_SPORT_MAP = {
    "basketball_nba": "NBA", "basketball_ncaab": "NBA",
    "americanfootball_nfl": "NFL", "americanfootball_ncaaf": "NFL",
    "baseball_mlb": "MLB", "icehockey_nhl": "NHL",
}


async def get_model_total(sport: str, venue: str = "", wind_mph: float = None,
                          wind_dir: str = "", temp_f: float = None,
                          humidity: float = None, refs: str = ""):
    """Pace model total projections + environment adjustments for a sport.

    Returns the pace model's independent fair total for each game in the latest
    odds snapshot, adjusted by environment (venue/weather/refs).  This is an
    independent total model beyond cross-book divergence.
    """
    from api import line_monitor
    from tools.edge_scanner import scan_pace_model_total_edges

    weather_data = build_weather_dict(wind_mph, wind_dir, temp_f, humidity)
    ref_list = split_refs(refs)

    # Get latest snapshot for this sport
    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot:
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot available for {sport}. Trigger a snapshot first.",
        )

    games = snapshot.get("games", [])
    if not games:
        raise HTTPException(status_code=503, detail=f"No games in snapshot for {sport}")

    edges = scan_pace_model_total_edges(
        games=games,
        sport=sport,
        weather_data=weather_data,
        venue_team=venue or None,
        refs=ref_list,
    )

    return {
        "sport": sport,
        "game_count": len(games),
        "model_edges": edges,
        "edge_count": len(edges),
        "venue_queried": venue or None,
        "weather_data": weather_data,
        "refs": ref_list,
    }


async def get_model_environment(venue: str, sport: str = "NFL",
                                wind_mph: float = None, wind_dir: str = "",
                                temp_f: float = None, humidity: float = None,
                                precipitation: str = "", refs: str = ""):
    """Environmental factors for a specific venue/game.

    Returns venue characteristics, weather adjustments, referee tendencies,
    and the combined total adjustment with confidence level.
    """
    from tools.environment import (
        total_environment_adjustment,
        get_venue_factors,
    )

    weather_data = build_weather_dict(wind_mph, wind_dir, temp_f, humidity, precipitation)
    ref_list = split_refs(refs)

    sport_code = sport.upper()
    venue_info = get_venue_factors(venue, sport_code)
    env_result = total_environment_adjustment(
        venue=venue,
        sport=sport_code,
        weather=weather_data,
        refs=ref_list,
    )

    return {
        "venue": venue_info,
        "environment": env_result,
        "weather_input": weather_data,
        "refs_input": ref_list,
    }


async def get_injuries(sport: str):
    """Get current injury report from ESPN with model analysis.

    Returns raw injury data plus, for each injured starter/key player,
    the injury model's quantified impact (spread points, usage redistribution).
    """
    from tools.contextual_data import get_injuries as _get_injuries
    from tools.injury_model import player_impact as _player_impact

    data = await _get_injuries(sport)
    if data.get("error") or not data.get("injuries"):
        return data

    model_sport = MODEL_SPORT_MAP.get(sport, "")

    # Enrich each injury with model analysis (lightweight — no matchup/timing)
    if model_sport:
        for inj in data["injuries"]:
            status = (inj.get("status") or "").lower()
            if status not in ("out", "doubtful"):
                continue
            try:
                result = _player_impact(
                    player_name=inj.get("player", ""),
                    team=inj.get("team", ""),
                    sport=model_sport,
                    position=inj.get("position", ""),
                )
                inj["model_analysis"] = {
                    "tier": result.tier,
                    "spread_impact": result.spread_impact,
                    "total_impact": result.total_impact,
                    "confidence": result.confidence,
                    "notes": result.notes[:3],
                }
            except Exception:
                pass  # silently skip model failures

    return data


async def injury_impact_model(sport: str):
    """Run full injury model analysis for today's games.

    Fetches current injuries and scoreboard, then for each game with
    significant injuries, runs full_injury_analysis (impact quantification,
    usage redistribution, matchup adjustment, market timing).

    Returns per-game injury impact summaries with prop opportunities.
    """
    from tools.contextual_data import get_injuries as _get_injuries, get_scoreboard as _get_sb
    from tools.injury_model import full_injury_analysis as _full_analysis

    model_sport = MODEL_SPORT_MAP.get(sport, "")
    if not model_sport:
        raise HTTPException(status_code=400, detail=f"Sport {sport} not supported by injury model")

    injuries_data = await _get_injuries(sport)
    scoreboard = await _get_sb(sport)
    injuries = injuries_data.get("injuries", [])
    games = scoreboard.get("games", [])

    if not injuries:
        return {"sport": sport, "games": [], "message": "No injuries reported"}

    # Build team-to-game mapping
    team_game_map = {}  # team_name_lower -> game dict
    for g in games:
        for side in ["home_team", "away_team"]:
            tn = g.get(side, "").lower()
            if tn:
                team_game_map[tn] = g

    # Group injuries by team
    team_injuries = {}
    for inj in injuries:
        status = (inj.get("status") or "").lower()
        if status not in ("out", "doubtful"):
            continue
        team = inj.get("team", "")
        team_injuries.setdefault(team, []).append(inj)

    results = []
    for team, injs in team_injuries.items():
        # Find the game for this team
        game = team_game_map.get(team.lower())
        if not game:
            # Try partial match
            for tn, g in team_game_map.items():
                if any(w in tn for w in team.lower().split() if len(w) > 3):
                    game = g
                    break
        if not game:
            continue

        home = game.get("home_team", "")
        away = game.get("away_team", "")
        opponent = away if team.lower() in home.lower() else home
        game_name = game.get("name", f"{away} at {home}")

        game_result = {
            "game": game_name,
            "team": team,
            "opponent": opponent,
            "injuries": [],
        }

        for inj in injs:
            try:
                analysis = _full_analysis(
                    player_name=inj.get("player", ""),
                    team=team,
                    sport=model_sport,
                    opponent=opponent,
                    position=inj.get("position", ""),
                    minutes_since_announced=30.0,
                )
                summary = {
                    "player": analysis["player"],
                    "actionable": analysis.get("actionable", False),
                    "edge_points": analysis.get("edge_points", 0),
                }
                impact = analysis.get("impact")
                if impact:
                    summary["impact"] = {
                        "tier": impact.tier,
                        "spread_impact": impact.spread_impact,
                        "total_impact": impact.total_impact,
                        "confidence": impact.confidence,
                        "notes": impact.notes[:3],
                    }
                matchup = analysis.get("matchup_adjusted")
                if matchup:
                    summary["matchup"] = {
                        "base_impact": matchup.base_impact,
                        "multiplier": matchup.matchup_multiplier,
                        "adjusted_spread_impact": matchup.adjusted_spread_impact,
                        "reasoning": matchup.reasoning[:3],
                    }
                mkt = analysis.get("market_timing")
                if mkt:
                    summary["market_timing"] = {
                        "pct_adjusted": mkt.pct_adjusted,
                        "window_remaining_minutes": mkt.window_remaining_minutes,
                        "edge_remaining": mkt.edge_remaining,
                        "tier": mkt.significance_tier,
                        "notes": mkt.notes[:2],
                    }
                # Usage redistribution — top 5 beneficiaries
                redist = analysis.get("redistribution", [])
                if redist:
                    summary["prop_opportunities"] = [
                        {
                            "player": r.player,
                            "role": r.role,
                            "usage_increase": r.usage_increase,
                            "stat_change": r.projected_stat_change,
                        }
                        for r in redist[:5]
                    ]
                game_result["injuries"].append(summary)
            except Exception as e:
                game_result["injuries"].append({
                    "player": inj.get("player", ""),
                    "error": str(e),
                })

        if game_result["injuries"]:
            results.append(game_result)

    return {
        "sport": sport,
        "model_sport": model_sport,
        "game_count": len(results),
        "games": results,
    }
