"""
Edge scanner — finds exploitable inefficiencies across bookmakers.

This is the quantitative core. Three edge types:

1. CROSS-BOOK DIVERGENCE: Same bet priced differently across books.
   If BetMGM has -105 and MyBookie has -125 on the same spread,
   BetMGM is giving you 4%+ better implied probability. Sharp money
   moves first on soft books — divergence tells you WHERE sharps are.

2. SHARP MONEY DETECTION: When one book moves while others don't,
   that book likely took a large sharp bet. The others will follow.
   Getting in before the cascade = buying at a discount.

3. MISPRICED LINES: When a book's juice structure creates +EV.
   Example: if both sides of a spread are -105 instead of -110,
   the total vig is lower and the line may be exploitable.
   Also: stale lines that haven't adjusted to news/injuries.

Implementation note: the scan/rank/filter helpers live in ``tools.edges``
(common, scanning, filters, wiki submodules). This module remains the
public entry point — every historical name stays importable from here.
"""

# Soft-book titles — single source of truth for the cross-book scanners.
# Includes 'fanatics' (added when odds-api.io adopted the book); every
# canonical form resolves to the same membership key via canonicalize_book.
SOFT_TITLES = {"fanduel", "draftkings", "betmgm", "pointsbet", "caesars", "betrivers", "mybookie.ag", "bovada", "betus", "fanatics", "fanatics sportsbook"}

# Re-export everything from the split-out modules so existing callers
# (tools.line_monitor, orchestrator.py, api.py, tests) keep working
# unchanged against tools.edge_scanner.
from tools.edges.common import (  # noqa: F401
    logger,
    _DEFAULT_HALF_LIFE_S,
    _ODDS_HALF_LIFE_S,
    _DEBUG_WEIGHTS,
    _parse_line_timestamp,
    _freshness_weight,
    weighted_sharp_consensus,
    _filter_in_progress_games,
    _PACE_SPORT_MAP,
    _LOW_SCORING_SPORTS,
    _STATIC_SHARP_TITLES,
    _granger_sharp_cache,
    _GRANGER_CACHE_TTL,
    get_sharp_titles_for_sport,
    _refresh_granger_cache,
    WIKI_EDGE_ADJUSTMENT_CAP,
)
from tools.edges.scanning import (  # noqa: F401
    scan_cross_book_edges,
    fetch_alt_lines_for_games,
    scan_alt_line_edges,
    _scan_line_group,
    detect_sharp_money,
)
from tools.edges.wiki import apply_wiki_adjustments_to_edges  # noqa: F401
from tools.edges.filters import (  # noqa: F401
    scan_vig_edges,
    scan_pace_model_total_edges,
    full_edge_scan,
    _simulation_validate_edges,
    _compute_market_hold,
    _scan_dead_number_steals,
)

import os  # noqa: F401  — kept for callers that patch env at import time

from tools.odds_api import (
    calculate_implied_probability,
    calculate_ev,
    find_best_line,
)
from tools.market_microstructure import compute_market_metrics
from tools.book_keys import canonicalize_book
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    analyze_spread as _analyze_spread,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)

# Alt-line cache lives with its consumers in tools.edges.scanning; alias it
# here for backwards compatibility (tests import _alt_cache_get/_alt_cache_put).

import time as _alt_time  # noqa: F401

from tools.edges.scanning import (  # noqa: F401
    _ALT_LINE_CACHE,
    _ALT_LINE_TTL_S,
    _alt_cache_get,
    _alt_cache_put,
)
