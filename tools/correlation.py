"""
SGP (Same Game Parlay) correlation exploitation engine.

Books price SGP legs as if they are independent events, but many prop and
game markets within the same game are correlated. When two legs are
positively correlated, the true joint probability of both hitting is HIGHER
than the product of their individual probabilities. That means the book is
underpricing the parlay — that gap is the edge.

Core insight:
    P(A and B) = P(A) * P(B)                        ... if independent
    P(A and B) = P(A) * P(B) + rho * sigma_A * sigma_B  ... with correlation

Where rho is the Pearson correlation between the underlying stat distributions.
A positive rho increases the joint probability, meaning the parlay is worth
more than the book implies. A negative rho does the opposite.

Correlation values are hardcoded from sports analytics research:
- NFL: Football Outsiders, PFF, nflfastR play-by-play data
- NBA: Cleaning the Glass, NBA.com tracking, pbpstats.com
- MLB: FanGraphs, Baseball Savant Statcast
- NHL: Natural Stat Trick, MoneyPuck, Evolving Hockey

These are base correlations. Actual game-specific correlations vary based on
matchup context (pace, game script, weather, etc.), but the base values
provide a structural edge when books ignore them entirely.

Implementation note:
    The engine now lives in ``tools.corr`` (split into matrices / lookup /
    odds / parlays / assessment). This module is a facade that re-exports
    the full public API so existing imports keep working unchanged.
"""

from tools.corr.matrices import (
    MARKET_ALIASES,
    MLB_CORRELATIONS,
    NBA_CORRELATIONS,
    NFL_CORRELATIONS,
    NHL_CORRELATIONS,
    SPORT_CORRELATIONS,
)
from tools.corr.lookup import (
    _learned_store,
    _normalize_market,
    get_all_correlations,
    get_correlation,
    get_learned_store,
    list_correlated_markets,
    set_learned_store,
)
from tools.corr.odds import (
    _adjust_joint_probability,
    _american_to_implied,
    _implied_to_american,
    _prob_to_decimal,
)
from tools.corr.parlays import (
    build_correlated_parlay,
    correlated_parlay_odds,
    detect_mispriced_correlation,
    independent_parlay_odds,
)
from tools.corr.assessment import (
    _assess_mispricing,
    _rate_correlation_edge,
    detect_anti_correlation,
    estimate_sgp_vig,
)

__all__ = [
    # matrices
    "MARKET_ALIASES",
    "MLB_CORRELATIONS",
    "NBA_CORRELATIONS",
    "NFL_CORRELATIONS",
    "NHL_CORRELATIONS",
    "SPORT_CORRELATIONS",
    # lookup
    "_normalize_market",
    "get_all_correlations",
    "get_correlation",
    "get_learned_store",
    "list_correlated_markets",
    "set_learned_store",
    # odds
    "_adjust_joint_probability",
    "_american_to_implied",
    "_implied_to_american",
    "_prob_to_decimal",
    # parlays
    "build_correlated_parlay",
    "correlated_parlay_odds",
    "detect_mispriced_correlation",
    "independent_parlay_odds",
    # assessment
    "_assess_mispricing",
    "_rate_correlation_edge",
    "detect_anti_correlation",
    "estimate_sgp_vig",
]
