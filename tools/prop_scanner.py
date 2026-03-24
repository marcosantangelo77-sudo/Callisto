"""
Prop edge scanner — single-call pipeline for player prop analysis.

This tool does what a sharp bettor does manually:
1. Pull player props from all available books
2. Devig each book's O/U independently (remove vig)
3. Average fair probabilities across books (consensus)
4. Compare consensus fair value to target book's implied price
5. Flag edges above threshold with EV and Kelly sizing

The Architect calls this ONE tool instead of chaining 5 separate calls.
This is what makes Callisto autonomous — the reasoning is in the math,
not in the LLM's ability to chain tool calls.
"""

import logging
from typing import Optional

from tools.odds_api import (
    get_player_props,
    calculate_implied_probability,
    calculate_ev,
)
from tools.devig import power_devig, multiplicative_devig
from tools.math_utils import american_to_decimal
from tools.edge_confidence import score_edge

logger = logging.getLogger("callisto.prop_scanner")

# Minimum books required for reliable cross-reference
MIN_BOOKS = 2
# Default edge threshold for flagging
DEFAULT_EDGE_THRESHOLD = 0.015  # 1.5%


async def scan_props_ev(
    sport: str,
    event_id: str,
    target_book: str = "draftkings",
    edge_threshold: float = DEFAULT_EDGE_THRESHOLD,
    prop_markets: str = "player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists",
) -> dict:
    """
    Full prop edge scan — pull, devig, compare, flag.

    Returns actionable edges on the target book with EV and Kelly sizing.
    Only compares same-line props (different line numbers across books are skipped
    because averaging different lines produces invalid fair values).

    Args:
        sport: Sport key (e.g., 'basketball_nba')
        event_id: The Odds API event ID
        target_book: Book key to find edges on (default: draftkings)
        edge_threshold: Minimum edge to flag (default: 1.5%)
        prop_markets: Comma-separated prop market keys

    Returns:
        Dict with edges list, scan metadata, and all devigged lines.
    """
    # Step 1: Pull props
    props_data = await get_player_props(sport, event_id, prop_markets=prop_markets)
    if props_data.get("error"):
        return {"error": props_data["error"], "edges": []}

    bookmakers = props_data.get("bookmakers", [])
    if not bookmakers:
        return {"error": "No bookmaker data returned", "edges": []}

    # Step 2: Organize by (player, market, line) -> book -> {Over, Under}
    prop_lines = {}
    book_names = {}

    for bk in bookmakers:
        bk_key = bk["key"]
        bk_name = bk["title"]
        book_names[bk_key] = bk_name

        for mkt in bk.get("markets", []):
            mkt_key = mkt["key"]
            for outcome in mkt.get("outcomes", []):
                player = outcome.get("description", "Unknown")
                line = outcome.get("point")
                side = outcome["name"]  # "Over" or "Under"
                price = outcome["price"]

                key = (player, mkt_key, line)
                if key not in prop_lines:
                    prop_lines[key] = {}
                if bk_key not in prop_lines[key]:
                    prop_lines[key][bk_key] = {}
                prop_lines[key][bk_key][side] = price

    # Step 3: Devig each book and find edges on target
    edges = []
    total_scanned = 0
    target_found = False

    for (player, mkt_key, line), books in prop_lines.items():
        # Must have target book with both sides
        if target_book not in books:
            continue
        target_data = books[target_book]
        if "Over" not in target_data or "Under" not in target_data:
            continue

        target_found = True
        target_over = target_data["Over"]
        target_under = target_data["Under"]

        # Devig all books that have SAME line with both sides
        fair_overs = []
        fair_unders = []
        book_details = []

        for bk_key, bk_data in books.items():
            if "Over" not in bk_data or "Under" not in bk_data:
                continue
            try:
                dec_o = american_to_decimal(bk_data["Over"])
                dec_u = american_to_decimal(bk_data["Under"])
                fair_list, _ = power_devig([dec_o, dec_u])
                fair_o, fair_u = fair_list[0], fair_list[1]
                fair_overs.append(fair_o)
                fair_unders.append(fair_u)
                book_details.append({
                    "book": book_names.get(bk_key, bk_key),
                    "over_price": bk_data["Over"],
                    "under_price": bk_data["Under"],
                    "fair_over": round(fair_o, 4),
                    "fair_under": round(fair_u, 4),
                })
            except (ValueError, ZeroDivisionError):
                continue

        if len(fair_overs) < MIN_BOOKS:
            continue

        total_scanned += 1

        # Consensus fair probability (average across all books' devigged values)
        avg_fair_over = sum(fair_overs) / len(fair_overs)
        avg_fair_under = sum(fair_unders) / len(fair_unders)

        target_over_implied = calculate_implied_probability(target_over)
        target_under_implied = calculate_implied_probability(target_under)

        # Check both Over and Under for edges
        for side, fair, target_price, target_implied in [
            ("Over", avg_fair_over, target_over, target_over_implied),
            ("Under", avg_fair_under, target_under, target_under_implied),
        ]:
            edge = fair - target_implied
            if edge < edge_threshold:
                continue

            ev = calculate_ev(fair, target_price, 100)
            market_label = mkt_key.replace("player_", "")

            # AGP confidence scoring
            confidence = score_edge(
                edge_pct=round(edge * 100, 2),
                books_compared=len(fair_overs),
                book_names=[bd["book"] for bd in book_details],
                market=mkt_key,
                is_live=False,
            )

            edges.append({
                "player": player,
                "market": market_label,
                "line": line,
                "side": side,
                "target_book": book_names.get(target_book, target_book),
                "target_price": target_price,
                "target_implied": round(target_implied, 4),
                "fair_probability": round(fair, 4),
                "edge_pct": round(edge * 100, 2),
                "ev_per_100": round(ev["expected_value"], 2),
                "kelly_fraction": round(ev["kelly_fraction"], 4),
                "kelly_stake_pct": round(ev["kelly_fraction"] * 100, 2),
                "books_compared": len(fair_overs),
                "book_details": book_details,
                "actionable": edge >= 0.02,
                "confidence": {
                    "score": confidence.score,
                    "tier": confidence.tier,
                    "source_class": confidence.source_class,
                    "ceiling": confidence.ceiling,
                    "reasoning": confidence.reasoning,
                },
            })

    # Sort by edge descending
    edges.sort(key=lambda x: x["edge_pct"], reverse=True)

    return {
        "sport": sport,
        "event_id": event_id,
        "target_book": book_names.get(target_book, target_book),
        "props_scanned": total_scanned,
        "books_available": list(book_names.values()),
        "edge_threshold_pct": round(edge_threshold * 100, 2),
        "edges_found": len(edges),
        "actionable_edges": sum(1 for e in edges if e["actionable"]),
        "edges": edges,
        "credits": props_data.get("credits", {}),
        "target_book_found": target_found,
    }
