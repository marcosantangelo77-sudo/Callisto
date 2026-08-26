"""Stake math and shared arb-construction path for the arbitrage scanner."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

from tools.book_keys import get_book_max_stake

from tools.arb.models import ArbLeg, ArbOpportunity
from tools.arb.prices import _age_seconds

logger = logging.getLogger("callisto.arbitrage_scanner")

from tools.arb.models import (  # noqa: E402
    MAX_IMPLIED_DIVERGENCE,
    MAX_PROFIT_PCT,
    MIN_PROFIT_PCT,
)


# ---------------------------------------------------------------------------
# Stake math — the core arb calculation.
# ---------------------------------------------------------------------------
def _compute_stakes(
    legs: list[ArbLeg],
    budget: float,
    apply_book_caps: bool = True,
    market_type: str = "default",
) -> tuple[float, bool]:
    """Fill in each leg's ``stake`` such that all legs pay out the same.

    Returns (effective_budget, limited_by_book_caps).

    Math: for n outcomes with decimal odds d_i, target equal return R means
    R = stake_i * d_i for all i, so stake_i = R/d_i. Total outlay T =
    sum(stake_i) = R * sum(1/d_i). Setting T = budget gives R = budget /
    sum(1/d_i) and stake_i = (budget/d_i) / sum(1/d_i), i.e.
    stake_i = budget * implied_i / total_implied.
    """
    total_implied = sum(leg.implied_prob for leg in legs)
    if total_implied <= 0:
        return 0.0, False

    # Unconstrained stakes.
    for leg in legs:
        leg.stake = round(budget * leg.implied_prob / total_implied, 2)

    if not apply_book_caps:
        return budget, False

    # Find the tightest (leg_stake / book_cap) ratio > 1 — that leg forces
    # us to scale everything down. Equivalently: the effective budget is
    # budget * min(book_cap_i / leg_stake_i, 1.0) over all i.
    min_ratio = 1.0
    limited = False
    for leg in legs:
        cap = get_book_max_stake(leg.bookmaker_canonical, market_type)
        if leg.stake > cap:
            r = cap / leg.stake
            if r < min_ratio:
                min_ratio = r
            limited = True

    if min_ratio < 1.0:
        new_budget = round(budget * min_ratio, 2)
        for leg in legs:
            leg.stake = round(new_budget * leg.implied_prob / total_implied, 2)
            cap = get_book_max_stake(leg.bookmaker_canonical, market_type)
            if leg.stake > cap + 0.01:
                # Shouldn't happen after the global scale, but guard against
                # rounding pushing us 1 cent over.
                leg.stake = cap
                leg.stake_capped_by_book = True
            elif abs(leg.stake - cap) < 0.51:
                # We are right at the cap — flag it so downstream knows
                # this leg is the binding constraint.
                leg.stake_capped_by_book = True
        return new_budget, True

    return budget, False


def _scan_spread_arbs(
    game: dict,
    home_team: str,
    away_team: str,
    abs_pts: set,
    *,
    epsilon: float,
    stale_seconds: float,
    budget: float,
    now,
    allow_missing_ts: bool,
    sport: str,
) -> list["ArbOpportunity"]:
    """Emit pure spread arbs by pairing Home@+X with Away@-X (and symmetric).

    Each candidate line is |X|. For each |X| we try two pairings:
        (1) Home@+X + Away@-X    — home is the underdog getting X points
        (2) Home@-X + Away@+X    — home is the favorite giving X points

    For |X|=0 (pk) only pairing (1) applies; we collapse to avoid dupes.
    """
    from tools.arb.prices import _best_at

    arbs: list[ArbOpportunity] = []

    for absx in abs_pts:
        pairings = [
            (absx, -absx),   # Home +X / Away -X
            (-absx, absx),   # Home -X / Away +X
        ]
        if absx == 0:
            pairings = [(0.0, 0.0)]

        for home_pt, away_pt in pairings:
            h = _best_at(game, "spreads", home_team, home_pt)
            a = _best_at(game, "spreads", away_team, away_pt)
            if not h or not a:
                continue
            arb = _build_arb_from_pair(
                game=game, entries=[h, a], market_type="spreads",
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, allow_missing_ts=allow_missing_ts, sport=sport,
            )
            if arb is not None:
                arbs.append(arb)

    return arbs


def _build_arb_from_pair(
    *,
    game: dict,
    entries: list[dict],
    market_type: str,
    epsilon: float,
    stale_seconds: float,
    budget: float,
    now,
    allow_missing_ts: bool,
    sport: str,
) -> Optional["ArbOpportunity"]:
    """Shared arb construction path — takes a list of per-leg entries and
    returns an ArbOpportunity if (a) every leg is fresh, (b) total implied
    probability is below 1-epsilon, (c) profit exceeds MIN_PROFIT_PCT.
    """
    legs: list[ArbLeg] = []
    max_age = 0.0
    for e in entries:
        age = _age_seconds(e.get("fetched_at"), now)
        if age is None and not allow_missing_ts:
            return None
        if age is not None:
            if age > stale_seconds:
                return None
            if age > max_age:
                max_age = age
        legs.append(ArbLeg(
            bookmaker=e["bookmaker"],
            bookmaker_canonical=e["bookmaker_canonical"],
            outcome=e.get("outcome", ""),
            american_odds=e["american"],
            decimal_odds=e["decimal"],
            implied_prob=e["implied"],
            point=e.get("point"),
            fetched_at=e.get("fetched_at"),
            age_seconds=age,
        ))

    # Two legs from the same book can't be "cross-book arb" — reject.
    if len({leg.bookmaker_canonical for leg in legs}) < 2:
        # Unless it's a multi-way single-book dutch scenario — we still
        # allow same-book for 3+ outcomes because the book itself may be
        # offering a dutch (rare but possible on exchanges).
        if len(legs) < 3:
            return None

    total_implied = sum(leg.implied_prob for leg in legs)
    if total_implied >= 1.0 - epsilon:
        return None
    profit_pct = (1.0 - total_implied) / total_implied
    if profit_pct < MIN_PROFIT_PCT:
        return None
    if profit_pct > MAX_PROFIT_PCT:
        # Data-contamination guard — don't silently emit a 50% "arb".
        logger.debug(
            f"Rejecting suspiciously large arb ({profit_pct:.1%}) on "
            f"{game.get('home_team')}/{game.get('away_team')} {market_type}; "
            f"legs={[(l.bookmaker, l.american_odds, l.point) for l in legs]}"
        )
        return None
    # Binary-market divergence check: two quotes that disagree by more than
    # MAX_IMPLIED_DIVERGENCE almost certainly have a side or team swap somewhere.
    # Skipped for 3+-way markets because legitimate dutch books often have one
    # very short favorite and two longer legs (soccer 1-draw-2 commonly has
    # favorite implied 0.6 / draw 0.25 / dog 0.12, spread of 0.48).
    if len(legs) == 2:
        div = abs(legs[0].implied_prob - legs[1].implied_prob)
        if div > MAX_IMPLIED_DIVERGENCE:
            logger.debug(
                f"Rejecting implied-divergent arb (|{legs[0].implied_prob:.3f} - "
                f"{legs[1].implied_prob:.3f}| = {div:.3f}) on "
                f"{game.get('home_team')}/{game.get('away_team')} {market_type}"
            )
            return None

    effective_budget, limited = _compute_stakes(
        legs, budget=budget, apply_book_caps=True, market_type=market_type
    )
    expected_profit = round(effective_budget * profit_pct, 2)
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    detected = now
    expires = detected + timedelta(seconds=60)

    return ArbOpportunity(
        game_id=str(game.get("id", "")),
        game=f"{away} @ {home}",
        sport=sport or game.get("sport_key", ""),
        market_type=market_type,
        thesis_tag="arb" if len(legs) == 2 else "dutch",
        total_implied=round(total_implied, 6),
        profit_pct=round(profit_pct, 6),
        expected_profit=expected_profit,
        budget_requested=budget,
        effective_budget=effective_budget,
        legs=legs,
        limited_by_book_caps=limited,
        max_leg_age_s=round(max_age, 1),
        detected_at=detected.isoformat(),
        expires_at=expires.isoformat(),
        notes=(
            f"pure {market_type} arb ({len(legs)} legs, "
            f"total_implied={total_implied:.4f})"
        ),
    )
