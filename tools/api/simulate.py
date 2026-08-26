"""Simulation route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``DB_PATH``) via a late
``from api import ...`` inside the function body to avoid a circular import
at module load time.
"""

from __future__ import annotations

import asyncio
import os
from collections import OrderedDict

from fastapi import HTTPException
from pydantic import BaseModel


class SimulationRequest(BaseModel):
    home_name: str
    away_name: str
    home_off_eff: float = 105.0
    home_def_eff: float = 100.0
    away_off_eff: float = 105.0
    away_def_eff: float = 100.0
    home_pace: float = 70.0
    away_pace: float = 70.0
    home_injuries_impact: float = 0.0
    away_injuries_impact: float = 0.0
    iterations: int = 10000
    sport: str = "basketball_ncaab"
    event_id: str = ""


class PoissonRequest(BaseModel):
    home_expected: float
    away_expected: float
    sport: str = "soccer_epl"
    event_id: str = ""


async def simulate_basketball_game(req: SimulationRequest):
    """Run Monte Carlo simulation and compare against market odds."""
    from tools.simulation import simulate_basketball, compare_to_market, TeamProfile

    home = TeamProfile(
        name=req.home_name,
        offensive_efficiency=req.home_off_eff,
        defensive_efficiency=req.home_def_eff,
        pace=req.home_pace,
        injuries_impact=req.home_injuries_impact,
    )
    away = TeamProfile(
        name=req.away_name,
        offensive_efficiency=req.away_off_eff,
        defensive_efficiency=req.away_def_eff,
        pace=req.away_pace,
        injuries_impact=req.away_injuries_impact,
    )

    sim = simulate_basketball(home, away, iterations=req.iterations)

    result = {
        "simulation": {
            "home_avg_score": round(sim.home_avg_score, 1),
            "away_avg_score": round(sim.away_avg_score, 1),
            "fair_spread": round(sim.fair_spread, 1),
            "fair_total": round(sim.fair_total, 1),
            "home_win_pct": round(sim.home_win_pct * 100, 1),
            "away_win_pct": round(sim.away_win_pct * 100, 1),
            "iterations": sim.iterations,
        },
    }

    # Compare to market if we have an event_id
    if req.event_id:
        from tools.odds_api import get_event_odds
        market = await get_event_odds(
            sport=req.sport, event_id=req.event_id,
            markets="h2h,spreads,totals",
        )
        if not market.get("error"):
            edges = compare_to_market(sim, market)
            result["market_edges"] = edges
            result["edge_count"] = len([e for e in edges if e["ev"]["is_positive_ev"]])

    return result


def simulate_poisson_game(req: PoissonRequest):
    """Run Poisson simulation for low-scoring sports."""
    from tools.simulation import simulate_poisson
    return simulate_poisson(req.home_expected, req.away_expected)


# =========================================================================
# Pre-LIVE bankroll Monte Carlo simulation endpoint
# feat/bankroll-montecarlo-sim (2026-04-22)
# =========================================================================
_PORTFOLIO_SIM_CACHE: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_PORTFOLIO_SIM_CACHE_MAX_ENTRIES = 32
_PORTFOLIO_SIM_CACHE_TTL = 3600  # 1 hour


def _get_portfolio_sim_cache(key):
    """Return (ts, payload) for a fresh cache entry, else None. LRU-refreshing."""
    entry = _PORTFOLIO_SIM_CACHE.get(key)
    if entry is None:
        return None
    ts, payload = entry
    import time as _time
    now = _time.time()
    if (now - ts) >= _PORTFOLIO_SIM_CACHE_TTL:
        # Expired: drop it so it cannot accumulate.
        _PORTFOLIO_SIM_CACHE.pop(key, None)
        return None
    # Refresh recency for LRU eviction.
    _PORTFOLIO_SIM_CACHE.move_to_end(key)
    return entry


def _store_portfolio_sim_cache(key, payload):
    """Insert into the bounded LRU cache; evict oldest past 32 entries."""
    while len(_PORTFOLIO_SIM_CACHE) >= _PORTFOLIO_SIM_CACHE_MAX_ENTRIES:
        _PORTFOLIO_SIM_CACHE.popitem(last=False)
    _PORTFOLIO_SIM_CACHE[key] = payload


async def _fetch_live_hypothesis_ids(db_path: str) -> list:
    """Read LIVE hypothesis IDs from sqlite off the event loop."""
    import sqlite3 as _sqlite3

    def _read():
        conn = _sqlite3.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    return await asyncio.to_thread(_read)


async def resolve_portfolio_ids(
    hypothesis_ids: str = "",
    all_live: bool = False,
) -> list:
    """Resolve the hypothesis IDs to simulate.

    ``all_live`` reads the current LIVE roster straight from sqlite;
    otherwise a CSV of IDs is split. Raises HTTPException when empty.
    """
    if all_live:
        db = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        ids = await _fetch_live_hypothesis_ids(db)
    else:
        ids = [x.strip() for x in hypothesis_ids.split(",") if x.strip()]

    if not ids:
        raise HTTPException(
            status_code=400,
            detail="No hypothesis_ids supplied (pass hypothesis_ids=a,b,c or all_live=1)",
        )
    return ids


def normalize_portfolio_params(n_sims: int, horizon_days: int):
    """Clamp simulation params to sane bounds."""
    return max(10, min(int(n_sims), 5000)), max(1, min(int(horizon_days), 365))


def build_portfolio_cache_key(
    ids: list,
    n_sims: int,
    horizon_days: int,
    starting_bankroll: float,
    kelly_fraction: float,
):
    """Unique input signature for the LRU result cache."""
    return (tuple(sorted(ids)), n_sims, horizon_days, float(starting_bankroll), float(kelly_fraction))


async def simulate_portfolio_endpoint(
    hypothesis_ids: str = "",
    n_sims: int = 500,
    horizon_days: int = 90,
    starting_bankroll: float = 10000.0,
    kelly_fraction: float = 0.25,
    all_live: bool = False,
):
    """Run a bankroll Monte Carlo simulation for a portfolio of hypotheses.

    Query params:
      hypothesis_ids: CSV of hypothesis IDs (ignored if all_live=1)
      all_live: if true, simulate the full current LIVE roster
      n_sims: number of paths (capped at 5000)
      horizon_days: per-path horizon (capped at 365)
      starting_bankroll: dollar amount each path starts with
      kelly_fraction: Kelly multiplier (0.25 default = quarter-Kelly)

    Results cached 1hr per unique input signature. The blocking
    ``simulate_portfolio`` call runs on a worker thread via
    ``asyncio.to_thread`` so the event loop stays responsive.
    """
    import time as _time
    from tools.bankroll_sim import simulate_portfolio

    ids = await resolve_portfolio_ids(hypothesis_ids=hypothesis_ids, all_live=all_live)
    n_sims, horizon_days = normalize_portfolio_params(n_sims, horizon_days)

    cache_key = build_portfolio_cache_key(
        ids, n_sims, horizon_days, starting_bankroll, kelly_fraction
    )
    now = _time.time()
    cached = _get_portfolio_sim_cache(cache_key)
    if cached:
        return {"cached": True, "age_seconds": round(now - cached[0], 1), **cached[1]}

    result = await asyncio.to_thread(
        simulate_portfolio,
        hypothesis_ids=ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
    )
    payload = result.to_dict(include_paths=False)
    _store_portfolio_sim_cache(cache_key, (now, payload))
    return {"cached": False, **payload}


async def simulate_portfolio_endpoint(
    hypothesis_ids: str = "",
    n_sims: int = 500,
    horizon_days: int = 90,
    starting_bankroll: float = 10000.0,
    kelly_fraction: float = 0.25,
    all_live: bool = False,
):
    """Bankroll Monte Carlo over a hypothesis portfolio (moved from api.py).

    Query params:
      hypothesis_ids: CSV of hypothesis IDs (ignored if all_live=1)
      all_live: if true, simulate the full current LIVE roster
      n_sims: number of paths (capped at 5000)
      horizon_days: per-path horizon (capped at 365)
      starting_bankroll: dollar amount each path starts with
      kelly_fraction: Kelly multiplier (0.25 default = quarter-Kelly)

    Results cached 1hr per unique input signature.
    """
    import time as _time
    from tools.bankroll_sim import simulate_portfolio

    ids = await resolve_portfolio_ids(hypothesis_ids=hypothesis_ids, all_live=all_live)
    n_sims, horizon_days = normalize_portfolio_params(n_sims, horizon_days)

    cache_key = build_portfolio_cache_key(
        ids, n_sims, horizon_days, starting_bankroll, kelly_fraction
    )
    now = _time.time()
    cached = _get_portfolio_sim_cache(cache_key)
    if cached:
        return {"cached": True, "age_seconds": round(now - cached[0], 1), **cached[1]}

    result = await asyncio.to_thread(
        simulate_portfolio,
        hypothesis_ids=ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
    )
    payload = result.to_dict(include_paths=False)
    _store_portfolio_sim_cache(cache_key, (now, payload))
    return {"cached": False, **payload}
