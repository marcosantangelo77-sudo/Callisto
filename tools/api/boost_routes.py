"""Profit-boost evaluator route handler bodies (moved from api.py, slice 4).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

All heavy lifting is delegated to ``tools.boost_evaluator``; this module
only owns the request schemas and HTTP-facing bodies.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------

class FixedBoostRequest(BaseModel):
    boosted_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class PctBoostRequest(BaseModel):
    boost_pct: float
    base_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class FreeBetRequest(BaseModel):
    free_bet_amount: float
    bet_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    stake_returned: bool = False
    description: str = ""
    book: str = ""


class HedgeRequest(BaseModel):
    boost_stake: float
    boosted_odds: int
    hedge_odds: int
    fair_probability: float


class BoostedParlayLeg(BaseModel):
    american_odds: int
    market: str
    description: str = ""


class BoostedParlayRequest(BaseModel):
    legs: list[BoostedParlayLeg]
    boosted_parlay_odds: int
    sport: str
    max_stake: float = 100
    description: str = ""
    book: str = ""


class DevigRequest(BaseModel):
    odds_a: int
    odds_b: int


# ---------------------------------------------------------------------------
# Handler bodies
# ---------------------------------------------------------------------------

async def eval_fixed_boost(req: FixedBoostRequest):
    """Evaluate a fixed profit boost — devig, compare to fair, calculate edge."""
    from tools.boost_evaluator import evaluate_fixed_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_fixed_boost(
        boosted_odds=req.boosted_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


async def eval_pct_boost(req: PctBoostRequest):
    """Evaluate a percentage profit boost token."""
    from tools.boost_evaluator import evaluate_percentage_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_percentage_boost(
        boost_pct=req.boost_pct,
        base_odds=req.base_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


async def eval_free_bet(req: FreeBetRequest):
    """Evaluate a free bet or no-sweat bet."""
    from tools.boost_evaluator import evaluate_free_bet, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_free_bet(
        free_bet_amount=req.free_bet_amount,
        bet_odds=req.bet_odds,
        fair_probability=fair_prob,
        stake_returned=req.stake_returned,
        description=req.description,
        book=req.book,
    )


async def hedge_calc(req: HedgeRequest):
    """Calculate optimal hedge for guaranteed profit."""
    from tools.boost_evaluator import calculate_hedge

    return calculate_hedge(
        boost_stake=req.boost_stake,
        boosted_odds=req.boosted_odds,
        hedge_odds=req.hedge_odds,
        fair_probability=req.fair_probability,
    )


async def devig(req: DevigRequest):
    """Devig a two-way market using multiplicative method."""
    from tools.boost_evaluator import devig_multiplicative, devig_additive

    mult_a, mult_b = devig_multiplicative(req.odds_a, req.odds_b)
    add_a, add_b = devig_additive(req.odds_a, req.odds_b)

    return {
        "multiplicative": {"side_a": mult_a, "side_b": mult_b},
        "additive": {"side_a": add_a, "side_b": add_b},
        "recommended": "multiplicative",
    }


async def eval_boosted_parlay(req: BoostedParlayRequest):
    """Evaluate a boosted parlay using correlation-adjusted fair odds.

    Books often boost parlays with correlated legs, making the boost look
    more generous than it is. This computes the TRUE fair probability using
    the correlation engine, then compares to the boosted odds.
    """
    from tools.boost_evaluator import evaluate_boosted_parlay

    legs = [leg.dict() for leg in req.legs]
    return evaluate_boosted_parlay(
        legs=legs,
        boosted_parlay_odds=req.boosted_parlay_odds,
        sport=req.sport,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )
