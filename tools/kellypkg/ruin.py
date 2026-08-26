"""
Bankroll ruin probability modeling (split from tools/kelly.py).
"""

import math
from typing import Optional

import numpy as np

from tools.kellypkg.odds import _american_to_decimal


def ruin_probability(
    bankroll: float,
    avg_stake: float,
    win_rate: float,
    avg_odds,
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
):
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
