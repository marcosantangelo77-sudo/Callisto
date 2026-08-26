"""
Kelly Criterion and bankroll optimization for Callisto.

Full-spectrum bankroll management:
- Classic full Kelly and fractional Kelly (quarter-Kelly default for sharps)
- Dynamic Kelly that integrates AGP confidence tiers and variance
- Simultaneous Kelly for correlated multi-bet portfolios
- Bankroll ruin probability modeling
- Optimal bet timing via line movement EV estimation
- Unit sizing from bankroll with confidence-weighted scaling

The central insight: bet sizing matters as much as bet selection.
A 3% edge with tight variance and VERIFIED confidence deserves
more capital than a 5% edge with wide variance and SPECULATIVE data.
Kelly maximizes long-run geometric growth rate — but full Kelly is
too aggressive for real-world variance. Quarter Kelly is the sweet spot
for sharps who want growth without ruin.
"""

import logging
import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.kelly")


# ---------------------------------------------------------------------------
# AGP confidence tier -> Kelly multiplier mapping
# VERIFIED gets full fraction; lower tiers get proportionally reduced.
# These are multiplicative on top of the fractional Kelly factor.
# ---------------------------------------------------------------------------
AGP_TIER_MULTIPLIERS = {
    "VERIFIED":      1.00,   # >= 0.90: sharp-book confirmed edge
    "CORROBORATED":  0.80,   # >= 0.75: multi-source confirmed
    "PROBABLE":      0.55,   # >= 0.55: reasonable evidence
    "SPECULATIVE":   0.30,   # >= 0.30: thin evidence, size down hard
    "UNVERIFIED":    0.00,   # <  0.30: do not bet
}

# Sport-level line movement volatility profiles (std dev of closing line
# movement in points/percentage per hour remaining).  Estimated from
# historical CLV distributions.  Used by timing_value().
LINE_MOVEMENT_PROFILES = {
    "basketball_nba": {
        "early_vol": 0.025,   # 24h+ out: low volatility
        "mid_vol":   0.040,   # 4-24h out: moderate
        "late_vol":  0.070,   # <4h out:   highest
        "steam_prob": 0.12,   # probability of a steam move in any hour
    },
    "basketball_ncaab": {
        "early_vol": 0.030,
        "mid_vol":   0.050,
        "late_vol":  0.080,
        "steam_prob": 0.15,
    },
    "americanfootball_nfl": {
        "early_vol": 0.015,
        "mid_vol":   0.025,
        "late_vol":  0.050,
        "steam_prob": 0.08,
    },
    "americanfootball_ncaaf": {
        "early_vol": 0.020,
        "mid_vol":   0.035,
        "late_vol":  0.060,
        "steam_prob": 0.10,
    },
    "baseball_mlb": {
        "early_vol": 0.020,
        "mid_vol":   0.045,
        "late_vol":  0.075,
        "steam_prob": 0.14,
    },
    "icehockey_nhl": {
        "early_vol": 0.018,
        "mid_vol":   0.030,
        "late_vol":  0.055,
        "steam_prob": 0.09,
    },
}

# Default profile for sports not explicitly listed
_DEFAULT_MOVEMENT_PROFILE = {
    "early_vol": 0.025,
    "mid_vol":   0.040,
    "late_vol":  0.065,
    "steam_prob": 0.11,
}

# Market-level CLV decay — how quickly edges close as game approaches.
# 1.0 = edge closes linearly; higher = faster decay.
MARKET_CLV_DECAY = {
    "h2h":       1.2,    # moneylines close fast
    "spreads":   1.1,    # spreads nearly as fast
    "totals":    0.9,    # totals are stickier
    "player_points": 0.6,  # props can hold value longer
    "player_rebounds": 0.6,
    "player_assists": 0.6,
    "player_threes": 0.5,
    "alternate_spreads": 0.7,
    "alternate_totals": 0.7,
}


# =========================================================================
# Helper: American odds -> decimal odds
# =========================================================================
def _american_to_decimal(american: int | float) -> float:
    """Convert American odds to decimal odds."""
    if american > 0:
        return 1.0 + (american / 100.0)
    elif american < 0:
        return 1.0 + (100.0 / abs(american))
    else:
        return 2.0  # even money


def _confidence_tier_from_score(score: float) -> str:
    """Map a 0-1 confidence score to its AGP tier string."""
    if score >= 0.90:
        return "VERIFIED"
    elif score >= 0.75:
        return "CORROBORATED"
    elif score >= 0.55:
        return "PROBABLE"
    elif score >= 0.30:
        return "SPECULATIVE"
    else:
        return "UNVERIFIED"


# =========================================================================
# 1. Full Kelly
# =========================================================================
def kelly_full(edge: float, odds: int | float) -> float:
    """
    Classic Kelly criterion: optimal fraction of bankroll to wager.

    f* = (b*p - q) / b

    where:
        b = net decimal payout (decimal_odds - 1)
        p = true probability of winning
        q = 1 - p

    Args:
        edge: Your estimated edge as a decimal (e.g., 0.05 for 5% edge).
              This is true_probability - implied_probability.
        odds: American odds being offered.

    Returns:
        Optimal fraction of bankroll (0.0 if no edge).  Never negative.

    Invalid American odds — booleans, non-numbers, non-finite values,
    fractional quotes such as 100.9, or out-of-policy magnitudes — are
    never silently coerced: they return 0.0 (no trusted stake) rather
    than a positive fraction derived from a bogus price.

    The same holds for the edge input: a boolean or non-finite edge
    (NaN/inf) is not an edge at all and returns 0.0 — never a stake.
    """
    # An edge must be a real finite number to carry any information about
    # the bet. NaN previously slipped through every comparison and yielded
    # a full-stake 1.0; bools are ints in Python but not probabilities.
    if isinstance(edge, bool) or not isinstance(edge, (int, float)) \
            or not math.isfinite(edge):
        return 0.0
    from tools.math_utils import validate_american_odds
    try:
        validated = validate_american_odds(odds)
    except (ValueError, TypeError):
        return 0.0
    implied = calculate_implied_probability(validated)
    p = implied + edge  # true probability
    p = max(0.0, min(1.0, p))  # clamp
    q = 1.0 - p

    decimal_odds = _american_to_decimal(validated)
    b = decimal_odds - 1.0  # net payout per unit risked

    if b <= 0:
        return 0.0

    fraction = (b * p - q) / b
    # FLOOR, never round(): rounding can raise the stake (red-team M2).
    return max(0.0, math.floor(fraction * 1_000_000.0) / 1_000_000.0)


# =========================================================================
# 2. Fractional Kelly
# =========================================================================
def kelly_fractional(
    edge: float,
    odds: int | float,
    fraction: float = 0.25,
) -> float:
    """
    Fractional Kelly: reduce full Kelly by a fixed factor.

    Most sharps use quarter-Kelly (fraction=0.25).  This sacrifices ~6%
    of geometric growth rate but cuts drawdown variance by ~75%.  The
    growth-rate curve is flat near the Kelly peak, so you give up
    almost nothing by sizing down.

    Args:
        edge:     Edge as decimal (true_prob - implied_prob).
        odds:     American odds offered.
        fraction: Kelly fraction (0.25 = quarter-Kelly, 0.5 = half-Kelly).

    Returns:
        Reduced fraction of bankroll to wager.

    A non-finite or out-of-range fraction multiplier is rejected outright
    (returns 0.0): it would otherwise scale a valid Kelly fraction into an
    oversized or negative stake.
    """
    if isinstance(fraction, bool) or not isinstance(fraction, (int, float)) \
            or not math.isfinite(fraction) or not 0.0 < fraction <= 1.0:
        return 0.0
    full = kelly_full(edge, odds)
    # Floor here too: double rounding must never raise the quarter-Kelly stake.
    return math.floor(full * fraction * 1_000_000.0) / 1_000_000.0


# =========================================================================
# 3. Dynamic Kelly with confidence bands
# =========================================================================
def kelly_dynamic(
    edge: float,
    odds: int | float,
    confidence_score: float,
    variance_estimate: float,
    bankroll: float,
    kelly_base_fraction: float = 0.25,
) -> dict:
    """
    Dynamic Kelly that factors in AGP confidence tier and edge variance.

    A 3% edge with tight variance and VERIFIED confidence gets more units
    than a 5% edge with wide variance and SPECULATIVE data.

    The formula:
        stake = bankroll * kelly_fractional * tier_multiplier * variance_dampener

    Variance dampener:
        dampener = 1 / (1 + k * variance_estimate)
        where k is a sensitivity constant.  High variance -> smaller bets.

    Args:
        edge:               Edge as decimal.
        odds:               American odds.
        confidence_score:   AGP confidence score (0.0 - 1.0).
        variance_estimate:  Standard deviation of the edge estimate (in probability
                            units, e.g., 0.03 means +/-3% uncertainty on the edge).
        bankroll:           Current bankroll in dollars.
        kelly_base_fraction: Base Kelly fraction before adjustments (default 0.25).

    Returns:
        Dict with stake, fraction, reasoning, and component breakdown.
    """
    # Step 1: Base fractional Kelly
    base_fraction = kelly_fractional(edge, odds, fraction=kelly_base_fraction)

    # Step 2: AGP tier multiplier
    tier = _confidence_tier_from_score(confidence_score)
    tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)

    # Smooth scaling within tier: interpolate between this tier's mult and
    # the next-higher tier's mult based on where the score falls.
    # This avoids cliff effects at tier boundaries.
    if tier == "VERIFIED":
        smooth_mult = tier_mult
    elif tier == "CORROBORATED":
        # 0.75-0.89 -> lerp between 0.80 and 1.00
        t = (confidence_score - 0.75) / 0.15
        smooth_mult = 0.80 + t * 0.20
    elif tier == "PROBABLE":
        # 0.55-0.74 -> lerp between 0.55 and 0.80
        t = (confidence_score - 0.55) / 0.20
        smooth_mult = 0.55 + t * 0.25
    elif tier == "SPECULATIVE":
        # 0.30-0.54 -> lerp between 0.30 and 0.55
        t = (confidence_score - 0.30) / 0.25
        smooth_mult = 0.30 + t * 0.25
    else:
        smooth_mult = 0.0

    smooth_mult = max(0.0, min(1.0, smooth_mult))

    # Step 3: Variance dampener
    # Higher variance -> smaller bet.  The sensitivity constant k controls
    # how aggressively we penalize uncertainty.
    # At variance_estimate = 0 (perfect info), dampener = 1.0.
    # At variance_estimate = edge (uncertainty equals the edge), dampener ~ 0.5.
    k = 1.0 / max(abs(edge), 0.001)  # normalize so dampener halves when var == edge
    variance_dampener = 1.0 / (1.0 + k * variance_estimate)
    variance_dampener = max(0.05, min(1.0, variance_dampener))

    # Step 4: Combine
    adjusted_fraction = base_fraction * smooth_mult * variance_dampener

    # Step 5: Safety caps
    # Never risk more than 5% of bankroll on a single bet regardless of Kelly
    hard_cap = 0.05
    final_fraction = min(adjusted_fraction, hard_cap)

    # Step 6: Dollar amount
    stake = round(bankroll * final_fraction, 2)

    # Build reasoning
    reasons = []
    reasons.append(f"Base quarter-Kelly: {base_fraction:.4f} ({base_fraction*100:.2f}% of bankroll)")
    reasons.append(f"AGP tier: {tier} (score={confidence_score:.2f}, multiplier={smooth_mult:.3f})")
    reasons.append(f"Variance dampener: {variance_dampener:.3f} (edge_uncertainty={variance_estimate:.4f})")
    if adjusted_fraction > hard_cap:
        reasons.append(f"Hard-capped from {adjusted_fraction*100:.2f}% to {hard_cap*100:.1f}%")
    reasons.append(f"Final: {final_fraction*100:.3f}% of ${bankroll:,.0f} = ${stake:,.2f}")

    return {
        "stake": stake,
        "fraction": round(final_fraction, 6),
        "kelly_full": round(kelly_full(edge, odds), 6),
        "kelly_base": round(base_fraction, 6),
        "tier": tier,
        "tier_multiplier": round(smooth_mult, 4),
        "variance_dampener": round(variance_dampener, 4),
        "hard_cap_applied": adjusted_fraction > hard_cap,
        "reasoning": " | ".join(reasons),
        "components": {
            "edge": round(edge, 5),
            "odds": odds,
            "confidence_score": round(confidence_score, 3),
            "variance_estimate": round(variance_estimate, 5),
            "bankroll": bankroll,
        },
    }


# =========================================================================
# 4. Simultaneous Kelly for correlated portfolio
# =========================================================================
def kelly_portfolio(bets: list[dict]) -> list[dict]:
    """
    Optimal simultaneous Kelly sizing for a portfolio of open bets.

    Each bet has:
        - edge: float (decimal)
        - odds: int (American)
        - correlation_with_others: float (-1 to 1, average pairwise correlation)
        - Optional: confidence_score, variance_estimate, description

    Correlated bets reduce effective bankroll.  Two bets on the same game
    (e.g., spread and total) at correlation 0.3 should be sized as if the
    bankroll is smaller.  Perfectly correlated bets (same outcome, different
    books) should be treated as one position.

    The approach:
    1. Compute independent Kelly for each bet.
    2. Build a correlation-adjusted budget: total Kelly allocation is capped
       at a portfolio-level maximum.
    3. Scale each bet proportionally if the sum exceeds the cap.
    4. Apply correlation penalties: higher correlation -> more reduction.

    Args:
        bets: List of bet dicts, each with at minimum {edge, odds, correlation_with_others}.

    Returns:
        List of dicts with sizing info for each bet, plus portfolio summary.
    """
    if not bets:
        return []

    n = len(bets)

    # Step 1: Individual Kelly fractions
    individual_kellys = []
    for bet in bets:
        edge = bet.get("edge", 0.0)
        odds = bet.get("odds", -110)
        conf = bet.get("confidence_score", 0.75)  # default CORROBORATED
        var_est = bet.get("variance_estimate", abs(edge) * 0.5)

        # Use dynamic Kelly for each
        base_frac = kelly_fractional(edge, odds, fraction=0.25)

        # Confidence adjustment
        tier = _confidence_tier_from_score(conf)
        tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)
        adj_frac = base_frac * tier_mult

        individual_kellys.append({
            "raw_fraction": round(base_frac, 6),
            "confidence_adjusted": round(adj_frac, 6),
            "tier": tier,
        })

    # Step 2: Correlation-adjusted portfolio allocation
    # Build a simple correlation matrix from pairwise correlation estimates
    correlations = np.array([bet.get("correlation_with_others", 0.0) for bet in bets])

    # Portfolio variance scaling: for N bets with average pairwise correlation rho,
    # the portfolio variance scales as:
    #   var_portfolio = N * var_individual * (1 + (N-1) * rho) / N
    #                 = var_individual * (1 + (N-1) * rho)
    # We use this to compute a correlation penalty factor.
    avg_correlation = float(np.mean(np.clip(correlations, -1.0, 1.0)))
    # Effective diversification ratio: 1.0 = fully diversified, higher = concentrated
    diversification_ratio = 1.0 + max(0.0, (n - 1) * avg_correlation)
    # Penalty: scale down total allocation as correlation increases
    # At rho=0: penalty=1.0 (no penalty).  At rho=1: penalty = 1/sqrt(N).
    correlation_penalty = 1.0 / math.sqrt(max(1.0, diversification_ratio))

    # Step 3: Portfolio-level cap
    # Total simultaneous Kelly allocation should not exceed 20% of bankroll.
    # This is the "don't blow up" constraint.
    PORTFOLIO_CAP = 0.20
    raw_total = sum(ik["confidence_adjusted"] for ik in individual_kellys)
    penalized_total = raw_total * correlation_penalty

    if penalized_total > PORTFOLIO_CAP:
        scale_factor = PORTFOLIO_CAP / penalized_total
    else:
        scale_factor = correlation_penalty

    # Step 4: Per-bet correlation adjustment
    # Bets with higher individual correlation get penalized more.
    results = []
    for i, bet in enumerate(bets):
        ik = individual_kellys[i]
        rho_i = max(0.0, correlations[i])

        # Individual correlation penalty: additional reduction for highly correlated bets
        # A bet with rho=0.8 gets an extra 20% reduction on top of the portfolio scaling.
        individual_corr_penalty = 1.0 - (rho_i * 0.25)
        individual_corr_penalty = max(0.1, individual_corr_penalty)

        final_fraction = ik["confidence_adjusted"] * scale_factor * individual_corr_penalty
        # Per-bet hard cap at 5%
        final_fraction = min(final_fraction, 0.05)

        results.append({
            "description": bet.get("description", f"Bet {i+1}"),
            "edge": bet.get("edge", 0.0),
            "odds": bet.get("odds", -110),
            "independent_kelly": ik["raw_fraction"],
            "confidence_adjusted_kelly": ik["confidence_adjusted"],
            "correlation": round(correlations[i], 3),
            "individual_corr_penalty": round(individual_corr_penalty, 4),
            "final_fraction": round(final_fraction, 6),
            "final_pct": round(final_fraction * 100, 3),
            "tier": ik["tier"],
        })

    # Portfolio summary
    total_allocated = sum(r["final_fraction"] for r in results)
    portfolio_summary = {
        "bet_count": n,
        "avg_correlation": round(avg_correlation, 4),
        "diversification_ratio": round(diversification_ratio, 4),
        "correlation_penalty": round(correlation_penalty, 4),
        "raw_total_allocation": round(raw_total, 6),
        "final_total_allocation": round(total_allocated, 6),
        "final_total_pct": round(total_allocated * 100, 3),
        "portfolio_cap": PORTFOLIO_CAP,
        "cap_hit": penalized_total > PORTFOLIO_CAP,
    }

    # Attach summary to each result for easy access
    for r in results:
        r["portfolio_summary"] = portfolio_summary

    return results


# =========================================================================
# 5. Bankroll ruin probability
# =========================================================================
def ruin_probability(
    bankroll: float,
    avg_stake: float,
    win_rate: float,
    avg_odds: int | float,
    method: str = "analytical",
) -> dict:
    """
    Estimate the probability of total bankroll ruin.

    Two methods:
    - "analytical": Closed-form approximation for fixed-stake betting.
    - "simulation": Monte Carlo simulation for more realistic scenarios.

    The analytical formula for gambler's ruin with edge:
        P(ruin) = ((1-p)/p)^(bankroll/stake)  when p > 0.5 (in unit terms)

    For fractional Kelly bets, the ruin probability is theoretically 0
    (Kelly never goes bust), but in practice discrete bet sizing and
    estimation error make ruin possible.

    Args:
        bankroll:   Current bankroll in dollars.
        avg_stake:  Average bet size in dollars.
        win_rate:   Historical or estimated win rate (0.0 - 1.0).
        avg_odds:   Average American odds of bets placed.
        method:     "analytical" or "simulation".

    Returns:
        Dict with ruin probability, recommended max stake, and analysis.
    """
    decimal_odds = _american_to_decimal(avg_odds)
    b = decimal_odds - 1.0  # net payout ratio
    q = 1.0 - win_rate

    # Expected profit per bet (in units of stake)
    ev_per_bet = win_rate * b - q
    units = min(bankroll / avg_stake, 10000.0) if avg_stake > 0 else 10000.0

    result = {
        "bankroll": bankroll,
        "avg_stake": avg_stake,
        "units_in_bankroll": round(units, 1),
        "win_rate": round(win_rate, 4),
        "avg_odds": avg_odds,
        "decimal_odds": round(decimal_odds, 3),
        "ev_per_bet": round(ev_per_bet, 4),
    }

    if ev_per_bet <= 0:
        # Negative EV: ruin is certain given enough bets
        result["ruin_probability"] = 1.0
        result["expected_bets_to_ruin"] = _expected_bets_to_ruin_neg_ev(
            units, win_rate, b
        )
        result["analysis"] = (
            "NEGATIVE EV: Ruin is mathematically certain with continued play. "
            f"EV per bet = {ev_per_bet:+.4f} units. "
            "Stop betting or find +EV spots."
        )
        result["recommended_max_stake"] = 0.0
        result["recommended_max_stake_pct"] = 0.0
        return result

    if method == "simulation":
        ruin_prob, median_path, drawdown_95 = _simulate_ruin(
            bankroll, avg_stake, win_rate, b, n_simulations=10000, n_bets=5000
        )
    else:
        # Analytical approximation: gambler's ruin formula
        # P(ruin) = (q / (p * b))^(bankroll / avg_stake)
        # This is the fixed-stake approximation.
        ratio = q / (win_rate * b) if (win_rate * b) > 0 else 1.0
        if ratio >= 1.0:
            ruin_prob = 1.0
        else:
            ruin_prob = ratio ** units
            ruin_prob = min(1.0, ruin_prob)
        median_path = None
        drawdown_95 = None

    result["ruin_probability"] = round(ruin_prob, 6)
    result["ruin_pct"] = round(ruin_prob * 100, 4)

    if median_path is not None:
        result["simulation"] = {
            "median_final_bankroll": round(median_path, 2),
            "drawdown_95th_percentile": round(drawdown_95, 4),
        }

    # Find the stake size where ruin probability equals threshold
    acceptable_ruin = 0.01  # 1%
    if q / (win_rate * b) < 1.0:
        ratio = q / (win_rate * b)
        # Solve: ratio^(bankroll/stake) = acceptable_ruin
        # (bankroll/stake) * ln(ratio) = ln(acceptable_ruin)
        # stake = bankroll * ln(ratio) / ln(acceptable_ruin)
        if ratio > 0 and ratio < 1.0:
            safe_stake = bankroll * math.log(ratio) / math.log(acceptable_ruin)
            safe_stake = max(0, safe_stake)
        else:
            safe_stake = 0.0
    else:
        safe_stake = 0.0

    result["recommended_max_stake"] = round(safe_stake, 2)
    result["recommended_max_stake_pct"] = round(
        (safe_stake / bankroll * 100) if bankroll > 0 else 0, 2
    )

    # Risk assessment
    if ruin_prob < 0.001:
        risk_level = "NEGLIGIBLE"
        advice = "Current sizing is conservative. Could increase if edge is stable."
    elif ruin_prob < 0.01:
        risk_level = "LOW"
        advice = "Acceptable risk. Current sizing is in the sweet spot."
    elif ruin_prob < 0.05:
        risk_level = "MODERATE"
        advice = f"Reduce average stake to ${safe_stake:.0f} to bring ruin below 1%."
    elif ruin_prob < 0.20:
        risk_level = "HIGH"
        advice = (
            f"Dangerously oversized. Reduce to ${safe_stake:.0f}/bet immediately. "
            f"Current sizing risks ruin with {ruin_prob:.1%} probability."
        )
    else:
        risk_level = "CRITICAL"
        advice = (
            f"Ruin probability {ruin_prob:.1%} is unacceptable. "
            f"Reduce to ${safe_stake:.0f}/bet or stop until bankroll rebuilds."
        )

    result["risk_level"] = risk_level
    result["analysis"] = advice

    return result


def _expected_bets_to_ruin_neg_ev(
    units: float, win_rate: float, b: float
) -> Optional[float]:
    """Estimate expected number of bets until ruin for -EV bettor."""
    ev_per_bet = win_rate * b - (1 - win_rate)
    if ev_per_bet >= 0:
        return None  # Not -EV
    # Rough estimate: bankroll / expected_loss_per_bet
    expected_loss = abs(ev_per_bet)
    if expected_loss > 0:
        return round(units / expected_loss, 0)
    return None


def _simulate_ruin(
    bankroll: float,
    avg_stake: float,
    win_rate: float,
    b: float,
    n_simulations: int = 10000,
    n_bets: int = 5000,
) -> tuple[float, float, float]:
    """
    Monte Carlo ruin simulation.

    Returns (ruin_probability, median_final_bankroll, 95th_percentile_max_drawdown).
    """
    rng = np.random.default_rng(seed=42)

    # Simulate outcomes: 1 = win, 0 = loss
    outcomes = rng.random((n_simulations, n_bets)) < win_rate

    # Profit per bet: win = +b*stake, loss = -stake
    profits = np.where(outcomes, b * avg_stake, -avg_stake)

    # Cumulative bankroll paths
    cum_profits = np.cumsum(profits, axis=1)
    bankroll_paths = bankroll + cum_profits

    # Ruin: bankroll hits 0 or below at any point
    min_bankroll = np.min(bankroll_paths, axis=1)
    ruined = min_bankroll <= 0
    ruin_prob = float(np.mean(ruined))

    # Median final bankroll (excluding ruined paths for meaningful stat)
    final_bankrolls = bankroll_paths[:, -1]
    median_final = float(np.median(final_bankrolls))

    # Max drawdown: peak-to-trough
    running_max = np.maximum.accumulate(bankroll_paths, axis=1)
    drawdowns = (running_max - bankroll_paths) / np.maximum(running_max, 1.0)
    max_drawdowns = np.max(drawdowns, axis=1)
    drawdown_95 = float(np.percentile(max_drawdowns, 95))

    return ruin_prob, median_final, drawdown_95


# =========================================================================
# 6. Optimal bet timing value
# =========================================================================
def timing_value(
    current_edge: float,
    hours_to_game: float,
    sport: str = "basketball_nba",
    market: str = "spreads",
) -> dict:
    """
    Estimate whether to bet now or wait for a better number.

    The tradeoff:
    - Betting NOW locks in the current edge.
    - WAITING risks the line moving against you (edge shrinks or disappears)
      but also has a chance the line moves in your favor (edge grows).

    We model this as:
        EV(bet_now)  = current_edge
        EV(wait)     = E[edge_at_close | current_edge, time, sport, market]
                     = current_edge * decay + line_improvement_prob * improvement_size
                       - line_worsening_prob * worsening_size

    Key insight: in efficient markets, lines tend to move TOWARD the true price.
    If you have an edge, the expected direction of movement is AGAINST you
    (the market will correct).  But in inefficient sub-markets (props, alts),
    or when sharp action hasn't arrived yet, there can be +EV in waiting.

    Args:
        current_edge:  Current edge as a decimal (e.g., 0.03 for 3%).
        hours_to_game: Hours until game starts.
        sport:         Sport key for line movement profile.
        market:        Market type for CLV decay rate.

    Returns:
        Dict with bet_now_ev, wait_ev, and recommendation.
    """
    profile = LINE_MOVEMENT_PROFILES.get(sport, _DEFAULT_MOVEMENT_PROFILE)
    decay_rate = MARKET_CLV_DECAY.get(market, 1.0)

    # Select volatility regime based on time to game
    if hours_to_game > 24:
        vol = profile["early_vol"]
        regime = "early"
    elif hours_to_game > 4:
        vol = profile["mid_vol"]
        regime = "mid"
    else:
        vol = profile["late_vol"]
        regime = "late"

    steam_prob = profile["steam_prob"]

    # EV of betting now is simply the current edge
    bet_now_ev = current_edge

    # Model expected edge change from waiting:
    # 1. Edge decay: markets correct toward true price.
    #    Expected decay per hour = decay_rate * vol (faster decay = more correction).
    #    Over T hours, remaining edge fraction:
    #    edge_remaining = edge * exp(-decay_rate * vol * T)
    hours_remaining = max(0.01, hours_to_game)
    edge_remaining_frac = math.exp(-decay_rate * vol * hours_remaining)
    expected_edge_after_decay = current_edge * edge_remaining_frac

    # 2. Steam move probability: chance of a sharp move in your direction.
    #    If steam comes in on YOUR side, the line moves toward you and you
    #    lose the edge (line corrects).  But occasionally steam comes on the
    #    OTHER side, increasing your edge briefly.
    #    Net steam effect is slightly negative (market is more likely to
    #    correct than diverge further).
    #    P(favorable steam) = steam_prob * 0.3 (most steam corrects toward true)
    #    P(unfavorable steam) = steam_prob * 0.7
    favorable_steam_boost = steam_prob * 0.3 * vol * hours_remaining
    unfavorable_steam_cost = steam_prob * 0.7 * vol * hours_remaining

    # 3. Stale-line opportunity: in less efficient markets, there's a chance
    #    of a BETTER number appearing at a different book as lines adjust.
    #    This is a function of market inefficiency.
    efficiency = 1.0 - (1.0 / max(decay_rate, 0.01)) * 0.1
    efficiency = max(0.0, min(1.0, efficiency))
    stale_line_bonus = (1.0 - efficiency) * vol * 0.5 * min(hours_remaining, 12.0)

    # Composite wait EV
    wait_ev = (
        expected_edge_after_decay
        + favorable_steam_boost
        - unfavorable_steam_cost
        + stale_line_bonus
    )

    # Uncertainty on wait estimate (wider with more time)
    wait_uncertainty = vol * math.sqrt(hours_remaining)

    # Decision
    ev_diff = wait_ev - bet_now_ev
    if bet_now_ev <= 0:
        recommendation = "NO_BET"
        reasoning = "Current edge is zero or negative. No bet at any timing."
    elif ev_diff > wait_uncertainty * 0.5:
        recommendation = "WAIT"
        reasoning = (
            f"Waiting has +{ev_diff*100:.2f}% higher expected edge. "
            f"Market regime: {regime}. Volatility {vol:.3f}/hr. "
            f"Edge decay to {edge_remaining_frac:.1%} but stale-line "
            f"opportunity outweighs ({stale_line_bonus*100:.2f}% bonus)."
        )
    elif ev_diff < -wait_uncertainty * 0.25:
        recommendation = "BET_NOW"
        reasoning = (
            f"Line is likely to move against you. Edge decays to "
            f"{expected_edge_after_decay*100:.2f}% (from {current_edge*100:.2f}%). "
            f"Market regime: {regime}. Lock it in now."
        )
    else:
        recommendation = "SLIGHT_LEAN_NOW"
        reasoning = (
            f"Close call. Wait EV ({wait_ev*100:.2f}%) vs now EV ({bet_now_ev*100:.2f}%). "
            f"Difference {ev_diff*100:.3f}% is within noise ({wait_uncertainty*100:.2f}%). "
            f"Default to betting now to avoid execution risk."
        )

    return {
        "bet_now_ev": round(bet_now_ev, 5),
        "wait_ev": round(wait_ev, 5),
        "ev_difference": round(ev_diff, 5),
        "recommendation": recommendation,
        "reasoning": reasoning,
        "details": {
            "sport": sport,
            "market": market,
            "hours_to_game": round(hours_to_game, 2),
            "regime": regime,
            "volatility_per_hour": round(vol, 4),
            "edge_decay_rate": round(decay_rate, 3),
            "edge_remaining_fraction": round(edge_remaining_frac, 4),
            "expected_edge_after_decay": round(expected_edge_after_decay, 5),
            "steam_move_probability": round(steam_prob, 3),
            "favorable_steam_boost": round(favorable_steam_boost, 5),
            "unfavorable_steam_cost": round(unfavorable_steam_cost, 5),
            "stale_line_bonus": round(stale_line_bonus, 5),
            "wait_uncertainty": round(wait_uncertainty, 5),
        },
    }


# =========================================================================
# 7. Unit sizing from bankroll
# =========================================================================
def calculate_units(
    bankroll: float,
    edge: float,
    confidence: float,
    kelly_fraction: float = 0.25,
    unit_size: Optional[float] = None,
) -> dict:
    """
    Convert Kelly output into practical unit sizing.

    Most bettors think in "units" (1 unit = 1% of bankroll by convention).
    This function bridges the gap between Kelly math and the unit system.

    If unit_size is not provided, 1 unit = 1% of bankroll (standard).

    Args:
        bankroll:       Current bankroll in dollars.
        edge:           Edge as a decimal (e.g., 0.03 for 3%).
        confidence:     AGP confidence score (0.0 - 1.0).
        kelly_fraction: Fractional Kelly factor (default 0.25).
        unit_size:      Dollar value of 1 unit (default: bankroll * 0.01).

    Returns:
        Dict with units, dollar_amount, pct_of_bankroll, and breakdown.
    """
    if unit_size is None:
        unit_size = bankroll * 0.01

    if unit_size <= 0 or bankroll <= 0:
        return {
            "units": 0.0,
            "dollar_amount": 0.0,
            "pct_of_bankroll": 0.0,
            "unit_size": unit_size,
            "error": "Invalid bankroll or unit size",
        }

    # Use the Kelly fraction from dynamic Kelly (without variance — that requires
    # separate variance_estimate).  Here we apply confidence directly.
    # For a quick sizing call, use the tier multiplier on fractional Kelly.
    tier = _confidence_tier_from_score(confidence)
    tier_mult = AGP_TIER_MULTIPLIERS.get(tier, 0.0)

    # Compute base Kelly (needs odds — estimate from edge)
    # To avoid requiring odds as a separate param, we back-calculate
    # approximate odds from the edge magnitude.  For a more precise result,
    # call kelly_dynamic() directly with actual odds.
    #
    # However, edge alone is ambiguous without odds.  We use a heuristic:
    # assume standard -110 odds (most common) unless edge is large enough
    # to suggest plus-money.
    #
    # For unit sizing, the practical formula is:
    #   fraction = edge * kelly_fraction * tier_mult
    # This is a linearized approximation of Kelly that works well for
    # small edges (which is what sharps typically bet on).
    fraction = edge * kelly_fraction * tier_mult

    # Safety: cap at 5% of bankroll
    fraction = max(0.0, min(fraction, 0.05))

    dollar_amount = round(bankroll * fraction, 2)
    units = round(dollar_amount / unit_size, 2) if unit_size > 0 else 0.0
    pct = round(fraction * 100, 3)

    # Unit rating for readability
    if units >= 3.0:
        unit_label = "MAX"
    elif units >= 2.0:
        unit_label = "STRONG"
    elif units >= 1.0:
        unit_label = "STANDARD"
    elif units >= 0.5:
        unit_label = "HALF"
    elif units > 0:
        unit_label = "LEAN"
    else:
        unit_label = "NO_BET"

    return {
        "units": units,
        "unit_label": unit_label,
        "dollar_amount": dollar_amount,
        "pct_of_bankroll": pct,
        "unit_size": round(unit_size, 2),
        "bankroll": bankroll,
        "breakdown": {
            "edge": round(edge, 5),
            "confidence": round(confidence, 3),
            "tier": tier,
            "tier_multiplier": round(tier_mult, 3),
            "kelly_fraction": kelly_fraction,
            "raw_fraction": round(edge * kelly_fraction * tier_mult, 7),
            "capped_fraction": round(fraction, 7),
        },
    }
