"""
Composite dead number analysis: spread analysis, line shopping rankings,
buy-points analysis, and dead number steal detection.
"""

from typing import Optional

from .data import KEY_NUMBERS, _normalize_sport
from .valuation import (
    half_quarter_key_value,
    is_dead_number,
    key_number_value,
    line_shopping_value,
    push_probability,
)
from .shading import detect_public_shading


# ---------------------------------------------------------------------------
# Composite analysis functions
# ---------------------------------------------------------------------------

def analyze_spread(
    spread: float,
    sport: str,
    alt_spread: Optional[float] = None,
    period: str = "FG",
) -> dict:
    """
    Full dead number and key number analysis for a single spread.

    This is the main entry point for spread analysis. Pass a spread and sport,
    get back everything: is it dead, how important is it, what's the push
    probability, is it being shaded, and if there's an alternate available,
    what's the line shopping value.

    Args:
        spread: The spread (e.g., -3.0, +7.5).
        sport: Sport identifier.
        alt_spread: Optional alternate spread for line shopping comparison.
        period: "FG" (full game), "1H" (first half), "1Q" (first quarter).
    """
    normalized = _normalize_sport(sport)

    if period == "FG":
        importance = key_number_value(spread, sport)
    else:
        importance = half_quarter_key_value(spread, sport, period)

    dead = is_dead_number(spread, sport)
    push_prob = push_probability(spread, sport)
    shading = detect_public_shading(spread, sport)

    result = {
        "spread": spread,
        "sport": normalized,
        "period": period,
        "key_number_importance": importance,
        "is_dead_number": dead,
        "push_probability": push_prob,
        "push_probability_pct": round(push_prob * 100, 2),
        "public_shading": shading,
    }

    if alt_spread is not None:
        shopping = line_shopping_value(spread, alt_spread, sport)
        result["line_shopping"] = shopping

    # Context-specific commentary
    if normalized in ("NFL", "NCAAF"):
        abs_s = abs(spread)
        if 2.5 <= abs_s <= 3.5:
            result["commentary"] = (
                "This is in the 3-point zone — the most critical number in "
                f"{'NFL' if normalized == 'NFL' else 'college'} football. "
                "14.8% of NFL games are decided by exactly 3. Every half point "
                "matters enormously here."
            )
        elif 6.5 <= abs_s <= 7.5:
            result["commentary"] = (
                "This is in the 7-point zone — the second most critical number. "
                "9.1% of games decided by exactly 7. Getting on the right side "
                "of 7 is a major edge."
            )
        elif dead:
            result["commentary"] = (
                f"Dead number territory ({abs_s}). Very few games land here. "
                "Line shopping between adjacent dead numbers has minimal value."
            )
    elif normalized in ("MLB", "NHL"):
        abs_s = abs(spread)
        if abs_s <= 1.5:
            result["commentary"] = (
                f"Standard {'run' if normalized == 'MLB' else 'puck'} line zone. "
                f"{'28%' if normalized == 'MLB' else '30%'} of games are decided "
                f"by 1 {'run' if normalized == 'MLB' else 'goal'}. The .5 matters "
                "enormously here."
            )
    elif normalized in ("NBA", "NCAAB"):
        if dead:
            result["commentary"] = (
                "Basketball margins are more uniformly distributed — key numbers "
                "matter less than in football. Focus on line value over number theory."
            )

    return result


def rank_line_shopping_opportunities(
    lines: list[dict],
    sport: str,
) -> list[dict]:
    """
    Given multiple spread offerings for the same game across different books,
    rank them by the actual probability value of the differences.

    This tells you: "DraftKings -3 vs FanDuel -2.5 on the same game —
    that half point is worth X% of outcomes."

    Args:
        lines: List of {"bookmaker": str, "spread": float, "price": int}
        sport: Sport identifier.

    Returns:
        Sorted list of line comparisons with value analysis.
    """
    if len(lines) < 2:
        return []

    comparisons = []

    # Compare each pair of bookmakers
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            a = lines[i]
            b = lines[j]
            spread_a = a["spread"]
            spread_b = b["spread"]

            if spread_a == spread_b:
                # Same spread, only juice differs — still valuable but different calc
                price_diff = abs(a.get("price", -110) - b.get("price", -110))
                comparisons.append({
                    "book_a": a["bookmaker"],
                    "book_b": b["bookmaker"],
                    "spread_a": spread_a,
                    "spread_b": spread_b,
                    "price_a": a.get("price", -110),
                    "price_b": b.get("price", -110),
                    "spread_diff": 0,
                    "prob_difference": 0,
                    "prob_difference_pct": 0,
                    "juice_difference": price_diff,
                    "type": "JUICE_ONLY",
                    "recommendation": (
                        f"Same spread — take the better juice "
                        f"({price_diff} cents difference)"
                    ),
                })
                continue

            shopping = line_shopping_value(spread_a, spread_b, sport)

            # Determine which book is better
            if spread_a > spread_b:
                better_book = a["bookmaker"]
                worse_book = b["bookmaker"]
            else:
                better_book = b["bookmaker"]
                worse_book = a["bookmaker"]

            comparisons.append({
                "book_a": a["bookmaker"],
                "book_b": b["bookmaker"],
                "spread_a": spread_a,
                "spread_b": spread_b,
                "price_a": a.get("price", -110),
                "price_b": b.get("price", -110),
                "spread_diff": abs(spread_a - spread_b),
                "prob_difference": shopping["prob_difference"],
                "prob_difference_pct": shopping["prob_difference_pct"],
                "cents_value": shopping["cents_value"],
                "crossed_key_numbers": shopping["crossed_key_numbers"],
                "type": "SPREAD_DIFF",
                "better_book": better_book,
                "recommendation": shopping["recommendation"],
            })

    # Sort by probability difference descending
    comparisons.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    return comparisons


def buy_points_analysis(
    current_spread: float,
    target_spread: float,
    point_cost_cents: int,
    sport: str,
) -> dict:
    """
    Analyze whether buying points is +EV.

    Sportsbooks let you buy points (move the spread) for extra juice. Typically
    10-20 cents per half point, but crossing key numbers costs more (sometimes
    25-30 cents for the 3 in NFL).

    The question: is the probability gained worth the juice paid?

    Args:
        current_spread: The standard spread (e.g., -3.0).
        target_spread: The bought-down spread (e.g., -2.5).
        point_cost_cents: Extra juice in cents (e.g., 20 means -110 becomes -130).
        sport: Sport identifier.

    Returns:
        Dict with buy analysis and recommendation.
    """
    shopping = line_shopping_value(current_spread, target_spread, sport)
    prob_gain = shopping["prob_difference"]

    # Standard -110 implies 52.38% breakeven
    # Extra juice of N cents means the line becomes -(110+N)
    new_juice = -(110 + point_cost_cents)

    # Calculate implied probability at the new juice
    if new_juice < 0:
        implied_at_new_juice = abs(new_juice) / (abs(new_juice) + 100)
    else:
        implied_at_new_juice = 100 / (new_juice + 100)

    standard_implied = 110 / (110 + 100)  # 0.5238
    juice_cost_pct = implied_at_new_juice - standard_implied

    # Net value: probability gained minus probability cost of extra juice
    net_value = prob_gain - juice_cost_pct

    is_profitable = net_value > 0

    return {
        "current_spread": current_spread,
        "target_spread": target_spread,
        "sport": _normalize_sport(sport),
        "point_cost_cents": point_cost_cents,
        "new_juice_line": new_juice,
        "probability_gained": round(prob_gain, 4),
        "probability_gained_pct": round(prob_gain * 100, 2),
        "juice_cost_pct": round(juice_cost_pct, 4),
        "juice_cost_pct_display": round(juice_cost_pct * 100, 2),
        "net_value": round(net_value, 4),
        "net_value_pct": round(net_value * 100, 2),
        "is_profitable": is_profitable,
        "crossed_key_numbers": shopping["crossed_key_numbers"],
        "recommendation": (
            f"BUY — gaining {prob_gain*100:.1f}% probability for {juice_cost_pct*100:.1f}% "
            f"juice cost. Net +{net_value*100:.1f}% EV."
            if is_profitable
            else f"PASS — gaining only {prob_gain*100:.1f}% probability but paying "
            f"{juice_cost_pct*100:.1f}% in juice. Net {net_value*100:.1f}% EV."
        ),
    }


def find_dead_number_steals(
    lines: list[dict],
    sport: str,
) -> list[dict]:
    """
    Find opportunities where a book has moved OFF a key number onto a dead
    number, creating value.

    When a line moves from 3 to 4 in the NFL, the book crossed a key number
    (3) and landed on a semi-dead number (4). The move from 3 to 4 was
    expensive in probability terms, but from 4 to 5 is cheap. This means:
    - If you can still get 3 at another book, that's a steal
    - If the book has the favorite at -4 and you can get -3 elsewhere, massive value

    Args:
        lines: List of {"bookmaker": str, "spread": float, "price": int}
        sport: Sport identifier.

    Returns:
        Opportunities ranked by dead number value.
    """
    if len(lines) < 2:
        return []

    normalized = _normalize_sport(sport)
    opportunities = []

    # Find the best and worst spreads
    sorted_lines = sorted(lines, key=lambda x: x["spread"], reverse=True)
    best = sorted_lines[0]
    worst = sorted_lines[-1]

    if best["spread"] == worst["spread"]:
        return []

    # Check if any book is on a dead number while another is on a key number
    for line in lines:
        importance = key_number_value(line["spread"], sport)
        dead = is_dead_number(line["spread"], sport)

        # Find the nearest key number this book is AWAY from
        key_table = KEY_NUMBERS.get(normalized, {})
        abs_spread = abs(line["spread"])
        nearest_key = None
        nearest_key_dist = float("inf")

        for kn, val in key_table.items():
            if val >= 0.4:  # Only consider significant key numbers
                dist = abs(abs_spread - kn)
                if 0 < dist < nearest_key_dist:
                    nearest_key_dist = dist
                    nearest_key = kn

        # Check if another book is on that key number
        if nearest_key is not None and dead:
            for other in lines:
                if other["bookmaker"] == line["bookmaker"]:
                    continue
                if abs(abs(other["spread"]) - nearest_key) < 0.01:
                    shopping = line_shopping_value(
                        line["spread"], other["spread"], sport
                    )
                    opportunities.append({
                        "dead_book": line["bookmaker"],
                        "dead_spread": line["spread"],
                        "dead_price": line.get("price", -110),
                        "key_book": other["bookmaker"],
                        "key_spread": other["spread"],
                        "key_price": other.get("price", -110),
                        "key_number_crossed": nearest_key,
                        "prob_difference": shopping["prob_difference"],
                        "prob_difference_pct": shopping["prob_difference_pct"],
                        "cents_value": shopping["cents_value"],
                        "recommendation": (
                            f"STEAL at {other['bookmaker']}: {other['spread']:+.1f} vs "
                            f"{line['bookmaker']}'s {line['spread']:+.1f}. "
                            f"Crosses key number {nearest_key}, worth "
                            f"{shopping['prob_difference_pct']:.1f}% probability."
                        ),
                    })

    opportunities.sort(key=lambda x: x["prob_difference"], reverse=True)
    return opportunities
