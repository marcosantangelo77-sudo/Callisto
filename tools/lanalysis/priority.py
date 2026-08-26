"""Expected value of information — prioritize analysis resources on likely edges."""

import numpy as np


def ev_of_analysis(game_data: dict) -> dict:
    """
    Estimate whether a game is worth spending analysis resources on.

    Before burning GPU cycles running simulations and deep research on a game,
    estimate whether there's likely edge to find. This is meta-optimization:
    allocating analysis bandwidth to the games most likely to yield +EV bets.

    High priority signals:
    - Late scratches or injury news (information asymmetry)
    - Weather uncertainty (totals impact)
    - Large line movement (something happened — find out what)
    - Thin market / low book count (less efficient pricing)
    - Public lopsided (contrarian opportunity)

    Low priority signals:
    - Stable line with no movement (market consensus, efficiently priced)
    - No news or injury changes (status quo)
    - Heavily traded game with tight consensus across books (efficient)
    - Key number lines with heavy juice (books protecting themselves)

    Args:
        game_data: Dict with any of the following keys:
            - line_movement: float (total points of line movement)
            - books_count: int (number of books with lines)
            - hours_to_game: float
            - has_injury_news: bool
            - has_weather_concern: bool
            - estimated_public_pct: float (public on popular side)
            - price_spread_across_books: float (max divergence in cents)
            - is_primetime: bool
            - sport: str
            - line_stable_hours: float (hours since last movement)

    Returns:
        Dict with priority score (0-100), reasoning, and recommendation.
    """
    score = 0.0
    reasons: list[str] = []
    penalties: list[str] = []

    # --- Positive factors (reasons to analyze) ---

    # Large line movement
    line_mv = abs(float(game_data.get("line_movement", 0)))
    if line_mv >= 2.0:
        score += 25
        reasons.append(f"Large line movement ({line_mv:.1f} pts) — investigate cause")
    elif line_mv >= 1.0:
        score += 15
        reasons.append(f"Notable line movement ({line_mv:.1f} pts)")
    elif line_mv >= 0.5:
        score += 5
        reasons.append(f"Moderate line movement ({line_mv:.1f} pts)")

    # Injury / late scratch news
    if game_data.get("has_injury_news", False):
        score += 20
        reasons.append("Injury news creates information asymmetry")

    # Weather concerns (especially for totals)
    if game_data.get("has_weather_concern", False):
        score += 15
        reasons.append("Weather uncertainty affects totals and game script")

    # Thin market / low book coverage
    books = int(game_data.get("books_count", 8))
    if books <= 3:
        score += 18
        reasons.append(f"Thin market ({books} books) — less efficient pricing")
    elif books <= 5:
        score += 8
        reasons.append(f"Limited book coverage ({books} books)")

    # Cross-book divergence
    price_spread = float(game_data.get("price_spread_across_books", 0))
    if price_spread >= 30:
        score += 20
        reasons.append(f"Large cross-book spread ({price_spread:.0f} cents) — disagreement")
    elif price_spread >= 15:
        score += 10
        reasons.append(f"Notable cross-book divergence ({price_spread:.0f} cents)")

    # Public lopsided
    public_pct = float(game_data.get("estimated_public_pct", 50))
    if public_pct >= 75:
        score += 15
        reasons.append(f"Public heavily lopsided ({public_pct:.0f}%) — contrarian opportunity")
    elif public_pct >= 65:
        score += 7
        reasons.append(f"Public leaning ({public_pct:.0f}%)")

    # Primetime game (more public money, more distortion)
    if game_data.get("is_primetime", False):
        score += 8
        reasons.append("Primetime game — amplified public bias")

    # Timing: closer to game = more urgent but less time to act
    htg = float(game_data.get("hours_to_game", 24))
    if 1 < htg < 6:
        score += 5
        reasons.append("Game approaching — time-sensitive analysis window")

    # --- Negative factors (reasons NOT to analyze) ---

    # Stable line
    stable_hours = float(game_data.get("line_stable_hours", 0))
    if stable_hours > 24 and line_mv < 0.5:
        score -= 15
        penalties.append(f"Line stable for {stable_hours:.0f} hours — market consensus")

    # Heavily traded with tight consensus
    if books >= 8 and price_spread < 10:
        score -= 10
        penalties.append("Heavily traded game with tight consensus — efficiently priced")

    # Game already started or too close to find edge
    if htg < 0.25:
        score -= 20
        penalties.append("Game imminent — insufficient time to act on analysis")

    # No movement, no news, no weather = boring
    if line_mv < 0.25 and not game_data.get("has_injury_news") and not game_data.get("has_weather_concern"):
        score -= 10
        penalties.append("No movement, no news, no weather concerns — status quo")

    # Clamp score
    priority_score = float(np.clip(score, 0, 100))

    # Classification
    if priority_score >= 60:
        priority = "HIGH"
        recommendation = "Allocate full analysis — multiple edge signals detected"
    elif priority_score >= 35:
        priority = "MEDIUM"
        recommendation = "Worth a quick analysis pass — some signals present"
    elif priority_score >= 15:
        priority = "LOW"
        recommendation = "Skim only — limited edge signals, don't spend heavy resources"
    else:
        priority = "SKIP"
        recommendation = "Skip analysis — efficiently priced, no actionable signals"

    return {
        "priority_score": round(priority_score, 1),
        "priority": priority,
        "recommendation": recommendation,
        "positive_signals": reasons,
        "negative_signals": penalties,
        "reasoning": (
            f"Priority: {priority} ({priority_score:.0f}/100). "
            f"Positive: {'; '.join(reasons) if reasons else 'none'}. "
            f"Negative: {'; '.join(penalties) if penalties else 'none'}."
        ),
    }
