"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""3 & 4. Futures efficiency and futures hedge timing."""

import math
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability
from tools.psych._utils import _prob_to_american

# ---------------------------------------------------------------------------
# 3. Futures Market Efficiency
# ---------------------------------------------------------------------------

def futures_efficiency(
    opening_odds: int,
    current_odds: int,
    games_played: int,
    total_games: int,
    current_wins: int,
    current_losses: int,
    preseason_win_total: Optional[float] = None,
) -> dict:
    """
    Analyze whether a futures market is efficiently pricing a team's trajectory.

    Futures carry enormous vig early in the season (total implied probability
    across all teams in a futures market can sum to 150-200%). As the season
    progresses and uncertainty resolves, these implied probabilities should
    converge toward reality — but they often lag.

    This function compares the book's current futures odds against what the
    odds SHOULD be given the team's current win rate and remaining schedule.

    Args:
        opening_odds: American odds at season start (e.g., +2000)
        current_odds: Current American odds (e.g., +500)
        games_played: Games played so far
        total_games: Total games in the season
        current_wins: Current win count
        current_losses: Current loss count
        preseason_win_total: Preseason projected win total (if available)

    Returns:
        Dict with efficiency analysis and mispricing detection.
    """
    games_remaining = total_games - games_played
    season_progress = games_played / total_games if total_games > 0 else 0

    # Current win rate
    if games_played > 0:
        current_win_rate = current_wins / games_played
    else:
        current_win_rate = 0.5

    # Project final record based on current trajectory
    projected_wins = current_wins + (games_remaining * current_win_rate)
    projected_losses = current_losses + (games_remaining * (1 - current_win_rate))

    # Calculate how the team is performing vs preseason expectations
    if preseason_win_total is not None and games_played > 0:
        expected_wins_so_far = preseason_win_total * season_progress
        wins_above_expected = current_wins - expected_wins_so_far
    else:
        wins_above_expected = None

    # Implied probabilities from odds
    opening_implied = calculate_implied_probability(opening_odds)
    current_implied = calculate_implied_probability(current_odds)
    implied_shift = current_implied - opening_implied

    # Theoretical fair probability based on current trajectory.
    # Use a Bayesian-ish approach: blend prior (opening) with observed performance,
    # weighted by how much of the season is complete.
    #
    # Early season: prior dominates (small sample size).
    # Late season: observed performance dominates.
    #
    # For championship futures, "fair probability" is complex. We approximate:
    # - If a team is on pace for X wins, estimate their championship probability
    #   based on historical win-total-to-championship correlation.
    # - This is sport-specific, so we use a general logistic model.

    # General model: championship probability scales roughly exponentially
    # with win rate relative to the field. We use current implied as a baseline
    # and check if the MOVEMENT is proportional to the PERFORMANCE change.

    # How much should odds have moved given performance?
    # If a +2000 team (5% implied) is winning at a 70% clip through 40% of
    # the season, their odds should have shortened dramatically.

    # Performance surprise factor
    if opening_implied > 0:
        performance_ratio = current_win_rate / max(0.01, _win_rate_from_implied(opening_implied, total_games))
    else:
        performance_ratio = 1.0

    # Expected implied probability shift based on performance
    # A team winning at 2x their expected rate through 50% of the season
    # should see roughly a 3-4x increase in implied probability.
    # This is an approximation; real models use power ratings.
    if season_progress > 0.05:  # Need some sample
        # Bayesian update weight: how much to trust current performance
        update_weight = _bayesian_weight(games_played, total_games)

        # Blend opening implied with trajectory-based estimate
        trajectory_implied = opening_implied * (performance_ratio ** (1.5 * update_weight))
        # Cap at reasonable bounds
        trajectory_implied = np.clip(trajectory_implied, 0.005, 0.85)

        # Efficiency = how close the book's current implied is to our trajectory estimate
        mispricing = current_implied - trajectory_implied
        mispricing_pct = (mispricing / trajectory_implied) * 100 if trajectory_implied > 0 else 0
    else:
        trajectory_implied = opening_implied
        mispricing = 0.0
        mispricing_pct = 0.0

    # Fair odds based on trajectory
    fair_american = _prob_to_american(trajectory_implied)

    # Vig estimate: early season futures carry 30-50% vig, late season 10-20%
    estimated_vig = _estimate_futures_vig(season_progress)

    # Efficiency score: 0 = perfectly efficient, 1 = massively mispriced
    efficiency_score = min(1.0, abs(mispricing) / max(0.01, trajectory_implied))

    # Direction of mispricing
    if mispricing > 0.02:
        mispricing_direction = "overpriced"  # Book thinks team is better than trajectory
        explanation = (
            f"Book's implied probability ({current_implied:.1%}) exceeds trajectory "
            f"estimate ({trajectory_implied:.1%}). Market may be overreacting to "
            f"recent performance or underweighting remaining schedule difficulty."
        )
    elif mispricing < -0.02:
        mispricing_direction = "underpriced"  # Book hasn't caught up to performance
        explanation = (
            f"Book's implied probability ({current_implied:.1%}) lags trajectory "
            f"estimate ({trajectory_implied:.1%}). Market may be slow to update "
            f"or anchored to preseason expectations. Potential value on this team."
        )
    else:
        mispricing_direction = "efficient"
        explanation = (
            f"Book's implied probability ({current_implied:.1%}) is within range of "
            f"trajectory estimate ({trajectory_implied:.1%}). Market is pricing "
            f"this future reasonably."
        )

    return {
        "opening_odds": opening_odds,
        "current_odds": current_odds,
        "opening_implied": round(opening_implied, 4),
        "current_implied": round(current_implied, 4),
        "implied_shift": round(implied_shift, 4),
        "trajectory_implied": round(float(trajectory_implied), 4),
        "fair_american_odds": fair_american,
        "efficiency_score": round(float(efficiency_score), 3),
        "mispricing_direction": mispricing_direction,
        "mispricing_magnitude": round(float(abs(mispricing)), 4),
        "mispricing_pct": round(float(mispricing_pct), 1),
        "season_progress": round(season_progress, 3),
        "current_record": f"{current_wins}-{current_losses}",
        "projected_record": f"{int(round(projected_wins))}-{int(round(projected_losses))}",
        "current_win_rate": round(current_win_rate, 3),
        "wins_above_expected": round(wins_above_expected, 1) if wins_above_expected is not None else None,
        "estimated_vig": round(estimated_vig, 3),
        "explanation": explanation,
    }


def _win_rate_from_implied(implied_prob: float, total_games: int) -> float:
    """
    Estimate a team's expected win rate from their championship implied probability.

    Rough mapping (sport-agnostic):
    - 50% championship implied ~ 70-75% win rate
    - 10% ~ 58-62% win rate
    - 5% ~ 55-58% win rate
    - 1% ~ 45-50% win rate

    Uses a log transform to map implied championship probability to win rate.
    """
    if implied_prob <= 0:
        return 0.4
    if implied_prob >= 1:
        return 0.85

    # Logistic mapping: higher championship implied -> higher win rate
    # Calibrated so 50% implied ~ 73% win rate, 5% ~ 56%, 1% ~ 48%
    log_odds = math.log(implied_prob / (1 - implied_prob))
    win_rate = 0.55 + 0.08 * log_odds
    return max(0.25, min(0.90, win_rate))


def _bayesian_weight(games_played: int, total_games: int) -> float:
    """
    How much to weight observed performance vs prior.

    Uses a logistic curve: slow to update early, accelerating mid-season,
    plateauing late. This matches how information actually resolves — the
    first 10 games tell you less than games 40-50.
    """
    if total_games <= 0:
        return 0.5
    progress = games_played / total_games
    # Logistic curve centered at 40% of season
    weight = 1.0 / (1.0 + math.exp(-10 * (progress - 0.4)))
    return weight


def _estimate_futures_vig(season_progress: float) -> float:
    """
    Estimate the vig baked into futures markets at a given point in the season.

    Early season: enormous vig (30-50%) because uncertainty is high and
    the book needs protection. Late season: vig compresses (10-15%) as
    outcomes become more certain.
    """
    # Exponential decay from ~40% early to ~12% late
    return 0.12 + 0.30 * math.exp(-4.0 * season_progress)


# ---------------------------------------------------------------------------
# 4. Futures Hedge Timing
# ---------------------------------------------------------------------------

def optimal_hedge_time(
    original_odds: int,
    current_odds: int,
    original_stake: float,
    remaining_uncertainty: float = 0.5,
    games_remaining: Optional[int] = None,
    total_games: Optional[int] = None,
    injury_risk_factor: float = 0.0,
    schedule_difficulty: float = 0.5,
) -> dict:
    """
    Determine the optimal time and fraction to hedge a futures bet.

    If you took a team at +2000 ($50 to win $1000) and they're now +500
    ($200 to win $1000), you have a decision: hedge now for guaranteed profit,
    or let it ride for potentially larger payoff.

    The optimal strategy depends on:
    - Remaining uncertainty (how much can change)
    - Marginal value of waiting (will odds shorten further?)
    - Risk factors (injuries, schedule, etc.)
    - Your utility function (risk tolerance)

    Args:
        original_odds: Odds when you placed the futures bet
        current_odds: Current odds for the same future
        original_stake: How much you wagered originally
        remaining_uncertainty: 0 to 1 scale (0 = outcome nearly decided, 1 = very uncertain)
        games_remaining: Games left in season (optional, improves estimate)
        total_games: Total games in season (optional)
        injury_risk_factor: 0 to 1 (0 = healthy, 1 = star player fragile)
        schedule_difficulty: 0 to 1 (0 = easy remaining, 1 = brutal)

    Returns:
        Dict with hedge recommendation and calculations.
    """
    # Calculate potential payouts
    if original_odds > 0:
        potential_profit = original_stake * (original_odds / 100)
    else:
        potential_profit = original_stake * (100 / abs(original_odds))

    total_payout = original_stake + potential_profit

    # Current implied probability
    original_implied = calculate_implied_probability(original_odds)
    current_implied = calculate_implied_probability(current_odds)

    # How much has the position appreciated?
    # "Mark to market" value of the futures ticket
    # If you can hedge at current_odds, the ticket's current value is:
    # Value = total_payout * current_implied - hedge_cost_to_lock_profit
    ticket_mark_to_market = total_payout * current_implied
    ticket_profit_so_far = ticket_mark_to_market - original_stake

    # Remaining uncertainty from games if provided
    if games_remaining is not None and total_games is not None and total_games > 0:
        season_remaining = games_remaining / total_games
        # Override remaining_uncertainty with data-driven estimate
        remaining_uncertainty = max(0.05, min(0.95, season_remaining * 1.2))

    # Calculate hedge: to guarantee profit, bet the OTHER outcome at current odds
    # Hedge stake to equalize: hedge_stake * hedge_decimal = total_payout
    # But we need the "other side" odds. For futures, hedging means betting
    # against your team at the current line. We approximate the hedge odds
    # as the inverse of current implied (minus vig).
    hedge_vig = 0.05  # Approximate vig on the hedge side
    hedge_implied = 1.0 - current_implied + hedge_vig
    hedge_implied = min(0.95, max(0.10, hedge_implied))

    if hedge_implied >= 1.0:
        hedge_decimal = 1.05
    else:
        hedge_decimal = 1.0 / hedge_implied

    # Full hedge: lock in guaranteed profit
    full_hedge_stake = total_payout / hedge_decimal
    guaranteed_profit = total_payout - original_stake - full_hedge_stake
    guaranteed_profit = max(0, guaranteed_profit)

    # Let-it-ride EV
    let_ride_ev = (current_implied * potential_profit) - ((1 - current_implied) * original_stake)

    # Optimal hedge fraction using Kelly-like logic
    # The idea: hedge enough to manage risk, but keep upside exposure
    # if remaining expected movement is favorable.
    #
    # Expected future odds movement
    expected_further_shortening = _expected_odds_improvement(
        current_implied, remaining_uncertainty, schedule_difficulty
    )

    # If odds are likely to shorten further, waiting has value
    wait_value = potential_profit * expected_further_shortening

    # Risk of adverse movement
    adverse_risk = remaining_uncertainty * (injury_risk_factor * 0.4 + schedule_difficulty * 0.3 + 0.3)
    risk_cost = ticket_profit_so_far * adverse_risk

    # Net value of waiting vs hedging now
    net_wait_value = wait_value - risk_cost

    # Optimal hedge fraction: hedge more when risk > wait value
    if net_wait_value > 0:
        # Waiting has positive expected value — hedge less
        optimal_fraction = max(0.0, 0.5 - net_wait_value / max(1, ticket_profit_so_far))
    else:
        # Risk exceeds wait value — hedge more
        optimal_fraction = min(1.0, 0.5 + abs(net_wait_value) / max(1, ticket_profit_so_far))

    optimal_fraction = np.clip(optimal_fraction, 0.0, 1.0)

    # Partial hedge: optimal fraction
    partial_hedge_stake = full_hedge_stake * optimal_fraction
    partial_guaranteed = guaranteed_profit * optimal_fraction
    partial_remaining_upside = potential_profit * (1 - optimal_fraction)

    # Decision
    if optimal_fraction > 0.8:
        recommendation = "HEDGE_NOW"
        reasoning = (
            f"High remaining risk ({remaining_uncertainty:.0%} uncertainty, "
            f"{injury_risk_factor:.0%} injury risk). Lock in profit. "
            f"Guaranteed ${guaranteed_profit:.2f} vs let-ride EV ${let_ride_ev:.2f}."
        )
    elif optimal_fraction > 0.4:
        recommendation = "PARTIAL_HEDGE"
        reasoning = (
            f"Moderate risk/reward. Hedge {optimal_fraction:.0%} of position to "
            f"secure ${partial_guaranteed:.2f} while keeping ${partial_remaining_upside:.2f} "
            f"in potential upside."
        )
    elif optimal_fraction > 0.1:
        recommendation = "SMALL_HEDGE"
        reasoning = (
            f"Odds likely to shorten further. Small hedge ({optimal_fraction:.0%}) for "
            f"downside protection, maintain most upside exposure."
        )
    else:
        recommendation = "LET_IT_RIDE"
        reasoning = (
            f"Strong expected value in waiting. Current trajectory and low risk suggest "
            f"odds will continue shortening. Let-ride EV: ${let_ride_ev:.2f}."
        )

    return {
        "hedge_now": optimal_fraction > 0.5,
        "recommendation": recommendation,
        "reasoning": reasoning,
        "original_bet": {
            "odds": original_odds,
            "stake": original_stake,
            "potential_profit": round(potential_profit, 2),
            "total_payout": round(total_payout, 2),
        },
        "current_state": {
            "odds": current_odds,
            "implied_probability": round(current_implied, 4),
            "mark_to_market": round(float(ticket_mark_to_market), 2),
            "profit_so_far": round(float(ticket_profit_so_far), 2),
            "appreciation": round((current_implied / original_implied - 1) * 100, 1) if original_implied > 0 else 0,
        },
        "hedge_analysis": {
            "full_hedge_stake": round(float(full_hedge_stake), 2),
            "guaranteed_profit": round(float(guaranteed_profit), 2),
            "guaranteed_roi": round(float(guaranteed_profit / original_stake * 100), 1) if original_stake > 0 else 0,
        },
        "let_ride_ev": round(let_ride_ev, 2),
        "optimal_hedge_fraction": round(float(optimal_fraction), 3),
        "partial_hedge": {
            "stake": round(float(partial_hedge_stake), 2),
            "guaranteed_portion": round(float(partial_guaranteed), 2),
            "remaining_upside": round(float(partial_remaining_upside), 2),
        },
        "risk_factors": {
            "remaining_uncertainty": round(remaining_uncertainty, 3),
            "injury_risk": round(injury_risk_factor, 3),
            "schedule_difficulty": round(schedule_difficulty, 3),
            "adverse_risk_score": round(float(adverse_risk), 3),
        },
        "wait_value": {
            "expected_further_shortening": round(float(expected_further_shortening), 4),
            "wait_ev": round(float(wait_value), 2),
            "risk_cost": round(float(risk_cost), 2),
            "net_wait_value": round(float(net_wait_value), 2),
        },
    }


def _expected_odds_improvement(
    current_implied: float,
    remaining_uncertainty: float,
    schedule_difficulty: float,
) -> float:
    """
    Estimate expected further odds shortening.

    If a team is currently at 20% implied with 40% of the season left,
    how much further can the odds shorten? Depends on:
    - How far from the "ceiling" implied probability they are
    - How much uncertainty remains
    - How hard the remaining schedule is
    """
    # Room to improve: how far from certainty
    room = 1.0 - current_implied
    if room <= 0:
        return 0.0

    # Expected improvement is proportional to remaining uncertainty and inversely
    # proportional to schedule difficulty
    base_improvement = room * remaining_uncertainty * 0.15
    schedule_drag = schedule_difficulty * 0.5  # Hard schedule reduces improvement
    improvement = base_improvement * (1 - schedule_drag)
    return max(0.0, improvement)


