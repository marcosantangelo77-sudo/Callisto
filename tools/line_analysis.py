"""
Line movement analysis and public betting module — decompose, detect, and exploit.

This module goes deeper than raw line movement detection. It separates the
SIGNAL (sharp money, steam moves, reverse line movement) from the NOISE
(random public fluctuations, small bet volume variance).

Core capabilities:
1. Movement decomposition — HP-filter-inspired trend/noise separation
2. Reverse line movement — the strongest sharp money indicator
3. Steam move detection — coordinated sharp action across books
4. Bet timing optimization — sport-specific windows where edges are widest
5. Public side estimation — infer where the public is without ticket data
6. Contrarian value — historically +EV fading the public
7. Expected value of analysis — prioritize GPU cycles on games with likely edge

Every function returns structured dicts consumable by the orchestrator agents.
"""

import logging
import math
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from tools.odds_api import calculate_implied_probability, calculate_ev

logger = logging.getLogger("callisto.line_analysis")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Team brand tiers — higher tier = more public action
# Tier 3: massive public brands that attract casual money
# Tier 2: popular teams with strong followings
# Tier 1: average public interest
TEAM_BRAND_TIERS: dict[str, int] = {
    # NFL
    "Dallas Cowboys": 3, "Kansas City Chiefs": 3, "San Francisco 49ers": 3,
    "Green Bay Packers": 3, "New England Patriots": 3, "Buffalo Bills": 2,
    "Philadelphia Eagles": 2, "Miami Dolphins": 2, "Detroit Lions": 2,
    "Baltimore Ravens": 2, "Las Vegas Raiders": 2, "Denver Broncos": 2,
    "Pittsburgh Steelers": 2, "Chicago Bears": 2, "New York Giants": 2,
    "Los Angeles Rams": 2, "Tampa Bay Buccaneers": 2,
    # NBA
    "Los Angeles Lakers": 3, "Golden State Warriors": 3, "Boston Celtics": 3,
    "Brooklyn Nets": 2, "New York Knicks": 2, "Chicago Bulls": 2,
    "Philadelphia 76ers": 2, "Dallas Mavericks": 2, "Miami Heat": 2,
    "Phoenix Suns": 2, "Milwaukee Bucks": 2, "Denver Nuggets": 2,
    # MLB
    "New York Yankees": 3, "Los Angeles Dodgers": 3, "Boston Red Sox": 3,
    "Chicago Cubs": 2, "Houston Astros": 2, "Atlanta Braves": 2,
    "San Francisco Giants": 2, "St. Louis Cardinals": 2,
    "Philadelphia Phillies": 2, "New York Mets": 2,
    # NCAAF / NCAAB — programs, not franchises
    "Alabama Crimson Tide": 3, "Ohio State Buckeyes": 3, "Notre Dame Fighting Irish": 3,
    "Michigan Wolverines": 3, "Georgia Bulldogs": 3, "Texas Longhorns": 3,
    "LSU Tigers": 2, "Clemson Tigers": 2, "USC Trojans": 2,
    "Duke Blue Devils": 2, "Kentucky Wildcats": 2, "North Carolina Tar Heels": 2,
    "Kansas Jayhawks": 2, "UCLA Bruins": 2,
}

# Sport-specific key numbers where lines cluster
NFL_KEY_NUMBERS = {3, 7, 6, 10, 14, 1, 4, 17, 21}

# Historical contrarian ROI by public percentage bucket (from database studies
# across 10+ NFL/NCAAF seasons — Bet Labs, SDQL, etc.).
# Format: (min_public_pct, max_public_pct) -> historical_roi for fading
CONTRARIAN_ROI_TABLE: dict[str, dict[tuple[int, int], float]] = {
    "americanfootball_nfl": {
        (50, 60): -0.5,   # Basically break-even minus vig
        (60, 70): 0.8,    # Slight positive
        (70, 80): 2.4,    # Meaningful edge
        (80, 90): 4.1,    # Strong contrarian zone
        (90, 100): 5.8,   # Rare, very strong
    },
    "americanfootball_ncaaf": {
        (50, 60): -0.3,
        (60, 70): 1.0,
        (70, 80): 2.8,
        (80, 90): 4.5,
        (90, 100): 6.2,
    },
    "basketball_nba": {
        (50, 60): -1.2,   # NBA market is more efficient
        (60, 70): -0.2,
        (70, 80): 1.1,
        (80, 90): 2.3,
        (90, 100): 3.5,
    },
    "basketball_ncaab": {
        (50, 60): -0.8,
        (60, 70): 0.5,
        (70, 80): 1.8,
        (80, 90): 3.2,
        (90, 100): 4.8,
    },
    "baseball_mlb": {
        (50, 60): -1.5,
        (60, 70): 0.3,
        (70, 80): 1.5,
        (80, 90): 3.0,
        (90, 100): 4.2,
    },
}

# Default ROI table for sports not explicitly modeled
_DEFAULT_ROI_TABLE: dict[tuple[int, int], float] = {
    (50, 60): -1.0,
    (60, 70): 0.0,
    (70, 80): 1.5,
    (80, 90): 3.0,
    (90, 100): 4.5,
}

# ---------------------------------------------------------------------------
# 1. Line movement decomposition
# ---------------------------------------------------------------------------


def decompose_movement(
    line_history: list[dict],
    sport: str = "americanfootball_nfl",
) -> dict:
    """
    Decompose a line history into trend (sharp) and noise (public) components.

    Uses a simplified Hodrick-Prescott-inspired filter. The HP filter separates
    a time series into a smooth trend component (lambda-penalized for curvature)
    and a cyclical/noise component. We use numpy to solve the linear system.

    For sports betting:
    - Trend = sustained directional movement from sharp money
    - Noise = random fluctuation from small/public bets

    The sharp component is the trend derivative: persistent moves in one direction.
    The public component is residual oscillation that doesn't survive smoothing.

    Args:
        line_history: List of dicts, each with:
            - timestamp: ISO string or Unix epoch
            - line: float (spread, total, or price)
            - book: str (bookmaker name, optional)
        sport: Sport key for lambda calibration.

    Returns:
        Dict with trend, noise, sharp_component, public_component, and metadata.
    """
    if len(line_history) < 3:
        return {
            "error": "Need at least 3 data points for decomposition",
            "trend": [],
            "noise": [],
            "sharp_component": 0.0,
            "public_component": 0.0,
        }

    # Extract time-ordered line values
    entries = sorted(line_history, key=lambda e: _parse_timestamp(e.get("timestamp", 0)))
    lines = np.array([float(e["line"]) for e in entries], dtype=np.float64)
    n = len(lines)

    # HP filter smoothing parameter.
    # Higher lambda = smoother trend (more aggressively separates noise).
    # NFL lines move slowly → higher lambda. NBA lines are more volatile → lower.
    lambda_map = {
        "americanfootball_nfl": 1600.0,
        "americanfootball_ncaaf": 1600.0,
        "basketball_nba": 400.0,
        "basketball_ncaab": 400.0,
        "baseball_mlb": 800.0,
        "icehockey_nhl": 800.0,
    }
    lam = lambda_map.get(sport, 800.0)

    # Solve the HP filter: minimise sum((y - tau)^2) + lambda * sum((tau_{t+1} - 2*tau_t + tau_{t-1})^2)
    # This is equivalent to: (I + lambda * K'K) * tau = y
    # where K is the second-difference matrix.
    identity = np.eye(n)

    # Build second-difference matrix K (n-2 x n)
    K = np.zeros((n - 2, n))
    for i in range(n - 2):
        K[i, i] = 1.0
        K[i, i + 1] = -2.0
        K[i, i + 2] = 1.0

    # Solve for trend
    A = identity + lam * (K.T @ K)
    trend = np.linalg.solve(A, lines)
    noise = lines - trend

    # Sharp component: net directional trend movement (first to last)
    sharp_component = float(trend[-1] - trend[0])

    # Public component: RMS of the noise — measures magnitude of random oscillation
    public_component = float(np.sqrt(np.mean(noise ** 2)))

    # Trend direction and strength
    if abs(sharp_component) < 0.25:
        trend_direction = "flat"
    elif sharp_component > 0:
        trend_direction = "rising"
    else:
        trend_direction = "falling"

    # Compute incremental trend velocities for steam detection
    trend_velocity = np.diff(trend)
    max_velocity = float(np.max(np.abs(trend_velocity))) if len(trend_velocity) > 0 else 0.0

    # Signal-to-noise ratio: how much of total variance is trend vs noise
    total_var = float(np.var(lines)) if np.var(lines) > 0 else 1e-9
    trend_var = float(np.var(trend))
    noise_var = float(np.var(noise))
    snr = trend_var / noise_var if noise_var > 1e-9 else float("inf")

    return {
        "trend": trend.tolist(),
        "noise": noise.tolist(),
        "raw_values": lines.tolist(),
        "sharp_component": round(sharp_component, 4),
        "public_component": round(public_component, 4),
        "trend_direction": trend_direction,
        "max_velocity": round(max_velocity, 4),
        "signal_to_noise": round(snr, 4),
        "trend_variance_pct": round((trend_var / total_var) * 100, 2) if total_var > 1e-9 else 0.0,
        "noise_variance_pct": round((noise_var / total_var) * 100, 2) if total_var > 1e-9 else 0.0,
        "data_points": n,
        "interpretation": (
            f"Sharp money moved the line {abs(sharp_component):.2f} points "
            f"({'toward favorite' if sharp_component < 0 else 'toward underdog'}) "
            f"with public noise RMS of {public_component:.2f}. "
            f"SNR={snr:.1f} — {'clean sharp signal' if snr > 3 else 'noisy, mixed action' if snr > 1 else 'mostly noise'}."
        ),
    }


# ---------------------------------------------------------------------------
# 2. Reverse line movement detection
# ---------------------------------------------------------------------------


def detect_rlm(
    line_movement_direction: float,
    public_ticket_pct: float,
    public_money_pct: float,
) -> dict:
    """
    Detect reverse line movement — the strongest sharp money indicator.

    RLM occurs when the line moves AGAINST where the majority of bets (tickets)
    are placed. This means fewer but LARGER (sharper) bets on the other side
    are moving the line despite being outnumbered in ticket count.

    The money percentage vs ticket percentage divergence is the key signal.
    If 70% of tickets are on Team A but only 45% of money is on Team A,
    sharp money is clearly on Team B.

    Args:
        line_movement_direction: Positive = line moved toward side A being
            more expensive (i.e., A got shorter / more favored).
            Negative = line moved away from side A.
        public_ticket_pct: Percentage of tickets on side A (0-100).
        public_money_pct: Percentage of total money on side A (0-100).

    Returns:
        Dict with is_rlm flag, confidence score, and interpretation.
    """
    # Normalize inputs
    ticket_pct = float(np.clip(public_ticket_pct, 0, 100))
    money_pct = float(np.clip(public_money_pct, 0, 100))

    # Ticket/money divergence: positive means more tickets than money on side A
    # → sharp money is on the OTHER side (side B)
    ticket_money_divergence = ticket_pct - money_pct

    # Determine if RLM exists
    # RLM case 1: majority of tickets on A, but line moves to make B cheaper
    #   → ticket_pct > 50, line_movement_direction < 0 (moved away from A)
    # RLM case 2: majority of tickets on B, but line moves to make A cheaper
    #   → ticket_pct < 50, line_movement_direction > 0 (moved toward A)
    public_side_is_a = ticket_pct > 50
    line_favors_a = line_movement_direction > 0

    # RLM = public on one side, line moves the other way
    is_rlm = (public_side_is_a and not line_favors_a) or (not public_side_is_a and line_favors_a)

    # Confidence scoring (0-1 scale)
    # Factors: ticket imbalance, ticket/money divergence, movement magnitude
    ticket_imbalance = abs(ticket_pct - 50) / 50.0  # 0 at 50%, 1 at 0/100%
    divergence_score = abs(ticket_money_divergence) / 40.0  # Normalize to ~0-1 range
    movement_magnitude = min(abs(line_movement_direction) / 3.0, 1.0)  # 3+ pts = max

    if is_rlm:
        # Weight the components — ticket/money divergence is the strongest signal
        confidence = float(np.clip(
            0.30 * ticket_imbalance + 0.45 * divergence_score + 0.25 * movement_magnitude,
            0.0, 1.0,
        ))
    else:
        confidence = 0.0

    # Determine the sharp side
    if is_rlm:
        sharp_side = "B (opposite of public)" if public_side_is_a else "A (opposite of public)"
    else:
        sharp_side = "aligned with public (no RLM)"

    # Build detailed interpretation
    if is_rlm and confidence > 0.6:
        strength = "STRONG"
        action = "High-confidence sharp signal — strongly consider the contrarian side"
    elif is_rlm and confidence > 0.35:
        strength = "MODERATE"
        action = "Meaningful RLM detected — worth including in analysis"
    elif is_rlm:
        strength = "WEAK"
        action = "Marginal RLM — may be noise, seek confirming signals"
    else:
        strength = "NONE"
        action = "No reverse line movement — line moving with public consensus"

    return {
        "is_rlm": is_rlm,
        "confidence": round(confidence, 4),
        "strength": strength,
        "sharp_side": sharp_side,
        "ticket_pct_side_a": round(ticket_pct, 1),
        "money_pct_side_a": round(money_pct, 1),
        "ticket_money_divergence": round(ticket_money_divergence, 1),
        "line_movement": round(line_movement_direction, 2),
        "interpretation": (
            f"Public: {ticket_pct:.0f}% tickets / {money_pct:.0f}% money on side A. "
            f"Line moved {'toward' if line_favors_a else 'away from'} A by "
            f"{abs(line_movement_direction):.1f} pts. "
            f"{'RLM DETECTED (' + strength + '): ' if is_rlm else 'No RLM: '}"
            f"{action}."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Steam move detection
# ---------------------------------------------------------------------------


def detect_steam(
    line_snapshots: list[dict],
    threshold_cents: int = 15,
    time_window_minutes: int = 10,
) -> list[dict]:
    """
    Detect steam moves — rapid coordinated line movement across multiple books.

    A steam move is when sharp syndicates hit multiple sportsbooks simultaneously,
    causing lines to move rapidly across the market. These are among the most
    reliable sharp signals because they represent coordinated, informed action.

    Detection logic:
    1. Group snapshots by time window
    2. For each window, measure movement magnitude per book
    3. If multiple books move >= threshold in the same direction within the window,
       flag as steam

    Velocity matters more than magnitude — a 15-cent move in 5 minutes is sharper
    than a 30-cent move over 6 hours.

    Args:
        line_snapshots: List of dicts, each with:
            - timestamp: ISO string or Unix epoch
            - line: float (the spread/total/price)
            - book: str (bookmaker name)
        threshold_cents: Minimum movement in cents (for prices) or 0.1*pts (for
            spreads) to qualify. Default 15 cents.
        time_window_minutes: Time window to check for coordinated moves.

    Returns:
        List of detected steam moves with metadata.
    """
    if len(line_snapshots) < 4:
        return []

    # Parse and sort by timestamp
    parsed = []
    for snap in line_snapshots:
        ts = _parse_timestamp(snap.get("timestamp", 0))
        parsed.append({
            "timestamp": ts,
            "line": float(snap["line"]),
            "book": snap.get("book", "unknown"),
        })
    parsed.sort(key=lambda x: x["timestamp"])

    window_seconds = time_window_minutes * 60
    threshold = threshold_cents / 100.0  # Convert cents to points

    # For each snapshot, look forward within the window and track per-book movement
    steam_moves = []
    processed_windows: set[tuple] = set()  # Avoid duplicate detections

    for i, anchor in enumerate(parsed):
        # Collect all snapshots within the time window after this anchor
        window_end = anchor["timestamp"] + window_seconds
        window_snaps = [s for s in parsed[i:] if s["timestamp"] <= window_end]

        if len(window_snaps) < 3:
            continue

        # Track first and last line per book within window
        book_first: dict[str, float] = {}
        book_last: dict[str, float] = {}
        book_first_ts: dict[str, float] = {}
        book_last_ts: dict[str, float] = {}

        for s in window_snaps:
            bk = s["book"]
            if bk not in book_first:
                book_first[bk] = s["line"]
                book_first_ts[bk] = s["timestamp"]
            book_last[bk] = s["line"]
            book_last_ts[bk] = s["timestamp"]

        # Calculate movement per book
        movements: dict[str, float] = {}
        velocities: dict[str, float] = {}
        for bk in book_first:
            mv = book_last[bk] - book_first[bk]
            elapsed = max(book_last_ts[bk] - book_first_ts[bk], 1.0)
            movements[bk] = mv
            velocities[bk] = mv / (elapsed / 60.0)  # Points per minute

        # Count books moving in the same direction above threshold
        up_movers = {bk: mv for bk, mv in movements.items() if mv >= threshold}
        down_movers = {bk: mv for bk, mv in movements.items() if mv <= -threshold}

        # Steam = 2+ books moving in same direction above threshold
        for direction_label, movers in [("up", up_movers), ("down", down_movers)]:
            if len(movers) < 2:
                continue

            # Create a signature to deduplicate
            sig = (
                direction_label,
                frozenset(movers.keys()),
                round(anchor["timestamp"] / (window_seconds / 2)),  # Bucket
            )
            if sig in processed_windows:
                continue
            processed_windows.add(sig)

            avg_movement = float(np.mean(list(movers.values())))
            max_movement = max(movers.values(), key=abs)
            avg_velocity = float(np.mean([velocities[bk] for bk in movers]))

            # Confidence based on coordination breadth, speed, and magnitude
            books_fraction = len(movers) / max(len(book_first), 1)
            magnitude_score = min(abs(avg_movement) / 0.5, 1.0)
            velocity_score = min(abs(avg_velocity) / 0.1, 1.0)  # 0.1 pts/min = high
            confidence = float(np.clip(
                0.35 * books_fraction + 0.35 * magnitude_score + 0.30 * velocity_score,
                0.0, 1.0,
            ))

            steam_moves.append({
                "direction": direction_label,
                "books_moved": len(movers),
                "total_books_tracked": len(book_first),
                "book_movements": {bk: round(mv, 4) for bk, mv in movers.items()},
                "book_velocities": {bk: round(velocities[bk], 4) for bk in movers},
                "avg_movement": round(avg_movement, 4),
                "max_movement": round(max_movement, 4),
                "avg_velocity_per_min": round(avg_velocity, 4),
                "window_start": anchor["timestamp"],
                "window_minutes": time_window_minutes,
                "confidence": round(confidence, 4),
                "interpretation": (
                    f"STEAM MOVE {direction_label.upper()}: {len(movers)}/{len(book_first)} "
                    f"books moved {direction_label} by avg {abs(avg_movement):.2f} pts in "
                    f"{time_window_minutes} min (velocity: {abs(avg_velocity):.3f} pts/min). "
                    f"Confidence: {confidence:.0%}."
                ),
            })

    # Sort by confidence descending
    steam_moves.sort(key=lambda x: x["confidence"], reverse=True)
    return steam_moves


# ---------------------------------------------------------------------------
# 4. Line timing optimization
# ---------------------------------------------------------------------------


def optimal_bet_timing(
    sport: str,
    market: str = "spreads",
    day_of_week: str = "sunday",
    hours_to_game: float = 24.0,
) -> dict:
    """
    Recommend optimal bet timing windows based on sport-specific market dynamics.

    Different sports have different patterns of line movement and efficiency.
    Knowing WHEN to bet is as important as knowing WHAT to bet.

    Key principles:
    - Lines are least efficient when first posted (opener) and become more
      efficient as they absorb information and sharp action.
    - Specific events create temporary inefficiency: injury reports, lineup
      cards, weather changes, etc.
    - Closing line value (CLV) is the gold standard — consistently beating the
      closing line is the hallmark of a winning bettor.

    Args:
        sport: Sport key (e.g., 'americanfootball_nfl', 'basketball_nba').
        market: Market type ('spreads', 'totals', 'h2h').
        day_of_week: Day of the week (lowercase).
        hours_to_game: Hours until game start.

    Returns:
        Dict with optimal window, reasoning, and historical edge estimates.
    """
    day = day_of_week.lower().strip()
    htg = float(hours_to_game)

    # Sport-specific timing profiles
    profiles: dict[str, dict] = {
        "americanfootball_nfl": _nfl_timing(market, day, htg),
        "americanfootball_ncaaf": _ncaaf_timing(market, day, htg),
        "basketball_nba": _nba_timing(market, day, htg),
        "basketball_ncaab": _ncaab_timing(market, day, htg),
        "baseball_mlb": _mlb_timing(market, day, htg),
        "icehockey_nhl": _nhl_timing(market, day, htg),
    }

    profile = profiles.get(sport, _generic_timing(market, day, htg))

    return {
        "sport": sport,
        "market": market,
        "day_of_week": day,
        "hours_to_game": htg,
        **profile,
    }


def _nfl_timing(market: str, day: str, htg: float) -> dict:
    """NFL-specific timing optimization."""
    windows = []
    reasoning_parts = []

    # Sunday night look-aheads (posted Sunday evening for next week)
    if day == "sunday" and htg > 144:  # > 6 days out
        windows.append({
            "window": "Sunday night look-ahead (6-7 days out)",
            "edge_estimate": 2.5,
            "urgency": "high",
        })
        reasoning_parts.append(
            "Look-ahead lines posted Sunday night have the widest edges — "
            "books set them quickly with minimal sharp action. Best time to "
            "grab value on strong opinions."
        )

    # Monday morning after injury reports
    if day == "monday" and 120 < htg < 144:
        windows.append({
            "window": "Monday morning post-injury report (5-6 days out)",
            "edge_estimate": 1.8,
            "urgency": "medium",
        })
        reasoning_parts.append(
            "Monday injury reports cause line adjustments. Books react to "
            "official reports but may overshoot or undershoot injury impact."
        )

    # Wednesday-Thursday: sharp money starts flowing
    if day in ("wednesday", "thursday") and 48 < htg < 96:
        windows.append({
            "window": "Mid-week sharp action (2-4 days out)",
            "edge_estimate": 1.2,
            "urgency": "medium",
        })
        reasoning_parts.append(
            "Mid-week is when sharp syndicates begin placing. Lines tighten "
            "but retail books lag behind the sharp moves."
        )

    # 30 minutes before kickoff — weather, inactive lists
    if htg <= 0.5:
        windows.append({
            "window": "Pre-kickoff (0-30 minutes)",
            "edge_estimate": 1.5,
            "urgency": "high",
        })
        reasoning_parts.append(
            "Final inactives and weather conditions are locked in. Totals "
            "are especially affected by wind/rain. Books adjust slowly on "
            "game-day weather shifts."
        )

    # Spread-specific: key number movement windows
    if market == "spreads" and 1 < htg < 6:
        windows.append({
            "window": "Game-day spread settling (1-6 hours out)",
            "edge_estimate": 0.8,
            "urgency": "low",
        })
        reasoning_parts.append(
            "Spreads settle near key numbers. If a line is sitting at -2.5 "
            "or -3.5 and you expect it to land on -3, timing matters for "
            "which side of the key number you want."
        )

    # Default: current moment assessment
    if not windows:
        if htg > 96:
            edge = 2.0
            window_label = "Early market (4+ days out)"
            note = "Early lines have more inefficiency but also more uncertainty. Good for strong opinions."
        elif 24 < htg <= 96:
            edge = 1.2
            window_label = "Mid-range (1-4 days out)"
            note = "Lines are tightening. Look for retail books lagging sharp moves."
        elif 6 < htg <= 24:
            edge = 0.8
            window_label = "Day-before to game-day morning"
            note = "Most sharp money already in. Edges are smaller but injury news can create windows."
        else:
            edge = 1.0
            window_label = "Pre-game (0-6 hours)"
            note = "Final adjustments. Weather and inactives are the main drivers."

        windows.append({
            "window": window_label,
            "edge_estimate": edge,
            "urgency": "medium",
        })
        reasoning_parts.append(note)

    optimal = max(windows, key=lambda w: w["edge_estimate"])

    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "NFL: look-ahead lines (Sunday night) have the widest inefficiencies. "
            "Lines sharpen through the week as sharps act. Pre-kickoff windows "
            "exist for weather/inactives. Key numbers (3, 7) create timing edges."
        ),
    }


def _ncaaf_timing(market: str, day: str, htg: float) -> dict:
    """NCAAF-specific timing optimization."""
    windows = []
    reasoning_parts = []

    # Sunday night openers for next Saturday
    if day == "sunday" and htg > 120:
        windows.append({"window": "Sunday night opener (5+ days out)", "edge_estimate": 3.0, "urgency": "high"})
        reasoning_parts.append(
            "College football openers have the widest edges in all of sports betting. "
            "Limited sharp action, massive public bias toward brand-name programs."
        )

    # Mid-week: weather and injury clarity for outdoor games
    if day in ("wednesday", "thursday") and 48 < htg < 96:
        windows.append({"window": "Mid-week (2-4 days out)", "edge_estimate": 2.0, "urgency": "medium"})
        reasoning_parts.append(
            "NCAAF lines see less sharp refinement than NFL. Mid-week value "
            "persists longer, especially on non-marquee matchups."
        )

    # Saturday morning — game-day for most NCAAF
    if day == "saturday" and 1 < htg < 6:
        windows.append({"window": "Saturday morning (1-6 hours out)", "edge_estimate": 1.5, "urgency": "medium"})
        reasoning_parts.append("Game-day adjustments for weather and late injury news.")

    if not windows:
        windows.append({"window": "Current", "edge_estimate": 1.5, "urgency": "medium"})
        reasoning_parts.append("NCAAF generally has wider edges than NFL at any timing point.")

    optimal = max(windows, key=lambda w: w["edge_estimate"])
    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "NCAAF: the most inefficient major market. Public brand bias is extreme. "
            "Openers have massive edges. Lines sharpen less than NFL due to lower limits."
        ),
    }


def _nba_timing(market: str, day: str, htg: float) -> dict:
    """NBA-specific timing optimization."""
    windows = []
    reasoning_parts = []

    # Early lines: 6+ hours before
    if htg > 6:
        windows.append({"window": "Early line (6+ hours out)", "edge_estimate": 1.8, "urgency": "high"})
        reasoning_parts.append(
            "NBA lines have the most inefficiency early — before rest/load management "
            "decisions are announced. Lines posted the night before are exploitable "
            "if you have strong opinions on lineup status."
        )

    # Post-lineup confirmation: ~1-2 hours out
    if 1 < htg <= 3:
        windows.append({"window": "Post-lineup lock (1-3 hours out)", "edge_estimate": 1.2, "urgency": "medium"})
        reasoning_parts.append(
            "After lineups are confirmed (including rest decisions), lines adjust. "
            "But retail books sometimes under-adjust for star player absences."
        )

    # Last 2 hours: lines sharpen significantly
    if 0 < htg <= 2:
        windows.append({"window": "Final 2 hours", "edge_estimate": 0.5, "urgency": "low"})
        reasoning_parts.append(
            "Lines sharpen significantly in the last 2 hours as sharp money "
            "concentrates. Edges are thin unless you have late-breaking info."
        )

    # Back-to-back detection (conceptual — actual b2b data comes from schedule)
    if day in ("saturday", "sunday", "monday", "wednesday"):
        reasoning_parts.append(
            "Check for back-to-back situations — NBA books systematically "
            "under-adjust for fatigue on the second night of a B2B, especially "
            "when travel is involved."
        )

    if not windows:
        windows.append({"window": "Current", "edge_estimate": 1.0, "urgency": "medium"})
        reasoning_parts.append("NBA market efficiency increases as tipoff approaches.")

    optimal = max(windows, key=lambda w: w["edge_estimate"])
    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "NBA: early lines (opener to 6 hours before) have the most inefficiency. "
            "Rest/load management creates information asymmetry. Lines sharpen "
            "dramatically in the last 2 hours. Back-to-back and travel fatigue "
            "are systematically underpriced."
        ),
    }


def _ncaab_timing(market: str, day: str, htg: float) -> dict:
    """NCAAB-specific timing — similar to NBA but with wider edges."""
    windows = []
    reasoning_parts = []

    if htg > 8:
        windows.append({"window": "Early line (8+ hours out)", "edge_estimate": 2.2, "urgency": "high"})
        reasoning_parts.append(
            "NCAAB has less sharp action than NBA. Early lines stay inefficient "
            "longer, especially for mid-major and non-marquee games."
        )

    if 2 < htg <= 8:
        windows.append({"window": "Pre-game (2-8 hours out)", "edge_estimate": 1.5, "urgency": "medium"})
        reasoning_parts.append("Lines tightening but still wider than NBA at same stage.")

    if htg <= 2:
        windows.append({"window": "Final 2 hours", "edge_estimate": 0.8, "urgency": "low"})
        reasoning_parts.append("Late sharp action narrows edges but less than NBA.")

    if not windows:
        windows.append({"window": "Current", "edge_estimate": 1.5, "urgency": "medium"})

    optimal = max(windows, key=lambda w: w["edge_estimate"])
    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "NCAAB: wider edges than NBA at every timing point due to lower limits "
            "and less sharp volume. Conference tournaments and March Madness see "
            "massive public money that distorts lines."
        ),
    }


def _mlb_timing(market: str, day: str, htg: float) -> dict:
    """MLB-specific timing optimization."""
    windows = []
    reasoning_parts = []

    # Before lineup cards: opening line
    if htg > 3:
        windows.append({"window": "Pre-lineup opening line (3+ hours out)", "edge_estimate": 1.5, "urgency": "medium"})
        reasoning_parts.append(
            "MLB opening lines are set based on probable pitchers. If you have "
            "strong opinions on bullpen usage, batting order, or platoon matchups, "
            "early lines offer value."
        )

    # After lineup cards: ~2 hours before first pitch
    if 1 < htg <= 3:
        windows.append({"window": "Post-lineup card (1-3 hours out)", "edge_estimate": 2.0, "urgency": "high"})
        reasoning_parts.append(
            "Lineup cards posted ~2 hours before first pitch are when pitcher-adjusted "
            "lines firm up. Late pitching changes and batting order shuffles create "
            "the best windows. This is the optimal MLB betting window."
        )

    # Weather window for totals
    if market == "totals" and htg <= 2:
        windows.append({"window": "Pre-game weather window (0-2 hours)", "edge_estimate": 1.8, "urgency": "high"})
        reasoning_parts.append(
            "Wind speed and direction at game time heavily impacts totals. "
            "Wrigley Field wind blowing out is worth 1-2 runs. Late weather "
            "updates create exploitable windows."
        )

    if not windows:
        windows.append({"window": "Current", "edge_estimate": 1.2, "urgency": "medium"})
        reasoning_parts.append("MLB market — timing around lineup cards is the key edge.")

    optimal = max(windows, key=lambda w: w["edge_estimate"])
    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "MLB: after lineup cards (~2 hours before first pitch) is when "
            "pitcher-adjusted lines firm up. Late pitching changes, weather "
            "shifts (especially wind for totals), and batting order surprises "
            "create the best timing edges."
        ),
    }


def _nhl_timing(market: str, day: str, htg: float) -> dict:
    """NHL-specific timing optimization."""
    windows = []
    reasoning_parts = []

    if htg > 6:
        windows.append({"window": "Early line (6+ hours out)", "edge_estimate": 1.3, "urgency": "medium"})
        reasoning_parts.append(
            "NHL opening lines are based on expected goaltender matchups. "
            "Goalie confirmations change the line significantly."
        )

    if 1 < htg <= 3:
        windows.append({"window": "Post-goalie confirmation (1-3 hours)", "edge_estimate": 1.8, "urgency": "high"})
        reasoning_parts.append(
            "Goalie confirmations (~2-3 hours before puck drop) cause the biggest "
            "line moves in NHL. Backup goalies are systematically under-adjusted "
            "at retail books."
        )

    if htg <= 1:
        windows.append({"window": "Pre-puck-drop (0-1 hour)", "edge_estimate": 0.8, "urgency": "low"})
        reasoning_parts.append("Lines are mostly set. Late scratches on defense can create small windows.")

    if not windows:
        windows.append({"window": "Current", "edge_estimate": 1.0, "urgency": "medium"})

    optimal = max(windows, key=lambda w: w["edge_estimate"])
    return {
        "optimal_window": optimal["window"],
        "historical_edge_pct": optimal["edge_estimate"],
        "all_windows": windows,
        "reasoning": " ".join(reasoning_parts),
        "general_principle": (
            "NHL: goalie confirmation is the single biggest information event. "
            "Lines move 15-30+ cents on backup goalie news. The window between "
            "confirmation and line adjustment is the primary edge."
        ),
    }


def _generic_timing(market: str, day: str, htg: float) -> dict:
    """Generic timing for sports without a specific profile."""
    if htg > 24:
        optimal = "Early line (24+ hours out)"
        edge = 1.5
        reasoning = "Early lines generally have more inefficiency across all sports."
    elif 6 < htg <= 24:
        optimal = "Day-of, pre-sharp (6-24 hours)"
        edge = 1.0
        reasoning = "Lines are tightening but retail lag creates opportunities."
    elif 1 < htg <= 6:
        optimal = "Pre-game (1-6 hours)"
        edge = 0.8
        reasoning = "Most sharp money is in. Late news is the main driver."
    else:
        optimal = "Pre-start (0-1 hour)"
        edge = 0.6
        reasoning = "Lines are near-efficient. Only late-breaking info creates edges."

    return {
        "optimal_window": optimal,
        "historical_edge_pct": edge,
        "all_windows": [{"window": optimal, "edge_estimate": edge, "urgency": "medium"}],
        "reasoning": reasoning,
        "general_principle": "General: earlier = wider edges but more uncertainty.",
    }


# ---------------------------------------------------------------------------
# 5. Public percentage estimation
# ---------------------------------------------------------------------------


def estimate_public_side(
    line_open: float,
    line_current: float,
    sport: str = "americanfootball_nfl",
    is_primetime: bool = False,
    is_rivalry: bool = False,
    team_a: str = "",
    team_b: str = "",
    team_a_recent_wins: int = 0,
    team_b_recent_wins: int = 0,
) -> dict:
    """
    Estimate which side the public is on without actual ticket data.

    Books don't publish real ticket/money percentages (except in limited
    contexts). But we can infer public lean from observable signals:

    1. Line movement: if the line moves toward the favorite, public money
       is likely on the favorite (books shade to balance exposure).
    2. Team brand value: big-name teams attract public money.
    3. Primetime/national TV: these games get disproportionate public handle.
    4. Recency bias: teams on 3+ game win streaks attract "hot team" public bets.
    5. Rivalry games: public gravitates to the historically dominant program.

    Args:
        line_open: Opening line (spread). Negative = team A favored.
        line_current: Current line.
        sport: Sport key.
        is_primetime: True if nationally televised / primetime slot.
        is_rivalry: True if known rivalry matchup.
        team_a: Name of team A (favorite side at open).
        team_b: Name of team B.
        team_a_recent_wins: Team A wins in last 3 games (0-3).
        team_b_recent_wins: Team B wins in last 3 games (0-3).

    Returns:
        Dict with estimated public percentage on each side and fade value.
    """
    # Base: start at 50/50 and adjust
    public_lean_a = 50.0  # Percentage estimated on team A

    # --- Factor 1: Line movement direction ---
    # If line moved to make A more favored (more negative), public is likely on A
    line_move = line_current - line_open
    # For spreads: line_move < 0 means A got more points (more favored)
    if line_move < -1.0:
        public_lean_a += 8.0  # Strong move toward A
    elif line_move < -0.5:
        public_lean_a += 4.0
    elif line_move > 1.0:
        public_lean_a -= 8.0  # Line moved away from A
    elif line_move > 0.5:
        public_lean_a -= 4.0

    # --- Factor 2: Favorite bias ---
    # Public likes favorites, especially big favorites
    if line_open < -7:
        public_lean_a += 10.0  # Big favorite attracts heavy public action
    elif line_open < -3:
        public_lean_a += 6.0
    elif line_open < 0:
        public_lean_a += 3.0
    elif line_open > 7:
        public_lean_a -= 10.0  # A is a big underdog
    elif line_open > 3:
        public_lean_a -= 6.0
    elif line_open > 0:
        public_lean_a -= 3.0

    # --- Factor 3: Team brand value ---
    brand_a = TEAM_BRAND_TIERS.get(team_a, 1)
    brand_b = TEAM_BRAND_TIERS.get(team_b, 1)
    brand_diff = brand_a - brand_b  # Positive = A is bigger brand
    public_lean_a += brand_diff * 5.0  # Each tier difference = ~5%

    # --- Factor 4: Primetime/national TV multiplier ---
    if is_primetime:
        # Primetime amplifies all public biases by ~30%
        excess = public_lean_a - 50.0
        public_lean_a = 50.0 + excess * 1.3

    # --- Factor 5: Rivalry boost ---
    if is_rivalry:
        # Rivalries attract casual bets — amplify brand and favorite bias
        excess = public_lean_a - 50.0
        public_lean_a = 50.0 + excess * 1.15

    # --- Factor 6: Recency bias (hot team effect) ---
    recency_a = min(team_a_recent_wins, 3)
    recency_b = min(team_b_recent_wins, 3)
    if recency_a == 3:
        public_lean_a += 6.0  # 3-game win streak = "hot" team
    elif recency_a >= 2:
        public_lean_a += 3.0
    if recency_b == 3:
        public_lean_a -= 6.0
    elif recency_b >= 2:
        public_lean_a -= 3.0

    # Clamp to reasonable range
    public_lean_a = float(np.clip(public_lean_a, 15.0, 85.0))
    public_lean_b = 100.0 - public_lean_a

    # Fade value: how much contrarian edge exists from fading the public side
    fade_side = "B" if public_lean_a > 55 else "A" if public_lean_b > 55 else "neither"
    public_pct_on_popular = max(public_lean_a, public_lean_b)

    # Look up historical contrarian ROI for this sport and public pct
    roi_table = CONTRARIAN_ROI_TABLE.get(sport, _DEFAULT_ROI_TABLE)
    fade_roi = 0.0
    for (lo, hi), roi in roi_table.items():
        if lo <= public_pct_on_popular < hi:
            fade_roi = roi
            break

    return {
        "estimated_public_pct_a": round(public_lean_a, 1),
        "estimated_public_pct_b": round(public_lean_b, 1),
        "public_favorite": "A" if public_lean_a > 55 else "B" if public_lean_b > 55 else "split",
        "fade_side": fade_side,
        "fade_value": round(fade_roi, 2),
        "confidence": _public_estimation_confidence(public_lean_a, is_primetime, sport),
        "factors": {
            "line_movement_impact": round(line_move, 2),
            "favorite_bias": round(line_open, 1),
            "brand_a_tier": brand_a,
            "brand_b_tier": brand_b,
            "primetime": is_primetime,
            "rivalry": is_rivalry,
            "recency_a": recency_a,
            "recency_b": recency_b,
        },
        "interpretation": (
            f"Estimated public split: {public_lean_a:.0f}% on {team_a or 'A'} / "
            f"{public_lean_b:.0f}% on {team_b or 'B'}. "
            f"{'Fade ' + (team_a or 'A') + ' (contrarian on ' + (team_b or 'B') + ')' if fade_side == 'B' else 'Fade ' + (team_b or 'B') + ' (contrarian on ' + (team_a or 'A') + ')' if fade_side == 'A' else 'No strong fade signal'}. "
            f"Historical contrarian ROI at this public %: {fade_roi:+.1f}%."
        ),
    }


def _public_estimation_confidence(public_lean_a: float, is_primetime: bool, sport: str) -> str:
    """Rate confidence in the public estimate."""
    # More extreme estimates are higher confidence (strong signals)
    extremity = abs(public_lean_a - 50)
    if extremity > 20 and is_primetime:
        return "high"
    elif extremity > 15:
        return "medium-high"
    elif extremity > 8:
        return "medium"
    else:
        return "low"


# ---------------------------------------------------------------------------
# 6. Contrarian value
# ---------------------------------------------------------------------------


def contrarian_value(
    estimated_public_pct: float,
    sport: str = "americanfootball_nfl",
    spread: float = 0.0,
) -> dict:
    """
    Calculate the expected contrarian edge from fading the public.

    Historical data shows that when 75%+ of the public is on one side in
    NFL/NCAAF, the other side has been marginally +EV. This effect is:
    - Stronger at non-key numbers (not 3, 7 in NFL)
    - Stronger in larger spreads
    - Strongest in NCAAF (least efficient market)
    - Weaker in NBA (more efficient, lower scoring variance)

    This is NOT a primary signal — it's a tiebreaker and overlay. Use it to
    add confidence to positions already supported by sharp indicators.

    Args:
        estimated_public_pct: Estimated percentage of public on the popular side (50-100).
        sport: Sport key.
        spread: The point spread (absolute value used for key number check).

    Returns:
        Dict with contrarian edge, historical ROI, and confidence.
    """
    pct = float(np.clip(estimated_public_pct, 50, 100))
    abs_spread = abs(spread)

    # Look up base ROI
    roi_table = CONTRARIAN_ROI_TABLE.get(sport, _DEFAULT_ROI_TABLE)
    base_roi = 0.0
    for (lo, hi), roi in roi_table.items():
        if lo <= pct < hi:
            base_roi = roi
            break

    # Key number adjustment (NFL/NCAAF only)
    is_football = "football" in sport
    on_key_number = False
    key_number_adjustment = 0.0
    if is_football:
        # Check if spread is on a key number
        on_key_number = abs_spread in NFL_KEY_NUMBERS or (abs_spread % 1 == 0 and int(abs_spread) in NFL_KEY_NUMBERS)
        if on_key_number:
            # Key numbers are more efficiently priced — less contrarian value
            key_number_adjustment = -0.8
        else:
            # Off key numbers: contrarian value is amplified
            key_number_adjustment = 0.5

    adjusted_roi = base_roi + key_number_adjustment

    # Large spread adjustment: big favorites attract more uninformed public action
    if abs_spread > 10:
        adjusted_roi += 0.5
    elif abs_spread > 7:
        adjusted_roi += 0.3

    # Confidence based on public percentage and sample strength
    if pct >= 75:
        confidence = "high" if adjusted_roi > 2.0 else "medium"
    elif pct >= 65:
        confidence = "medium"
    else:
        confidence = "low"

    # Calculate implied edge as a probability bump
    # 2% ROI at standard -110 vig implies roughly a 1% probability edge
    contrarian_edge_pct = adjusted_roi / 2.0  # Rough conversion: ROI/2 ≈ prob edge

    return {
        "estimated_public_pct": round(pct, 1),
        "sport": sport,
        "spread": spread,
        "base_historical_roi": round(base_roi, 2),
        "key_number_adjustment": round(key_number_adjustment, 2),
        "on_key_number": on_key_number,
        "adjusted_roi": round(adjusted_roi, 2),
        "contrarian_edge": round(contrarian_edge_pct, 2),
        "confidence": confidence,
        "historical_roi": round(adjusted_roi, 2),
        "interpretation": (
            f"With {pct:.0f}% public on the popular side in {sport}, "
            f"historical contrarian ROI is {adjusted_roi:+.1f}% "
            f"({'on' if on_key_number else 'off'} key number{', reduced edge' if on_key_number else ', amplified edge'}). "
            f"Confidence: {confidence}. "
            f"{'Use as confirming signal alongside sharp indicators.' if confidence != 'low' else 'Weak signal alone — need sharps to confirm.'}"
        ),
    }


# ---------------------------------------------------------------------------
# 7. Expected value of information (analysis prioritization)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Composite analysis
# ---------------------------------------------------------------------------


def full_line_analysis(
    line_history: list[dict],
    sport: str,
    line_open: float,
    line_current: float,
    public_ticket_pct: Optional[float] = None,
    public_money_pct: Optional[float] = None,
    is_primetime: bool = False,
    is_rivalry: bool = False,
    team_a: str = "",
    team_b: str = "",
    hours_to_game: float = 24.0,
    day_of_week: str = "sunday",
    game_data: Optional[dict] = None,
) -> dict:
    """
    Run all line analysis components and return a unified report.

    This is the main entry point — call with whatever data is available.
    Components that lack required data will be skipped gracefully.
    """
    report: dict = {
        "sport": sport,
        "team_a": team_a,
        "team_b": team_b,
        "line_open": line_open,
        "line_current": line_current,
    }

    # 1. Decomposition
    if line_history and len(line_history) >= 3:
        report["decomposition"] = decompose_movement(line_history, sport)
    else:
        report["decomposition"] = {"note": "Insufficient line history for decomposition (need 3+ points)"}

    # 2. RLM detection
    line_movement_direction = line_current - line_open
    if public_ticket_pct is not None and public_money_pct is not None:
        report["rlm"] = detect_rlm(line_movement_direction, public_ticket_pct, public_money_pct)
    else:
        # Use estimated public side
        est = estimate_public_side(
            line_open, line_current, sport, is_primetime, is_rivalry,
            team_a, team_b,
        )
        report["public_estimate"] = est
        # Synthesize an RLM check with estimated data
        est_ticket = est["estimated_public_pct_a"]
        est_money = est_ticket * 0.85  # Public money % is typically less skewed than tickets
        report["rlm"] = detect_rlm(line_movement_direction, est_ticket, est_money)
        report["rlm"]["note"] = "Based on estimated (not actual) public percentages"

    # 3. Steam moves (requires multi-book snapshots — just check if history qualifies)
    if line_history and len(line_history) >= 4:
        books_in_history = set(e.get("book", "") for e in line_history)
        if len(books_in_history) >= 2:
            report["steam_moves"] = detect_steam(line_history)
        else:
            report["steam_moves"] = {"note": "Need multi-book snapshots for steam detection"}
    else:
        report["steam_moves"] = {"note": "Insufficient data for steam detection"}

    # 4. Timing
    report["timing"] = optimal_bet_timing(sport, "spreads", day_of_week, hours_to_game)

    # 5. Public estimate (if not already computed)
    if "public_estimate" not in report:
        report["public_estimate"] = estimate_public_side(
            line_open, line_current, sport, is_primetime, is_rivalry,
            team_a, team_b,
        )

    # 6. Contrarian value
    public_popular_pct = max(
        report["public_estimate"]["estimated_public_pct_a"],
        report["public_estimate"]["estimated_public_pct_b"],
    )
    report["contrarian"] = contrarian_value(public_popular_pct, sport, line_current)

    # 7. EV of further analysis
    if game_data:
        report["analysis_priority"] = ev_of_analysis(game_data)
    else:
        # Build minimal game_data from what we have
        synthetic_game_data = {
            "line_movement": abs(line_movement_direction),
            "hours_to_game": hours_to_game,
            "is_primetime": is_primetime,
            "sport": sport,
            "estimated_public_pct": public_popular_pct,
        }
        report["analysis_priority"] = ev_of_analysis(synthetic_game_data)

    # Summary
    signals = []
    if report.get("rlm", {}).get("is_rlm"):
        signals.append(f"RLM ({report['rlm']['strength']})")
    if isinstance(report.get("steam_moves"), list) and report["steam_moves"]:
        signals.append(f"Steam moves ({len(report['steam_moves'])})")
    decomp = report.get("decomposition", {})
    if isinstance(decomp, dict) and decomp.get("signal_to_noise", 0) > 3:
        signals.append(f"Clean sharp trend (SNR={decomp['signal_to_noise']:.1f})")
    if report.get("contrarian", {}).get("adjusted_roi", 0) > 2:
        signals.append(f"Contrarian value ({report['contrarian']['adjusted_roi']:+.1f}% ROI)")

    report["summary"] = {
        "signals_detected": signals,
        "signal_count": len(signals),
        "overall_assessment": (
            "STRONG — multiple confirming signals" if len(signals) >= 3
            else "MODERATE — some signals present" if len(signals) >= 1
            else "WEAK — no strong signals detected"
        ),
    }

    return report


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def _parse_timestamp(ts) -> float:
    """Convert various timestamp formats to Unix epoch float."""
    if isinstance(ts, (int, float)):
        # Already numeric — assume Unix epoch
        # If it's in milliseconds (> year 2100 in seconds), convert
        if ts > 4_102_444_800:
            return float(ts) / 1000.0
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
        # Try common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    # Fallback: return 0 (will sort to beginning)
    logger.warning(f"Could not parse timestamp: {ts}")
    return 0.0
