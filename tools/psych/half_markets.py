"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""5. Half/quarter market inefficiency analysis."""

from typing import Optional

from tools.psych.constants import SCORING_DISTRIBUTION, HALF_QUARTER_EDGES

# ---------------------------------------------------------------------------
# 5. Half / Quarter Market Inefficiency
# ---------------------------------------------------------------------------

def half_market_adjustment(
    full_game_line: float,
    sport: str,
    half: str = "first",
    market: str = "totals",
    is_ace_pitching: Optional[bool] = None,
) -> dict:
    """
    Project a half/quarter line from the full-game line and identify inefficiencies.

    Books set half and quarter lines by roughly halving the full game number,
    but scoring is NOT uniformly distributed across game segments:

    - NBA 1Q unders are historically underpriced (structured play, starters
      feeling out the game, lower pace in first few minutes)
    - NFL 2H totals are less efficient (books reprice with less infrastructure
      mid-game; the models they use for 2H are thinner)
    - MLB first 5 innings vs full game has completely different dynamics
      (starter vs bullpen; aces suppress scoring then bullpens give it back)

    Args:
        full_game_line: The full-game spread or total
        sport: Sport key
        half: 'first', 'second', 'first_quarter', 'second_quarter',
              'third_quarter', 'fourth_quarter', 'first_5' (MLB)
        market: 'totals' or 'spreads'
        is_ace_pitching: MLB-specific — is an ace starting? (affects F5 analysis)

    Returns:
        Dict with projected half line, edge estimate, and reasoning.
    """
    dist = SCORING_DISTRIBUTION.get(sport, {})
    edges = HALF_QUARTER_EDGES.get(sport, {})

    # Map half parameter to distribution key
    half_key_map = {
        "first": "first_half",
        "second": "second_half",
        "first_quarter": "first_quarter",
        "second_quarter": "second_quarter",
        "third_quarter": "third_quarter",
        "fourth_quarter": "fourth_quarter",
        "first_5": "first_5",
        "last_4": "last_4",
        "first_period": "first_period",
        "second_period": "second_period",
        "third_period": "third_period",
    }

    dist_key = half_key_map.get(half, "first_half")
    fraction = dist.get(dist_key, 0.5)

    # Project the half/quarter line
    if market == "totals":
        projected_line = full_game_line * fraction
        # Books typically round to nearest 0.5
        projected_line = round(projected_line * 2) / 2.0
        naive_line = full_game_line * 0.5 if "half" in dist_key else full_game_line * 0.25
        naive_line = round(naive_line * 2) / 2.0
    else:
        # Spreads: first half spread is roughly half the full game spread
        # but home court/field advantage is not evenly distributed
        projected_line = full_game_line * fraction * 2  # *2 because fraction is of total points, not spread
        # Actually for spreads the conversion is different:
        # Full game spread of -6 means team is 6 points better.
        # First half spread should be fraction * full_game_spread
        projected_line = full_game_line * fraction / 0.5 if fraction != 0 else full_game_line * 0.5
        # Simpler: for half spreads, the convention is roughly half the full game spread
        projected_line = full_game_line * (fraction / (dist.get("first_half", 0.5) + dist.get("second_half", 0.5))) if market == "spreads" else projected_line
        projected_line = round(projected_line * 2) / 2.0
        naive_line = full_game_line * 0.5
        naive_line = round(naive_line * 2) / 2.0

    # Identify known edges for this sport/half combination
    edge_candidates = []
    total_edge_pct = 0.0
    reasoning_parts = []

    if market == "totals":
        # Check for known edges
        under_key = f"{dist_key}_under"
        over_key = f"{dist_key}_over"
        total_key = f"{dist_key}_total"

        if under_key in edges:
            edge_val = edges[under_key]
            edge_candidates.append({"side": "under", "edge_pct": edge_val})
            total_edge_pct += edge_val

        if over_key in edges:
            edge_val = edges[over_key]
            edge_candidates.append({"side": "over", "edge_pct": edge_val})
            total_edge_pct += edge_val

        if total_key in edges:
            edge_val = edges[total_key]
            edge_candidates.append({"side": "total", "edge_pct": edge_val})
            total_edge_pct += edge_val

    # Sport-specific reasoning
    if "nba" in sport:
        if half in ("first_quarter",):
            reasoning_parts.append(
                "NBA first quarters feature structured play, starters feeling out "
                "matchups, and lower pace. Historical data shows 1Q unders are "
                "slightly underpriced."
            )
        if half in ("third_quarter",):
            reasoning_parts.append(
                "NBA third quarters often see scoring runs as teams make halftime "
                "adjustments. The over can have slight value here."
            )
        if half == "second":
            reasoning_parts.append(
                "NBA second halves feature more lineup variation, strategic fouling "
                "late in close games (increases scoring), and garbage time in blowouts."
            )

    elif "nfl" in sport:
        if half == "first":
            reasoning_parts.append(
                "NFL first halves tend slightly lower-scoring. Teams are conservative "
                "early, especially in playoff/primetime games."
            )
        if half == "second":
            reasoning_parts.append(
                "NFL second half totals are less efficiently priced. Books have less "
                "model infrastructure for mid-game repricing. Look for stale 2H lines "
                "that don't account for first-half game flow."
            )
        if half == "first_quarter":
            reasoning_parts.append(
                "NFL first quarters are the lowest-scoring period. Scripted drives, "
                "conservative play calling, and feel-out possessions."
            )

    elif "mlb" in sport:
        if half in ("first_5",):
            if is_ace_pitching is True:
                reasoning_parts.append(
                    "Ace on the mound for first 5 innings. Starters dominate F5 scoring. "
                    "F5 under may be underpriced because full-game total includes "
                    "bullpen innings where scoring typically increases."
                )
                edge_candidates.append({"side": "under", "edge_pct": 0.025})
                total_edge_pct += 0.025
            elif is_ace_pitching is False:
                reasoning_parts.append(
                    "Weaker starter pitching F5. Full-game line may underweight how "
                    "much damage occurs before the bullpen arrives. F5 over can be "
                    "value when a bad starter is on the mound."
                )
                edge_candidates.append({"side": "over", "edge_pct": 0.020})
                total_edge_pct += 0.020
            else:
                reasoning_parts.append(
                    "MLB F5 innings are dominated by the starting pitcher matchup. "
                    "Full-game totals blend starter and bullpen, but F5 isolates "
                    "the starter. The F5/full-game ratio varies dramatically by "
                    "pitching quality."
                )

    if not reasoning_parts:
        reasoning_parts.append(
            f"Projected {half} {market} line based on historical scoring distribution "
            f"({fraction:.1%} of full game scoring occurs in the {half})."
        )

    # Scoring distribution variance — halves and quarters have higher variance
    # per unit time than full games, which means the market needs more vig
    # to compensate, which means there's more room for mispricing.
    variance_multiplier = 1.0
    if "quarter" in half or "period" in half:
        variance_multiplier = 1.8  # Quarter results are much more variable
    elif "half" in half or half in ("first", "second", "first_5", "last_4"):
        variance_multiplier = 1.3

    return {
        "full_game_line": full_game_line,
        "projected_half_line": projected_line,
        "naive_half_line": naive_line if market == "totals" else round(full_game_line * 0.5 * 2) / 2.0,
        "scoring_fraction": round(fraction, 3),
        "sport": sport,
        "half": half,
        "market": market,
        "edge_vs_book_half": round(total_edge_pct, 4),
        "edge_candidates": edge_candidates,
        "variance_multiplier": round(variance_multiplier, 2),
        "reasoning": " ".join(reasoning_parts),
        "recommendation": (
            f"Look at {edge_candidates[0]['side']} on the {half} "
            f"(~{edge_candidates[0]['edge_pct']:.1%} historical edge)"
            if edge_candidates
            else f"No strong historical edge on {half} {market} for {sport}. "
                 f"Use projected line {projected_line} as a fair-value benchmark."
        ),
    }


