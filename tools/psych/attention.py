"""
Market psychology submodules — split from the former tools/market_psychology.py.
"""

"""6. Cross-sport attention arbitrage — thin markets during marquee events."""

from tools.psych.constants import ATTENTION_WEIGHTS

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


