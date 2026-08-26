"""
Profit boost evaluator — systematic +EV extraction from sportsbook promotions.

Every profit boost is a mispriced option. Books offer them for customer
acquisition/retention. Our job: determine if boosted odds exceed true
probability, and if so, exploit optimally.

Three evaluation modes:
1. Fixed boost: Specific bet with specific boosted odds → devig and compare
2. Percentage boost: X% token on a bet of your choosing → optimize selection
3. Free bet / no-sweat: Refunded if lost → different EV calculation

Devigging priority: Pinnacle > sharp multi-book consensus > model estimate.
"""

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, calculate_ev
from tools.devig import additive_devig
from tools.math_utils import (
    no_vig_price,
    calculate_hold,
    american_to_decimal,
    fair_prob_to_american,
)

logger = logging.getLogger("callisto.boost_evaluator")


def devig_multiplicative(side_a_odds: int, side_b_odds: int) -> tuple[float, float]:
    """
    Devig a two-way market using the multiplicative method (preferred).

    Delegates to math_utils.no_vig_price which implements the same
    multiplicative devigging: divide each side's implied probability
    by the total overround. Most accurate for balanced markets.

    Returns (fair_prob_a, fair_prob_b).
    """
    fair_a, fair_b = no_vig_price(side_a_odds, side_b_odds)
    return round(fair_a, 6), round(fair_b, 6)


def devig_additive(side_a_odds: int, side_b_odds: int) -> tuple[float, float]:
    """
    Devig using the additive method — subtract equal vig from each side.

    Less accurate than multiplicative for lopsided markets but simpler.

    Routes through the shared market-sanity gate (tools.devig.additive_devig):
    invalid American odds or an unsanitary book (zero-hold, crossed, or
    excessive overround) raises ValueError instead of returning
    trustworthy-looking probabilities.

    Returns (fair_prob_a, fair_prob_b).
    """
    dec_a = american_to_decimal(side_a_odds)
    dec_b = american_to_decimal(side_b_odds)
    fair_a, fair_b = additive_devig([dec_a, dec_b])
    return round(fair_a, 6), round(fair_b, 6)


def devig_multibook(book_odds: list[dict]) -> Optional[float]:
    """
    Devig using multi-book consensus — average devigged probabilities
    across multiple books, weighting sharper books more heavily.

    Args:
        book_odds: List of {"bookmaker": str, "odds_for": int, "odds_against": int}

    Returns fair probability for the "for" side, or None when no book in
    the list yields a trustworthy fair value (empty/malformed entries,
    invalid American odds, or an unsanitary book). Never fabricates a
    neutral 0.5 — an unpriceable market must not look like a fair coin.
    """
    if not book_odds or not isinstance(book_odds, list):
        return None

    SHARP_WEIGHT = {"pinnacle": 3.0, "lowvig.ag": 2.5, "circa": 2.5,
                    "bookmaker.eu": 2.0, "betonline.ag": 2.0, "betcris": 2.0}
    DEFAULT_WEIGHT = 1.0

    weighted_sum = 0.0
    total_weight = 0.0

    for entry in book_odds:
        if not isinstance(entry, dict):
            continue
        book = (entry.get("bookmaker") or "").lower()
        odds_for = entry.get("odds_for")
        odds_against = entry.get("odds_against")
        if odds_for is None or odds_against is None:
            continue

        try:
            fair_for, _ = devig_multiplicative(odds_for, odds_against)
        except (ValueError, TypeError):
            # Invalid odds or unsanitary book: this entry contributes
            # nothing rather than poisoning the average.
            continue

        weight = SHARP_WEIGHT.get(book, DEFAULT_WEIGHT)
        weighted_sum += fair_for * weight
        total_weight += weight

    if total_weight == 0:
        return None

    return round(weighted_sum / total_weight, 6)


def evaluate_fixed_boost(
    boosted_odds: int,
    fair_probability: float,
    max_stake: float = 100,
    description: str = "",
    book: str = "",
) -> dict:
    """
    Evaluate a fixed profit boost (specific bet with specific boosted odds).

    This is the most common boost type: "Celtics ML boosted to +100"
    or "Jokic 25+ points boosted to +200".

    Args:
        boosted_odds: The boosted American odds being offered
        fair_probability: Devigged true probability of the outcome
        max_stake: Maximum allowed wager
        description: Boost description
        book: Sportsbook offering the boost
    """
    from tools.math_utils import validate_american_odds
    boosted_am = validate_american_odds(boosted_odds)
    if not isinstance(fair_probability, (int, float)) or isinstance(fair_probability, bool) \
            or not (0.0 < float(fair_probability) < 1.0):
        raise ValueError(
            f"fair_probability must be strictly inside (0, 1), got {fair_probability!r}")

    boosted_implied = calculate_implied_probability(boosted_am)
    edge = fair_probability - boosted_implied

    # Calculate EV
    if boosted_odds > 0:
        profit_if_win = max_stake * (boosted_odds / 100)
    else:
        profit_if_win = max_stake * (100 / abs(boosted_odds))

    ev_dollar = (fair_probability * profit_if_win) - ((1 - fair_probability) * max_stake)
    ev_pct = (ev_dollar / max_stake) * 100

    # Edge rating
    if ev_pct > 15:
        rating = "EXCEPTIONAL"
    elif ev_pct > 7:
        rating = "STRONG"
    elif ev_pct > 3:
        rating = "GOOD"
    elif ev_pct > 0:
        rating = "MARGINAL"
    else:
        rating = "NO_EDGE"

    # Kelly criterion
    kelly = calculate_ev(probability=fair_probability, american_odds=boosted_odds, stake=max_stake)

    # Fair odds (what the odds SHOULD be)
    fair_american = fair_prob_to_american(fair_probability)

    return {
        "description": description,
        "book": book,
        "type": "FIXED_BOOST",
        "max_stake": max_stake,
        "boosted_odds": boosted_odds,
        "boosted_decimal": american_to_decimal(boosted_odds),
        "boosted_implied": round(boosted_implied, 4),
        "fair_probability": round(fair_probability, 4),
        "fair_odds_american": fair_american,
        "edge": round(edge, 4),
        "edge_pct": round(edge * 100, 2),
        "ev_dollar": round(ev_dollar, 2),
        "ev_pct": round(ev_pct, 2),
        "rating": rating,
        "kelly": kelly,
        "recommendation": _recommendation(ev_pct, max_stake),
    }


def evaluate_percentage_boost(
    boost_pct: float,
    base_odds: int,
    fair_probability: float,
    max_stake: float = 100,
    description: str = "",
    book: str = "",
) -> dict:
    """
    Evaluate a percentage profit boost token.

    The boost adds X% to the PROFIT (not the total payout).
    Example: 30% boost on +200 → profit goes from $200 to $260 on $100 bet.

    Args:
        boost_pct: Boost percentage (e.g., 30 for 30%)
        base_odds: The unboosted American odds you're applying the token to
        fair_probability: Devigged true probability
        max_stake: Maximum allowed wager
    """
    # Calculate base and boosted payouts
    if base_odds > 0:
        base_profit = max_stake * (base_odds / 100)
    else:
        base_profit = max_stake * (100 / abs(base_odds))

    boosted_profit = base_profit * (1 + boost_pct / 100)
    boosted_payout = max_stake + boosted_profit

    # Convert boosted payout back to effective odds
    effective_decimal = boosted_payout / max_stake
    if effective_decimal >= 2.0:
        effective_american = int((effective_decimal - 1) * 100)
    else:
        effective_american = int(-100 / (effective_decimal - 1))

    # EV calculation with boosted profit
    ev_dollar = (fair_probability * boosted_profit) - ((1 - fair_probability) * max_stake)
    ev_pct = (ev_dollar / max_stake) * 100

    # Also calculate EV WITHOUT boost for comparison
    ev_no_boost = (fair_probability * base_profit) - ((1 - fair_probability) * max_stake)

    boost_added_ev = ev_dollar - ev_no_boost

    if ev_pct > 15:
        rating = "EXCEPTIONAL"
    elif ev_pct > 7:
        rating = "STRONG"
    elif ev_pct > 3:
        rating = "GOOD"
    elif ev_pct > 0:
        rating = "MARGINAL"
    else:
        rating = "NO_EDGE"

    return {
        "description": description,
        "book": book,
        "type": "PERCENTAGE_BOOST",
        "boost_pct": boost_pct,
        "max_stake": max_stake,
        "base_odds": base_odds,
        "effective_odds": effective_american,
        "effective_decimal": round(effective_decimal, 3),
        "fair_probability": round(fair_probability, 4),
        "base_profit": round(base_profit, 2),
        "boosted_profit": round(boosted_profit, 2),
        "ev_dollar": round(ev_dollar, 2),
        "ev_pct": round(ev_pct, 2),
        "ev_without_boost": round(ev_no_boost, 2),
        "boost_added_ev": round(boost_added_ev, 2),
        "rating": rating,
        "recommendation": _recommendation(ev_pct, max_stake),
        "optimal_odds_range": (
            "Apply to +150 to +400 range for maximum dollar EV impact. "
            "Longer odds amplify the boost's dollar value."
        ),
    }


def evaluate_free_bet(
    free_bet_amount: float,
    bet_odds: int,
    fair_probability: float,
    stake_returned: bool = False,
    description: str = "",
    book: str = "",
) -> dict:
    """
    Evaluate a free bet or no-sweat bet.

    Key difference: on a free bet, you DON'T get the stake back if you win.
    Free bet value = probability × (decimal_odds - 1) × free_bet_amount

    For no-sweat bets: if it loses, you get the stake back as site credit.
    No-sweat value = (prob × profit) + ((1-prob) × credit_value)
    Credit conversion rate ≈ 70-80% of face value.

    Args:
        free_bet_amount: Size of the free bet
        bet_odds: Odds to use the free bet on
        fair_probability: True probability of the outcome
        stake_returned: True for regular bets, False for free bets
        description: Description of the promo
        book: Sportsbook
    """
    decimal_odds = american_to_decimal(bet_odds)

    if stake_returned:
        # Regular bet or no-sweat (stake returned as credit if lost)
        if bet_odds > 0:
            profit = free_bet_amount * (bet_odds / 100)
        else:
            profit = free_bet_amount * (100 / abs(bet_odds))

        # No-sweat: if lost, get credit back at ~75% conversion
        credit_conversion = 0.75
        ev = (fair_probability * profit) + ((1 - fair_probability) * free_bet_amount * credit_conversion)
        ev_dollar = ev - free_bet_amount  # Net of the original stake
    else:
        # Pure free bet: win = profit only (no stake return), lose = $0
        profit = free_bet_amount * (decimal_odds - 1)
        ev = fair_probability * profit
        ev_dollar = ev  # No stake at risk

    # Optimal free bet strategy: use on moderate underdog
    optimal_range_low = 200
    optimal_range_high = 400

    return {
        "description": description,
        "book": book,
        "type": "NO_SWEAT" if stake_returned else "FREE_BET",
        "free_bet_amount": free_bet_amount,
        "bet_odds": bet_odds,
        "decimal_odds": round(decimal_odds, 3),
        "fair_probability": round(fair_probability, 4),
        "profit_if_win": round(profit, 2),
        "expected_value": round(ev, 2),
        "ev_dollar": round(ev_dollar, 2),
        "ev_pct": round((ev_dollar / free_bet_amount) * 100, 2) if free_bet_amount > 0 else 0,
        "conversion_rate": round((ev / free_bet_amount) * 100, 1) if free_bet_amount > 0 else 0,
        "recommendation": (
            f"Use on a moderate underdog (+{optimal_range_low} to +{optimal_range_high}) "
            f"to maximize conversion. Current odds ({bet_odds}) "
            f"{'are in optimal range' if optimal_range_low <= bet_odds <= optimal_range_high else 'could be optimized'}."
        ),
    }


def calculate_hedge(
    boost_stake: float,
    boosted_odds: int,
    hedge_odds: int,
    fair_probability: float,
) -> dict:
    """
    Calculate the optimal hedge to lock in guaranteed profit.

    If a boost is +EV but you want to reduce variance, hedging the
    opposite side at another book guarantees profit regardless of outcome.

    Args:
        boost_stake: Amount wagered on the boosted side
        boosted_odds: The boosted American odds
        hedge_odds: Best available odds on the opposite side (at another book)
        fair_probability: True probability of the boosted side winning
    """
    boosted_decimal = american_to_decimal(boosted_odds)
    hedge_decimal = american_to_decimal(hedge_odds)

    # Total payout if boosted bet wins
    boosted_payout = boost_stake * boosted_decimal

    # Hedge stake to equalize payouts
    # If boost wins: boosted_payout - boost_stake - hedge_stake = profit
    # If hedge wins: hedge_stake × hedge_decimal - boost_stake - hedge_stake = profit
    # Set equal: boosted_payout - hedge_stake = hedge_stake × hedge_decimal
    # hedge_stake = boosted_payout / (1 + hedge_decimal)
    # Actually: we want guaranteed profit
    # If boost wins: boosted_payout - boost_stake - hedge_stake
    # If hedge wins: hedge_stake × hedge_decimal - boost_stake - hedge_stake
    # Set equal:
    # boosted_payout - boost_stake - hedge_stake = hedge_stake × hedge_decimal - boost_stake - hedge_stake
    # boosted_payout = hedge_stake × hedge_decimal
    # hedge_stake = boosted_payout / hedge_decimal

    hedge_stake = boosted_payout / hedge_decimal

    # Guaranteed profit
    profit_if_boost_wins = boosted_payout - boost_stake - hedge_stake
    profit_if_hedge_wins = (hedge_stake * hedge_decimal) - boost_stake - hedge_stake

    guaranteed_profit = min(profit_if_boost_wins, profit_if_hedge_wins)
    total_outlay = boost_stake + hedge_stake
    roi = (guaranteed_profit / total_outlay) * 100 if total_outlay > 0 else 0

    # Compare to letting it ride (EV-based decision)
    if boosted_odds > 0:
        boost_profit = boost_stake * (boosted_odds / 100)
    else:
        boost_profit = boost_stake * (100 / abs(boosted_odds))

    ride_ev = (fair_probability * boost_profit) - ((1 - fair_probability) * boost_stake)

    return {
        "boost_stake": boost_stake,
        "boosted_odds": boosted_odds,
        "hedge_odds": hedge_odds,
        "hedge_stake": round(hedge_stake, 2),
        "total_outlay": round(total_outlay, 2),
        "profit_if_boost_wins": round(profit_if_boost_wins, 2),
        "profit_if_hedge_wins": round(profit_if_hedge_wins, 2),
        "guaranteed_profit": round(guaranteed_profit, 2),
        "guaranteed_roi": round(roi, 2),
        "ride_ev": round(ride_ev, 2),
        "recommendation": (
            "LET IT RIDE — long-run EV of not hedging exceeds guaranteed profit"
            if ride_ev > guaranteed_profit and ride_ev > 5
            else "HEDGE — lock in guaranteed profit, reduce variance"
        ),
    }


def find_optimal_boost_target(
    boost_pct: float,
    available_bets: list[dict],
    max_stake: float = 100,
) -> list[dict]:
    """
    For a percentage boost token, find the optimal bet to apply it to.

    Principles:
    1. Longer odds benefit MORE from percentage boosts in dollar terms
    2. But the underlying bet must be near fair odds
    3. Sweet spot: +150 to +400 range
    4. Maximize: true_prob × (boost% × profit) = boost-added dollar EV

    Args:
        boost_pct: The boost percentage (e.g., 30)
        available_bets: List of {"odds": int, "fair_probability": float, "description": str}
        max_stake: Maximum bet size
    """
    candidates = []

    for bet in available_bets:
        odds = bet.get("odds", -110)
        fair_prob = bet.get("fair_probability", 0.5)
        desc = bet.get("description", "")

        # Calculate profit
        if odds > 0:
            base_profit = max_stake * (odds / 100)
        else:
            base_profit = max_stake * (100 / abs(odds))

        boosted_profit = base_profit * (1 + boost_pct / 100)
        boost_added_profit = boosted_profit - base_profit

        # EV of the boost component specifically
        boost_added_ev = fair_prob * boost_added_profit

        # Total EV with boost
        total_ev = (fair_prob * boosted_profit) - ((1 - fair_prob) * max_stake)

        candidates.append({
            "description": desc,
            "odds": odds,
            "fair_probability": round(fair_prob, 4),
            "base_profit": round(base_profit, 2),
            "boosted_profit": round(boosted_profit, 2),
            "boost_added_ev": round(boost_added_ev, 2),
            "total_ev": round(total_ev, 2),
            "total_ev_pct": round((total_ev / max_stake) * 100, 2),
            "is_positive_ev": total_ev > 0,
        })

    # Sort by boost-added EV (how much value the boost specifically creates)
    candidates.sort(key=lambda x: x["boost_added_ev"], reverse=True)
    return candidates


def evaluate_purchased_boost(
    boost_cost: float,
    boost_pct: float,
    base_odds: int,
    fair_probability: float,
    max_stake: float = 100,
    description: str = "",
    book: str = "Fanatics",
) -> dict:
    """
    Evaluate a PURCHASED profit boost (e.g., Fanatics boost tokens).

    Same as evaluate_percentage_boost but subtracts the purchase cost
    from the EV to determine if buying the boost is net +EV.

    Args:
        boost_cost: Dollar cost to purchase the boost
        boost_pct: Boost percentage (e.g., 50 for 50%)
        base_odds: Unboosted American odds
        fair_probability: Devigged true probability
        max_stake: Maximum bet with the boost
        description: What the boost is for
        book: Sportsbook selling the boost
    """
    # Get the standard boost evaluation
    boost_eval = evaluate_percentage_boost(
        boost_pct=boost_pct,
        base_odds=base_odds,
        fair_probability=fair_probability,
        max_stake=max_stake,
        description=description,
        book=book,
    )

    # Calculate net EV after purchase cost
    gross_ev = boost_eval["ev_dollar"]
    net_ev = gross_ev - boost_cost
    net_ev_pct = (net_ev / max_stake) * 100

    # What's the breakeven boost cost?
    breakeven_cost = gross_ev if gross_ev > 0 else 0

    return {
        **boost_eval,
        "type": "PURCHASED_BOOST",
        "boost_cost": boost_cost,
        "gross_ev": round(gross_ev, 2),
        "net_ev": round(net_ev, 2),
        "net_ev_pct": round(net_ev_pct, 2),
        "breakeven_cost": round(breakeven_cost, 2),
        "purchase_recommended": net_ev > 0,
        "recommendation": (
            f"BUY — net +${net_ev:.2f} after ${boost_cost:.2f} cost ({net_ev_pct:+.1f}% ROI)"
            if net_ev > 0
            else f"PASS — boost costs ${boost_cost:.2f} but only adds ${gross_ev:.2f} EV"
        ),
    }


def _recommendation(ev_pct: float, max_stake: float) -> str:
    """Generate a recommendation string based on EV percentage."""
    if ev_pct > 15:
        return f"SLAM — max bet ${max_stake:.0f}. Exceptional edge."
    elif ev_pct > 7:
        return f"Strong play — bet ${max_stake:.0f}. Clear +EV."
    elif ev_pct > 3:
        return f"Good value — bet ${max_stake:.0f}. Solid edge."
    elif ev_pct > 0:
        return f"Marginal — small edge. Bet if volume matters."
    else:
        return "Pass — no edge or negative EV."


def evaluate_boosted_parlay(
    legs: list[dict],
    boosted_parlay_odds: int,
    sport: str,
    max_stake: float = 100,
    description: str = "",
    book: str = "",
) -> dict:
    """
    Evaluate a boosted parlay using correlation-adjusted fair odds.

    Books often boost parlays with correlated legs, making the boost look
    more generous than it is. This function uses the correlation engine
    to compute the TRUE fair probability, then compares to the boosted odds.

    Without correlation adjustment, a "50% boost" on a 2-leg parlay where
    both legs are 0.65-correlated looks like +20% EV. With adjustment,
    the true probability is higher (because the legs tend to hit together),
    so the actual edge may be only +5%.

    Args:
        legs: List of dicts with:
            - "american_odds" (int): individual leg odds (pre-boost)
            - "market" (str): market type for correlation lookup
            - "description" (str, optional): leg description
        boosted_parlay_odds: The boosted American odds the book is offering
        sport: Sport key for correlation lookup
        max_stake: Maximum allowed wager
        description: Boost description
        book: Sportsbook offering the boost
    """
    from tools.correlation import (
        correlated_parlay_odds,
        independent_parlay_odds,
        detect_anti_correlation,
    )

    if not legs or len(legs) < 2:
        return {"error": "Need at least 2 legs for parlay evaluation"}

    # Independent parlay odds (naive: assumes no correlation)
    independent_odds = independent_parlay_odds(legs)

    # Correlation-adjusted fair parlay odds
    fair_odds = correlated_parlay_odds(legs, sport=sport)

    # Convert to probabilities for EV calculation
    boosted_implied = calculate_implied_probability(boosted_parlay_odds)
    independent_implied = calculate_implied_probability(independent_odds) if independent_odds != 0 else 0.0
    fair_implied = calculate_implied_probability(fair_odds) if fair_odds != 0 else 0.0

    # Edge: fair probability - boosted implied probability
    # If fair_implied > boosted_implied, the parlay hits more often than
    # the boosted price suggests -> +EV
    edge_naive = independent_implied - boosted_implied  # edge assuming independence
    edge_correlated = fair_implied - boosted_implied     # edge with correlation adjustment

    # EV calculation using correlation-adjusted fair probability
    if boosted_parlay_odds > 0:
        profit_if_win = max_stake * (boosted_parlay_odds / 100)
    else:
        profit_if_win = max_stake * (100 / abs(boosted_parlay_odds))

    ev_naive = (independent_implied * profit_if_win) - ((1 - independent_implied) * max_stake)
    ev_correlated = (fair_implied * profit_if_win) - ((1 - fair_implied) * max_stake)
    ev_pct_naive = (ev_naive / max_stake) * 100 if max_stake > 0 else 0
    ev_pct_correlated = (ev_correlated / max_stake) * 100 if max_stake > 0 else 0

    # Check for anti-correlated legs
    anti_warnings = detect_anti_correlation(legs, sport)

    # Rating based on correlation-adjusted EV
    if ev_pct_correlated > 15:
        rating = "EXCEPTIONAL"
    elif ev_pct_correlated > 7:
        rating = "STRONG"
    elif ev_pct_correlated > 3:
        rating = "GOOD"
    elif ev_pct_correlated > 0:
        rating = "MARGINAL"
    else:
        rating = "NO_EDGE"

    correlation_impact = ev_pct_correlated - ev_pct_naive

    return {
        "description": description,
        "book": book,
        "type": "BOOSTED_PARLAY",
        "max_stake": max_stake,
        "num_legs": len(legs),
        "boosted_odds": boosted_parlay_odds,
        "boosted_implied": round(boosted_implied, 4),
        "independent_odds": independent_odds,
        "independent_implied": round(independent_implied, 4),
        "fair_correlated_odds": fair_odds,
        "fair_correlated_implied": round(fair_implied, 4),
        "edge_naive": round(edge_naive, 4),
        "edge_correlated": round(edge_correlated, 4),
        "ev_naive_dollar": round(ev_naive, 2),
        "ev_naive_pct": round(ev_pct_naive, 2),
        "ev_correlated_dollar": round(ev_correlated, 2),
        "ev_correlated_pct": round(ev_pct_correlated, 2),
        "correlation_impact_pct": round(correlation_impact, 2),
        "anti_correlation_warnings": anti_warnings if anti_warnings else None,
        "rating": rating,
        "recommendation": (
            _recommendation(ev_pct_correlated, max_stake)
            + (f" Correlation adjustment: {correlation_impact:+.1f}% EV impact."
               if abs(correlation_impact) > 0.5 else "")
            + (" WARNING: legs are anti-correlated." if anti_warnings else "")
        ),
        "legs": [
            {
                "odds": leg.get("american_odds", 0),
                "market": leg.get("market", ""),
                "description": leg.get("description", ""),
            }
            for leg in legs
        ],
    }


# _prob_to_american removed — now uses math_utils.fair_prob_to_american
