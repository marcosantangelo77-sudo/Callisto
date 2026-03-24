"""
Parlay and player prop edge scanner.

Books price parlay legs independently, but correlated events create edges:
- Star player out → team total drops, bench player usage spikes
- Defensive matchup → affects O/U AND player props simultaneously
- Pace of play → correlated across all totals in a game

The parlay pricing assumption is INDEPENDENCE between legs.
When legs are correlated, the true probability of the parlay differs
from what the books price. That gap is the edge.

Also handles:
- Alternate line exploitation across books
- Player prop mispricings from role changes
- Live line overreaction detection
"""

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.parlay_scanner")


def parlay_odds_from_legs(legs: list[dict]) -> dict:
    """
    Calculate true parlay odds from individual legs.

    Books multiply implied probabilities of each leg (assuming independence).
    If legs are positively correlated, the true parlay probability is HIGHER
    than the book's price → the parlay is underpriced → +EV.

    Args:
        legs: List of dicts with 'american_odds' and optionally 'true_probability'

    Returns:
        Dict with parlay pricing analysis.
    """
    if not legs:
        return {"error": "No legs provided"}

    # Calculate implied probabilities from each leg's American odds
    implied_probs = []
    true_probs = []
    for leg in legs:
        odds = leg.get("american_odds", -110)
        implied = calculate_implied_probability(odds)
        implied_probs.append(implied)
        # If we have an estimated true probability, use it
        true_prob = leg.get("true_probability", implied)
        true_probs.append(true_prob)

    # Book's parlay probability = product of implied (assumes independence)
    book_parlay_prob = 1.0
    for p in implied_probs:
        book_parlay_prob *= p

    # Our estimated true parlay probability
    true_parlay_prob = 1.0
    for p in true_probs:
        true_parlay_prob *= p

    # Convert book parlay probability back to American odds
    if book_parlay_prob > 0:
        if book_parlay_prob >= 0.5:
            parlay_american = int(-100 * book_parlay_prob / (1 - book_parlay_prob))
        else:
            parlay_american = int(100 * (1 - book_parlay_prob) / book_parlay_prob)
    else:
        parlay_american = 0

    # Edge: difference between our estimated prob and book's implied prob
    edge = true_parlay_prob - book_parlay_prob

    # Calculate EV if we have true probabilities
    ev_result = None
    if any(leg.get("true_probability") for leg in legs):
        ev_result = calculate_ev(
            probability=true_parlay_prob,
            american_odds=parlay_american,
        )

    return {
        "legs": len(legs),
        "book_implied_probability": round(book_parlay_prob, 6),
        "true_parlay_probability": round(true_parlay_prob, 6),
        "parlay_american_odds": parlay_american,
        "edge": round(edge, 6),
        "edge_pct": round(edge * 100, 2),
        "ev_analysis": ev_result,
        "leg_details": [
            {
                "odds": leg.get("american_odds"),
                "implied": round(imp, 4),
                "true_prob": round(tp, 4),
                "description": leg.get("description", ""),
            }
            for leg, imp, tp in zip(legs, implied_probs, true_probs)
        ],
    }


def find_correlated_parlay_edges(
    game_odds: dict,
    alternate_lines: dict,
) -> list[dict]:
    """
    Find parlay legs within the same game that are correlated but priced independently.

    Key correlations:
    1. Team spread + game total: If a team covers a large spread, the total
       is more likely to go over (blowouts = more points).
    2. Team ML + alternate spread: Buying a cheaper spread with the ML creates
       a correlated parlay that's priced as if independent.
    3. O/U + team total: Correlated by definition — if total goes over,
       one or both team totals exceeded expectations.

    Args:
        game_odds: Standard odds for a game (h2h, spreads, totals)
        alternate_lines: Alternate spreads/totals for the same game
    """
    edges = []
    home = game_odds.get("home_team", "")
    away = game_odds.get("away_team", "")

    # Extract standard lines
    std_spreads = {}
    std_totals = {}
    std_ml = {}
    for bm in game_odds.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                if mkt["key"] == "spreads":
                    std_spreads.setdefault(o.get("name", ""), []).append({
                        "bookmaker": bm["title"],
                        "price": o["price"],
                        "point": o.get("point"),
                    })
                elif mkt["key"] == "totals":
                    std_totals.setdefault(o.get("name", ""), []).append({
                        "bookmaker": bm["title"],
                        "price": o["price"],
                        "point": o.get("point"),
                    })
                elif mkt["key"] == "h2h":
                    std_ml.setdefault(o.get("name", ""), []).append({
                        "bookmaker": bm["title"],
                        "price": o["price"],
                    })

    # Extract alternate lines
    alt_spreads = {}
    alt_totals = {}
    for bm in alternate_lines.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            for o in mkt.get("outcomes", []):
                if mkt["key"] == "alternate_spreads":
                    key = (o.get("name", ""), o.get("point"))
                    alt_spreads.setdefault(key, []).append({
                        "bookmaker": bm["title"],
                        "price": o["price"],
                        "point": o.get("point"),
                        "name": o.get("name", ""),
                    })
                elif mkt["key"] == "alternate_totals":
                    key = (o.get("name", ""), o.get("point"))
                    alt_totals.setdefault(key, []).append({
                        "bookmaker": bm["title"],
                        "price": o["price"],
                        "point": o.get("point"),
                        "name": o.get("name", ""),
                    })

    # Strategy 1: Favorite ML + bought-up spread
    # If a team is -300 ML and you can get them at +3.5 alternate spread at -150,
    # the favorite covering by 3.5+ is correlated with winning outright.
    for team in [home, away]:
        ml_lines = std_ml.get(team, [])
        if not ml_lines:
            continue
        best_ml = max(ml_lines, key=lambda x: x["price"])
        ml_implied = calculate_implied_probability(best_ml["price"])

        # Only look at favorites (implied > 55%)
        if ml_implied < 0.55:
            continue

        # Find alternate spreads where this team is giving more points
        for (name, point), lines in alt_spreads.items():
            if name != team or point is None:
                continue
            # Bought-up spread: team giving MORE points than standard
            std = std_spreads.get(team, [])
            if not std:
                continue
            std_point = std[0].get("point", 0) or 0
            if point is not None and point < std_point - 2:
                # This is a bought-up spread (giving more points)
                best_alt = max(lines, key=lambda x: x["price"])
                alt_implied = calculate_implied_probability(best_alt["price"])

                # Correlation: if team wins ML, they're more likely to cover
                # a spread that's wider than standard. Books price these independently.
                # True joint probability > product of individual probabilities.
                independent_prob = ml_implied * alt_implied
                # Estimate correlation boost (conservative 5-15% based on spread diff)
                spread_diff = abs(std_point - point)
                correlation_boost = min(0.15, spread_diff * 0.02)
                correlated_prob = min(alt_implied, independent_prob + correlation_boost)

                if correlated_prob > independent_prob * 1.03:  # 3% minimum edge
                    edges.append({
                        "type": "FAVORITE_ML_BOUGHT_SPREAD",
                        "game": f"{away} @ {home}",
                        "team": team,
                        "leg1": f"ML {team} @ {best_ml['price']} ({best_ml['bookmaker']})",
                        "leg2": f"Alt spread {point} @ {best_alt['price']} ({best_alt['bookmaker']})",
                        "independent_prob": round(independent_prob, 4),
                        "estimated_correlated_prob": round(correlated_prob, 4),
                        "edge_pct": round((correlated_prob - independent_prob) * 100, 2),
                        "correlation": "positive",
                        "reasoning": (
                            f"If {team} wins outright (ML), they're more likely to cover "
                            f"the wider {point} spread. Books price these as independent events."
                        ),
                    })

    # Strategy 2: Cross-book alternate spread arbitrage
    # Different books offer wildly different prices on the same alternate spread
    for (name, point), lines in alt_spreads.items():
        if len(lines) < 2:
            continue
        prices = sorted([l["price"] for l in lines], reverse=True)
        best = max(lines, key=lambda x: x["price"])
        worst = min(lines, key=lambda x: x["price"])
        spread = prices[0] - prices[-1]

        if spread >= 30:  # 30+ point spread on same line = significant
            best_implied = calculate_implied_probability(best["price"])
            worst_implied = calculate_implied_probability(worst["price"])
            edges.append({
                "type": "ALT_SPREAD_CROSS_BOOK",
                "game": f"{away} @ {home}",
                "team": name,
                "point": point,
                "best_price": best["price"],
                "best_book": best["bookmaker"],
                "worst_price": worst["price"],
                "worst_book": worst["bookmaker"],
                "price_spread": spread,
                "implied_range": round(abs(best_implied - worst_implied), 4),
                "reasoning": (
                    f"Same alternate spread ({name} {point}) priced {spread} points "
                    f"apart across books. {best['bookmaker']} is significantly softer."
                ),
            })

    # Strategy 3: Over/Under + spread correlation
    # Blowouts push totals over. If you like a big favorite to cover AND the over,
    # these are positively correlated but priced independently.
    for team in [home, away]:
        spread_lines = std_spreads.get(team, [])
        over_lines = std_totals.get("Over", [])
        if not spread_lines or not over_lines:
            continue

        best_spread = max(spread_lines, key=lambda x: x["price"])
        spread_point = best_spread.get("point", 0) or 0

        # Only for favorites with large spreads (covering = more points scored)
        if spread_point > -4:
            continue

        best_over = max(over_lines, key=lambda x: x["price"])
        spread_implied = calculate_implied_probability(best_spread["price"])
        over_implied = calculate_implied_probability(best_over["price"])

        independent_prob = spread_implied * over_implied
        # Favorites covering big spreads correlates with higher scoring
        correlation_boost = min(0.10, abs(spread_point) * 0.01)
        correlated_prob = min(
            min(spread_implied, over_implied),
            independent_prob + correlation_boost,
        )

        if correlated_prob > independent_prob * 1.02:
            edges.append({
                "type": "SPREAD_OVER_CORRELATION",
                "game": f"{away} @ {home}",
                "team": team,
                "spread": f"{team} {spread_point} @ {best_spread['price']}",
                "over": f"Over {best_over.get('point')} @ {best_over['price']}",
                "independent_prob": round(independent_prob, 4),
                "estimated_correlated_prob": round(correlated_prob, 4),
                "edge_pct": round((correlated_prob - independent_prob) * 100, 2),
                "reasoning": (
                    f"If {team} covers {spread_point}, the game likely went over. "
                    f"Blowout wins produce more total points. Parlay is underpriced."
                ),
            })

    edges.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)
    return edges


def analyze_prop_mispricing(
    player_props: dict,
    context: Optional[dict] = None,
) -> list[dict]:
    """
    Find mispriced player props by comparing cross-bookmaker pricing
    and identifying contextual factors that shift true probability.

    Context dict can contain:
    - starter_out: Name of injured/inactive starter
    - starter_avg_stats: {"points": 18, "rebounds": 7, "assists": 5}
    - replacement_player: Name of the bench player getting elevated role
    - replacement_avg_stats: {"points": 8, "rebounds": 3, "assists": 2}
    - replacement_starter_stats: Stats when this player starts (if available)
    - opponent_defensive_rating: 1-30 ranking
    - pace: team pace factor

    This is where the real edge is:
    Books set prop lines based on season averages. But if the starting PG
    is out and the backup historically averages 6 more assists when starting,
    the book's line based on overall season average is systematically low.
    """
    edges = []
    players = player_props.get("players", {})

    for player_name, lines in players.items():
        # Group by market type
        by_market = {}
        for line in lines:
            mkt = line["market"]
            by_market.setdefault(mkt, []).append(line)

        for market, market_lines in by_market.items():
            # Split into overs and unders
            overs = [l for l in market_lines if l["name"] == "Over"]
            unders = [l for l in market_lines if l["name"] == "Under"]

            if not overs:
                continue

            # Cross-book analysis on the over
            over_prices = sorted([l["price"] for l in overs], reverse=True)
            if len(overs) >= 2:
                best_over = max(overs, key=lambda x: x["price"])
                worst_over = min(overs, key=lambda x: x["price"])
                price_spread = over_prices[0] - over_prices[-1]

                # The point (line) may differ across books too
                points = set(l.get("point") for l in overs if l.get("point") is not None)

                if price_spread >= 15 or len(points) > 1:
                    best_implied = calculate_implied_probability(best_over["price"])
                    worst_implied = calculate_implied_probability(worst_over["price"])

                    edge_entry = {
                        "player": player_name,
                        "market": market,
                        "best_over": {
                            "bookmaker": best_over["bookmaker"],
                            "price": best_over["price"],
                            "point": best_over.get("point"),
                            "implied": round(best_implied, 4),
                        },
                        "worst_over": {
                            "bookmaker": worst_over["bookmaker"],
                            "price": worst_over["price"],
                            "point": worst_over.get("point"),
                            "implied": round(worst_implied, 4),
                        },
                        "price_spread": price_spread,
                        "implied_range": round(abs(best_implied - worst_implied), 4),
                        "distinct_lines": sorted(points),
                    }

                    # Apply contextual adjustment if we have info about role changes
                    if context and player_name == context.get("replacement_player", ""):
                        starter_stats = context.get("starter_avg_stats", {})
                        replacement_starter_stats = context.get("replacement_starter_stats", {})

                        stat_key = _market_to_stat(market)
                        if stat_key:
                            season_avg = context.get("replacement_avg_stats", {}).get(stat_key)
                            starter_avg = replacement_starter_stats.get(stat_key)
                            line_point = best_over.get("point", 0)

                            if season_avg and starter_avg and line_point:
                                # If their starting avg is significantly higher than season avg,
                                # and the line is based on season avg, there's an edge
                                usage_bump = starter_avg - season_avg
                                if usage_bump > 2:
                                    edge_entry["contextual_edge"] = {
                                        "type": "ROLE_CHANGE_BOOST",
                                        "season_avg": season_avg,
                                        "starter_avg": starter_avg,
                                        "usage_bump": round(usage_bump, 1),
                                        "line_set_at": line_point,
                                        "starter_out": context.get("starter_out", ""),
                                        "assessment": (
                                            f"{player_name} averages {starter_avg} {stat_key} when starting "
                                            f"vs {season_avg} overall. Line set at {line_point}. "
                                            f"With {context.get('starter_out', 'starter')} out, "
                                            f"the over is likely underpriced by ~{usage_bump:.0f} {stat_key}."
                                        ),
                                    }

                    edges.append(edge_entry)

    edges.sort(key=lambda x: x.get("price_spread", 0), reverse=True)
    return edges


def _market_to_stat(market: str) -> Optional[str]:
    """Map prop market key to stat category."""
    mapping = {
        "player_points": "points",
        "player_rebounds": "rebounds",
        "player_assists": "assists",
        "player_threes": "threes",
        "player_points_rebounds_assists": "pra",
        "player_points_rebounds": "points_rebounds",
        "player_points_assists": "points_assists",
    }
    return mapping.get(market)


def analyze_live_overreaction(
    pre_game_odds: dict,
    live_odds: dict,
    game_context: Optional[str] = None,
) -> list[dict]:
    """
    Compare pre-game odds to live odds and identify overreactions.

    This is your Vanderbilt example: pre-game line near even, team starts slow,
    live line jumps to +148. But the game ended close. The market overreacted
    to early game flow — variance, not signal.

    Key overreaction indicators:
    1. Large movement early in the game (first quarter/half) — variance is high
    2. Movement caused by a single run/event that's unlikely to sustain
    3. Movement exceeds what the score differential warrants
    4. Team's pre-game fundamentals haven't changed (no injury)

    Args:
        pre_game_odds: Odds snapshot from before tipoff
        live_odds: Current live odds
        game_context: Optional description of what happened (e.g., "opponent went on 12-0 run")
    """
    overreactions = []

    # Build lookup for pre-game lines
    pre_lines = {}
    for game in pre_game_odds.get("games", [pre_game_odds]):
        gid = game.get("id", "")
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    key = (gid, mkt["key"], o.get("name", ""))
                    if key not in pre_lines:  # Keep first (pre-game) value
                        pre_lines[key] = {
                            "price": o.get("price", 0),
                            "point": o.get("point"),
                            "bookmaker": bm["title"],
                        }

    # Compare with live lines
    for game in live_odds.get("games", [live_odds]):
        gid = game.get("id", "")
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    key = (gid, mkt["key"], o.get("name", ""))
                    pre = pre_lines.get(key)
                    if not pre:
                        continue

                    live_price = o.get("price", 0)
                    pre_price = pre["price"]
                    movement = live_price - pre_price

                    live_point = o.get("point")
                    pre_point = pre.get("point")
                    point_movement = 0
                    if live_point is not None and pre_point is not None:
                        point_movement = live_point - pre_point

                    # Flag large movements as potential overreactions
                    if abs(movement) >= 30 or abs(point_movement) >= 2:
                        pre_implied = calculate_implied_probability(pre_price)
                        live_implied = calculate_implied_probability(live_price)
                        implied_shift = live_implied - pre_implied

                        overreactions.append({
                            "game_id": gid,
                            "team": o.get("name", ""),
                            "market": mkt["key"],
                            "bookmaker": bm["title"],
                            "pre_game_price": pre_price,
                            "live_price": live_price,
                            "price_movement": movement,
                            "pre_point": pre_point,
                            "live_point": live_point,
                            "point_movement": point_movement,
                            "pre_implied": round(pre_implied, 4),
                            "live_implied": round(live_implied, 4),
                            "implied_shift": round(implied_shift, 4),
                            "context": game_context,
                            "assessment": _assess_overreaction(
                                movement, point_movement, pre_implied, live_implied
                            ),
                        })

    overreactions.sort(key=lambda x: abs(x["price_movement"]), reverse=True)
    return overreactions


def _assess_overreaction(
    price_movement: int,
    point_movement: float,
    pre_implied: float,
    live_implied: float,
) -> str:
    """Generate a quick assessment of whether a line move is an overreaction."""
    implied_shift = abs(live_implied - pre_implied)

    if implied_shift > 0.20:
        severity = "EXTREME"
    elif implied_shift > 0.12:
        severity = "LARGE"
    elif implied_shift > 0.06:
        severity = "MODERATE"
    else:
        severity = "MINOR"

    direction = "AGAINST" if price_movement > 0 else "TOWARD"

    return (
        f"{severity} movement ({direction} this outcome). "
        f"Implied probability shifted {implied_shift:.1%}. "
        f"{'Likely overreaction if early in game — consider fading.' if severity in ('EXTREME', 'LARGE') else 'Monitor for continued movement.'}"
    )
