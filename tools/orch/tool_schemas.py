"""AGP pipeline tool schemas (extracted from orchestrator.py).

All Ollama native-tool-calling JSON schemas for the sports/web/claude tool
payloads. Pure data — no behavior.
"""

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
