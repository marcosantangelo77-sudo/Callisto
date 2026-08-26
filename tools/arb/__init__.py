"""tools.arb — modular home for the arbitrage/dutch-book scanner.

The public API is re-exported by ``tools.arbitrage_scanner`` (facade) and
also importable directly from here.
"""

from tools.arb.models import (
    ArbLeg,
    ArbOpportunity,
    DEFAULT_BUDGET,
    DEFAULT_EPSILON,
    DEFAULT_STALE_SECONDS,
    MAX_IMPLIED_DIVERGENCE,
    MAX_PROFIT_PCT,
    MIN_EFFECTIVE_BUDGET_PCT,
    MIN_PROFIT_PCT,
)
from tools.arb.prices import (
    _age_seconds,
    _best_at,
    _collect_best_prices,
    _collect_point_groups,
    _extract_line_ts,
    _parse_ts,
)
from tools.arb.stakes import _build_arb_from_pair, _compute_stakes, _scan_spread_arbs
from tools.arb.scanner import scan_dutch_book, scan_pure_arb
from tools.arb.synthetic import scan_cross_market_synthetic
from tools.arb.orchestrator import full_arbitrage_scan
from tools.arb.persistence import persist_opportunity
from tools.arb.backtest import backtest_arbs

__all__ = [
    "ArbLeg",
    "ArbOpportunity",
    "DEFAULT_BUDGET",
    "DEFAULT_EPSILON",
    "DEFAULT_STALE_SECONDS",
    "MAX_IMPLIED_DIVERGENCE",
    "MAX_PROFIT_PCT",
    "MIN_EFFECTIVE_BUDGET_PCT",
    "MIN_PROFIT_PCT",
    "_age_seconds",
    "_best_at",
    "_build_arb_from_pair",
    "_collect_best_prices",
    "_collect_point_groups",
    "_compute_stakes",
    "_extract_line_ts",
    "_parse_ts",
    "_scan_spread_arbs",
    "backtest_arbs",
    "full_arbitrage_scan",
    "persist_opportunity",
    "scan_cross_market_synthetic",
    "scan_dutch_book",
    "scan_pure_arb",
]
