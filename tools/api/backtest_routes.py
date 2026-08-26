"""Backtest/historical route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singletons (``backtest_engine``,
``historical_fetcher``) via a late ``from api import ...`` inside the
function body to avoid a circular import at module load time.
"""

from __future__ import annotations


async def run_backtest(req):
    """Start a backtest run on a hypothesis against historical data."""
    from api import backtest_engine
    return await backtest_engine.run_backtest(
        hypothesis_id=req.hypothesis_id,
        start_date=req.start_date,
        end_date=req.end_date,
        credit_budget=req.credit_budget,
    )


async def get_backtest_results(run_id: str):
    """Get backtest results for a run."""
    from api import backtest_engine
    return await backtest_engine.get_run_results(run_id)


async def resolve_backtest(run_id: str, sport: str = "basketball_nba"):
    """Resolve backtest events against actual game results."""
    from api import backtest_engine
    return await backtest_engine.resolve_with_scores(run_id, sport)


async def historical_cache_stats():
    """Get historical odds cache statistics."""
    from api import historical_fetcher
    return await historical_fetcher.get_cache_stats()


async def fetch_historical(sport: str, start_date: str, end_date: str,
                           credit_budget: int = 50):
    """Fetch historical odds for a date range (cached after first fetch)."""
    from api import historical_fetcher
    return await historical_fetcher.bulk_fetch_date_range(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        credit_budget=credit_budget,
    )
