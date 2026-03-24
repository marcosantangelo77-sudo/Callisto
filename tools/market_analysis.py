"""
Market structure analysis — key numbers, reverse line movement, stale lines, vig structure.

This is how books build lines. Understanding the structure reveals where edges hide:
- Key number shading (3, 7 in NFL; round totals) attracts public money
- Reverse line movement = sharp money signal
- Stale lines = information asymmetry = free money
- Pinnacle benchmark comparison = are you beating the efficient market?
"""

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.market_analysis")

# Key numbers by sport — books shade lines toward these because public clusters there
KEY_NUMBERS = {
    "americanfootball_nfl": {
        "spreads": [3, 7, 6, 10, 14, 1, 4, 17, 21],  # NFL margin frequencies
        "totals": list(range(40, 56)),  # Round totals
    },
    "americanfootball_ncaaf": {
        "spreads": [3, 7, 6, 10, 14, 1, 4, 17, 21],
        "totals": list(range(42, 60)),
    },
    "basketball_nba": {
        "spreads": [1, 2, 3, 4, 5, 6, 7, 8],
        "totals": list(range(210, 240, 5)),  # Round number totals
    },
    "basketball_ncaab": {
        "spreads": [1, 2, 3, 4, 5, 6, 7],
        "totals": list(range(130, 165, 5)),
    },
}

# Sharp books — their closing lines are the benchmark
# Keys use API format (no dots): lowvig, betonlineag, pinnacle
SHARP_BOOKS = {"pinnacle", "lowvig", "bookmaker", "betonlineag", "betcris", "circa"}
RETAIL_BOOKS = {"fanduel", "draftkings", "betmgm", "pointsbet", "caesars", "betrivers", "betway", "mybookieag", "bovada", "betus"}


def analyze_key_numbers(games: list[dict], sport: str) -> list[dict]:
    """
    Detect lines sitting on or near key numbers.

    Books shade lines toward key numbers because public money clusters there.
    -2.5 in NFL attracts less action than -3, so -2.5 may offer better value.
    If you see -3 at -115 instead of -110, the book is charging extra juice
    to sit on the key number.

    The edge: buy off key numbers when possible. Half-point off a key number
    can be worth 2-4% in implied probability.
    """
    sport_keys = KEY_NUMBERS.get(sport, {})
    spread_keys = sport_keys.get("spreads", [])
    total_keys = sport_keys.get("totals", [])

    findings = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    price = o.get("price", 0)
                    if point is None:
                        continue

                    abs_point = abs(point)
                    keys = spread_keys if mkt["key"] == "spreads" else total_keys if mkt["key"] == "totals" else []

                    # Check if on a key number
                    on_key = abs_point in keys or int(abs_point) in keys

                    # Check if half-point off a key number (the sweet spot)
                    half_off_key = (abs_point + 0.5) in keys or (abs_point - 0.5) in keys

                    if on_key:
                        implied = calculate_implied_probability(price)
                        standard_juice = calculate_implied_probability(-110)

                        # Extra juice for sitting on key number?
                        juice_premium = implied - standard_juice if price < -110 else 0

                        findings.append({
                            "game": f"{away} @ {home}",
                            "bookmaker": bm["title"],
                            "market": mkt["key"],
                            "team": o.get("name", ""),
                            "point": point,
                            "price": price,
                            "key_number": True,
                            "juice_premium": round(juice_premium, 4),
                            "implied": round(implied, 4),
                            "note": (
                                f"Line sitting on key number {abs_point}. "
                                f"{'Extra juice charged: ' + f'{juice_premium:.1%}' if juice_premium > 0.005 else 'Standard juice.'} "
                                f"Look for half-point off at other books for better value."
                            ),
                        })

    return findings


def detect_reverse_line_movement(
    old_snapshot: dict,
    new_snapshot: dict,
    ticket_percentages: Optional[dict] = None,
) -> list[dict]:
    """
    Detect reverse line movement — when the line moves AGAINST the public ticket %.

    If 70% of tickets are on Team A but the line moves toward Team A (making
    Team A more expensive), that's sharp money on Team B moving the line
    despite public betting the other way.

    Reverse line movement is one of the strongest indicators of sharp action.

    Args:
        old_snapshot: Previous odds snapshot
        new_snapshot: Current odds snapshot
        ticket_percentages: Optional dict of {team_name: ticket_pct} from public data
    """
    signals = []

    # Build price comparison
    old_prices = {}
    new_prices = {}

    for snapshot, store in [(old_snapshot, old_prices), (new_snapshot, new_prices)]:
        for game in snapshot.get("games", []):
            gid = game.get("id", "")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for o in mkt.get("outcomes", []):
                        key = (gid, mkt["key"], o.get("name", ""))
                        if key not in store:
                            store[key] = {
                                "price": o.get("price", 0),
                                "point": o.get("point"),
                                "bookmaker": bm["title"],
                            }

    for key in new_prices:
        if key not in old_prices:
            continue

        gid, market, team = key
        old_price = old_prices[key]["price"]
        new_price = new_prices[key]["price"]
        movement = new_price - old_price

        if abs(movement) < 5:
            continue

        # Line moved to make this team MORE expensive (price went more negative)
        # = sharp money is ON this team
        line_direction = "toward" if movement < 0 else "away_from"

        # If we have ticket data, check for reverse movement
        if ticket_percentages and team in ticket_percentages:
            ticket_pct = ticket_percentages[team]

            # Reverse line movement: public is on this team (>55%) but line moves away
            # OR public is against this team (<45%) but line moves toward
            is_reverse = (
                (ticket_pct > 55 and line_direction == "away_from") or
                (ticket_pct < 45 and line_direction == "toward")
            )

            if is_reverse:
                signals.append({
                    "game_id": gid,
                    "market": market,
                    "team": team,
                    "movement": movement,
                    "direction": line_direction,
                    "ticket_pct": ticket_pct,
                    "signal": "REVERSE_LINE_MOVEMENT",
                    "sharp_side": team if line_direction == "toward" else "opposite",
                    "interpretation": (
                        f"{ticket_pct}% of tickets on {team} but line moved "
                        f"{line_direction} {team}. Sharp money is "
                        f"{'on' if line_direction == 'toward' else 'against'} {team}."
                    ),
                })
        else:
            # Without ticket data, just flag significant movement
            signals.append({
                "game_id": gid,
                "market": market,
                "team": team,
                "movement": movement,
                "direction": line_direction,
                "signal": "SIGNIFICANT_MOVEMENT",
                "interpretation": (
                    f"Line moved {abs(movement)} points {line_direction} {team}. "
                    f"Need ticket % data to confirm if reverse."
                ),
            })

    return signals


def find_stale_lines(
    games: list[dict],
    benchmark_book: str = "lowvig.ag",
) -> list[dict]:
    """
    Find lines at retail books that are stale relative to the sharp benchmark.

    Sharp books (Pinnacle, LowVig) update first. When a retail book hasn't
    adjusted yet, their line is stale and exploitable. This is the core of
    market origination arbitrage.

    The window is often minutes. Callisto's advantage is speed — detecting
    the discrepancy before the retail book adjusts.
    """
    stale = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Group lines by market and team
        by_line = {}
        for bm in game.get("bookmakers", []):
            book_key = bm.get("key", "").lower()
            is_sharp = book_key in SHARP_BOOKS
            is_retail = book_key in RETAIL_BOOKS

            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    line_key = (mkt["key"], o.get("name", ""))
                    if line_key not in by_line:
                        by_line[line_key] = {"sharp": [], "retail": []}

                    entry = {
                        "bookmaker": bm["title"],
                        "book_key": book_key,
                        "price": o.get("price", 0),
                        "point": o.get("point"),
                        "last_update": bm.get("last_update", ""),
                    }

                    if is_sharp:
                        by_line[line_key]["sharp"].append(entry)
                    elif is_retail:
                        by_line[line_key]["retail"].append(entry)

        # Compare retail to sharp benchmark
        for (market, team), groups in by_line.items():
            if not groups["sharp"] or not groups["retail"]:
                continue

            # Use best sharp price as benchmark
            sharp_benchmark = groups["sharp"][0]
            sharp_implied = calculate_implied_probability(sharp_benchmark["price"])

            for retail in groups["retail"]:
                retail_implied = calculate_implied_probability(retail["price"])
                divergence = retail_implied - sharp_implied

                # Retail implying HIGHER probability than sharp = retail is stale
                # (sharp already moved, retail hasn't caught up)
                # OR retail implying LOWER probability = retail offers value
                if abs(divergence) >= 0.025:  # 2.5% divergence minimum
                    is_value = divergence < 0  # Retail underpricing vs sharp = value

                    stale.append({
                        "game": f"{away} @ {home}",
                        "market": market,
                        "team": team,
                        "sharp_book": sharp_benchmark["bookmaker"],
                        "sharp_price": sharp_benchmark["price"],
                        "sharp_point": sharp_benchmark.get("point"),
                        "sharp_implied": round(sharp_implied, 4),
                        "retail_book": retail["bookmaker"],
                        "retail_price": retail["price"],
                        "retail_point": retail.get("point"),
                        "retail_implied": round(retail_implied, 4),
                        "divergence": round(divergence, 4),
                        "divergence_pct": round(divergence * 100, 2),
                        "is_value": is_value,
                        "signal": "STALE_VALUE" if is_value else "STALE_OVERPRICED",
                        "interpretation": (
                            f"{retail['bookmaker']} has {team} at {retail['price']} "
                            f"(implied {retail_implied:.1%}) while sharp benchmark "
                            f"{sharp_benchmark['bookmaker']} has {sharp_benchmark['price']} "
                            f"(implied {sharp_implied:.1%}). "
                            f"{'VALUE: retail underpricing by ' + f'{abs(divergence):.1%}' if is_value else 'Overpriced by ' + f'{divergence:.1%}'}"
                        ),
                    })

    stale.sort(key=lambda x: abs(x["divergence"]), reverse=True)
    return stale


def pinnacle_benchmark_comparison(games: list[dict]) -> dict:
    """
    Compare all bookmaker lines against the sharpest available benchmark.

    Pinnacle's closing line is the most efficient number in the market.
    Any sustained deviation from Pinnacle = potential edge or leak.

    Since we may not always have Pinnacle, we use LowVig/BetOnline as proxies.
    """
    sharp_lines = {}
    retail_lines = {}

    for game in games:
        for bm in game.get("bookmakers", []):
            book_key = bm.get("key", "").lower()
            is_sharp = book_key in SHARP_BOOKS

            for mkt in bm.get("markets", []):
                for o in mkt.get("outcomes", []):
                    line_key = (game.get("id", ""), mkt["key"], o.get("name", ""))
                    entry = {
                        "bookmaker": bm["title"],
                        "price": o.get("price", 0),
                        "point": o.get("point"),
                        "implied": calculate_implied_probability(o.get("price", -110)),
                    }

                    if is_sharp:
                        if line_key not in sharp_lines:
                            sharp_lines[line_key] = entry
                    else:
                        retail_lines.setdefault(line_key, []).append(entry)

    # Aggregate divergences by bookmaker
    book_divergences = {}
    for line_key, retail_list in retail_lines.items():
        sharp = sharp_lines.get(line_key)
        if not sharp:
            continue

        for retail in retail_list:
            book = retail["bookmaker"]
            div = retail["implied"] - sharp["implied"]

            if book not in book_divergences:
                book_divergences[book] = {"total_div": 0, "count": 0, "lines": []}

            book_divergences[book]["total_div"] += div
            book_divergences[book]["count"] += 1

    # Calculate average divergence per book
    rankings = []
    for book, data in book_divergences.items():
        avg_div = data["total_div"] / data["count"] if data["count"] > 0 else 0
        rankings.append({
            "bookmaker": book,
            "avg_divergence_from_sharp": round(avg_div, 4),
            "avg_divergence_pct": round(avg_div * 100, 2),
            "lines_compared": data["count"],
            "assessment": (
                "SOFT — consistently overpricing vs sharp" if avg_div > 0.01
                else "VALUE — consistently underpricing vs sharp" if avg_div < -0.01
                else "EFFICIENT — tracking sharp benchmark"
            ),
        })

    rankings.sort(key=lambda x: x["avg_divergence_from_sharp"])

    return {
        "benchmark": "Sharpest available (LowVig/BetOnline/Pinnacle)",
        "sharp_lines_found": len(sharp_lines),
        "retail_books_compared": len(rankings),
        "rankings": rankings,
    }


def full_market_analysis(games: list[dict], sport: str) -> dict:
    """Run all market structure analyses on a game set."""
    return {
        "key_numbers": analyze_key_numbers(games, sport),
        "stale_lines": find_stale_lines(games),
        "pinnacle_benchmark": pinnacle_benchmark_comparison(games),
        "sport": sport,
        "games_analyzed": len(games),
    }
