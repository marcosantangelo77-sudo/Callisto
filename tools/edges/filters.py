"""Vig / pace-model / dead-number / simulation scan helpers (split from edge_scanner)."""

from __future__ import annotations

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, find_best_line
from tools.dead_numbers import find_dead_number_steals, SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES

from tools.edges.common import (
    logger,
    _PACE_SPORT_MAP,
    _DEAD_NUM_SPORT_ALIASES,
    _filter_in_progress_games,
)
from tools.edges.scanning import (
    scan_cross_book_edges,
    scan_alt_line_edges,
)

def scan_vig_edges(games: list[dict], market: str = "spreads") -> list[dict]:
    """
    Find books offering unusually low vig (juice) on specific games.

    Standard vig: both sides at -110 = 4.55% total vig.
    Low vig: -105/-105 = 2.44% total vig.
    Reduced vig = the book is either promoting or mispricing.

    Books with lower vig give you better prices structurally —
    over thousands of bets, reduced vig is the simplest edge.
    """
    games = _filter_in_progress_games(games)
    vig_edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != market:
                    continue

                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Use math_utils for overround and hold calculations
                try:
                    from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
                    decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                    vig = calculate_overround(decimal_odds)
                    hold = calculate_hold(decimal_odds)
                except ImportError:
                    # Fallback: inline calculation
                    total_implied = sum(
                        calculate_implied_probability(o.get("price", -110))
                        for o in outcomes
                    )
                    vig = total_implied - 1.0
                    hold = vig / (1.0 + vig) if (1.0 + vig) > 0 else 0

                # Standard vig on spreads is ~4.5% (-110/-110)
                # Anything under 3% is notable, under 2% is exceptional
                if vig < 0.035:
                    vig_edges.append({
                        "game": f"{away} @ {home}",
                        "game_id": game.get("id", ""),
                        "bookmaker": bm["title"],
                        "market": market,
                        "vig_pct": round(vig * 100, 2),
                        "hold_pct": round(hold * 100, 2),
                        "total_implied": round(1.0 + vig, 4),
                        "outcomes": [
                            {
                                "name": o.get("name", ""),
                                "price": o.get("price", 0),
                                "point": o.get("point"),
                                "implied": round(calculate_implied_probability(o.get("price", -110)), 4),
                            }
                            for o in outcomes
                        ],
                        "edge_type": "LOW_VIG",
                        "note": (
                            f"Vig at {round(vig * 100, 1)}% (hold {round(hold * 100, 1)}%) vs standard ~4.5%. "
                            f"{'Exceptional value' if vig < 0.02 else 'Notable reduction'}."
                        ),
                    })

    vig_edges.sort(key=lambda x: x["vig_pct"])
    return vig_edges

def scan_pace_model_total_edges(
    games: list[dict],
    sport: str,
    weather_data: Optional[dict] = None,
    venue_team: Optional[str] = None,
    refs: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scan games for total (over/under) edges using the pace model + environment.

    This provides an INDEPENDENT total estimate beyond cross-book divergence.
    The pace model projects totals from first principles (pace x efficiency),
    then the environment module adjusts for weather/venue/refs.

    The result supplements — does not replace — cross-book edge detection.

    Args:
        games: List of game dicts from odds snapshot.
        sport: Odds API sport key (e.g. 'basketball_nba').
        weather_data: Optional weather dict for outdoor games.
        venue_team: Home team abbreviation for venue lookup.
        refs: Optional referee names for the game.

    Returns:
        List of model-based total edge dicts.
    """
    games = _filter_in_progress_games(games)
    pace_sport = _PACE_SPORT_MAP.get(sport.lower())
    if not pace_sport:
        return []

    try:
        from tools.pace_model import (
            project_game_total,
            detect_total_edge,
            poisson_total_distribution,
            LEAGUE_DEFAULTS,
            Sport,
        )
        from tools.environment import total_environment_adjustment
    except ImportError as e:
        logger.debug(f"Pace/environment import failed: {e}")
        return []

    sport_enum = Sport(pace_sport)
    defaults = LEAGUE_DEFAULTS.get(sport_enum, {})
    if not defaults:
        return []

    # Get league average values for this sport
    if sport_enum == Sport.NBA:
        league_avg_pace = defaults["pace"]
        league_avg_eff = defaults["off_eff"]
    elif sport_enum == Sport.NFL:
        league_avg_pace = defaults["plays_per_game"]
        league_avg_eff = defaults["yards_per_play"]
    elif sport_enum == Sport.MLB:
        league_avg_pace = defaults["runs_per_game"]  # PA proxy
        league_avg_eff = defaults["runs_per_game"]
    elif sport_enum == Sport.NHL:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["goals_per_game"]
    elif sport_enum == Sport.SOCCER:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["xg_per_game"]
    else:
        return []

    # Compute environment adjustment (venue + weather + refs)
    env_adj = 0.0
    env_detail = None
    env_sport_code = pace_sport.upper()
    if venue_team:
        try:
            env_result = total_environment_adjustment(
                venue=venue_team,
                sport=env_sport_code,
                weather=weather_data,
                refs=refs,
            )
            env_adj = env_result.get("total_adj", 0.0)
            env_detail = env_result
        except Exception as e:
            logger.debug(f"Environment adjustment failed: {e}")

    edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        if not home or not away:
            continue

        # Extract book total line from the game data
        book_total = None
        book_over_odds = None
        book_under_odds = None

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "totals":
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    if point is None:
                        continue
                    if o.get("name", "").lower() == "over":
                        book_total = point
                        book_over_odds = o.get("price", -110)
                    elif o.get("name", "").lower() == "under":
                        book_under_odds = o.get("price", -110)
                if book_total is not None:
                    break
            if book_total is not None:
                break

        if book_total is None or book_over_odds is None or book_under_odds is None:
            continue

        # Project total using pace model with league average inputs
        # (We use league averages as a baseline; specific team data would improve this
        # when available from team stats tools)
        try:
            projection = project_game_total(
                home_pace=league_avg_pace,
                away_pace=league_avg_pace,
                home_off_eff=league_avg_eff,
                away_off_eff=league_avg_eff,
                home_def_eff=league_avg_eff,
                away_def_eff=league_avg_eff,
                league_avg_pace=league_avg_pace,
                sport=pace_sport,
                league_avg_eff=league_avg_eff,
            )
        except Exception as e:
            logger.debug(f"Pace model projection failed for {away} @ {home}: {e}")
            continue

        # Apply environment adjustment
        model_total = projection.projected_total + env_adj

        # Detect edge: model total vs book total
        try:
            edge = detect_total_edge(
                projected_total=model_total,
                book_total=book_total,
                book_over_odds=book_over_odds,
                book_under_odds=book_under_odds,
                sport=pace_sport,
                home_expected=projection.home_projected + (env_adj / 2.0),
                away_expected=projection.away_projected + (env_adj / 2.0),
            )
        except Exception as e:
            logger.debug(f"Total edge detection failed for {away} @ {home}: {e}")
            continue

        # Only report edges above 1% (model-based edges are noisier)
        if abs(edge.edge_pct) < 1.0:
            continue

        edge_dict = {
            "game": f"{away} @ {home}",
            "game_id": game.get("id", ""),
            "edge_type": "PACE_MODEL_TOTAL",
            "direction": edge.edge_direction,
            "edge_pct": edge.edge_pct,
            "model_total": round(model_total, 1),
            "book_total": book_total,
            "delta": round(model_total - book_total, 1),
            "over_probability": edge.over_probability,
            "under_probability": edge.under_probability,
            "kelly_fraction": edge.kelly_fraction,
            "ev": edge.ev,
            "pace_factor": projection.pace_factor,
            "methodology": projection.methodology,
            "environment_adj": round(env_adj, 2),
            "environment_detail": env_detail,
        }
        edges.append(edge_dict)

    # Sort by edge magnitude
    edges.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    if edges:
        logger.info(
            f"Pace model total edges ({sport}): {len(edges)} found, "
            f"best edge: {edges[0]['edge_pct']:.1f}% {edges[0]['direction']}"
        )
    return edges

def _simulation_validate_edges(games: list[dict], sport: str, report: dict) -> list[dict]:
    """
    Use Monte Carlo simulation to independently validate cross-book edges.

    For spread edges: run simulate_spread() and compare sim-implied prob vs book.
    For totals in low-scoring sports: run compare_poisson_to_market().
    Only validates edges that passed the cross-book divergence filter.
    """
    games = _filter_in_progress_games(games)
    try:
        from tools.simulation import simulate_spread, simulate_poisson, compare_poisson_to_market, _classify_sport
    except ImportError:
        logger.debug("Simulation module not available for edge validation")
        return []

    validated = []
    classification = _classify_sport(sport) if sport else "high_scoring"

    # Only validate the top cross-book edges to avoid burning CPU
    spread_edges = report.get("cross_book_spreads", [])[:5]
    total_edges = report.get("cross_book_totals", [])[:5]

    # Build game lookup for simulation
    game_by_id = {g.get("id", ""): g for g in games}

    # Validate spread edges with simulate_spread
    for edge_info in spread_edges:
        game_id = edge_info.get("game_id", "")
        game = game_by_id.get(game_id)
        if not game:
            continue
        try:
            sim_result = simulate_spread(game, sport=sport, n_sims=5000)
            fair_spread = sim_result.get("fair_spread", 0)
            sim_edges = sim_result.get("edges", [])
            if sim_edges:
                best_sim_edge = sim_edges[0]
                if abs(best_sim_edge.get("edge", 0)) >= 0.02:
                    hold_info = _compute_market_hold(game, "spreads")
                    validated.append({
                        "source": "simulation",
                        "type": "spread",
                        "game": edge_info.get("game", ""),
                        "team": edge_info.get("team", ""),
                        "fair_spread": fair_spread,
                        "sim_edge": best_sim_edge.get("edge", 0),
                        "sim_edge_pct": best_sim_edge.get("edge_pct", 0),
                        "sim_prob": best_sim_edge.get("simulated_prob", 0),
                        "book_prob": best_sim_edge.get("book_prob", 0),
                        "rating": best_sim_edge.get("rating", "NO_EDGE"),
                        "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                        **hold_info,
                    })
        except Exception as e:
            logger.debug(f"Sim validation failed for spread edge {game_id}: {e}")

    # Validate total edges for low-scoring sports with Poisson model
    if classification == "low_scoring":
        for edge_info in total_edges:
            game_id = edge_info.get("game_id", "")
            game = game_by_id.get(game_id)
            if not game:
                continue
            try:
                import numpy as np
                totals_found = []
                for bm in game.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == "totals":
                            for o in mkt.get("outcomes", []):
                                if o.get("point") is not None:
                                    totals_found.append(o["point"])
                if not totals_found:
                    continue
                consensus_total = float(np.median(totals_found))
                home_exp = consensus_total * 0.52
                away_exp = consensus_total * 0.48

                poisson_result = simulate_poisson(home_exp, away_exp)
                poisson_edges = compare_poisson_to_market(
                    poisson_result, game,
                    game.get("home_team", "Home"),
                    game.get("away_team", "Away"),
                )
                for pe in poisson_edges[:2]:
                    if abs(pe.get("edge", 0)) >= 0.02:
                        hold_info = _compute_market_hold(game, "totals")
                        validated.append({
                            "source": "poisson_simulation",
                            "type": "total",
                            "game": edge_info.get("game", ""),
                            "team": pe.get("team", ""),
                            "market": pe.get("market", "totals"),
                            "model_prob": pe.get("model_probability", 0),
                            "market_implied": pe.get("market_implied", 0),
                            "edge": pe.get("edge", 0),
                            "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                            **hold_info,
                        })
            except Exception as e:
                logger.debug(f"Poisson validation failed for total edge {game_id}: {e}")

    return validated


def _compute_market_hold(game: dict, market_key: str) -> dict:
    """Compute overround and hold for a specific market using math_utils."""
    try:
        from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] == market_key:
                    outcomes = mkt.get("outcomes", [])
                    if len(outcomes) >= 2:
                        decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                        return {
                            "overround": round(calculate_overround(decimal_odds), 4),
                            "hold": round(calculate_hold(decimal_odds), 4),
                        }
    except Exception:
        pass
    return {"overround": None, "hold": None}


def _scan_dead_number_steals(games: list[dict], sport: str) -> list[dict]:
    """Scan all games for dead number steal opportunities across books.

    For each game, collects spread lines from all books and runs
    find_dead_number_steals() to find books sitting on dead numbers
    while others are on key numbers.
    """
    games = _filter_in_progress_games(games)
    all_steals = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = find_best_line(game, market="spreads", team=team)
            all_lines = best.get("all_lines", [])
            if len(all_lines) < 2:
                continue

            # Build the lines list for find_dead_number_steals
            lines_for_dn = [
                {
                    "bookmaker": l["bookmaker"],
                    "spread": l.get("point", 0),
                    "price": l.get("price", -110),
                }
                for l in all_lines
                if l.get("point") is not None
            ]

            if len(lines_for_dn) < 2:
                continue

            try:
                steals = find_dead_number_steals(lines_for_dn, sport)
                for s in steals:
                    s["game"] = f"{away} @ {home}"
                    s["team"] = team
                all_steals.extend(steals)
            except (ValueError, KeyError):
                continue

    all_steals.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    return all_steals

def full_edge_scan(snapshot: dict) -> dict:
    """
    Run all edge scanners on a snapshot and return a unified report.

    This is the main entry point — call after each odds snapshot.
    """
    games = snapshot.get("games", [])
    if not games:
        return {"error": "No games in snapshot", "edges": []}

    # Filter out in-progress games — their odds are live lines, not pre-game.
    # Mixing live and pre-game odds produces phantom edges (see BAL@PIT 2026-04-04).
    pre_game = _filter_in_progress_games(games)
    in_progress_count = len(games) - len(pre_game)
    games = pre_game

    if in_progress_count:
        logger.info(
            f"Filtered {in_progress_count} in-progress game(s) from edge scan "
            f"(live odds contaminate sharp consensus)"
        )

    if not games:
        return {"error": "All games in progress — no pre-game edges", "edges": [],
                "filtered_in_progress": in_progress_count}

    report = {
        "game_count": len(games),
        "filtered_in_progress": in_progress_count,
        "sport": snapshot.get("sport", "unknown"),
    }

    # Cross-book divergence
    sport = snapshot.get("sport", "")
    for market in ["spreads", "h2h", "totals"]:
        key = f"cross_book_{market}"
        edges = scan_cross_book_edges(games, market=market, sport=sport)
        report[key] = edges
        if edges:
            logger.info(
                f"Cross-book {market}: {len(edges)} divergences found, "
                f"max implied range: {edges[0]['implied_range']:.1%}"
            )

    # Vig analysis
    for market in ["spreads", "h2h", "totals"]:
        key = f"low_vig_{market}"
        vig = scan_vig_edges(games, market=market)
        report[key] = vig
        if vig:
            logger.info(f"Low vig {market}: {len(vig)} edges, lowest: {vig[0]['vig_pct']}%")

    # Pace model total edges (independent fair value estimate)
    pace_model_edges = scan_pace_model_total_edges(games, sport)
    report["pace_model_totals"] = pace_model_edges
    if pace_model_edges:
        logger.info(f"Pace model totals: {len(pace_model_edges)} edges found")

    # Dead number steals — find books sitting on dead numbers while others
    # are on key numbers for the same game (spreads only)
    _dn_sport = sport.lower() if sport else ""
    if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
        dead_steals = _scan_dead_number_steals(games, sport)
        report["dead_number_steals"] = dead_steals
        if dead_steals:
            logger.info(f"Dead number steals: {len(dead_steals)} found")
    else:
        report["dead_number_steals"] = []

    # Alt-line edges — scan alternate spreads / totals / prop alts
    # for cross-book divergence at each alt point value. The caller is
    # expected to have enriched games with `alt_bookmakers` via
    # ``fetch_alt_lines_for_games`` before handing the snapshot off (the
    # sync path can't itself fire the async fetch). Every alt point becomes
    # its own edge candidate.
    alt_enriched_games = [g for g in games if g.get("alt_bookmakers")]
    if alt_enriched_games:
        alt_edges = scan_alt_line_edges(alt_enriched_games, sport=sport)
        report["alt_line_edges"] = alt_edges
        if alt_edges:
            logger.info(f"Alt-line edges: {len(alt_edges)} found across {len(alt_enriched_games)} game(s)")
    else:
        report["alt_line_edges"] = []

    # Simulation-based edge validation — independently validate cross-book
    # edges using Monte Carlo simulations (simulate_spread for high-scoring,
    # compare_poisson_to_market for low-scoring sports)
    sim_validated = _simulation_validate_edges(games, sport, report)
    report["simulation_validated"] = sim_validated
    if sim_validated:
        logger.info(f"Simulation validation: {len(sim_validated)} edges validated")

    # Summary
    total_edges = sum(
        len(report.get(k, []))
        for k in report
        if k.startswith("cross_book_") or k.startswith("low_vig_") or k in ("pace_model_totals", "dead_number_steals", "simulation_validated", "alt_line_edges")
    )
    report["total_edges"] = total_edges

    return report
