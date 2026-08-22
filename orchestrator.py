"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
Uses Brave Search for real evidence gathering. Enforces honest confidence calibration.

Optimizations: parallel Brave searches, pipelined model loading, compressed prompts.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiosqlite

from agp import (
    AGPSealRefused,
    AGPSession,
    AGPViolation,
    Contradiction,
    Domain,
    EMPTY_SYNTHESIS_MARKER,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
    ConfidenceTier,
)
from agp.thresholds import (
    CONTRADICTION_PENALTY,
    DB_CONFIDENCE_FLOOR,
    ESCALATION_THRESHOLD,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
from inference import (
    get_architect,
    get_manager,
    get_sentinel,
    execute_function_call,
    _parse_json_response,
    escalate_with_ladder,
)
from memory import MemoryStore
from tools.search import web_search
from tools.claude_code import claude_code_available, is_available as claude_available
from tools.odds_api import (
    get_odds as odds_get_odds,
    get_scores as odds_get_scores,
    get_event_odds as odds_get_event_odds,
    get_alternate_lines as odds_get_alternate_lines,
    get_player_props as odds_get_player_props,
    find_best_line,
    detect_line_movement,
    calculate_ev,
    calculate_implied_probability,
    get_credit_status as odds_credit_status,
)
from tools.edge_scanner import full_edge_scan
from tools.parlay_scanner import (
    parlay_odds_from_legs,
    find_correlated_parlay_edges,
    analyze_prop_mispricing,
    analyze_live_overreaction,
)
from tools.contextual_data import get_injuries, get_scoreboard, get_team_roster
from tools.line_gaps import scan_line_gaps, scan_prop_gaps
from tools.prop_scanner import scan_props_ev
from tools.clv_tracker import CLVTracker
from tools.edge_confidence import score_edge, score_parlay
from tools.boost_evaluator import (
    devig_multiplicative,
    evaluate_fixed_boost,
    evaluate_percentage_boost,
    evaluate_purchased_boost,
    evaluate_free_bet,
    calculate_hedge,
    find_optimal_boost_target,
)
from tools.hermes_memory import get_hermes_memory
from tools.cache_manager import get_cache_manager
from tools.devig import devig_market, devig_american, devig_pinnacle, devig_retail
from tools.sim import nba_game_sim, nfl_game_sim, poisson_game, player_prop_sim, sim_from_odds, compare_sim_to_book
from tools.ev import evaluate_edge, ev_binary, ev_with_push, ev_free_bet, EDGE_THRESHOLDS
from tools.sizing import bet_size_american, best_price
from tools.sgp import evaluate_sgp, correlated_parlay_prob
from tools.math_utils import american_to_decimal, american_to_implied, implied_scores

logger = logging.getLogger("callisto.orchestrator")

# Brave Search tool schema for Ollama native tool calling
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the web for current information.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "count": {"type": "integer", "description": "Number of results (1-20)", "default": 5},
            },
            "required": ["query"],
        },
    },
}

# Confidence ceilings + escalation threshold live in agp.thresholds
# (imported above). MAX_CONFIDENCE_BY_SOURCE, MAX_CONFIDENCE_NO_TOOL, and
# ESCALATION_THRESHOLD are re-exported from that module.
MAX_TOOL_CALL_ROUNDS = 3

# Claude Code tool schema for Ollama native tool calling
CLAUDE_CODE_TOOL = {
    "type": "function",
    "function": {
        "name": "claude_code",
        "description": (
            "Escalate to Claude Code (Opus 4.6) for frontier-quality analysis. "
            "Use when local reasoning is insufficient or high-confidence PRIMARY evidence is needed. "
            "This is a SOTA model — use sparingly for critical questions only."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The analysis request for Claude Code",
                },
                "system_context": {
                    "type": "string",
                    "description": "Context: domain, evidence gathered so far, specific question",
                    "default": "",
                },
            },
            "required": ["prompt"],
        },
    },
}

# Odds API tool schemas for Ollama native tool calling
ODDS_GET_ODDS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_odds",
        "description": (
            "Get live and upcoming odds from 40+ bookmakers. "
            "Use to detect line movements and find cross-bookmaker edges. "
            "Credit cost: len(markets) * len(regions)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key, e.g. 'basketball_ncaab', 'americanfootball_nfl'", "default": "basketball_ncaab"},
                "regions": {"type": "string", "description": "Bookmaker regions, comma-separated: 'us', 'us,uk'", "default": "us"},
                "markets": {"type": "string", "description": "Market types: 'h2h', 'spreads', 'totals' — comma-separated", "default": "h2h,spreads,totals"},
                "odds_format": {"type": "string", "description": "'american' or 'decimal'", "default": "american"},
            },
            "required": [],
        },
    },
}

ODDS_GET_SCORES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_scores",
        "description": "Get live scores and recently completed games. Free (0 credits) for in-season sports.",
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key", "default": "basketball_ncaab"},
                "days_from": {"type": "integer", "description": "Days back for completed games (1-3)", "default": 1},
            },
            "required": [],
        },
    },
}

ODDS_GET_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "get_event_odds",
        "description": "Get odds for a single event by ID. Use for tracking line movement on a specific game.",
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key"},
                "event_id": {"type": "string", "description": "Event ID from get_odds()"},
                "regions": {"type": "string", "description": "Bookmaker regions", "default": "us"},
                "markets": {"type": "string", "description": "Market types", "default": "h2h,spreads,totals"},
            },
            "required": ["sport", "event_id"],
        },
    },
}

ODDS_CALCULATE_EV_TOOL = {
    "type": "function",
    "function": {
        "name": "calculate_ev",
        "description": (
            "Calculate expected value and Kelly criterion sizing for a bet. "
            "+EV = edge exists. This determines whether a bet is worth placing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "probability": {"type": "number", "description": "Your estimated true probability (0.0-1.0)"},
                "american_odds": {"type": "integer", "description": "The line being offered (e.g. -110, +150)"},
                "stake": {"type": "number", "description": "Bet amount in dollars", "default": 100},
            },
            "required": ["probability", "american_odds"],
        },
    },
}

ODDS_ALT_LINES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_alternate_lines",
        "description": (
            "Get alternate spreads and totals for a specific event. "
            "Foundation for parlay construction — each alternate is a different risk/reward profile."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key"},
                "event_id": {"type": "string", "description": "Event ID from get_odds()"},
                "regions": {"type": "string", "default": "us"},
            },
            "required": ["sport", "event_id"],
        },
    },
}

ODDS_PLAYER_PROPS_TOOL = {
    "type": "function",
    "function": {
        "name": "get_player_props",
        "description": (
            "Get player prop lines (points, rebounds, assists, threes) for a specific event. "
            "Player props have the biggest edges — books price on season averages but "
            "context (injuries, matchups, role changes) creates systematic mispricings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key"},
                "event_id": {"type": "string", "description": "Event ID"},
                "prop_markets": {
                    "type": "string",
                    "description": "Comma-separated prop markets: player_points,player_rebounds,player_assists,player_threes",
                    "default": "player_points,player_rebounds,player_assists",
                },
            },
            "required": ["sport", "event_id"],
        },
    },
}

EDGE_SCAN_TOOL = {
    "type": "function",
    "function": {
        "name": "edge_scan",
        "description": (
            "Run full edge scan: cross-book divergence, sharp money signals, "
            "low-vig opportunities, and parlay correlations. "
            "Call after getting odds to find exploitable mispricings."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key to scan", "default": "basketball_ncaab"},
            },
            "required": [],
        },
    },
}

INJURIES_TOOL = {
    "type": "function",
    "function": {
        "name": "get_injuries",
        "description": "Get current injury report from ESPN. Injuries create fast-decaying edges when books are slow to adjust.",
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key", "default": "basketball_ncaab"},
            },
            "required": [],
        },
    },
}

ROSTER_TOOL = {
    "type": "function",
    "function": {
        "name": "get_team_roster",
        "description": (
            "Get CURRENT team roster from ESPN. ALWAYS use this before analyzing team-specific "
            "player data to verify who is actually on the team RIGHT NOW. Do NOT assume rosters "
            "from previous seasons — players get traded."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key (e.g. basketball_nba)"},
                "team_id": {"type": "string", "description": "ESPN team ID (e.g. 'bos' for Celtics, 'lal' for Lakers)"},
            },
            "required": ["sport", "team_id"],
        },
    },
}

SCOREBOARD_TOOL = {
    "type": "function",
    "function": {
        "name": "get_scoreboard",
        "description": "Get live scoreboard — scores, game status, period/clock. Use for live betting overreaction detection.",
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key", "default": "basketball_ncaab"},
            },
            "required": [],
        },
    },
}

LINE_GAPS_TOOL = {
    "type": "function",
    "function": {
        "name": "scan_line_gaps",
        "description": (
            "Scan alternate lines for gaps in a bookmaker's offerings. "
            "When a book skips a point (offers 15+ and 17+ but not 16+), "
            "interpolate fair value and cross-reference other books for edges."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key", "default": "basketball_ncaab"},
                "event_id": {"type": "string", "description": "Event ID to scan for gaps"},
            },
            "required": ["sport", "event_id"],
        },
    },
}

BOOST_EVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_boost",
        "description": (
            "Evaluate a profit boost for +EV. Devigs the market, compares boosted odds "
            "to fair probability, calculates edge and Kelly sizing. "
            "Supports fixed boosts, percentage tokens, and free bets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "boost_type": {
                    "type": "string",
                    "description": "'fixed', 'percentage', 'free_bet', or 'purchased' (for Fanatics-style purchasable boosts)",
                    "default": "fixed",
                },
                "boost_cost": {"type": "number", "description": "Dollar cost to purchase the boost (for 'purchased' type)"},
                "book": {"type": "string", "description": "Sportsbook name (for 'purchased' type)", "default": "Fanatics"},
                "boosted_odds": {"type": "integer", "description": "Boosted American odds (for fixed boost)"},
                "base_odds": {"type": "integer", "description": "Unboosted odds (for percentage boost)"},
                "boost_pct": {"type": "number", "description": "Boost percentage (for percentage boost)"},
                "fair_probability": {"type": "number", "description": "True probability (0.0-1.0)"},
                "odds_for": {"type": "integer", "description": "Side A odds for devigging"},
                "odds_against": {"type": "integer", "description": "Side B odds for devigging"},
                "max_stake": {"type": "number", "description": "Maximum wager", "default": 100},
                "description": {"type": "string", "description": "Boost description", "default": ""},
            },
            "required": ["boost_type"],
        },
    },
}

PROP_SCANNER_TOOL = {
    "type": "function",
    "function": {
        "name": "scan_props_ev",
        "description": (
            "Full player prop edge scan — pulls props from all books, devigs each book's "
            "O/U independently, averages fair probabilities, and flags edges on the target book. "
            "Returns actionable edges with EV and Kelly sizing. Call this FIRST for any prop analysis."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key (e.g., 'basketball_nba')"},
                "event_id": {"type": "string", "description": "The Odds API event ID"},
                "target_book": {"type": "string", "description": "Book to find edges on", "default": "draftkings"},
                "edge_threshold": {"type": "number", "description": "Min edge to flag (0.015 = 1.5%)", "default": 0.015},
            },
            "required": ["sport", "event_id"],
        },
    },
}

RECORD_BET_TOOL = {
    "type": "function",
    "function": {
        "name": "record_bet",
        "description": (
            "Record a recommended bet for CLV tracking. Call this when you identify a +EV play "
            "so the system tracks placement line vs closing line over time. "
            "CLV is the single most important metric for proving edge exists."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sport": {"type": "string", "description": "Sport key"},
                "game_description": {"type": "string", "description": "e.g., 'Nets @ Kings'"},
                "team": {"type": "string", "description": "Team or player name"},
                "market": {"type": "string", "description": "Market type (spreads, h2h, player_points, etc.)"},
                "bookmaker": {"type": "string", "description": "Bookmaker name"},
                "placement_odds": {"type": "integer", "description": "American odds at placement"},
                "placement_point": {"type": "number", "description": "Point spread/total/prop line"},
                "stake": {"type": "number", "description": "Wager amount", "default": 100},
                "event_id": {"type": "string", "description": "Event ID for closing line matching"},
                "edge_estimate": {"type": "number", "description": "Estimated edge (e.g., 0.03 = 3%)"},
                "notes": {"type": "string", "description": "Analysis notes", "default": ""},
            },
            "required": ["sport", "game_description", "team", "market", "bookmaker", "placement_odds"],
        },
    },
}

# ──────────────────────────────────────────
# NEW FRAMEWORK TOOLS (Phase 2-6)
# ──────────────────────────────────────────

DEVIG_TOOL = {
    "type": "function",
    "function": {
        "name": "devig_market",
        "description": (
            "Remove bookmaker vig to find true fair probabilities. "
            "Auto-selects method: power for retail (DK/Fanatics), "
            "multiplicative for sharp (Pinnacle), shin for 3-way markets."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "side_a_american": {"type": "integer", "description": "Side A American odds"},
                "side_b_american": {"type": "integer", "description": "Side B American odds"},
                "method": {"type": "string", "description": "'auto', 'power', 'multiplicative', 'shin', 'additive'", "default": "auto"},
            },
            "required": ["side_a_american", "side_b_american"],
        },
    },
}

SIM_GAME_TOOL = {
    "type": "function",
    "function": {
        "name": "simulate_game",
        "description": (
            "Run Monte Carlo simulation from a game's spread and total. "
            "Returns win/cover/over probabilities for all standard lines. "
            "NBA: possession-based 100K sims. NFL: negative binomial. MLB/NHL/Soccer: Poisson."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "spread": {"type": "number", "description": "Point spread (negative = home favored)"},
                "total": {"type": "number", "description": "Over/under total"},
                "sport": {"type": "string", "description": "Sport: nba, ncaab, nfl, ncaaf, mlb, nhl, soccer"},
            },
            "required": ["spread", "total", "sport"],
        },
    },
}

SIM_PROP_TOOL = {
    "type": "function",
    "function": {
        "name": "simulate_prop",
        "description": (
            "Simulate a player prop with context adjustments. "
            "Models per-minute rate x projected minutes with pace, matchup, and usage factors. "
            "THIS IS WHERE THE EDGE LIVES — books price on season averages, we model context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "stat_per_min": {"type": "number", "description": "Player's per-minute rate for this stat"},
                "stat_per_min_std": {"type": "number", "description": "Game-to-game variance in rate", "default": 0.15},
                "projected_minutes": {"type": "number", "description": "Context-adjusted minutes this game"},
                "minutes_std": {"type": "number", "description": "Minutes variance", "default": 4.0},
                "pace_factor": {"type": "number", "description": "Matchup pace / league avg (1.0 = avg)", "default": 1.0},
                "defense_factor": {"type": "number", "description": "Opponent def vs position (>1 = weaker)", "default": 1.0},
                "usage_factor": {"type": "number", "description": "Usage shift from injuries (>1 = more)", "default": 1.0},
                "stat_name": {"type": "string", "description": "Stat name (points, rebounds, etc.)", "default": "points"},
            },
            "required": ["stat_per_min", "projected_minutes"],
        },
    },
}

EVALUATE_EDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_edge",
        "description": (
            "Full edge evaluation: compare our fair prob to book odds. "
            "Returns EV, edge, confidence threshold check, and actionability. "
            "Handles push probability for whole-number spreads."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "fair_prob": {"type": "number", "description": "Our estimated true probability (0-1)"},
                "book_odds_american": {"type": "integer", "description": "Book's American odds"},
                "confidence": {"type": "string", "description": "'high', 'medium', 'low', or 'boost'", "default": "medium"},
                "p_push": {"type": "number", "description": "Push probability (0 for half-point)", "default": 0.0},
            },
            "required": ["fair_prob", "book_odds_american"],
        },
    },
}

BET_SIZE_TOOL = {
    "type": "function",
    "function": {
        "name": "bet_size",
        "description": (
            "Kelly criterion bet sizing with uncertainty adjustment. "
            "Scales by info_ratio = edge/noise. Quarter-Kelly default. "
            "Never risks more than noise level justifies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bankroll": {"type": "number", "description": "Current bankroll"},
                "fair_prob": {"type": "number", "description": "True probability (0-1)"},
                "book_odds_american": {"type": "integer", "description": "Book's American odds"},
                "confidence": {"type": "string", "description": "'high', 'medium', 'low'"},
                "max_wager": {"type": "number", "description": "Max bet limit"},
                "p_push": {"type": "number", "description": "Push probability", "default": 0.0},
            },
            "required": ["bankroll", "fair_prob", "book_odds_american", "confidence"],
        },
    },
}

BEST_PRICE_TOOL = {
    "type": "function",
    "function": {
        "name": "best_price",
        "description": "Compare DraftKings vs Fanatics odds and return the better price. Free edge from having two books.",
        "parameters": {
            "type": "object",
            "properties": {
                "dk_odds_american": {"type": "integer", "description": "DraftKings American odds"},
                "fan_odds_american": {"type": "integer", "description": "Fanatics American odds"},
            },
            "required": ["dk_odds_american", "fan_odds_american"],
        },
    },
}

SGP_EVAL_TOOL = {
    "type": "function",
    "function": {
        "name": "evaluate_sgp",
        "description": (
            "Evaluate a Same Game Parlay for correlation edge. "
            "Books price SGP legs as independent — when positively correlated, "
            "true joint probability is higher than book's price = +EV."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "legs": {
                    "type": "array",
                    "description": "Array of {type: str, fair_prob: float} for each leg",
                    "items": {"type": "object"},
                },
                "sport": {"type": "string", "description": "Sport key"},
                "book_sgp_decimal": {"type": "number", "description": "Book's SGP decimal odds"},
            },
            "required": ["legs", "sport", "book_sgp_decimal"],
        },
    },
}

WARM_CACHE_TOOL = {
    "type": "function",
    "function": {
        "name": "query_warm_cache",
        "description": (
            "Query historical data summaries (warm cache). "
            "Types: clv_summary, recent_signals, model_calibration, boost_history. "
            "Returns aggregated summaries, NOT raw data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {"type": "string", "description": "clv_summary, recent_signals, model_calibration, boost_history"},
                "days_back": {"type": "integer", "description": "Days of history", "default": 30},
                "sport": {"type": "string", "description": "Filter by sport"},
                "book": {"type": "string", "description": "Filter by book"},
                "n": {"type": "integer", "description": "Number of results (for recent_signals)", "default": 10},
            },
            "required": ["query_type"],
        },
    },
}

# All available tool schemas — the SPORTS plugin's payload. Registered into
# the ToolRegistry below; sessions get them only when the query/domain matches.
ODDS_TOOLS = [
    ODDS_GET_ODDS_TOOL, ODDS_GET_SCORES_TOOL, ODDS_GET_EVENT_TOOL,
    ODDS_CALCULATE_EV_TOOL, ODDS_ALT_LINES_TOOL, ODDS_PLAYER_PROPS_TOOL,
    EDGE_SCAN_TOOL, INJURIES_TOOL, ROSTER_TOOL, SCOREBOARD_TOOL,
    LINE_GAPS_TOOL, BOOST_EVAL_TOOL,
    PROP_SCANNER_TOOL, RECORD_BET_TOOL,
    # New framework tools
    DEVIG_TOOL, SIM_GAME_TOOL, SIM_PROP_TOOL,
    EVALUATE_EDGE_TOOL, BET_SIZE_TOOL, BEST_PRICE_TOOL,
    SGP_EVAL_TOOL, WARM_CACHE_TOOL,
]

# Hermes-style tool prompt for models without native tool support
HERMES_TOOL_PROMPT = (
    "\nAvailable tools (output <tool_call> to use):\n"
    "SEARCH: web_search(query)\n"
    "ROSTER: get_team_roster(sport, team_id) — ALWAYS verify current roster before team analysis. Players get traded.\n"
    "ODDS: get_odds(sport), get_scores(sport), get_event_odds(sport, event_id), get_player_props(sport, event_id)\n"
    "DEVIG: devig_market(side_a_american, side_b_american) — remove vig, find true fair probabilities\n"
    "SIM: simulate_game(spread, total, sport) — Monte Carlo simulation for any game\n"
    "SIM: simulate_prop(stat_per_min, projected_minutes, pace_factor, defense_factor) — player prop sim\n"
    "EV: evaluate_edge(fair_prob, book_odds_american, confidence) — check if edge is actionable\n"
    "SIZE: bet_size(bankroll, fair_prob, book_odds_american, confidence) — Kelly sizing\n"
    "PRICE: best_price(dk_odds_american, fan_odds_american) — compare DK vs Fanatics\n"
    "SGP: evaluate_sgp(legs, sport, book_sgp_decimal) — correlation-adjusted parlay edge\n"
    "BOOST: evaluate_boost(boost_type, boosted_odds, fair_probability) — profit boost evaluation\n"
    "CACHE: query_warm_cache(query_type) — historical CLV, calibration, boost data\n"
    "RECORD: record_bet(sport, game_description, team, market, bookmaker, placement_odds)\n"
    "Example: "
    '<tool_call>{"name":"devig_market","arguments":{"side_a_american":-145,"side_b_american":125}}</tool_call>'
)

import re as _re

from tools.domain_registry import get_tool_registry
from tools.domains.sports import build_sports_plugin


def _default_registry():
    """Build the process-wide ToolRegistry. Registration is the extension
    point: adding a domain = register a plugin here, never edit the loop."""
    global _registry_seeded
    if not _registry_seeded:
        get_tool_registry().core_tools[:] = [WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL]
        get_tool_registry().register(
            build_sports_plugin(ODDS_TOOLS, _execute_sports_tool)
        )
        _registry_seeded = True
    return get_tool_registry()


_registry_seeded = False


async def _execute_sports_tool(name: str, arguments: dict):
    """Sports tool dispatcher (moved verbatim from Orchestrator._execute_tool;
    it uses no instance state). Lives behind the plugin boundary now."""
    return await _sports_tool_dispatch(name, arguments)


# (Freshness patterns moved to tools/domains/sports.py — plugin-supplied now.)


def _detect_freshness(query: str) -> Optional[str]:
    """Return Brave freshness filter for freshness-sensitive queries.

    Freshness rules are supplied by registered DomainPlugins (formerly a
    hardcoded team-name regex). A security query mentioning "Warriors" no
    longer gets mis-freshened unless a plugin claims it.
    """
    return _default_registry().freshness_for(query)


# Compact JSON serialization — fewer tokens in prompts
_json_compact = lambda obj: json.dumps(obj, separators=(",", ":"))


def _safe_parse(response: dict, fallback=None):
    """Extract parsed JSON from inference response, with fallback.

    Normalizes list-wrapped responses: if the model returns a JSON array
    containing a single dict, unwrap it automatically.
    """
    parsed = response.get("parsed_json")
    if parsed is None:
        return fallback
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        return parsed[0]
    return parsed


def _parse_domain(text: str) -> Domain:
    """Parse a domain from text, defaulting to GENERAL."""
    text_upper = text.upper().strip()
    for domain in Domain:
        if domain.value in text_upper:
            return domain
    return Domain.GENERAL


def _clamp_confidence(score: float, best_source_class: str = "INFERRED") -> float:
    """Enforce confidence ceiling based on the best source class available.

    This is the hard enforcement layer — code, not policy.
    A model cannot self-report higher confidence than its evidence warrants.
    """
    score = max(0.0, min(1.0, score))
    ceiling = MAX_CONFIDENCE_BY_SOURCE.get(best_source_class, MAX_CONFIDENCE_NO_TOOL)
    score = min(score, ceiling)
    return round(score, 2)


def _best_source_class(evidence: list, used_tools: bool) -> str:
    """Determine the best (most authoritative) source class from evidence."""
    if not evidence:
        # Tools used but no evidence collected → SECONDARY (not INFERRED)
        return "SECONDARY" if used_tools else "INFERRED"
    rank = {"PRIMARY": 4, "SECONDARY": 3, "SIGNAL": 2, "INFERRED": 1}
    best = "INFERRED"
    for ev in evidence:
        sc = ev.source_class.value if hasattr(ev, "source_class") else ev.get("source_class", "INFERRED")
        if rank.get(sc, 0) > rank.get(best, 0):
            best = sc
    return best


def _response_cites_urls(text: str) -> bool:
    """Does the Claude Code response contain at least one URL citation?

    Used to decide whether Claude's synthesis is grounded enough to be tiered
    as SECONDARY (web-corroborated) vs INFERRED (pure reasoning). A response
    that merely restates a conclusion without any source link is INFERRED —
    the 0.75 CORROBORATED ceiling is not available to it.
    """
    if not text:
        return False
    lowered = text.lower()
    return ("http://" in lowered) or ("https://" in lowered)


def _dedup_search_results(results: list[dict]) -> list[dict]:
    """Deduplicate search results by URL, keeping the first occurrence."""
    seen_urls = set()
    deduped = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)
    return deduped



async def _sports_tool_dispatch(name: str, arguments: dict):
    """Sports tool implementations (moved verbatim from the old
    Orchestrator._execute_tool chain). Module-level so the sports
    DomainPlugin can own them without importing Orchestrator."""
    if name == "get_odds":
        return await odds_get_odds(
            sport=arguments.get("sport", "basketball_ncaab"),
            regions=arguments.get("regions", "us"),
            markets=arguments.get("markets", "h2h,spreads,totals"),
            odds_format=arguments.get("odds_format", "american"),
        )
    if name == "get_scores":
        return await odds_get_scores(
            sport=arguments.get("sport", "basketball_ncaab"),
            days_from=arguments.get("days_from", 1),
        )
    if name == "get_event_odds":
        return await odds_get_event_odds(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            regions=arguments.get("regions", "us"),
            markets=arguments.get("markets", "h2h,spreads,totals"),
            odds_format=arguments.get("odds_format", "american"),
        )
    if name == "calculate_ev":
        return calculate_ev(
            probability=float(arguments.get("probability", 0.5)),
            american_odds=int(arguments.get("american_odds", -110)),
            stake=float(arguments.get("stake", 100)),
        )
    if name == "get_alternate_lines":
        return await odds_get_alternate_lines(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            regions=arguments.get("regions", "us"),
        )
    if name == "get_player_props":
        return await odds_get_player_props(
            sport=arguments.get("sport", ""),
            event_id=arguments.get("event_id", ""),
            prop_markets=arguments.get("prop_markets", "player_points,player_rebounds,player_assists"),
        )
    if name == "edge_scan":
        sport = arguments.get("sport", "basketball_ncaab")
        snapshot = await odds_get_odds(sport=sport)
        report = full_edge_scan(snapshot)
        # Attach AGP confidence scores to each detected edge
        for market_key in ["cross_book_spreads", "cross_book_h2h", "cross_book_totals"]:
            for edge in report.get(market_key, []):
                conf = score_edge(
                    edge_pct=round(edge.get("implied_range", 0) * 100, 2),
                    books_compared=edge.get("book_count", edge.get("num_bookmakers", 1)),
                    book_names=[edge.get("best_line", {}).get("bookmaker", "")],
                    market=market_key.replace("cross_book_", ""),
                    has_sharp_book=edge.get("sharp_consensus") is not None,
                )
                edge["confidence"] = {
                    "score": conf.score, "tier": conf.tier,
                    "source_class": conf.source_class, "reasoning": conf.reasoning,
                }
        return report
    if name == "get_injuries":
        return await get_injuries(sport=arguments.get("sport", "basketball_ncaab"))
    if name == "get_team_roster":
        return await get_team_roster(
            sport=arguments.get("sport", "basketball_nba"),
            team_id=arguments.get("team_id", ""),
        )
    if name == "get_scoreboard":
        return await get_scoreboard(sport=arguments.get("sport", "basketball_ncaab"))
    if name == "scan_line_gaps":
        sport = arguments.get("sport", "basketball_ncaab")
        event_id = arguments.get("event_id", "")
        alt_data = await odds_get_alternate_lines(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            return alt_data
        gaps = scan_line_gaps(
            alt_data.get("bookmakers", []),
            market_key="alternate_spreads",
        )
        prop_data = await odds_get_player_props(sport=sport, event_id=event_id)
        prop_gaps = []
        if not prop_data.get("error"):
            prop_gaps = scan_prop_gaps(prop_data)
        return {"line_gaps": gaps, "prop_gaps": prop_gaps}
    if name == "evaluate_boost":
        bt = arguments.get("boost_type", "fixed")
        fair_prob = arguments.get("fair_probability")
        # Auto-devig if fair_probability not given but odds_for/against provided
        if fair_prob is None:
            odds_for = arguments.get("odds_for", -110)
            odds_against = arguments.get("odds_against", -110)
            fair_prob, _ = devig_multiplicative(odds_for, odds_against)
        if bt == "fixed":
            return evaluate_fixed_boost(
                boosted_odds=int(arguments.get("boosted_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
            )
        elif bt == "percentage":
            return evaluate_percentage_boost(
                boost_pct=float(arguments.get("boost_pct", 20)),
                base_odds=int(arguments.get("base_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
            )
        elif bt == "free_bet":
            return evaluate_free_bet(
                free_bet_amount=float(arguments.get("max_stake", 100)),
                bet_odds=int(arguments.get("boosted_odds", arguments.get("base_odds", 200))),
                fair_probability=float(fair_prob),
                description=arguments.get("description", ""),
            )
        elif bt == "purchased":
            return evaluate_purchased_boost(
                boost_cost=float(arguments.get("boost_cost", 0)),
                boost_pct=float(arguments.get("boost_pct", 20)),
                base_odds=int(arguments.get("base_odds", -110)),
                fair_probability=float(fair_prob),
                max_stake=float(arguments.get("max_stake", 100)),
                description=arguments.get("description", ""),
                book=arguments.get("book", "Fanatics"),
            )
        return {"error": f"Unknown boost type: {bt}"}
    if name == "scan_props_ev":
        return await scan_props_ev(
            sport=arguments.get("sport", "basketball_nba"),
            event_id=arguments.get("event_id", ""),
            target_book=arguments.get("target_book", "draftkings"),
            edge_threshold=float(arguments.get("edge_threshold", 0.015)),
        )
    # ── New framework tools ──
    if name == "devig_market":
        return devig_american(
            side_a_american=int(arguments.get("side_a_american", -110)),
            side_b_american=int(arguments.get("side_b_american", -110)),
            method=arguments.get("method", "auto"),
        )
    if name == "simulate_game":
        return sim_from_odds(
            spread=float(arguments.get("spread", 0)),
            total=float(arguments.get("total", 0)),
            sport=arguments.get("sport", "nba"),
        )
    if name == "simulate_prop":
        return player_prop_sim(
            stat_per_min=float(arguments.get("stat_per_min", 0.5)),
            stat_per_min_std=float(arguments.get("stat_per_min_std", 0.15)),
            projected_minutes=float(arguments.get("projected_minutes", 30)),
            minutes_std=float(arguments.get("minutes_std", 4.0)),
            pace_factor=float(arguments.get("pace_factor", 1.0)),
            defense_factor=float(arguments.get("defense_factor", 1.0)),
            usage_factor=float(arguments.get("usage_factor", 1.0)),
            stat_name=arguments.get("stat_name", "points"),
        )
    if name == "evaluate_edge":
        return evaluate_edge(
            fair_prob=float(arguments.get("fair_prob", 0.5)),
            book_odds_american=int(arguments.get("book_odds_american", -110)),
            confidence=arguments.get("confidence", "medium"),
            p_push=float(arguments.get("p_push", 0.0)),
        )
    if name == "bet_size":
        return bet_size_american(
            bankroll=float(arguments.get("bankroll", 1000)),
            fair_prob=float(arguments.get("fair_prob", 0.5)),
            book_odds_american=int(arguments.get("book_odds_american", -110)),
            confidence=arguments.get("confidence", "medium"),
            max_wager=arguments.get("max_wager"),
            p_push=float(arguments.get("p_push", 0.0)),
        )
    if name == "best_price":
        return best_price(
            dk_odds_american=int(arguments.get("dk_odds_american", -110)),
            fan_odds_american=int(arguments.get("fan_odds_american", -110)),
        )
    if name == "evaluate_sgp":
        return evaluate_sgp(
            legs=arguments.get("legs", []),
            sport=arguments.get("sport", "nba"),
            book_sgp_decimal=float(arguments.get("book_sgp_decimal", 3.0)),
        )
    if name == "query_warm_cache":
        cm = get_cache_manager()
        kwargs = {}
        for k in ["days_back", "sport", "book", "n"]:
            if k in arguments and arguments[k] is not None:
                kwargs[k] = arguments[k]
        return await cm.get_warm_data(
            query_type=arguments.get("query_type", "clv_summary"),
            **kwargs,
        )
    if name == "record_bet":
        tracker = CLVTracker()
        await tracker.initialize()
        try:
            bet_id = await tracker.record_bet(
                sport=arguments.get("sport", ""),
                game_description=arguments.get("game_description", ""),
                team=arguments.get("team", ""),
                market=arguments.get("market", ""),
                bookmaker=arguments.get("bookmaker", ""),
                placement_odds=int(arguments.get("placement_odds", -110)),
                placement_point=arguments.get("placement_point"),
                stake=float(arguments.get("stake", 100)),
                event_id=arguments.get("event_id", ""),
                edge_estimate=arguments.get("edge_estimate"),
                notes=arguments.get("notes", ""),
            )
            return {"bet_id": bet_id, "status": "recorded", "message": f"Bet #{bet_id} recorded for CLV tracking"}
        finally:
            await tracker.close()

class Orchestrator:
    """Coordinates the 3-agent AGP session flow."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.architect = get_architect()
        self.manager = get_manager()
        self.sentinel = get_sentinel()
        # Registry: asyncio.Task → live AGPSession. Lets an external watcher
        # (the task_worker adaptive-timeout loop) inspect liveness without
        # changing run_session's return contract. Scoped to the asyncio Task
        # that invoked run_session so concurrent callers can't clobber each
        # other. Cleaned up in a finally: block inside run_session.
        self._active_sessions: dict = {}

    def active_session_for(self, task) -> Optional[AGPSession]:
        """Return the live AGPSession for a given asyncio.Task, or None.

        Used by api.py::task_worker to poll `last_progress_at`, `current_step`,
        and evidence counts for the adaptive-timeout extension logic. The
        Orchestrator instance is shared process-wide; this indirection is the
        least-invasive way to expose in-flight state.
        """
        return self._active_sessions.get(task)

    async def run_session(self, query: str, skip_search: bool = False) -> dict:
        """Execute a full 7-step AGP session. Returns the sealed session dict.

        skip_search: If True, skip web searches entirely (for internal tasks
        like edge analysis that already have all data they need).
        """
        session = AGPSession(query)
        _current_task = asyncio.current_task()
        if _current_task is not None:
            self._active_sessions[_current_task] = session
        logger.info(f"Session {session.session_id}: starting — {query}")
        t0 = time.monotonic()

        # Load tiered cache (hot cache auto-injected, warm available via tools).
        # Local variable, not instance attribute, to prevent cross-session pollution
        # if the same Orchestrator instance handles concurrent run_session() calls.
        cache = get_cache_manager()
        memory_context = await cache.get_memory_context()
        logger.info(f"Session {session.session_id}: hot cache loaded ({len(memory_context)} chars)")

        try:
            # Step 1: Declare Scope
            session.scope = query
            logger.info(f"Session {session.session_id}: step 1 — scope declared")

            # Step 2: Sentinel classifies WHILE Brave pre-searches run in parallel
            session.advance_to(SessionStep.ASSIGN_DOMAIN)
            domain_task = asyncio.create_task(self._step_assign_domain(session))

            if skip_search:
                pre_results = []
            else:
                # Extract a short search query — use first line only, max 200 chars
                search_query = query.split("\n")[0][:200].strip()
                pre_queries = [search_query, f"{search_query.rstrip('?').strip()} 2025 2026 latest"]
                # Sports/player/team queries: enforce freshness to avoid stale roster data
                freshness = _detect_freshness(query)
                search_task = asyncio.create_task(
                    self._run_searches_parallel(pre_queries, freshness=freshness)
                )

            domain = await domain_task
            session.domain = domain
            t_domain = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 2 — domain={domain.value} [{t_domain:.1f}s]")

            # Collect pre-search results + one domain-specific search
            if not skip_search:
                pre_results = await search_task
                domain_q = self._domain_search_query(query, domain)
                if domain_q:
                    extra = await self._run_searches_parallel(
                        [domain_q], freshness=freshness
                    )
                    pre_results.extend(extra)
                pre_results = _dedup_search_results(pre_results)

            # Step 3: Source Enumeration (Architect)
            session.advance_to(SessionStep.SOURCE_ENUMERATION)
            sources = await self._step_enumerate_sources(session)
            session.sources = sources
            t_sources = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 3 — {len(sources)} sources [{t_sources:.1f}s]")

            # Run any additional searches from Architect's source list (parallel)
            if not skip_search:
                # Use only first line of query to avoid URL overflow on multi-line prompts
                short_query = query.split("\n")[0][:200].rstrip("?").strip()
                extra_queries = []
                for src in sources[:2]:
                    q = f"{src} {short_query}"
                    if q not in pre_queries:
                        extra_queries.append(q)
                if extra_queries:
                    extra_results = await self._run_searches_parallel(
                        extra_queries, freshness=freshness
                    )
                    pre_results.extend(extra_results)
                    pre_results = _dedup_search_results(pre_results)

            # Step 4: Primary Collection (Architect + search results)
            session.advance_to(SessionStep.PRIMARY_COLLECTION)
            evidence_list, used_tools = await self._step_collect_evidence(
                session, pre_results
            )
            for ev in evidence_list:
                session.add_evidence(ev)
            t_evidence = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 4 — "
                f"{len(session.evidence)} evidence, tools={used_tools} [{t_evidence:.1f}s]"
            )

            # Step 5: Contradiction Check (Architect — already loaded)
            session.advance_to(SessionStep.CONTRADICTION_CHECK)
            contradictions = await self._step_check_contradictions(session)
            for c in contradictions:
                session.add_contradiction(c)
            t_contra = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 5 — {len(contradictions)} contradictions [{t_contra:.1f}s]")

            # Step 6: Synthesis (Claude Code primary, local fallback) + Enhancement + Manager Review
            session.advance_to(SessionStep.SYNTHESIS)
            summary = await self._step_synthesize(session, used_tools)
            t_synth = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 6a — "
                f"synthesis confidence={summary.confidence_score} [{t_synth:.1f}s]"
            )

            # Claude Code enhancement pass — always attempted when available
            summary, escalated = await self._step_escalate_to_claude(session, summary)
            if escalated:
                used_tools = True  # Claude Code counts as a tool
                t_escalate = time.monotonic() - t0
                logger.info(
                    f"Session {session.session_id}: step 6b — "
                    f"Claude Code enhancement → confidence={summary.confidence_score} [{t_escalate:.1f}s]"
                )

            summary = await self._step_manager_review(session, summary, used_tools)
            session.summary = summary
            t_review = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 6c — "
                f"final confidence={summary.confidence_score} ({summary.confidence_tier.value}) [{t_review:.1f}s]"
            )

            # Step 7: Session Close — seal and store
            session.advance_to(SessionStep.SESSION_CLOSE)
            try:
                seal_hash = session.seal()
            except AGPSealRefused as e:
                # Seal refused — garbage synthesis, empty evidence, or mostly
                # filtered. Do NOT write a SPECULATIVE row to DB; return an
                # error shape so callers know the result is unsealed/unstored.
                logger.warning(
                    f"Session {session.session_id}: seal refused: {e}"
                )
                out = session.to_dict()
                out["stored"] = False
                out["sealed"] = False
                out["seal_refused_reason"] = str(e)
                out["error"] = "seal_refused"
                return out
            logger.info(f"Session {session.session_id}: step 7 — sealed {seal_hash[:16]}...")

            # Persist to memory
            for ev in session.evidence:
                await self.memory.store_evidence(session.session_id, ev)
            await self.memory.store_session(session)

            total = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: complete in {total:.1f}s")
            out = session.to_dict()
            out["stored"] = True
            out["sealed"] = True
            return out

        except AGPViolation as e:
            logger.error(f"Session {session.session_id}: AGP violation: {e}")
            raise
        except Exception as e:
            logger.error(f"Session {session.session_id}: failed: {e}", exc_info=True)
            raise
        finally:
            # Drop the session from the active registry so task_worker's
            # adaptive-timeout watcher doesn't see a stale reference after
            # normal completion or cancellation.
            if _current_task is not None:
                self._active_sessions.pop(_current_task, None)

    def _domain_search_query(self, query: str, domain: Domain) -> Optional[str]:
        """Generate a domain-specific search refinement.

        Uses only the first line (max 200 chars) to avoid URL overflow
        on multi-line queries like edge analysis prompts.
        """
        core = query.split("\n")[0][:200].rstrip("?").strip()
        if domain == Domain.FINANCIAL:
            return f"{core} market analysis financial data"
        elif domain == Domain.TECHNICAL:
            return f"{core} research breakthrough"
        elif domain == Domain.SIGNAL:
            return f"{core} trend indicator"
        return None

    # JSON schema for Ollama structured output — guarantees valid domain classification
    DOMAIN_SCHEMA = {
        "type": "object",
        "properties": {
            "domain": {
                "type": "string",
                "enum": ["FINANCIAL", "TECHNICAL", "SIGNAL", "SYNTHESIS", "GENERAL"],
            }
        },
        "required": ["domain"],
    }

    async def _step_assign_domain(self, session: AGPSession) -> Domain:
        """Sentinel classifies the query into a domain.

        Uses Ollama structured output (constrained decoding) for guaranteed valid JSON.
        """
        messages = [
            {"role": "system", "content": self.sentinel.config.system_prompt},
            {"role": "user", "content": (
                f"Classify into one domain: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL.\n"
                f"Query: {session.query}"
            )},
        ]
        response = await self.sentinel.achat(
            messages, format=self.DOMAIN_SCHEMA, options={"num_predict": 32}
        )
        parsed = _safe_parse(response)
        if parsed and "domain" in parsed:
            return _parse_domain(parsed["domain"])
        return _parse_domain(response.get("content", "GENERAL"))

    def _architect_system_prompt(self) -> str:
        """Build the Architect's system prompt with Hermes persistent memory."""
        base = self.architect.config.system_prompt
        memory = getattr(self, "_memory_context", "")
        if memory:
            return f"{base}\n\n{memory}"
        return base

    async def _step_enumerate_sources(self, session: AGPSession) -> list[str]:
        """Architect lists sources to consult."""
        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"List sources and search queries to consult.\n"
                f'JSON: {{"sources":["source1","source2"],"search_queries":["query1"]}}'
            )},
        ]
        response = await self.architect.achat(messages, options={"num_predict": 256})
        parsed = _safe_parse(response)
        if parsed and "sources" in parsed:
            return parsed["sources"]
        return [session.scope]

    async def _step_collect_evidence(
        self, session: AGPSession, search_results: list[dict]
    ) -> tuple[list[Evidence], bool]:
        """Architect analyzes search results and extracts structured evidence.

        Claude Code is the PRIMARY reasoning engine. Local models are the fallback
        when Claude is rate-limited or unavailable.

        Wiki-in-the-loop (feat/wiki-in-the-loop, 2026-04-22):
          Before LLM analysis, the knowledge wiki is queried for articles
          relevant to ``session.scope``. High-similarity hits (>0.85) are
          injected as PRIMARY evidence WITH cites, short-circuiting cheap
          look-ups and providing a citation trail. We AUGMENT external
          search rather than replacing it (per AGP rigor).
        """
        used_tools = len(search_results) > 0

        # ── Wiki retrieval (pre-LLM) ──
        wiki_evidence: list[Evidence] = []
        wiki_in_loop = os.getenv("CALLISTO_WIKI_IN_LOOP", "1") == "1"
        if wiki_in_loop:
            try:
                from tools.knowledge_wiki import get_wiki
                wiki = get_wiki()
                async with aiosqlite.connect(wiki.db_path) as wdb:
                    await wdb.execute("PRAGMA busy_timeout = 30000")
                    wiki_hits = await wiki.search(
                        wdb, session.scope, top_k=5, min_similarity=0.0,
                    )
                for hit in wiki_hits:
                    sim = hit.get("similarity")
                    # High-similarity hits: promote to SECONDARY with wiki cite.
                    if isinstance(sim, (int, float)) and sim >= 0.85:
                        content = (
                            f"[wiki prior: {hit.get('topic')}] "
                            f"{(hit.get('summary') or hit.get('content') or '')[:400]}"
                        )
                        ev = Evidence(
                            content=content,
                            source_class=SourceClass.SECONDARY,
                            confidence_score=min(0.75, float(hit.get("confidence", 0.5))),
                            domain=session.domain,
                            origin_agent="knowledge_wiki",
                            source_name=f"wiki://{hit.get('topic')}",
                        )
                        wiki_evidence.append(ev)
                if wiki_evidence:
                    logger.info(
                        f"Session {session.session_id}: wiki retrieval injected "
                        f"{len(wiki_evidence)} high-similarity evidence items"
                    )
            except Exception as e:
                logger.warning(
                    f"Session {session.session_id}: wiki retrieval failed (non-fatal): {e}"
                )

        if search_results:
            compact = [
                {"t": r["title"][:80], "u": r["url"], "d": r["description"][:150]}
                for r in search_results[:12]
            ]
            search_context = (
                f"Web results (SECONDARY sources):\n{_json_compact(compact)}\n\n"
                f"Extract up to 8 evidence items from these results."
            )
        else:
            search_context = (
                "No web results. All evidence is INFERRED. "
                "Source class=INFERRED, max confidence=0.55."
            )

        # ── Claude Code PRIMARY path ──
        if claude_available():
            logger.info(f"Session {session.session_id}: step 4 using Claude Code (primary)")
            claude_prompt = (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n\n"
                f"{search_context}\n\n"
                f"For each piece of evidence, provide: content (1 sentence), "
                f"source_class (SECONDARY if from web results, INFERRED if from your training), "
                f"confidence_score (0.0-1.0, max 0.55 for INFERRED, max 0.75 for SECONDARY), "
                f"source_name (URL if available).\n"
                f'Respond with JSON: {{"evidence":[{{"content":"...","source_class":"SECONDARY",'
                f'"confidence_score":0.7,"source_name":"url"}}]}}'
            )
            claude_context = (
                f"You are the evidence collection agent in an AGP (Agentic Governance Protocol) session. "
                f"Analyze the provided web search results and extract structured evidence items. "
                f"Be rigorous: only claim SECONDARY for web-sourced evidence, INFERRED for reasoning."
            )
            result = await escalate_with_ladder(
                claude_prompt,
                system_context=claude_context,
                task_type="reasoning",
                timeout=120,
            )

            if not result.get("error") and not result.get("rate_limited"):
                used_tools = True
                content = result.get("content", "")
                parsed = _parse_json_response(content) if content else None
                evidence_list = []
                if parsed and isinstance(parsed, dict) and "evidence" in parsed:
                    for item in parsed["evidence"]:
                        try:
                            source_class = SourceClass(item.get("source_class", "INFERRED"))
                            raw_confidence = float(item.get("confidence_score", 0.3))
                            confidence = _clamp_confidence(raw_confidence, source_class.value)
                            ev = Evidence(
                                content=item.get("content", ""),
                                source_class=source_class,
                                confidence_score=confidence,
                                domain=session.domain,
                                origin_agent="claude_code",
                                source_name=item.get("source_name", ""),
                            )
                            evidence_list.append(ev)
                        except (ValueError, KeyError) as e:
                            logger.warning(f"Skipping malformed evidence from Claude: {e}")
                if evidence_list:
                    # Prepend wiki evidence so its cites appear first in the trail.
                    combined = wiki_evidence + evidence_list
                    logger.info(
                        f"Session {session.session_id}: Claude Code extracted "
                        f"{len(evidence_list)} evidence items (+ {len(wiki_evidence)} wiki priors)"
                    )
                    return combined, used_tools or bool(wiki_evidence)
                else:
                    logger.warning(f"Session {session.session_id}: Claude Code returned no parseable evidence, falling back to local")
            else:
                logger.info(
                    f"Session {session.session_id}: Claude Code unavailable for evidence collection "
                    f"(error={result.get('error')}), falling back to local model"
                )

        # ── Local model FALLBACK path ──
        logger.info(f"Session {session.session_id}: step 4 using local model (fallback)")

        tool_prompt = ""
        if not self.architect.config.supports_native_tools:
            tool_prompt = HERMES_TOOL_PROMPT

        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n\n"
                f"{search_context}\n\n"
                f"For each: content (1 sentence), source_class (SECONDARY/INFERRED), "
                f"confidence_score (0.0-1.0, max 0.55 for INFERRED), source_name (URL).\n"
                f'JSON: {{"evidence":[{{"content":"...","source_class":"SECONDARY",'
                f'"confidence_score":0.7,"source_name":"url"}}]}}'
                f"{tool_prompt}"
            )},
        ]

        # Domain-scoped toolkit: core tools + only the plugins this
        # session's domain/query actually calls for (BUILD_MANDATE item 3).
        available_tools = _default_registry().tools_for(session.domain, session.query)
        response = await self.architect.achat(
            messages, tools=available_tools, options={"num_predict": 2048}
        )

        # Handle tool calls
        for _ in range(MAX_TOOL_CALL_ROUNDS):
            if not response.get("tool_calls"):
                break
            used_tools = True
            for tc in response["tool_calls"]:
                result = await self._execute_tool(tc["name"], tc["arguments"])
                messages.append({"role": "assistant", "content": response["content"] or ""})
                messages.append({
                    "role": "tool" if self.architect.config.supports_native_tools else "user",
                    "content": f"Tool result for {tc['name']}:\n" + (
                        json.dumps(result) if not isinstance(result, str) else result
                    ),
                })
            response = await self.architect.achat(
                messages, tools=available_tools, options={"num_predict": 2048}
            )

        parsed = _safe_parse(response)
        evidence_list = []
        if parsed and "evidence" in parsed:
            for item in parsed["evidence"]:
                try:
                    source_class = SourceClass(item.get("source_class", "INFERRED"))
                    raw_confidence = float(item.get("confidence_score", 0.3))
                    confidence = _clamp_confidence(raw_confidence, source_class.value)

                    ev = Evidence(
                        content=item.get("content", ""),
                        source_class=source_class,
                        confidence_score=confidence,
                        domain=session.domain,
                        origin_agent="architect",
                        source_name=item.get("source_name", ""),
                    )
                    evidence_list.append(ev)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed evidence: {e}")
        # Prepend wiki priors so their cites appear first in the trail.
        combined = wiki_evidence + evidence_list
        return combined, used_tools or bool(wiki_evidence)

    async def _run_searches_parallel(
        self, queries: list[str], freshness: Optional[str] = None
    ) -> list[dict]:
        """Run multiple web search queries in parallel with optional freshness filter."""
        async def _single_search(q: str) -> list[dict]:
            try:
                result = await web_search(q, count=5, freshness=freshness)
                return [
                    {
                        "title": r.get("title", ""),
                        "url": r.get("url", ""),
                        "description": r.get("description", ""),
                        "source_class": "SECONDARY",
                    }
                    for r in result.get("results", [])
                ]
            except Exception as e:
                logger.warning(f"Brave search failed for '{q}': {e}")
                return []

        # SECURITY (audit H-13): return_exceptions=True so a single failed query
        # doesn't crash the whole batch. _single_search already returns [] on
        # caught exceptions, but defense-in-depth covers any unhandled raise.
        results_lists = await asyncio.gather(
            *[_single_search(q) for q in queries],
            return_exceptions=True,
        )
        out: list = []
        for entry in results_lists:
            if isinstance(entry, Exception):
                logger.warning(f"web_search subquery raised: {entry!r}")
                continue
            out.extend(entry)
        return out

    async def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool call."""
        if name == "web_search":
            # Truncate query to prevent Brave 422 on massive tool-generated queries
            raw_q = arguments.get("query", "")
            safe_q = raw_q.split("\n")[0][:300].strip()
            return await web_search(query=safe_q, count=arguments.get("count", 5))
        if name == "claude_code":
            # Route through the ladder so CALLISTO_LOCAL_ONLY + cost-aware
            # routing + time-of-day demotion all apply uniformly.
            return await escalate_with_ladder(
                prompt=arguments.get("prompt", ""),
                system_context=arguments.get("system_context", ""),
                task_type="reasoning",
            )
        # Domain-plugin tools (sports today). Registration is the extension
        # point; unknown names fall through to the legacy generic dispatcher.
        handled, result = await _default_registry().dispatch(name, arguments)
        if handled:
            return result
        return execute_function_call(name, arguments)

    async def _step_check_contradictions(self, session: AGPSession) -> list[Contradiction]:
        """Architect actively searches for contradictions.

        Claude Code is the PRIMARY reasoning engine (mirrors _step_synthesize).
        This step's entire purpose is rigor — using the weakest model for it
        was the original silent-failure pattern.
        """
        if not session.evidence:
            return []

        evidence_compact = _json_compact([e.to_dict() for e in session.evidence])

        def _parse_contradictions(parsed: Optional[dict]) -> list[Contradiction]:
            found: list[Contradiction] = []
            if parsed and "contradictions" in parsed:
                for item in parsed["contradictions"]:
                    try:
                        found.append(Contradiction(
                            claim_a=item.get("claim_a", ""),
                            claim_b=item.get("claim_b", ""),
                            source_a=item.get("source_a", ""),
                            source_b=item.get("source_b", ""),
                            severity=item.get("severity", "MINOR"),
                            resolution=item.get("resolution", ""),
                        ))
                    except (ValueError, KeyError) as e:
                        logger.warning(f"Skipping malformed contradiction: {e}")
            return found

        # ── Claude Code PRIMARY path ──
        if claude_available():
            logger.info(
                f"Session {session.session_id}: step 5 contradictions using Claude Code (primary)"
            )
            claude_prompt = (
                f"Audit the following evidence for contradictions.\n"
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n\n"
                f"Find pairwise contradictions. Each contradiction must name the exact "
                f"claims and sources. Rate severity honestly:\n"
                f"  CRITICAL = outcome-changing, conclusion cannot hold\n"
                f"  MAJOR    = meaningfully weakens conclusion\n"
                f"  MINOR    = definitional / scope / phrasing disagreement\n"
                f"Absence of contradictions in conflicting-looking evidence is itself a flag.\n"
                f'Respond with JSON: {{"contradictions":[{{"claim_a":"...","claim_b":"...",'
                f'"source_a":"...","source_b":"...","severity":"CRITICAL|MAJOR|MINOR",'
                f'"resolution":"..."}}],"notes":"..."}}'
            )
            claude_context = (
                "You are the contradiction-check agent in an AGP (Agentic Governance "
                "Protocol) session. Your output directly penalizes session confidence — "
                "CRITICAL = -0.15, MAJOR = -0.05. Be accurate: overcalling severity "
                "wastes confidence, undercalling hides real conflicts."
            )
            try:
                result = await claude_code_query(
                    claude_prompt, system_context=claude_context, timeout=120
                )
                if not result.get("error") and not result.get("rate_limited"):
                    content = result.get("content", "")
                    parsed = _parse_json_response(content) if content else None
                    contradictions = _parse_contradictions(parsed)
                    logger.info(
                        f"Session {session.session_id}: Claude Code found "
                        f"{len(contradictions)} contradictions"
                    )
                    return contradictions
                logger.info(
                    f"Session {session.session_id}: Claude Code unavailable for "
                    f"contradictions (error={result.get('error')}), falling back to local"
                )
            except Exception as e:
                logger.warning(
                    f"Session {session.session_id}: Claude Code contradiction check "
                    f"raised {type(e).__name__}: {e}; falling back to local"
                )

        # ── Local model FALLBACK path ──
        logger.info(
            f"Session {session.session_id}: step 5 contradictions using local model (fallback)"
        )
        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence:\n{evidence_compact}\n\n"
                f"Find contradictions. Absence is a flag.\n"
                f'JSON: {{"contradictions":[{{"claim_a":"...","claim_b":"...","source_a":"...",'
                f'"source_b":"...","severity":"MINOR|MAJOR|CRITICAL","resolution":"..."}}],'
                f'"notes":"..."}}'
            )},
        ]
        response = await self.architect.achat(messages, options={"num_predict": 512})
        parsed = _safe_parse(response)
        return _parse_contradictions(parsed)

    async def _step_synthesize(self, session: AGPSession, used_tools: bool) -> SessionSummary:
        """Architect synthesizes evidence into a conclusion.

        Claude Code is the PRIMARY reasoning engine. Local models are the fallback
        when Claude is rate-limited or unavailable.
        """
        evidence_compact = _json_compact([e.to_dict() for e in session.evidence])

        tool_warning = ""
        if not used_tools:
            tool_warning = "\nNo real-time sources. All INFERRED. Max confidence=0.55.\n"

        # ── Claude Code PRIMARY path ──
        if claude_available():
            logger.info(f"Session {session.session_id}: step 6 synthesis using Claude Code (primary)")
            claude_prompt = (
                f"Synthesize the following evidence into a conclusion.\n"
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n"
                f"Contradictions: {len(session.contradictions)}\n"
                f"{tool_warning}"
                f'Respond with JSON: {{"conclusion":"...","confidence_score":0.0-1.0}}'
            )
            claude_context = (
                f"You are the synthesis agent in an AGP (Agentic Governance Protocol) session. "
                f"Synthesize all evidence into a coherent conclusion with calibrated confidence. "
                f"Confidence ceilings: INFERRED max=0.55, SECONDARY max=0.75, PRIMARY max=1.0. "
                f"Be honest about uncertainty — never inflate confidence beyond what evidence supports."
            )
            result = await escalate_with_ladder(
                claude_prompt,
                system_context=claude_context,
                task_type="reasoning",
                timeout=120,
            )

            if not result.get("error") and not result.get("rate_limited"):
                content = result.get("content", "")
                parsed = _parse_json_response(content) if content else None

                conclusion = EMPTY_SYNTHESIS_MARKER
                confidence = DB_CONFIDENCE_FLOOR
                if parsed and isinstance(parsed, dict):
                    conclusion = parsed.get("conclusion", conclusion)
                    confidence = float(parsed.get("confidence_score", confidence))
                elif content:
                    # Claude responded but not in JSON — use raw text
                    conclusion = content[:1000]
                    confidence = 0.70

                best_sc = _best_source_class(session.evidence, used_tools)
                confidence = _clamp_confidence(confidence, best_sc)

                logger.info(f"Session {session.session_id}: Claude Code synthesis confidence={confidence}")
                return SessionSummary(
                    scope=session.scope,
                    domain=session.domain,
                    conclusion=conclusion,
                    confidence_score=confidence,
                    evidence_count=len(session.evidence),
                    contradiction_count=len(session.contradictions),
                )
            else:
                logger.info(
                    f"Session {session.session_id}: Claude Code unavailable for synthesis "
                    f"(error={result.get('error')}), falling back to local model"
                )

        # ── Local model FALLBACK path ──
        logger.info(f"Session {session.session_id}: step 6 synthesis using local model (fallback)")

        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Synthesize.\n"
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n"
                f"Contradictions: {len(session.contradictions)}\n"
                f"{tool_warning}"
                f'JSON: {{"conclusion":"...","confidence_score":0.0-1.0}}'
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)

        conclusion = EMPTY_SYNTHESIS_MARKER
        confidence = DB_CONFIDENCE_FLOOR
        if parsed:
            conclusion = parsed.get("conclusion", conclusion)
            confidence = float(parsed.get("confidence_score", confidence))

        best_sc = _best_source_class(session.evidence, used_tools)
        confidence = _clamp_confidence(confidence, best_sc)

        return SessionSummary(
            scope=session.scope,
            domain=session.domain,
            conclusion=conclusion,
            confidence_score=confidence,
            evidence_count=len(session.evidence),
            contradiction_count=len(session.contradictions),
        )

    async def _step_escalate_to_claude(
        self, session: AGPSession, summary: SessionSummary
    ) -> tuple[SessionSummary, bool]:
        """Claude Code enhancement pass — ALWAYS attempted when available.

        Claude Code is the primary reasoning engine. This step enhances or
        replaces the local synthesis with Claude's analysis. Only skipped
        when Claude is rate-limited or unavailable.

        Returns updated summary and whether enhancement occurred.
        """
        if not await claude_code_available():
            logger.info("Claude enhancement skipped: Claude Code CLI not available")
            return summary, False

        is_low_confidence = summary.confidence_score < ESCALATION_THRESHOLD
        logger.info(
            f"Session {session.session_id}: Claude Code enhancement pass "
            f"(current confidence={summary.confidence_score}, "
            f"low_conf={is_low_confidence})"
        )

        # Build concise context — keep under 2K tokens for fast processing
        evidence_summary = "\n".join(
            f"- [{e.source_class.value}] {e.content[:150]}"
            for e in session.evidence[:6]
        )
        context = (
            f"Domain: {session.domain.value}\n"
            f"Question: {session.scope}\n"
            f"Prior synthesis (conf={summary.confidence_score}):\n"
            f"{summary.conclusion[:500]}\n\n"
            f"Evidence ({len(session.evidence)} items):\n{evidence_summary}\n"
            f"Contradictions: {len(session.contradictions)}"
        )
        if is_low_confidence:
            prompt = (
                f"The prior synthesis has low confidence ({summary.confidence_score}). "
                f"Provide a superior, well-supported analysis that addresses the gaps. "
                f"Respond with JSON: {{\"conclusion\":\"...\",\"confidence_score\":0.0-1.0,"
                f"\"key_findings\":[\"...\"],\"gaps\":[\"...\"]}}"
            )
        else:
            prompt = (
                f"Review and enhance the prior synthesis (confidence={summary.confidence_score}). "
                f"Strengthen the analysis, identify any missed nuances, and provide your own "
                f"calibrated confidence. If the prior synthesis is solid, confirm it with your reasoning. "
                f"Respond with JSON: {{\"conclusion\":\"...\",\"confidence_score\":0.0-1.0,"
                f"\"key_findings\":[\"...\"],\"gaps\":[\"...\"]}}"
            )

        result = await escalate_with_ladder(
            prompt,
            system_context=context,
            task_type="deep_work",
            timeout=180,
        )

        if result.get("error"):
            logger.warning(f"Claude Code enhancement failed: {result['error']}")
            return summary, False

        content = result.get("content", "")
        if not content:
            return summary, False

        # Parse Claude's response
        parsed = _parse_json_response(content) if content else None

        if parsed and isinstance(parsed, dict):
            # Claude Code is reasoning/synthesis, not primary documents.
            # Default tier: INFERRED (ceiling 0.55). Only upgrade to SECONDARY
            # (ceiling 0.75) when the response actually cites URLs that ground
            # the synthesis — a response without citations is pure reasoning.
            conclusion_text = parsed.get("conclusion", content[:500])
            cited = _response_cites_urls(content) or _response_cites_urls(conclusion_text)
            tier = SourceClass.SECONDARY if cited else SourceClass.INFERRED
            source_name = (
                f"Claude Code ({result['model']})"
                + (" [cited]" if cited else " [uncited]")
            )
            claude_evidence = Evidence(
                content=conclusion_text,
                source_class=tier,
                confidence_score=_clamp_confidence(
                    float(parsed.get("confidence_score", 0.85)), tier.value
                ),
                domain=session.domain,
                origin_agent="claude_code",
                source_name=source_name,
            )
            session.add_evidence(claude_evidence)

            # Update summary with Claude's analysis — clamped to the tier
            # the response actually earned (cited → SECONDARY, else INFERRED)
            summary.conclusion = parsed.get("conclusion", summary.conclusion)
            new_confidence = _clamp_confidence(
                float(parsed.get("confidence_score", 0.85)), tier.value
            )
            summary.confidence_score = new_confidence
            summary.evidence_count = len(session.evidence)
            logger.info(
                f"Claude Code enhancement ({tier.value}, cited={cited}) "
                f"→ confidence={new_confidence}"
            )
        else:
            # Couldn't parse JSON — use raw text, tier by citation presence
            cited = _response_cites_urls(content)
            tier = SourceClass.SECONDARY if cited else SourceClass.INFERRED
            ceiling = MAX_CONFIDENCE_BY_SOURCE[tier.value]
            claude_evidence = Evidence(
                content=content[:500],
                source_class=tier,
                confidence_score=ceiling,
                domain=session.domain,
                origin_agent="claude_code",
                source_name=(
                    f"Claude Code ({result['model']})"
                    + (" [cited]" if cited else " [uncited]")
                ),
            )
            session.add_evidence(claude_evidence)
            summary.conclusion = content[:1000]
            summary.confidence_score = ceiling
            summary.evidence_count = len(session.evidence)
            logger.info(
                f"Claude Code enhancement used raw text ({tier.value}, "
                f"cited={cited}, conf={ceiling})"
            )

        return summary, True

    async def _step_manager_review(
        self, session: AGPSession, summary: SessionSummary, used_tools: bool
    ) -> SessionSummary:
        """Manager reviews synthesis. Enforces confidence discipline."""
        source_classes = set(e.source_class.value for e in session.evidence)
        best_sc = _best_source_class(session.evidence, used_tools)

        # Compact evidence for review
        evidence_compact = _json_compact([
            {"c": e.content[:200], "sc": e.source_class.value, "conf": e.confidence_score}
            for e in session.evidence
        ])

        messages = [
            {"role": "user", "content": (
                f"Review AGP synthesis.\n"
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Conclusion: {summary.conclusion}\n"
                f"Confidence: {summary.confidence_score} | Sources: {source_classes} | Tools: {used_tools}\n"
                f"Evidence ({summary.evidence_count}):\n{evidence_compact}\n"
                f"Contradictions: {summary.contradiction_count}\n\n"
                f"Rules: adjust confidence DOWN only. "
                f"INFERRED max=0.55, SECONDARY max=0.75, only PRIMARY supports VERIFIED(0.90+).\n"
                f'JSON: {{"approved":true/false,"adjusted_confidence":null/float,'
                f'"objections":["..."],"reasoning":"..."}}'
            )},
        ]
        response = await self.manager.achat(messages)
        parsed = _safe_parse(response)

        if parsed and isinstance(parsed, dict):
            adjusted = parsed.get("adjusted_confidence")
            if adjusted is not None:
                adjusted = float(adjusted)
                if adjusted < summary.confidence_score:
                    summary.confidence_score = adjusted
                    logger.info(f"Manager adjusted confidence to {adjusted}")

            objections = parsed.get("objections", [])
            if objections:
                summary.manager_objections = objections
                for obj in objections:
                    session.add_manager_objection(obj)

        # Final hard enforcement — code, not policy
        summary.confidence_score = _clamp_confidence(summary.confidence_score, best_sc)

        # ── Contradiction penalty pass ──
        # Applied AFTER _clamp_confidence, BEFORE seal(). Previously
        # contradictions were passed to the LLM prompt as a count string
        # and had zero code-path effect on confidence. That made the
        # "contradiction-checked" claim cosmetic.
        pre_penalty = summary.confidence_score
        critical = sum(
            1 for c in session.contradictions if c.severity.upper() == "CRITICAL"
        )
        major = sum(
            1 for c in session.contradictions if c.severity.upper() == "MAJOR"
        )
        penalty = (
            critical * CONTRADICTION_PENALTY["CRITICAL"]
            + major * CONTRADICTION_PENALTY["MAJOR"]
        )
        if penalty > 0:
            penalized = max(DB_CONFIDENCE_FLOOR, round(pre_penalty - penalty, 2))
            logger.info(
                f"Session {session.session_id}: contradiction penalty "
                f"(CRITICAL={critical}, MAJOR={major}) → "
                f"confidence {pre_penalty} - {round(penalty, 2)} = {penalized}"
            )
            summary.confidence_score = penalized
        else:
            logger.info(
                f"Session {session.session_id}: no contradiction penalty "
                f"(CRITICAL=0, MAJOR=0, confidence={pre_penalty})"
            )

        return summary
