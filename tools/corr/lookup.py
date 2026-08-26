"""Correlation lookup: market normalization, learned store, get_correlation.

Extracted verbatim from tools/correlation.py.
"""

import logging
from typing import Optional

from tools.corr.matrices import MARKET_ALIASES, SPORT_CORRELATIONS

logger = logging.getLogger("callisto.correlation")

# ---------------------------------------------------------------------------
# Module-level learned correlation store (set by api.py at startup)
# ---------------------------------------------------------------------------
# When set, get_correlation() blends hardcoded priors with empirically
# learned estimates (Bayesian shrinkage). When None, falls back to
# hardcoded values only (backward compatible).
_learned_store: "Optional[LearnedCorrelationStore]" = None


def set_learned_store(store) -> None:
    """Wire the LearnedCorrelationStore into all correlation lookups."""
    global _learned_store
    _learned_store = store
    logger.info("Learned correlation store wired into correlation engine")


def get_learned_store():
    """Return the module-level learned store (or None)."""
    return _learned_store


def _normalize_market(market: str) -> str:
    """Normalize a market name to its canonical form."""
    key = market.strip().lower().replace(" ", "_").replace("-", "_")
    return MARKET_ALIASES.get(key, key)


def get_correlation(
    market_a: str,
    market_b: str,
    sport: str,
    learned_store: "Optional[LearnedCorrelationStore]" = None,
) -> float:
    """
    Look up the correlation coefficient between two markets for a given sport.

    If a learned_store is provided, returns a Bayesian blend of the hardcoded
    prior and the empirically learned estimate (weighted by sample size).
    Without learned_store, returns the hardcoded value (backward compatible).

    Args:
        market_a: First market (e.g., "qb_passing_yards", "passing_yards", "points")
        market_b: Second market (e.g., "team_total", "game_total")
        sport: Sport key (e.g., "nfl", "nba", "mlb", "nhl")
        learned_store: Optional learned correlation store for data-driven blending

    Returns:
        Pearson correlation coefficient from -1.0 to 1.0.
        Returns 0.0 if the pair is not found (assumes independence).
    """
    norm_a = _normalize_market(market_a)
    norm_b = _normalize_market(market_b)
    sport_key = sport.strip().lower()

    # Strip common API prefixes
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break

    matrix = SPORT_CORRELATIONS.get(sport_key)
    if matrix is None:
        logger.warning(f"No correlation matrix for sport '{sport}'. Assuming independence.")
        return 0.0

    # Check both orderings for hardcoded prior
    prior = matrix.get((norm_a, norm_b))
    if prior is None:
        prior = matrix.get((norm_b, norm_a))
    if prior is None:
        prior = 0.0

    # Blend with learned estimate if available
    # Use explicit parameter first, fall back to module-level singleton
    store = learned_store if learned_store is not None else _learned_store
    if store is not None:
        return store.get_blended(sport_key, norm_a, norm_b, prior)

    return prior


def get_all_correlations(sport: str) -> dict[tuple[str, str], float]:
    """
    Return the full correlation matrix for a sport.

    Useful for inspection, debugging, or building custom correlation overrides.
    """
    sport_key = sport.strip().lower()
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break
    return dict(SPORT_CORRELATIONS.get(sport_key, {}))


def list_correlated_markets(market: str, sport: str, min_abs_rho: float = 0.2) -> list[dict]:
    """
    List all markets that are correlated with the given market above a threshold.

    Useful for identifying which legs to pair in an SGP.

    Args:
        market: The market to find correlations for.
        sport: Sport key.
        min_abs_rho: Minimum absolute correlation to include.

    Returns:
        List of dicts with correlated markets and their correlation values,
        sorted by absolute correlation (strongest first).
    """
    norm = _normalize_market(market)
    sport_key = sport.strip().lower()
    for prefix in ("americanfootball_", "basketball_", "baseball_", "icehockey_"):
        if sport_key.startswith(prefix):
            sport_key = sport_key[len(prefix):]
            break

    matrix = SPORT_CORRELATIONS.get(sport_key, {})
    results = []

    for (a, b), rho in matrix.items():
        if abs(rho) < min_abs_rho:
            continue
        if a == norm:
            results.append({"market": b, "correlation": rho, "direction": "positive" if rho > 0 else "negative"})
        elif b == norm:
            results.append({"market": a, "correlation": rho, "direction": "positive" if rho > 0 else "negative"})

    results.sort(key=lambda x: abs(x["correlation"]), reverse=True)
    return results
