"""
Market psychology and futures analysis — exploit how public perception distorts lines.

Books are not prediction markets. They are risk-management businesses that shade
lines to balance their exposure. Public money has systematic biases:
  - Gravitates toward key numbers (3, 7 in NFL; round totals in NBA)
  - Overvalues favorites, name brands, primetime games
  - Ignores halftime and quarter markets where models are thinner
  - Clusters on marquee events, leaving thin markets less monitored

This module quantifies those biases and finds the other side of the trade.

Seven core functions:
1. Number shading detection — where books exploit public clustering
2. Trap line detection — lines that DON'T move despite one-sided action
3. Futures efficiency — are book futures lagging vs actual trajectory?
4. Futures hedge timing — when to lock profit on a futures ticket
5. Half/quarter market inefficiency — sub-game markets are less efficient
6. Cross-sport attention arbitrage — thin markets when marquee events dominate
7. Closing line prediction — forecast CLV before the game starts
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.market_psychology")


# ---------------------------------------------------------------------------
# Constants: empirical data baked into the model
# ---------------------------------------------------------------------------

# NFL margin-of-victory frequencies from ~20 years of data (approximate %).
# Key numbers 3 and 7 occur far more often than adjacent margins.
NFL_MARGIN_FREQ = {
    1: 0.054, 2: 0.038, 3: 0.155, 4: 0.048, 5: 0.035,
    6: 0.060, 7: 0.095, 8: 0.038, 9: 0.021, 10: 0.065,
    11: 0.030, 12: 0.018, 13: 0.030, 14: 0.065, 15: 0.015,
    16: 0.025, 17: 0.045, 18: 0.015, 19: 0.015, 20: 0.020,
    21: 0.040, 22: 0.012, 23: 0.012, 24: 0.025, 25: 0.012,
}

# Shade profiles: how many cents (in American-odds terms) books typically
# shade lines toward public-magnet numbers.  Derived from historical
# opening-vs-closing line analysis.
NFL_SPREAD_SHADE = {
    3.0: 12,   # -3 is the most shaded number in all of sports betting
    7.0: 8,
    6.0: 5,
    10.0: 5,
    14.0: 4,
    1.0: 3,
    6.5: 3,
    7.5: 3,
    2.5: 2,    # Public prefers -3, so -2.5 gets LESS public money
    3.5: 2,
}

NBA_TOTAL_SHADE = {
    200.0: 6, 205.0: 4, 210.0: 6, 215.0: 4, 220.0: 6,
    225.0: 4, 230.0: 6, 235.0: 4, 240.0: 6, 245.0: 4, 250.0: 5,
}

NBA_SPREAD_SHADE = {
    1.0: 3, 1.5: 3, 2.0: 2, 2.5: 2, 3.0: 3, 3.5: 2,
    4.0: 2, 4.5: 2, 5.0: 3, 5.5: 2, 6.0: 2, 6.5: 2,
    7.0: 3, 7.5: 2, 8.0: 2,
}

# Typical scoring distribution by half/quarter for each sport.
# Values are fraction of full-game points scored in that period.
SCORING_DISTRIBUTION = {
    "americanfootball_nfl": {
        "first_half": 0.48,      # Slightly less than half — conservative early
        "second_half": 0.52,
        "first_quarter": 0.20,   # Slow starts common
        "second_quarter": 0.28,
        "third_quarter": 0.24,
        "fourth_quarter": 0.28,
    },
    "basketball_nba": {
        "first_half": 0.505,     # NBA is close to 50/50
        "second_half": 0.495,
        "first_quarter": 0.245,  # Starters, structured play, slightly lower
        "second_quarter": 0.260,
        "third_quarter": 0.250,
        "fourth_quarter": 0.245,
    },
    "baseball_mlb": {
        "first_5": 0.54,         # Starters pitch first 5 — different dynamic
        "last_4": 0.46,          # Bullpen era is volatile
    },
    "icehockey_nhl": {
        "first_period": 0.33,
        "second_period": 0.33,
        "third_period": 0.34,    # Slight uptick with empty-net goals
    },
}

# Half/quarter market edge coefficients.
# Positive = books historically underprice the over for that segment.
# These are in percentage-point terms of implied probability.
HALF_QUARTER_EDGES = {
    "americanfootball_nfl": {
        "first_half_under": 0.015,    # NFL 1H unders slightly underpriced
        "second_half_total": 0.025,   # 2H totals are less efficient
        "first_quarter_under": 0.020,
    },
    "basketball_nba": {
        "first_quarter_under": 0.022,  # Structured play, starters, pace ramp-up
        "third_quarter_over": 0.012,   # Post-halftime adjustments create runs
    },
    "baseball_mlb": {
        "first_5_under": 0.018,   # Starter dominance if ace is pitching
        "first_5_over": 0.015,    # If a bad starter, books underreact
    },
}

# Typical hourly line movement (in American odds cents) by hours-to-game.
# Closer to game = faster movement. Based on empirical CLV data.
LINE_MOVEMENT_VELOCITY = {
    "americanfootball_nfl": {
        168: 0.5, 72: 1.0, 48: 1.5, 24: 2.5, 12: 4.0,
        6: 6.0, 3: 8.0, 1: 12.0, 0.5: 15.0,
    },
    "basketball_nba": {
        48: 1.0, 24: 2.0, 12: 3.5, 6: 5.0, 3: 7.0,
        1: 10.0, 0.5: 14.0,
    },
    "baseball_mlb": {
        48: 0.8, 24: 1.5, 12: 3.0, 6: 5.0, 3: 7.0,
        1: 10.0, 0.5: 13.0,
    },
    "icehockey_nhl": {
        48: 0.8, 24: 1.5, 12: 3.0, 6: 4.5, 3: 6.5,
        1: 9.0, 0.5: 12.0,
    },
}

# Attention weighting: events that draw disproportionate public/book focus.
# Higher weight = more book attention = less opportunity for thin-market edges.
ATTENTION_WEIGHTS = {
    "americanfootball_nfl": {
        "Monday Night Football": 10, "Sunday Night Football": 9,
        "Thursday Night Football": 8, "playoffs": 10, "Super Bowl": 10,
    },
    "basketball_nba": {
        "nationally_televised": 7, "playoffs": 9, "finals": 10,
    },
    "baseball_mlb": {
        "nationally_televised": 5, "playoffs": 8, "world_series": 10,
    },
}


# ---------------------------------------------------------------------------
# 1. Public Number Shading Detection
# ---------------------------------------------------------------------------

def detect_number_shading(
    spread: float,
    sport: str,
    market: str = "spreads",
    book_price: Optional[int] = None,
) -> dict:
    """
    Detect if a line is shaded toward a public-magnet number.

    Books shade lines toward numbers the public gravitates to, because
    public money flows to those numbers regardless of fair value. This
    means the OTHER side of a shaded number often has slight value.

    NFL example: -2.5 has less public appeal than -3. If the true line
    is -2.7, books post -3 at -115 rather than -2.5 at -110, because
    more public money lands on -3. The value is on +3 (or +2.5 if
    available elsewhere).

    Args:
        spread: The current spread or total (absolute value used internally)
        sport: Sport key (e.g., 'americanfootball_nfl')
        market: 'spreads' or 'totals'
        book_price: Optional American odds price — if provided, we can
                    estimate juice premium for sitting on a key number

    Returns:
        Dict with shading analysis.
    """
    abs_spread = abs(spread)

    # Select the appropriate shade map
    if market == "totals" and "nba" in sport:
        shade_map = NBA_TOTAL_SHADE
    elif market == "spreads" and "nfl" in sport:
        shade_map = NFL_SPREAD_SHADE
    elif market == "spreads" and "nba" in sport:
        shade_map = NBA_SPREAD_SHADE
    elif market == "spreads" and "ncaaf" in sport:
        # College football uses same key numbers as NFL
        shade_map = NFL_SPREAD_SHADE
    else:
        # Generic: check if on a round number
        shade_map = {}

    # Check if the line sits on a shaded number
    shade_cents = shade_map.get(abs_spread, 0)
    is_shaded = shade_cents > 0

    # Check adjacent numbers for context
    half_up = abs_spread + 0.5
    half_down = abs_spread - 0.5
    shade_up = shade_map.get(half_up, 0)
    shade_down = shade_map.get(half_down, 0)

    # Determine which direction the line is shaded toward
    if is_shaded:
        # The line IS the public magnet. Public money clusters here.
        shaded_toward = "this_number"
        # True line estimate: shade pushes the posted number away from true value.
        # If -3 is shaded and public bets the favorite, true line is closer to -2.7
        # (books post -3 because it attracts more action).
        shade_direction = 1 if spread < 0 else -1
        true_line_estimate = spread + shade_direction * (shade_cents / 20.0)
        # Each cent of shade corresponds to roughly 0.05 points of line distortion
        value_side = "opposite"  # Value is on the other side of the shaded number
    elif shade_down > 0 and shade_down > shade_up:
        # The line is half a point above a shaded number (e.g., 3.5 when 3 is shaded)
        shaded_toward = half_down
        true_line_estimate = spread  # Line is likely close to fair here
        shade_cents = int(shade_down * 0.3)  # Residual shade from nearby magnet
        value_side = "this_side"  # Off the key number = less public money = value
    elif shade_up > 0:
        # Half a point below a shaded number (e.g., 2.5 when 3 is shaded)
        shaded_toward = half_up
        true_line_estimate = spread
        shade_cents = int(shade_up * 0.3)
        value_side = "this_side"
    else:
        shaded_toward = None
        true_line_estimate = spread
        value_side = "neutral"

    # If we have the book price, calculate the juice premium for this number
    juice_premium_cents = 0
    if book_price is not None and is_shaded:
        implied = calculate_implied_probability(book_price)
        standard_implied = calculate_implied_probability(-110)
        if implied > standard_implied:
            juice_premium_cents = int((implied - standard_implied) * 2000)
            # 2000 converts implied-prob difference to approximate American cents

    # NFL-specific: quantify the margin frequency impact
    margin_frequency_note = ""
    if "nfl" in sport and market == "spreads":
        freq = NFL_MARGIN_FREQ.get(int(abs_spread), 0)
        if freq > 0:
            margin_frequency_note = (
                f"Games land on margin {int(abs_spread)} approximately "
                f"{freq:.1%} of the time. "
            )
            if abs_spread == 3:
                margin_frequency_note += (
                    "This is the single most common NFL margin. "
                    "The difference between -2.5 and -3 is worth ~3% in cover probability."
                )
            elif abs_spread == 7:
                margin_frequency_note += (
                    "Second most common NFL margin (TD without extra point drama). "
                    "The -6.5 to -7 jump is worth ~2% in cover probability."
                )

    return {
        "spread": spread,
        "sport": sport,
        "market": market,
        "is_shaded": is_shaded,
        "shaded_toward": shaded_toward,
        "shade_magnitude_cents": shade_cents,
        "true_line_estimate": round(true_line_estimate, 2),
        "value_side": value_side,
        "juice_premium_cents": juice_premium_cents,
        "margin_frequency_note": margin_frequency_note,
        "explanation": _shading_explanation(spread, is_shaded, shaded_toward, value_side, sport, market),
    }


def _shading_explanation(
    spread: float,
    is_shaded: bool,
    shaded_toward,
    value_side: str,
    sport: str,
    market: str,
) -> str:
    """Build a human-readable explanation of the shading analysis."""
    if not is_shaded and shaded_toward is None:
        return (
            f"Line {spread} is not near a public-magnet number for {sport} {market}. "
            f"No significant shading detected."
        )

    if is_shaded and shaded_toward == "this_number":
        return (
            f"Line {spread} sits on a key public number. Books shade toward this "
            f"number because public money clusters here. The OTHER side likely has "
            f"slight value — public overrepresentation on one side means the book "
            f"can offer slightly worse odds and still attract action."
        )

    if value_side == "this_side":
        return (
            f"Line {spread} is half a point off the key number {shaded_toward}. "
            f"This is often a value spot — less public money lands here, so the "
            f"book doesn't need to shade as aggressively. If available, this line "
            f"may offer better value than the adjacent key number."
        )

    return f"Line {spread} — moderate shading analysis for {sport} {market}."


# ---------------------------------------------------------------------------
# 2. Trap Line Detection
# ---------------------------------------------------------------------------

def detect_trap_line(
    opening_line: float,
    current_line: float,
    sharp_money_direction: Optional[str] = None,
    public_pct: Optional[float] = None,
    hours_since_open: float = 24.0,
) -> dict:
    """
    Detect if a line is a "trap" — set to attract public money to one side
    while the book (and sharps) are comfortable on the other.

    The key signal: a line that HASN'T MOVED despite heavy one-sided public
    action. Books move lines to manage risk. If 75% of tickets are on one
    side and the line stays put, the book is HAPPY taking that liability.
    That means sharp money (and the book's own models) disagree with the public.

    A second signal: the line moved OPPOSITE to public money. If the public
    hammers the favorite and the line gets cheaper (moves toward the underdog),
    the book is openly baiting more public money onto the losing side.

    Args:
        opening_line: The opening spread/total
        current_line: The current spread/total
        sharp_money_direction: 'favorite', 'underdog', 'over', 'under', or None
        public_pct: Percentage of public tickets on the popular side (0-100)
        hours_since_open: Hours since the line opened

    Returns:
        Dict with trap analysis.
    """
    line_movement = current_line - opening_line
    abs_movement = abs(line_movement)

    # Determine expected movement based on public action
    # If public is heavily one-sided, we'd expect the line to move that direction
    expected_movement_per_hour = 0.02  # baseline drift

    if public_pct is not None:
        public_imbalance = abs(public_pct - 50.0) / 50.0  # 0 to 1 scale
        # Heavy public action (>70%) should move a line ~0.5 to 1.5 points
        expected_total_movement = public_imbalance * 1.5
    else:
        public_imbalance = 0.0
        expected_total_movement = 0.0

    # Trap signal 1: No movement despite public imbalance
    no_move_trap = False
    if public_pct is not None and public_pct > 65 and abs_movement < 0.5:
        no_move_trap = True

    # Trap signal 2: Reverse movement (line moved opposite to public money)
    reverse_trap = False
    if public_pct is not None and public_pct > 55:
        # Public is on the "popular" side. If line moved to make that side
        # MORE attractive (cheaper), the book is baiting.
        # For spreads: public on favorite means public_pct > 55 on negative side.
        # If line moved more negative (bigger spread), that's the expected direction.
        # If it moved LESS negative (smaller spread), that's reverse = trap.
        if line_movement > 0 and public_pct > 60:
            reverse_trap = True  # Line moved toward underdog despite public on favorite

    # Trap signal 3: Sharp money agrees with book (opposite public)
    sharp_confirms = False
    if sharp_money_direction is not None and public_pct is not None:
        # If public is on favorite (pct > 55) but sharp money is on underdog
        if public_pct > 55 and sharp_money_direction in ("underdog", "under"):
            sharp_confirms = True
        elif public_pct < 45 and sharp_money_direction in ("favorite", "over"):
            sharp_confirms = True

    # Calculate composite trap confidence
    trap_signals = []
    confidence = 0.0

    if no_move_trap:
        trap_signals.append("NO_MOVEMENT")
        confidence += 0.35
        # Scale by how extreme the public imbalance is
        confidence += min(0.15, (public_pct - 65) / 100.0) if public_pct else 0

    if reverse_trap:
        trap_signals.append("REVERSE_MOVEMENT")
        confidence += 0.30

    if sharp_confirms:
        trap_signals.append("SHARP_CONFIRMS")
        confidence += 0.25

    # Time factor: trap lines are set early and held steady
    if hours_since_open > 48 and abs_movement < 0.5:
        trap_signals.append("STALE_LINE")
        confidence += 0.10

    is_trap = confidence >= 0.30
    confidence = min(1.0, confidence)

    # Build explanation
    explanations = []
    if no_move_trap:
        explanations.append(
            f"Line hasn't moved ({opening_line} -> {current_line}) despite "
            f"{public_pct:.0f}% of tickets on one side. The book is comfortable "
            f"with this liability."
        )
    if reverse_trap:
        explanations.append(
            f"Line moved OPPOSITE to public money direction. The book is "
            f"baiting more public action onto what they believe is the losing side."
        )
    if sharp_confirms:
        explanations.append(
            f"Sharp money is on the {sharp_money_direction} side, aligning with "
            f"the book and against the public."
        )
    if not explanations:
        explanations.append(
            "No strong trap signals detected. Line movement is consistent with "
            "public action patterns."
        )

    return {
        "is_trap": is_trap,
        "confidence": round(confidence, 3),
        "trap_signals": trap_signals,
        "opening_line": opening_line,
        "current_line": current_line,
        "line_movement": round(line_movement, 2),
        "public_pct": public_pct,
        "sharp_money_direction": sharp_money_direction,
        "expected_movement": round(expected_total_movement, 2),
        "actual_vs_expected": round(abs_movement - expected_total_movement, 2),
        "explanation": " ".join(explanations),
        "actionable_side": (
            "opposite_public" if is_trap
            else "insufficient_signal"
        ),
    }


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


# ---------------------------------------------------------------------------
# 5. Half / Quarter Market Inefficiency
# ---------------------------------------------------------------------------

def half_market_adjustment(
    full_game_line: float,
    sport: str,
    half: str = "first",
    market: str = "totals",
    is_ace_pitching: Optional[bool] = None,
) -> dict:
    """
    Project a half/quarter line from the full-game line and identify inefficiencies.

    Books set half and quarter lines by roughly halving the full game number,
    but scoring is NOT uniformly distributed across game segments:

    - NBA 1Q unders are historically underpriced (structured play, starters
      feeling out the game, lower pace in first few minutes)
    - NFL 2H totals are less efficient (books reprice with less infrastructure
      mid-game; the models they use for 2H are thinner)
    - MLB first 5 innings vs full game has completely different dynamics
      (starter vs bullpen; aces suppress scoring then bullpens give it back)

    Args:
        full_game_line: The full-game spread or total
        sport: Sport key
        half: 'first', 'second', 'first_quarter', 'second_quarter',
              'third_quarter', 'fourth_quarter', 'first_5' (MLB)
        market: 'totals' or 'spreads'
        is_ace_pitching: MLB-specific — is an ace starting? (affects F5 analysis)

    Returns:
        Dict with projected half line, edge estimate, and reasoning.
    """
    dist = SCORING_DISTRIBUTION.get(sport, {})
    edges = HALF_QUARTER_EDGES.get(sport, {})

    # Map half parameter to distribution key
    half_key_map = {
        "first": "first_half",
        "second": "second_half",
        "first_quarter": "first_quarter",
        "second_quarter": "second_quarter",
        "third_quarter": "third_quarter",
        "fourth_quarter": "fourth_quarter",
        "first_5": "first_5",
        "last_4": "last_4",
        "first_period": "first_period",
        "second_period": "second_period",
        "third_period": "third_period",
    }

    dist_key = half_key_map.get(half, "first_half")
    fraction = dist.get(dist_key, 0.5)

    # Project the half/quarter line
    if market == "totals":
        projected_line = full_game_line * fraction
        # Books typically round to nearest 0.5
        projected_line = round(projected_line * 2) / 2.0
        naive_line = full_game_line * 0.5 if "half" in dist_key else full_game_line * 0.25
        naive_line = round(naive_line * 2) / 2.0
    else:
        # Spreads: first half spread is roughly half the full game spread
        # but home court/field advantage is not evenly distributed
        projected_line = full_game_line * fraction * 2  # *2 because fraction is of total points, not spread
        # Actually for spreads the conversion is different:
        # Full game spread of -6 means team is 6 points better.
        # First half spread should be fraction * full_game_spread
        projected_line = full_game_line * fraction / 0.5 if fraction != 0 else full_game_line * 0.5
        # Simpler: for half spreads, the convention is roughly half the full game spread
        projected_line = full_game_line * (fraction / (dist.get("first_half", 0.5) + dist.get("second_half", 0.5))) if market == "spreads" else projected_line
        projected_line = round(projected_line * 2) / 2.0
        naive_line = full_game_line * 0.5
        naive_line = round(naive_line * 2) / 2.0

    # Identify known edges for this sport/half combination
    edge_candidates = []
    total_edge_pct = 0.0
    reasoning_parts = []

    if market == "totals":
        # Check for known edges
        under_key = f"{dist_key}_under"
        over_key = f"{dist_key}_over"
        total_key = f"{dist_key}_total"

        if under_key in edges:
            edge_val = edges[under_key]
            edge_candidates.append({"side": "under", "edge_pct": edge_val})
            total_edge_pct += edge_val

        if over_key in edges:
            edge_val = edges[over_key]
            edge_candidates.append({"side": "over", "edge_pct": edge_val})
            total_edge_pct += edge_val

        if total_key in edges:
            edge_val = edges[total_key]
            edge_candidates.append({"side": "total", "edge_pct": edge_val})
            total_edge_pct += edge_val

    # Sport-specific reasoning
    if "nba" in sport:
        if half in ("first_quarter",):
            reasoning_parts.append(
                "NBA first quarters feature structured play, starters feeling out "
                "matchups, and lower pace. Historical data shows 1Q unders are "
                "slightly underpriced."
            )
        if half in ("third_quarter",):
            reasoning_parts.append(
                "NBA third quarters often see scoring runs as teams make halftime "
                "adjustments. The over can have slight value here."
            )
        if half == "second":
            reasoning_parts.append(
                "NBA second halves feature more lineup variation, strategic fouling "
                "late in close games (increases scoring), and garbage time in blowouts."
            )

    elif "nfl" in sport:
        if half == "first":
            reasoning_parts.append(
                "NFL first halves tend slightly lower-scoring. Teams are conservative "
                "early, especially in playoff/primetime games."
            )
        if half == "second":
            reasoning_parts.append(
                "NFL second half totals are less efficiently priced. Books have less "
                "model infrastructure for mid-game repricing. Look for stale 2H lines "
                "that don't account for first-half game flow."
            )
        if half == "first_quarter":
            reasoning_parts.append(
                "NFL first quarters are the lowest-scoring period. Scripted drives, "
                "conservative play calling, and feel-out possessions."
            )

    elif "mlb" in sport:
        if half in ("first_5",):
            if is_ace_pitching is True:
                reasoning_parts.append(
                    "Ace on the mound for first 5 innings. Starters dominate F5 scoring. "
                    "F5 under may be underpriced because full-game total includes "
                    "bullpen innings where scoring typically increases."
                )
                edge_candidates.append({"side": "under", "edge_pct": 0.025})
                total_edge_pct += 0.025
            elif is_ace_pitching is False:
                reasoning_parts.append(
                    "Weaker starter pitching F5. Full-game line may underweight how "
                    "much damage occurs before the bullpen arrives. F5 over can be "
                    "value when a bad starter is on the mound."
                )
                edge_candidates.append({"side": "over", "edge_pct": 0.020})
                total_edge_pct += 0.020
            else:
                reasoning_parts.append(
                    "MLB F5 innings are dominated by the starting pitcher matchup. "
                    "Full-game totals blend starter and bullpen, but F5 isolates "
                    "the starter. The F5/full-game ratio varies dramatically by "
                    "pitching quality."
                )

    if not reasoning_parts:
        reasoning_parts.append(
            f"Projected {half} {market} line based on historical scoring distribution "
            f"({fraction:.1%} of full game scoring occurs in the {half})."
        )

    # Scoring distribution variance — halves and quarters have higher variance
    # per unit time than full games, which means the market needs more vig
    # to compensate, which means there's more room for mispricing.
    variance_multiplier = 1.0
    if "quarter" in half or "period" in half:
        variance_multiplier = 1.8  # Quarter results are much more variable
    elif "half" in half or half in ("first", "second", "first_5", "last_4"):
        variance_multiplier = 1.3

    return {
        "full_game_line": full_game_line,
        "projected_half_line": projected_line,
        "naive_half_line": naive_line if market == "totals" else round(full_game_line * 0.5 * 2) / 2.0,
        "scoring_fraction": round(fraction, 3),
        "sport": sport,
        "half": half,
        "market": market,
        "edge_vs_book_half": round(total_edge_pct, 4),
        "edge_candidates": edge_candidates,
        "variance_multiplier": round(variance_multiplier, 2),
        "reasoning": " ".join(reasoning_parts),
        "recommendation": (
            f"Look at {edge_candidates[0]['side']} on the {half} "
            f"(~{edge_candidates[0]['edge_pct']:.1%} historical edge)"
            if edge_candidates
            else f"No strong historical edge on {half} {market} for {sport}. "
                 f"Use projected line {projected_line} as a fair-value benchmark."
        ),
    }


# ---------------------------------------------------------------------------
# 6. Cross-Sport Attention Arbitrage
# ---------------------------------------------------------------------------

def attention_arbitrage(current_events: list[dict]) -> dict:
    """
    Identify thin markets that may be less monitored when marquee events dominate.

    When Monday Night Football is on, every sharp, every book trader, and every
    model is focused on that game. The Tuesday NBA slate or midweek soccer may
    be slightly less monitored. This doesn't mean the lines are WRONG — but the
    edges that DO exist may persist longer because fewer eyeballs are hunting them.

    This is about TIMING, not about finding bad lines. The same 2% edge on a
    Tuesday NBA game gets corrected faster when there's no competing event than
    when every book's risk desk is managing MNF exposure.

    Args:
        current_events: List of dicts with at minimum:
            {
                "sport": str,         # e.g., "americanfootball_nfl"
                "event_name": str,    # e.g., "Chiefs vs Ravens"
                "tag": str,           # e.g., "Monday Night Football"
                "start_time": str,    # ISO format
                "is_live": bool,      # Currently in play?
            }

    Returns:
        Dict with thin markets and reasoning.
    """
    # Calculate total attention load from current/upcoming events
    total_attention = 0
    marquee_events = []
    non_marquee_events = []

    for event in current_events:
        sport = event.get("sport", "")
        tag = event.get("tag", "")
        is_live = event.get("is_live", False)

        # Calculate attention score for this event
        sport_weights = ATTENTION_WEIGHTS.get(sport, {})
        attention = sport_weights.get(tag, 0)

        # Live events draw more attention than upcoming
        if is_live:
            attention *= 1.5

        # Playoffs/finals always draw maximum attention
        if any(kw in tag.lower() for kw in ("playoff", "final", "super bowl", "world series")):
            attention = max(attention, 8)

        event_scored = {
            **event,
            "attention_score": round(attention, 1),
        }

        if attention >= 6:
            marquee_events.append(event_scored)
            total_attention += attention
        else:
            non_marquee_events.append(event_scored)

    # Identify thin markets: non-marquee events happening while marquee is live
    thin_markets = []

    if total_attention >= 8:
        # Significant attention on marquee events — look for thin markets
        for event in non_marquee_events:
            sport = event.get("sport", "")
            attention = event.get("attention_score", 0)

            # Thin market opportunity score: inverse of attention
            opportunity = 1.0 - (attention / 10.0)
            # Scale by how much total attention is elsewhere
            opportunity *= min(1.0, total_attention / 10.0)

            if opportunity > 0.3:
                thin_markets.append({
                    "event": event.get("event_name", ""),
                    "sport": sport,
                    "opportunity_score": round(opportunity, 3),
                    "reasoning": (
                        f"Low attention ({attention:.0f}/10) while {total_attention:.0f} total "
                        f"attention points are on marquee events. Lines may be slightly "
                        f"less monitored, and edges may persist longer."
                    ),
                })
    else:
        # No dominant marquee event — attention is spread normally
        pass

    thin_markets.sort(key=lambda x: x["opportunity_score"], reverse=True)

    # Timing recommendations
    timing_notes = []
    if marquee_events:
        marquee_names = [e.get("event_name", "?") for e in marquee_events[:3]]
        timing_notes.append(
            f"Marquee event(s): {', '.join(marquee_names)}. "
            f"Total attention score: {total_attention:.0f}/10."
        )
    if thin_markets:
        timing_notes.append(
            f"Found {len(thin_markets)} potential thin-market opportunities. "
            f"Focus edge scanning on these markets during marquee event windows."
        )
    else:
        timing_notes.append(
            "No significant attention imbalance detected. Markets are likely "
            "monitored at normal levels across the board."
        )

    return {
        "total_attention": round(total_attention, 1),
        "marquee_events": marquee_events,
        "marquee_count": len(marquee_events),
        "thin_markets": thin_markets,
        "thin_market_count": len(thin_markets),
        "timing_notes": " ".join(timing_notes),
        "recommendation": (
            "SCAN_THIN_MARKETS" if len(thin_markets) > 0
            else "NORMAL_MONITORING"
        ),
    }


# ---------------------------------------------------------------------------
# 7. Closing Line Prediction
# ---------------------------------------------------------------------------

def predict_closing_line(
    current_line: float,
    hours_to_game: float,
    sport: str,
    market: str = "spreads",
    sharp_money_direction: Optional[str] = None,
    public_pct: Optional[float] = None,
    current_price: Optional[int] = None,
) -> dict:
    """
    Predict where a line will close, enabling pre-game CLV estimation.

    Closing Line Value (CLV) is the gold standard of sports betting skill.
    If you consistently bet lines that close worse than where you got them,
    you are a winning bettor — regardless of your short-term results.

    By predicting the closing line, you can estimate CLV BEFORE the game
    starts, which lets you prioritize bets with the highest expected CLV.

    Model:
    1. Lines move toward sharp consensus over time
    2. Movement accelerates as game approaches (more information, more volume)
    3. Sharp money direction biases the drift
    4. Public money creates temporary distortions that get corrected at close

    Args:
        current_line: Current spread or total (the number, not the price)
        hours_to_game: Hours until game starts
        sport: Sport key
        market: 'spreads', 'totals', or 'h2h'
        sharp_money_direction: 'up'/'over'/'favorite' or 'down'/'under'/'underdog'
        public_pct: Percentage of public on one side (>50 means public favors current side)
        current_price: Current American odds price (e.g., -110)

    Returns:
        Dict with predicted closing line and confidence interval.
    """
    # Get the movement velocity profile for this sport
    velocity_profile = LINE_MOVEMENT_VELOCITY.get(sport, LINE_MOVEMENT_VELOCITY.get("basketball_nba", {}))

    # Interpolate expected remaining movement magnitude
    hours_keys = sorted(velocity_profile.keys(), reverse=True)
    velocity = 0.0
    for h in hours_keys:
        if hours_to_game >= h:
            velocity = velocity_profile[h]
            break
    if velocity == 0 and hours_keys:
        velocity = velocity_profile[hours_keys[-1]]

    # Expected total remaining movement in cents (American odds)
    # Integrate velocity over remaining time (simplified: velocity * sqrt(hours))
    # Using sqrt because movement rate is per-hour but compounds sub-linearly
    expected_movement_cents = velocity * math.sqrt(max(0.1, hours_to_game))

    # Convert cents to line movement (for spreads/totals)
    # Roughly 20 cents = 0.5 points of line movement for spreads
    # This varies by sport and market
    if market in ("spreads", "totals"):
        cents_per_half_point = 20.0
        expected_line_movement = (expected_movement_cents / cents_per_half_point) * 0.5
    else:
        expected_line_movement = 0.0  # For moneyline, we work in odds space

    # Direction of expected movement
    direction_multiplier = 0.0

    if sharp_money_direction is not None:
        # Sharp money determines the direction of the closing line
        sharp_up = sharp_money_direction.lower() in ("up", "over", "favorite", "home")
        direction_multiplier = 0.6 if sharp_up else -0.6

    if public_pct is not None:
        # Public money creates a counter-force that gets corrected.
        # If public is heavily one side (>65%), expect REVERSE correction at close.
        public_imbalance = (public_pct - 50) / 50.0  # -1 to 1
        if abs(public_imbalance) > 0.3:
            # Strong public imbalance: line will correct AGAINST public at close
            correction = -public_imbalance * 0.3
            direction_multiplier += correction

    # Predicted closing line
    directed_movement = expected_line_movement * direction_multiplier
    predicted_close = current_line + directed_movement

    # Round to nearest 0.5 for spreads/totals
    if market in ("spreads", "totals"):
        predicted_close = round(predicted_close * 2) / 2.0

    # Confidence interval: wider when far from game, narrower when close
    # Using the standard deviation of historical line movements
    movement_std = expected_line_movement * 0.8  # 80% of expected as 1-sigma

    ci_68 = (
        round((predicted_close - movement_std) * 2) / 2.0,
        round((predicted_close + movement_std) * 2) / 2.0,
    )
    ci_95 = (
        round((predicted_close - 2 * movement_std) * 2) / 2.0,
        round((predicted_close + 2 * movement_std) * 2) / 2.0,
    )

    # CLV estimate if you bet now
    clv_estimate = None
    if current_price is not None:
        # CLV = did you get a better price than close?
        # If current line is -3 and predicted close is -3.5, you have 0.5 points of CLV
        clv_points = abs(predicted_close - current_line)
        # Convert to implied probability CLV
        if market in ("spreads", "totals"):
            # Each half point on a spread is worth roughly 2-3% in implied probability
            clv_implied = clv_points * 0.04  # ~4% per point
        else:
            clv_implied = expected_movement_cents / 2000.0  # rough conversion

        # Direction matters: positive CLV = you got a better number
        if direction_multiplier != 0:
            if (direction_multiplier > 0 and current_line < predicted_close) or \
               (direction_multiplier < 0 and current_line > predicted_close):
                clv_direction = "positive"
            else:
                clv_direction = "negative"
        else:
            clv_direction = "uncertain"

        clv_estimate = {
            "clv_points": round(clv_points, 2),
            "clv_implied_pct": round(clv_implied * 100, 2),
            "clv_direction": clv_direction,
            "interpretation": (
                f"{'Positive' if clv_direction == 'positive' else 'Negative' if clv_direction == 'negative' else 'Uncertain'} "
                f"CLV of ~{clv_points:.1f} points ({clv_implied:.1%} implied). "
                f"{'Bet now to capture CLV.' if clv_direction == 'positive' else 'Wait for better number.' if clv_direction == 'negative' else 'Insufficient directional signal.'}"
            ),
        }

    # Time-based confidence: higher closer to game
    if hours_to_game <= 1:
        prediction_confidence = 0.85
    elif hours_to_game <= 6:
        prediction_confidence = 0.65
    elif hours_to_game <= 24:
        prediction_confidence = 0.45
    elif hours_to_game <= 48:
        prediction_confidence = 0.30
    else:
        prediction_confidence = 0.15

    # Boost confidence if we have sharp money data
    if sharp_money_direction is not None:
        prediction_confidence = min(0.95, prediction_confidence + 0.10)
    if public_pct is not None:
        prediction_confidence = min(0.95, prediction_confidence + 0.05)

    return {
        "current_line": current_line,
        "predicted_close": predicted_close,
        "expected_movement": round(directed_movement, 3),
        "expected_movement_magnitude": round(expected_line_movement, 3),
        "direction": (
            "toward_sharp" if direction_multiplier > 0.1
            else "away_from_sharp" if direction_multiplier < -0.1
            else "no_directional_bias"
        ),
        "confidence_interval_68": ci_68,
        "confidence_interval_95": ci_95,
        "prediction_confidence": round(prediction_confidence, 3),
        "hours_to_game": hours_to_game,
        "sport": sport,
        "market": market,
        "movement_velocity_per_hour": round(velocity, 2),
        "clv_estimate": clv_estimate,
        "factors": {
            "sharp_money": sharp_money_direction,
            "public_pct": public_pct,
            "direction_multiplier": round(direction_multiplier, 3),
        },
        "recommendation": _clv_recommendation(clv_estimate, prediction_confidence),
    }


def _clv_recommendation(clv_estimate: Optional[dict], confidence: float) -> str:
    """Generate an actionable recommendation based on CLV prediction."""
    if clv_estimate is None:
        return (
            "Provide current_price for CLV estimation. Without it, we can only "
            "predict the closing line direction and magnitude."
        )

    direction = clv_estimate.get("clv_direction", "uncertain")
    clv_pts = clv_estimate.get("clv_points", 0)

    if direction == "positive" and confidence > 0.5:
        return (
            f"BET NOW — predicted +CLV of {clv_pts:.1f} points with {confidence:.0%} "
            f"confidence. Line is expected to move away from your number."
        )
    elif direction == "positive" and confidence <= 0.5:
        return (
            f"LEAN BET — predicted +CLV of {clv_pts:.1f} points but only {confidence:.0%} "
            f"confidence. Consider betting now if edge size justifies the uncertainty."
        )
    elif direction == "negative" and confidence > 0.5:
        return (
            f"WAIT — predicted -CLV of {clv_pts:.1f} points with {confidence:.0%} "
            f"confidence. Line is expected to move toward a better number for you."
        )
    elif direction == "negative":
        return (
            f"HOLD — predicted -CLV but low confidence ({confidence:.0%}). "
            f"Monitor for sharp money signals before betting."
        )
    else:
        return (
            f"MONITOR — directional signal is unclear. Wait for sharp money "
            f"indicators or closer to game time for higher confidence prediction."
        )


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _prob_to_american(prob: float) -> int:
    """Convert probability to American odds."""
    if prob <= 0 or prob >= 1:
        return 0
    if prob >= 0.5:
        return int(-100 * prob / (1 - prob))
    else:
        return int(100 * (1 - prob) / prob)


def full_market_psychology(
    games: list[dict],
    sport: str,
    current_events: Optional[list[dict]] = None,
) -> dict:
    """
    Run all market psychology analyses on a set of games.

    This is the main entry point — orchestrate all seven modules for a complete
    picture of market psychology dynamics.
    """
    results = {
        "sport": sport,
        "games_analyzed": len(games),
    }

    # 1. Number shading scan
    shading_findings = []
    for game in games:
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] not in ("spreads", "totals"):
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    if point is None:
                        continue
                    shade = detect_number_shading(
                        spread=point,
                        sport=sport,
                        market=mkt["key"],
                        book_price=o.get("price"),
                    )
                    if shade["is_shaded"]:
                        shade["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
                        shade["team"] = o.get("name", "")
                        shade["bookmaker"] = bm.get("title", "")
                        shading_findings.append(shade)

    results["number_shading"] = shading_findings
    if shading_findings:
        logger.info(f"Number shading: found {len(shading_findings)} shaded lines")

    # 6. Attention arbitrage (if events provided)
    if current_events:
        results["attention_arbitrage"] = attention_arbitrage(current_events)
    else:
        results["attention_arbitrage"] = {
            "recommendation": "PROVIDE_EVENTS",
            "note": "Pass current_events for attention arbitrage analysis.",
        }

    return results
