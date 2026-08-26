"""
Facade — re-exports everything from the tools.psych package.

The original monolithic market_psychology module was split into focused
submodules; this file keeps the public import path stable.
"""

import logging

from tools.psych import *  # noqa: F401,F403
from tools.psych import __all__  # noqa: F401
from tools.psych._utils import _prob_to_american  # noqa: F401
from tools.psych.attention import attention_arbitrage  # noqa: F401
from tools.psych.closing_line import predict_closing_line  # noqa: F401
from tools.psych.constants import (  # noqa: F401
    ATTENTION_WEIGHTS,
    HALF_QUARTER_EDGES,
    LINE_MOVEMENT_VELOCITY,
    NBA_SPREAD_SHADE,
    NBA_TOTAL_SHADE,
    NFL_MARGIN_FREQ,
    NFL_SPREAD_SHADE,
    SCORING_DISTRIBUTION,
)
from tools.psych.futures import (  # noqa: F401
    _bayesian_weight,
    _estimate_futures_vig,
    _expected_odds_improvement,
    _win_rate_from_implied,
    futures_efficiency,
    optimal_hedge_time,
)
from tools.psych.half_markets import half_market_adjustment  # noqa: F401
from tools.psych.shading import _shading_explanation, detect_number_shading  # noqa: F401
from tools.psych.trap_lines import detect_trap_line  # noqa: F401

from typing import Optional


logger = logging.getLogger("callisto.market_psychology")


def full_market_psychology(
    games: list[dict],
    sport: str,
    current_events: Optional[list[dict]] = None,
) -> dict:
    """
    Run all market psychology analyses on a set of games.

    This is the main entry point — orchestrate all seven modules for a complete
    picture of market psychology dynamics.
    """
    results = {
        "sport": sport,
        "games_analyzed": len(games),
    }

    # 1. Number shading scan
    shading_findings = []
    for game in games:
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] not in ("spreads", "totals"):
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    if point is None:
                        continue
                    shade = detect_number_shading(
                        spread=point,
                        sport=sport,
                        market=mkt["key"],
                        book_price=o.get("price"),
                    )
                    if shade["is_shaded"]:
                        shade["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
                        shade["team"] = o.get("name", "")
                        shade["bookmaker"] = bm.get("title", "")
                        shading_findings.append(shade)

    results["number_shading"] = shading_findings
    if shading_findings:
        logger.info(f"Number shading: found {len(shading_findings)} shaded lines")

    # 6. Attention arbitrage (if events provided)
    if current_events:
        results["attention_arbitrage"] = attention_arbitrage(current_events)
    else:
        results["attention_arbitrage"] = {
            "recommendation": "PROVIDE_EVENTS",
            "note": "Pass current_events for attention arbitrage analysis.",
        }

    return results
