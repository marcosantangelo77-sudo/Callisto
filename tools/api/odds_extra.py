"""Odds endpoint handler bodies (psychology, dead numbers, line analysis).

Moved verbatim from api.py; the FastAPI decorators stay in api.py.
"""

from __future__ import annotations

from fastapi import HTTPException


async def market_psychology(sport: str):
    """Run full market psychology analysis — number shading, attention arbitrage.

    Returns signals for all current games in the sport: shaded lines,
    thin-market opportunities, and closing line predictions.
    Uses cached snapshot data (zero extra API credits).
    """
    from tools.market_psychology import full_market_psychology

    from api import line_monitor

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    psych = full_market_psychology(
        games=snapshot["games"],
        sport=sport,
    )
    return psych


async def market_psychology_all():
    """Return cached market psychology signals for all monitored sports.

    This is the lightweight version — reads from the autonomous loop's
    cache rather than recomputing.  Zero cost, instant response.
    """
    from api import autonomous

    if not autonomous:
        raise HTTPException(status_code=503, detail="Autonomous loop not initialized")
    return autonomous.get_psychology_report()


async def dead_numbers_endpoint(sport: str):
    """Show dead number steals and key number analysis for a sport.

    Scans current odds snapshot for spreads sitting on dead numbers
    while other books are on key numbers. Also includes line shopping
    opportunities and buy-points analysis.

    Uses cached snapshot data (zero extra API credits).
    """
    from tools.dead_numbers import (
        find_dead_number_steals,
        rank_line_shopping_opportunities,
        analyze_spread as dn_analyze_spread,
        SPORT_ALIASES,
    )
    from tools.odds_api import find_best_line as _find_best_line

    from api import line_monitor

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    _dn_sport = sport.lower()
    if _dn_sport not in SPORT_ALIASES:
        raise HTTPException(
            status_code=400,
            detail=f"Sport '{sport}' not supported for dead number analysis. Supported: {list(set(SPORT_ALIASES.values()))}",
        )

    games = snapshot.get("games", [])
    all_steals: list = []
    all_shopping: list = []
    spread_analyses: list = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = _find_best_line(game, market="spreads", team=team)
            all_lines = best.get("all_lines", [])
            if not all_lines:
                continue

            # Build lines list for dead number functions
            lines_for_dn = [
                {
                    "bookmaker": l["bookmaker"],
                    "spread": l.get("point", 0),
                    "price": l.get("price", -110),
                }
                for l in all_lines
                if l.get("point") is not None
            ]

            if not lines_for_dn:
                continue

            # Analyze the primary spread
            primary_spread = lines_for_dn[0]["spread"]
            try:
                analysis = dn_analyze_spread(primary_spread, sport)
                analysis["game"] = f"{away} @ {home}"
                analysis["team"] = team
                spread_analyses.append(analysis)
            except (ValueError, KeyError):
                pass

            # Find dead number steals
            if len(lines_for_dn) >= 2:
                try:
                    steals = find_dead_number_steals(lines_for_dn, sport)
                    for s in steals:
                        s["game"] = f"{away} @ {home}"
                        s["team"] = team
                    all_steals.extend(steals)
                except (ValueError, KeyError):
                    pass

                # Rank line shopping opportunities
                try:
                    shopping = rank_line_shopping_opportunities(lines_for_dn, sport)
                    for s in shopping:
                        s["game"] = f"{away} @ {home}"
                        s["team"] = team
                    all_shopping.extend(shopping)
                except (ValueError, KeyError):
                    pass

    all_steals.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    all_shopping.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)

    return {
        "sport": sport,
        "games_scanned": len(games),
        "dead_number_steals": all_steals[:20],
        "line_shopping_opportunities": all_shopping[:20],
        "spread_analyses": spread_analyses[:30],
        "steal_count": len(all_steals),
        "shopping_count": len(all_shopping),
    }


async def line_analysis_endpoint(sport: str):
    """Show RLM, steam moves, public side analysis, and bet timing for a sport.

    Analyzes the current snapshot for reverse line movement (sharp money
    indicator), steam moves (coordinated sharp action), estimated public
    side distribution, and optimal bet timing windows.

    Uses cached snapshot data (zero extra API credits).
    """
    from tools.line_analysis import (
        estimate_public_side as la_estimate_public,
        contrarian_value as la_contrarian,
        optimal_bet_timing as la_timing,
        detect_steam as la_detect_steam,
    )
    from tools.odds_api import find_best_line as _find_best_line

    from api import line_monitor

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    games = snapshot.get("games", [])
    public_analyses = []
    contrarian_picks = []
    timing_info = None

    # Compute bet timing for the sport
    try:
        timing_info = la_timing(sport=sport)
    except Exception:
        pass

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Get spread lines for public side estimation
        for team_side, team_name in [("home", home), ("away", away)]:
            if not team_name:
                continue

            best = _find_best_line(game, market="spreads", team=team_name)
            all_lines = best.get("all_lines", [])
            if not all_lines:
                continue

            # Use best and worst as proxy for open/current
            prices = [l.get("price", -110) for l in all_lines]
            points = [l.get("point", 0) for l in all_lines if l.get("point") is not None]

            if not points:
                continue

            best_point = max(points)
            worst_point = min(points)

            try:
                public_est = la_estimate_public(
                    line_open=worst_point,
                    line_current=best_point,
                    sport=sport,
                    team_a=team_name,
                    team_b=away if team_side == "home" else home,
                )
                public_est["game"] = f"{away} @ {home}"
                public_est["team"] = team_name
                public_analyses.append(public_est)

                # If strong public lean, compute contrarian value
                est_public_pct = max(
                    public_est.get("estimated_public_pct_a", 50),
                    public_est.get("estimated_public_pct_b", 50),
                )
                if est_public_pct >= 60:
                    cv = la_contrarian(
                        estimated_public_pct=est_public_pct,
                        sport=sport,
                        spread=best_point,
                    )
                    cv["game"] = f"{away} @ {home}"
                    cv["team"] = team_name
                    contrarian_picks.append(cv)
            except Exception:
                pass

        # Steam detection from snapshot price data
        # (Note: steam detection works best across multiple snapshots over time;
        # single-snapshot detection is limited but still catches book-to-book divergence)

    # Sort contrarian picks by adjusted ROI
    contrarian_picks.sort(key=lambda x: x.get("adjusted_roi", 0), reverse=True)

    return {
        "sport": sport,
        "games_scanned": len(games),
        "public_side_analyses": public_analyses,
        "contrarian_picks": contrarian_picks[:10],
        "bet_timing": timing_info,
        "analysis_count": len(public_analyses),
        "contrarian_count": len(contrarian_picks),
    }
