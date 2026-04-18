"""Quantitative engine for Callisto — microstructure, consensus, edge ranking.

Submodules:
    consensus_engine  — per-market Pinnacle-anchored fair-probability estimator
                        over multi-book lines (devig, trim, tier-weight)
    sharp_detection   — steam / first-mover / reverse-line-movement classifier
                        over per-book odds time series
    edge_ranker       — ranks every currently open market by (consensus_fair −
                        book_implied) with detection-risk and book-limit
                        penalties; persists to live_edge_surface

Design principle: every function is pure and unit-testable given a list of
`BookLine` records. The engine does not fetch data itself — callers supply
the lines. This keeps the quant layer decoupled from the scraper cascade
and makes it easy to backtest the math against historical odds snapshots.
"""

from .consensus_engine import (
    BookLine,
    ConsensusResult,
    compute_consensus_fair_prob,
    pinnacle_devig,
    power_devig,
    BOOK_TIER,
    BOOK_TIER_WEIGHT,
)
from .sharp_detection import (
    LineTick,
    SharpSignal,
    detect_first_mover,
    detect_steam_event,
    detect_reverse_line_movement,
    scan_market_movement,
)
from .edge_ranker import (
    MarketSnapshot,
    RankedEdge,
    rank_edges,
    score_edge,
    persist_ranked_edges,
    EDGE_RANKER_SCHEMA_SQL,
)
from .scanner import scan_sport, scan_all_sports

__all__ = [
    "BookLine",
    "ConsensusResult",
    "compute_consensus_fair_prob",
    "pinnacle_devig",
    "power_devig",
    "BOOK_TIER",
    "BOOK_TIER_WEIGHT",
    "LineTick",
    "SharpSignal",
    "detect_first_mover",
    "detect_steam_event",
    "detect_reverse_line_movement",
    "scan_market_movement",
    "MarketSnapshot",
    "RankedEdge",
    "rank_edges",
    "score_edge",
    "persist_ranked_edges",
    "EDGE_RANKER_SCHEMA_SQL",
    "scan_sport",
    "scan_all_sports",
]
