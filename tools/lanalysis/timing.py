"""Bet timing optimization — sport-specific windows where edges are widest."""


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
