"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
Uses Brave Search for real evidence gathering. Enforces honest confidence calibration.

Optimizations: parallel Brave searches, pipelined model loading, compressed prompts.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from agp import (
    AGPSession,
    AGPViolation,
    Contradiction,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
    ConfidenceTier,
)
from inference import get_architect, get_manager, get_sentinel, execute_function_call, _parse_json_response
from memory import MemoryStore
from tools.search import web_search
from tools.claude_code import claude_code_query, claude_code_available
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
from tools.contextual_data import get_injuries, get_scoreboard
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

# Confidence ceilings enforced in code — not policy, not negotiable.
MAX_CONFIDENCE_BY_SOURCE = {
    "PRIMARY": 1.0,       # VERIFIED — direct analysis of primary documents
    "SECONDARY": 0.75,    # CORROBORATED — web search, third-party reports
    "SIGNAL": 0.55,       # PROBABLE — signals without primary corroboration
    "INFERRED": 0.55,     # PROBABLE — training data, no real-time verification
}
MAX_CONFIDENCE_NO_TOOL = 0.55
MAX_TOOL_CALL_ROUNDS = 3
ESCALATION_THRESHOLD = 0.60  # Below this, escalate to Claude Code if available

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

# All available tool schemas
ODDS_TOOLS = [
    ODDS_GET_ODDS_TOOL, ODDS_GET_SCORES_TOOL, ODDS_GET_EVENT_TOOL,
    ODDS_CALCULATE_EV_TOOL, ODDS_ALT_LINES_TOOL, ODDS_PLAYER_PROPS_TOOL,
    EDGE_SCAN_TOOL, INJURIES_TOOL, SCOREBOARD_TOOL,
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
    if not evidence and not used_tools:
        return "INFERRED"
    rank = {"PRIMARY": 4, "SECONDARY": 3, "SIGNAL": 2, "INFERRED": 1}
    best = "INFERRED"
    for ev in evidence:
        sc = ev.source_class.value if hasattr(ev, "source_class") else ev.get("source_class", "INFERRED")
        if rank.get(sc, 0) > rank.get(best, 0):
            best = sc
    return best


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


class Orchestrator:
    """Coordinates the 3-agent AGP session flow."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.architect = get_architect()
        self.manager = get_manager()
        self.sentinel = get_sentinel()

    async def run_session(self, query: str) -> dict:
        """Execute a full 7-step AGP session. Returns the sealed session dict."""
        session = AGPSession(query)
        logger.info(f"Session {session.session_id}: starting — {query}")
        t0 = time.monotonic()

        # Load tiered cache (hot cache auto-injected, warm available via tools)
        cache = get_cache_manager()
        self._memory_context = await cache.get_memory_context()
        logger.info(f"Session {session.session_id}: hot cache loaded ({len(self._memory_context)} chars)")

        try:
            # Step 1: Declare Scope
            session.scope = query
            logger.info(f"Session {session.session_id}: step 1 — scope declared")

            # Step 2: Sentinel classifies WHILE Brave pre-searches run in parallel
            session.advance_to(SessionStep.ASSIGN_DOMAIN)
            # Extract a short search query — use first line only, max 200 chars
            search_query = query.split("\n")[0][:200].strip()
            pre_queries = [search_query, f"{search_query.rstrip('?').strip()} 2025 2026 latest"]
            domain_task = asyncio.create_task(self._step_assign_domain(session))
            search_task = asyncio.create_task(self._run_searches_parallel(pre_queries))

            domain = await domain_task
            session.domain = domain
            t_domain = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 2 — domain={domain.value} [{t_domain:.1f}s]")

            # Collect pre-search results + one domain-specific search
            pre_results = await search_task
            domain_q = self._domain_search_query(query, domain)
            if domain_q:
                extra = await self._run_searches_parallel([domain_q])
                pre_results.extend(extra)
            pre_results = _dedup_search_results(pre_results)

            # Step 3: Source Enumeration (Architect)
            session.advance_to(SessionStep.SOURCE_ENUMERATION)
            sources = await self._step_enumerate_sources(session)
            session.sources = sources
            t_sources = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 3 — {len(sources)} sources [{t_sources:.1f}s]")

            # Run any additional searches from Architect's source list (parallel)
            extra_queries = []
            for src in sources[:2]:
                q = f"{src} {query.rstrip('?').strip()}"
                if q not in pre_queries:
                    extra_queries.append(q)
            if extra_queries:
                extra_results = await self._run_searches_parallel(extra_queries)
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

            # Step 6: Synthesis (Architect) + Claude Code Escalation + Manager Review
            session.advance_to(SessionStep.SYNTHESIS)
            summary = await self._step_synthesize(session, used_tools)
            t_synth = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 6a — "
                f"local confidence={summary.confidence_score} [{t_synth:.1f}s]"
            )

            # Tier 2 escalation: Claude Code for low-confidence sessions
            summary, escalated = await self._step_escalate_to_claude(session, summary)
            if escalated:
                used_tools = True  # Claude Code counts as a tool
                t_escalate = time.monotonic() - t0
                logger.info(
                    f"Session {session.session_id}: step 6b — "
                    f"Claude Code escalation → confidence={summary.confidence_score} [{t_escalate:.1f}s]"
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
            seal_hash = session.seal()
            logger.info(f"Session {session.session_id}: step 7 — sealed {seal_hash[:16]}...")

            # Persist to memory
            for ev in session.evidence:
                await self.memory.store_evidence(session.session_id, ev)
            await self.memory.store_session(session)

            total = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: complete in {total:.1f}s")
            return session.to_dict()

        except AGPViolation as e:
            logger.error(f"Session {session.session_id}: AGP violation: {e}")
            raise
        except Exception as e:
            logger.error(f"Session {session.session_id}: failed: {e}")
            raise

    def _domain_search_query(self, query: str, domain: Domain) -> Optional[str]:
        """Generate a domain-specific search refinement."""
        core = query.rstrip("?").strip()
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
        """Architect analyzes search results and extracts structured evidence."""
        used_tools = len(search_results) > 0

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

        available_tools = [WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL] + ODDS_TOOLS
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
        return evidence_list, used_tools

    async def _run_searches_parallel(self, queries: list[str]) -> list[dict]:
        """Run multiple Brave Search queries in parallel."""
        async def _single_search(q: str) -> list[dict]:
            try:
                result = await web_search(q, count=5)
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

        results_lists = await asyncio.gather(*[_single_search(q) for q in queries])
        return [r for sublist in results_lists for r in sublist]

    async def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool call."""
        if name == "web_search":
            return await web_search(
                query=arguments.get("query", ""),
                count=arguments.get("count", 5),
            )
        if name == "claude_code":
            return await claude_code_query(
                prompt=arguments.get("prompt", ""),
                system_context=arguments.get("system_context", ""),
            )
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
        return execute_function_call(name, arguments)

    async def _step_check_contradictions(self, session: AGPSession) -> list[Contradiction]:
        """Architect actively searches for contradictions."""
        if not session.evidence:
            return []

        evidence_compact = _json_compact([e.to_dict() for e in session.evidence])
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
        contradictions = []
        if parsed and "contradictions" in parsed:
            for item in parsed["contradictions"]:
                try:
                    contradictions.append(Contradiction(
                        claim_a=item.get("claim_a", ""),
                        claim_b=item.get("claim_b", ""),
                        source_a=item.get("source_a", ""),
                        source_b=item.get("source_b", ""),
                        severity=item.get("severity", "MINOR"),
                        resolution=item.get("resolution", ""),
                    ))
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed contradiction: {e}")
        return contradictions

    async def _step_synthesize(self, session: AGPSession, used_tools: bool) -> SessionSummary:
        """Architect synthesizes evidence into a conclusion."""
        evidence_compact = _json_compact([e.to_dict() for e in session.evidence])

        tool_warning = ""
        if not used_tools:
            tool_warning = "\nNo real-time sources. All INFERRED. Max confidence=0.55.\n"

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

        conclusion = "No synthesis produced."
        confidence = 0.30
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
        """Escalate to Claude Code when local confidence is below threshold.

        Returns updated summary and whether escalation occurred.
        Only triggers when:
        - Confidence is below ESCALATION_THRESHOLD
        - Claude Code CLI is available
        """
        if summary.confidence_score >= ESCALATION_THRESHOLD:
            return summary, False

        if not await claude_code_available():
            logger.info("Escalation skipped: Claude Code CLI not available")
            return summary, False

        logger.info(
            f"Session {session.session_id}: escalating to Claude Code "
            f"(confidence {summary.confidence_score} < {ESCALATION_THRESHOLD})"
        )

        # Build concise context — keep under 2K tokens for fast escalation
        evidence_summary = "\n".join(
            f"- [{e.source_class.value}] {e.content[:150]}"
            for e in session.evidence[:6]
        )
        context = (
            f"Domain: {session.domain.value}\n"
            f"Question: {session.scope}\n"
            f"Local synthesis (conf={summary.confidence_score}):\n"
            f"{summary.conclusion[:500]}\n\n"
            f"Evidence ({len(session.evidence)} items):\n{evidence_summary}\n"
            f"Contradictions: {len(session.contradictions)}"
        )
        prompt = (
            f"The local AI got low confidence ({summary.confidence_score}). "
            f"Give a concise, well-supported analysis. "
            f"Respond with JSON: {{\"conclusion\":\"...\",\"confidence_score\":0.0-1.0,"
            f"\"key_findings\":[\"...\"],\"gaps\":[\"...\"]}}"
        )

        result = await claude_code_query(prompt, system_context=context, timeout=180)

        if result.get("error"):
            logger.warning(f"Claude Code escalation failed: {result['error']}")
            return summary, False

        content = result.get("content", "")
        if not content:
            return summary, False

        # Parse Claude's response
        parsed = _parse_json_response(content) if content else None

        if parsed and isinstance(parsed, dict):
            # Add Claude Code evidence as PRIMARY
            claude_evidence = Evidence(
                content=parsed.get("conclusion", content[:500]),
                source_class=SourceClass.PRIMARY,
                confidence_score=_clamp_confidence(
                    float(parsed.get("confidence_score", 0.85)), "PRIMARY"
                ),
                domain=session.domain,
                origin_agent="claude_code",
                source_name=f"Claude Code ({result['model']})",
            )
            session.add_evidence(claude_evidence)

            # Update summary with Claude's superior analysis
            summary.conclusion = parsed.get("conclusion", summary.conclusion)
            new_confidence = _clamp_confidence(
                float(parsed.get("confidence_score", 0.85)), "PRIMARY"
            )
            summary.confidence_score = new_confidence
            summary.evidence_count = len(session.evidence)
            logger.info(
                f"Claude Code escalation raised confidence to {new_confidence}"
            )
        else:
            # Couldn't parse JSON — still use raw text as PRIMARY evidence
            claude_evidence = Evidence(
                content=content[:500],
                source_class=SourceClass.PRIMARY,
                confidence_score=0.80,
                domain=session.domain,
                origin_agent="claude_code",
                source_name=f"Claude Code ({result['model']})",
            )
            session.add_evidence(claude_evidence)
            summary.conclusion = content[:1000]
            summary.confidence_score = 0.80
            summary.evidence_count = len(session.evidence)
            logger.info("Claude Code escalation used raw text (JSON parse failed)")

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

        return summary
