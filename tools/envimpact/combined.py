"""
Combined environmental adjustment: weather + venue + referees in one call,
with confidence scoring and human-readable summaries.
"""

from typing import Optional

from tools.envimpact.referees import MLB_UMPIRES, NBA_REFEREES, NFL_REFEREES
from tools.envimpact.weather import (
    get_venue_factors,
    get_weather_adjustment,
)


# =============================================================================
# REFEREE / UMPIRE TENDENCY LOOKUP
# =============================================================================

def ref_tendency(ref_name: str, sport: str) -> dict:
    """
    Look up referee/umpire tendency data.

    Uses fuzzy-ish matching: case-insensitive, checks if the input is contained
    in any known ref name (or vice versa) to handle partial names.

    Args:
        ref_name: Referee or umpire name.
        sport: Sport code ("NFL", "NBA", "MLB").

    Returns:
        dict with pace_impact, foul_rate_impact, total_adjustment, and notes.
    """
    sport = sport.upper()
    ref_name_lower = ref_name.strip().lower()

    # Select the right database
    if sport == "NBA":
        db = NBA_REFEREES
    elif sport == "MLB":
        db = MLB_UMPIRES
    elif sport == "NFL":
        db = NFL_REFEREES
    else:
        return {
            "ref_name": ref_name,
            "sport": sport,
            "pace_impact": 0.0,
            "foul_rate_impact": 0.0,
            "total_adjustment": 0.0,
            "notes": f"No referee database for sport: {sport}",
            "found": False,
        }

    # Fuzzy lookup: check containment both ways
    matched_name = None
    matched_data = None
    for known_name, data in db.items():
        known_lower = known_name.lower()
        if ref_name_lower == known_lower:
            matched_name = known_name
            matched_data = data
            break
        if ref_name_lower in known_lower or known_lower in ref_name_lower:
            matched_name = known_name
            matched_data = data
            break

    if matched_data is None:
        return {
            "ref_name": ref_name,
            "sport": sport,
            "pace_impact": 0.0,
            "foul_rate_impact": 0.0,
            "total_adjustment": 0.0,
            "notes": f"Referee '{ref_name}' not found in {sport} database. Using neutral values.",
            "found": False,
        }

    # Build standardized response
    if sport == "NBA":
        return {
            "ref_name": matched_name,
            "sport": sport,
            "pace_impact": matched_data["pace_impact"],
            "foul_rate_impact": matched_data["foul_rate_delta"],
            "total_adjustment": matched_data["total_adj"],
            "notes": matched_data["notes"],
            "found": True,
        }
    elif sport == "MLB":
        return {
            "ref_name": matched_name,
            "sport": sport,
            "pace_impact": 0.0,  # Not directly applicable for umpires
            "foul_rate_impact": 0.0,
            "zone_size_delta": matched_data["zone_size_delta"],
            "k_rate_impact": matched_data["k_rate_impact"],
            "total_adjustment": matched_data["total_adj"],
            "notes": matched_data["notes"],
            "found": True,
        }
    elif sport == "NFL":
        return {
            "ref_name": matched_name,
            "sport": sport,
            "pace_impact": 0.0,
            "foul_rate_impact": matched_data["penalty_rate_delta"],
            "pass_interference_rate": matched_data["pass_interference_rate"],
            "total_adjustment": matched_data["total_adj"],
            "notes": matched_data["notes"],
            "found": True,
        }

    return {}  # Should never reach here


# =============================================================================
# COMBINED ENVIRONMENTAL ADJUSTMENT
# =============================================================================

def total_environment_adjustment(
    venue: str,
    sport: str,
    weather: Optional[dict] = None,
    refs: Optional[list[str]] = None,
) -> dict:
    """
    Compute the complete environmental adjustment combining weather, venue,
    and referee factors.

    This is the main entry point for the orchestrator. Provide whatever
    information you have and get back a single adjustment with confidence.

    Args:
        venue: Home team abbreviation.
        sport: Sport code.
        weather: Optional weather dict (wind_speed_mph, wind_direction, temp_f,
                 humidity_pct, precipitation).
        refs: Optional list of referee/umpire names assigned to the game.
              NBA: typically 3 refs, we average their tendencies.
              MLB: home plate umpire only (first in list).
              NFL: head referee only (first in list).

    Returns:
        dict with total_adj, spread_adj, confidence (0-1), and factors_breakdown.
    """
    sport = sport.upper()

    # Get weather/venue adjustment
    weather_result = get_weather_adjustment(venue, sport, weather)
    total_adj = weather_result["total_adj"]
    spread_adj = weather_result["spread_adj"]
    factors_breakdown = weather_result["factors"]
    confidence = 0.0

    # Base confidence from venue data (we always have this)
    confidence += 0.3

    # Confidence boost if we have weather data
    if weather is not None:
        weather_fields_present = sum(1 for k in ["wind_speed_mph", "temp_f", "humidity_pct", "precipitation"]
                                     if k in weather and weather[k] is not None)
        confidence += 0.1 * weather_fields_present  # Up to +0.4 for full weather data

    # Referee adjustment
    ref_total_adj = 0.0
    ref_factors = []

    if refs:
        if sport == "NBA":
            # Average all refs in the crew (typically 3)
            ref_adjs = []
            for r in refs:
                rt = ref_tendency(r, sport)
                if rt.get("found"):
                    ref_adjs.append(rt["total_adjustment"])
                    ref_factors.append(rt)
                else:
                    ref_factors.append(rt)

            if ref_adjs:
                ref_total_adj = sum(ref_adjs) / len(ref_adjs)
                confidence += 0.15

        elif sport == "MLB":
            # Only home plate umpire matters for zone
            rt = ref_tendency(refs[0], sport)
            ref_total_adj = rt["total_adjustment"]
            ref_factors.append(rt)
            if rt.get("found"):
                confidence += 0.15

        elif sport == "NFL":
            # Head referee sets the crew tone
            rt = ref_tendency(refs[0], sport)
            ref_total_adj = rt["total_adjustment"]
            ref_factors.append(rt)
            if rt.get("found"):
                confidence += 0.15

        total_adj += ref_total_adj
        if ref_total_adj != 0:
            factors_breakdown.append({
                "factor": "referees",
                "adjustment": round(ref_total_adj, 2),
                "refs": ref_factors,
            })

    # Cap confidence
    confidence = min(confidence, 1.0)

    # Classify the adjustment magnitude
    abs_adj = abs(total_adj)
    if abs_adj < 0.5:
        significance = "negligible"
    elif abs_adj < 1.5:
        significance = "minor"
    elif abs_adj < 3.0:
        significance = "moderate"
    elif abs_adj < 5.0:
        significance = "significant"
    else:
        significance = "extreme"

    # Build recommendation
    if total_adj > 0.5:
        lean = "OVER"
    elif total_adj < -0.5:
        lean = "UNDER"
    else:
        lean = "NEUTRAL"

    return {
        "total_adj": round(total_adj, 2),
        "spread_adj": round(spread_adj, 2),
        "confidence": round(confidence, 2),
        "significance": significance,
        "lean": lean,
        "factors_breakdown": factors_breakdown,
        "summary": _build_summary(venue, sport, total_adj, significance, lean, weather, refs),
    }


def _build_summary(
    venue: str,
    sport: str,
    total_adj: float,
    significance: str,
    lean: str,
    weather: Optional[dict],
    refs: Optional[list[str]],
) -> str:
    """Build a human-readable summary of the environmental assessment."""
    parts = [f"Environmental assessment for {venue} ({sport}):"]

    if significance == "negligible":
        parts.append(f"Net adjustment: {total_adj:+.1f} pts (negligible). No actionable edge.")
    else:
        parts.append(f"Net adjustment: {total_adj:+.1f} pts ({significance}). Lean: {lean}.")

    if weather:
        conditions = []
        if "wind_speed_mph" in weather and weather["wind_speed_mph"]:
            wind_dir = weather.get("wind_direction", "")
            conditions.append(f"Wind {weather['wind_speed_mph']} mph {wind_dir}".strip())
        if "temp_f" in weather and weather["temp_f"] is not None:
            conditions.append(f"{weather['temp_f']}°F")
        if "precipitation" in weather and weather["precipitation"] not in ("none", "", None):
            conditions.append(weather["precipitation"])
        if conditions:
            parts.append(f"Conditions: {', '.join(conditions)}.")

    if refs:
        parts.append(f"Refs: {', '.join(refs)}.")

    return " ".join(parts)
