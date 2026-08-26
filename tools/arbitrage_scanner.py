"""
Arbitrage + dutch-book scanner — the guaranteed-profit floor.

A pure arb exists when you can place bets on every outcome of a market across
different books such that the sum of implied probabilities (1/decimal_odds)
is strictly less than 1.0. Stake each leg proportionally to its implied prob
and the return is the same regardless of which outcome hits.

This module gives you four flavours:

1. ``scan_pure_arb(game, market_type)`` — binary or multi-way market across
   best price per side.
2. ``scan_dutch_book(game, market_type)`` — generalised multi-outcome cover.
3. ``scan_cross_market_synthetic(game)`` — team-total + opponent-total vs
   game-total, and same-game-parlay vs individual-leg equivalents.
4. ``full_arbitrage_scan(snapshot, **opts)`` — aggregate over every game +
   market in a snapshot; write qualifying rows to ``ev_opportunities`` with
   ``source='arbitrage'`` and ``thesis_tag in {'arb', 'dutch',
   'synthetic_arb'}``.

STALE-LINE FILTER
=================
Any leg older than ``STALE_SECONDS`` (default 120s) disqualifies the whole
arb. A real book will have already moved; the arb is phantom. Age is taken
from per-outcome ``fetched_at`` when present, else the bookmaker-level
``fetched_at``/``last_update``. No timestamp = unknown age = rejected (we
bias conservative; unmarked data is almost always stale scraper dumps).

BOOK-LIMIT AWARENESS
====================
After computing theoretical stakes for a target budget, we clamp every leg
to ``book_keys.get_book_max_stake(book, market_type)`` and record the
effective-budget reduction. If the limiting leg would cap our budget at less
than ``MIN_EFFECTIVE_BUDGET_PCT`` of the requested amount, the arb is flagged
``limited=True`` — still reported but separately counted so the user can
decide whether microsize is worth the operational risk.

OUTPUT
======
Nothing gets placed. Qualifying arbs go into ``ev_opportunities`` with:

    source         = 'arbitrage'
    status         = 'open'
    market         = '<market_type>'
    team           = '<outcome_name>'     (one row per leg)
    bookmaker      = '<book>'
    american_odds  = <leg price>
    edge           = 1 - total_implied    (the "gap")
    expected_value = expected_profit_per_dollar
    kelly_fraction = stake_pct_of_budget
    detected_at    = ISO timestamp
    expires_at     = detected_at + 60s (via detected_at parse; our status
                     marker — consumers should refuse to execute past this)

No schema changes strictly required — the existing ``ev_opportunities`` table
absorbs it via the ``source`` column. A tiny migration adds ``thesis_tag``
for clarity.

IMPLEMENTATION NOTE (split)
===========================
The implementation now lives in the ``tools.arb`` package::

    tools/arb/models.py         constants + ArbLeg/ArbOpportunity dataclasses
    tools/arb/prices.py         timestamp helpers + best-price collection
    tools/arb/stakes.py         stake math + shared arb construction
    tools/arb/scanner.py        scan_pure_arb / scan_dutch_book
    tools/arb/synthetic.py      scan_cross_market_synthetic
    tools/arb/orchestrator.py   full_arbitrage_scan
    tools/arb/persistence.py    persist_opportunity (sqlite3)
    tools/arb/backtest.py       backtest_arbs over odds_snapshots

This file remains a thin facade that re-exports everything so existing
imports keep working.
"""

from __future__ import annotations

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
