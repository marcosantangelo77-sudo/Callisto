"""Bets/bankroll route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

Handlers access api.py's module-level singleton ``clv_tracker`` via a
late ``from api import ...`` inside the function body to avoid a circular
import at module load time.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field


class BetSubmission(BaseModel):
    sport: str = Field(..., min_length=1, max_length=64)
    game_description: str = Field(..., min_length=1, max_length=512)
    team: str = Field(..., min_length=1, max_length=128)
    market: str = Field(..., min_length=1, max_length=64)
    bookmaker: str = Field(..., min_length=1, max_length=64)
    placement_odds: int = Field(..., ge=-10000, le=10000)
    placement_point: Optional[float] = Field(default=None, ge=-1000, le=1000)
    stake: float = Field(default=100, ge=0, le=1_000_000)
    event_id: str = Field(default="", max_length=128)
    edge_estimate: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    notes: str = Field(default="", max_length=2000)


class BetResolution(BaseModel):
    result: str = Field(..., pattern="^(won|lost|push)$")
    payout: Optional[float] = Field(default=None, ge=0, le=10_000_000)


async def record_bet(bet: BetSubmission):
    """Record a bet at placement time for CLV tracking."""
    from api import clv_tracker

    bet_id = await clv_tracker.record_bet(
        sport=bet.sport,
        game_description=bet.game_description,
        team=bet.team,
        market=bet.market,
        bookmaker=bet.bookmaker,
        placement_odds=bet.placement_odds,
        placement_point=bet.placement_point,
        stake=bet.stake,
        event_id=bet.event_id,
        edge_estimate=bet.edge_estimate,
        notes=bet.notes,
    )
    return {"bet_id": bet_id}


async def resolve_bet(bet_id: int, resolution: BetResolution):
    """Resolve a bet as won/lost/push."""
    from api import clv_tracker

    return await clv_tracker.resolve_bet(bet_id, resolution.result, resolution.payout)


async def clv_report(sport: Optional[str] = None):
    """Get CLV performance report — THE metric for edge measurement."""
    from api import clv_tracker

    return await clv_tracker.get_clv_report(sport=sport)


async def list_bets(result: Optional[str] = None, sport: Optional[str] = None, limit: int = 50):
    """Get bet history."""
    from api import clv_tracker

    return await clv_tracker.get_all_bets(result=result, sport=sport, limit=limit)


async def bankroll_history(limit: int = 50):
    """Get bankroll balance history."""
    from api import clv_tracker

    return await clv_tracker.get_bankroll_history(limit=limit)


async def init_bankroll(balance: float):
    """Set initial bankroll balance."""
    from api import clv_tracker

    if balance < 0 or balance > 100_000_000:
        raise HTTPException(status_code=422, detail="balance out of range (0..100M)")
    await clv_tracker.set_initial_bankroll(balance)
    return {"balance": balance}


async def clv_forecast(sport: Optional[str] = None):
    """Forecast pre-game CLV for all pending bets using closing line prediction.

    Uses market psychology's predict_closing_line to estimate where each
    bet's line will close, giving a CLV estimate before the game starts.
    Useful for paper-trading evaluation.
    """
    from api import clv_tracker

    if not clv_tracker:
        raise HTTPException(status_code=503, detail="CLV tracker not initialized")
    return await clv_tracker.forecast_clv(sport=sport)
