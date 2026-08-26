"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""7. Closing line prediction — forecast CLV before the game starts."""

import math
from typing import Optional

from tools.psych.constants import LINE_MOVEMENT_VELOCITY

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


