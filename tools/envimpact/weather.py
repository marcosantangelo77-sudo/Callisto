"""
Weather impact models: wind, temperature, altitude, humidity, precipitation,
and venue factor lookups.
"""

from typing import Optional

from tools.envimpact.venues import MLB_VENUES, NBA_VENUES, NFL_VENUES

# =============================================================================
# WIND IMPACT MODEL
# =============================================================================

def wind_impact(
    wind_speed_mph: float,
    wind_direction: Optional[str] = None,
    venue: Optional[str] = None,
    sport: str = "NFL",
) -> dict:
    """
    Calculate wind impact on game totals and spread.

    Args:
        wind_speed_mph: Sustained wind speed in mph.
        wind_direction: Direction string like "out_to_CF", "in_from_CF", "cross",
                        "N", "S", "E", "W" etc. Mostly matters for MLB.
        venue: Team abbreviation to look up venue specifics.
        sport: "NFL", "MLB", "MLS", etc.

    Returns:
        dict with total_adjustment, spread_adjustment, passing_adjustment, notes.
    """
    result = {
        "total_adjustment": 0.0,
        "spread_adjustment": 0.0,
        "passing_adjustment": 0.0,
        "notes": [],
    }

    sport = sport.upper()

    if sport == "NFL":
        # NFL wind model: passing efficiency drops significantly in high wind
        # Thresholds from historical analysis:
        # 10-14 mph: marginal impact (-0.5 to -1 total)
        # 15-19 mph: moderate passing suppression (-2 to -3 total)
        # 20-24 mph: significant (-3 to -4 total), deep ball nearly gone
        # 25+ mph: massive (-4 to -6 total), kicking game affected too

        if wind_speed_mph < 10:
            result["notes"].append("Minimal wind impact.")
        elif wind_speed_mph < 15:
            result["total_adjustment"] = -0.5 - (wind_speed_mph - 10) * 0.2
            result["passing_adjustment"] = -0.3 - (wind_speed_mph - 10) * 0.15
            result["notes"].append(f"Light wind ({wind_speed_mph} mph). Marginal passing reduction.")
        elif wind_speed_mph < 20:
            base = -1.5
            incremental = (wind_speed_mph - 15) * 0.4
            result["total_adjustment"] = base - incremental
            result["passing_adjustment"] = -1.0 - (wind_speed_mph - 15) * 0.3
            result["notes"].append(f"Moderate wind ({wind_speed_mph} mph). Deep ball compromised. "
                                   "Run-heavy game scripts expected.")
        elif wind_speed_mph < 25:
            base = -3.5
            incremental = (wind_speed_mph - 20) * 0.3
            result["total_adjustment"] = base - incremental
            result["passing_adjustment"] = -2.5 - (wind_speed_mph - 20) * 0.3
            result["notes"].append(f"Strong wind ({wind_speed_mph} mph). Passing game severely limited. "
                                   "FG attempts beyond 45 yards become risky.")
        else:
            # 25+ mph: extreme
            base = -5.0
            incremental = min((wind_speed_mph - 25) * 0.2, 2.0)  # Cap at -7 total
            result["total_adjustment"] = base - incremental
            result["passing_adjustment"] = -4.0 - min((wind_speed_mph - 25) * 0.2, 1.5)
            result["notes"].append(f"Extreme wind ({wind_speed_mph} mph). Game becomes almost entirely "
                                   "run-based. Long FGs nearly impossible. Punting is chaotic.")

        # Venue-specific wind exposure amplifier
        if venue and venue in NFL_VENUES:
            v = NFL_VENUES[venue]
            if v["dome"]:
                result["total_adjustment"] = 0.0
                result["passing_adjustment"] = 0.0
                result["spread_adjustment"] = 0.0
                result["notes"] = ["Dome venue — wind has no effect."]
                return result
            # Wind exposure factor: 0-10 scale, normalized to 0.7-1.3 multiplier
            exposure_mult = 0.7 + (v["wind_exposure"] / 10.0) * 0.6
            result["total_adjustment"] *= exposure_mult
            result["passing_adjustment"] *= exposure_mult
            result["notes"].append(f"Venue wind exposure: {v['wind_exposure']}/10 "
                                   f"(multiplier: {exposure_mult:.2f}).")

        # Wind affects spread slightly: favors team with stronger run game
        # but we can't know that here, so spread adjustment is minimal
        result["spread_adjustment"] = 0.0  # Wind is mostly a totals factor

    elif sport == "MLB":
        # MLB wind model: direction matters enormously
        # "out" = blowing out to field, "in" = blowing in from field, "cross" = crosswind

        direction = (wind_direction or "").lower()

        # Check for dome venue first
        if venue and venue in MLB_VENUES and MLB_VENUES[venue]["dome"]:
            result["notes"] = ["Dome venue — wind has no effect."]
            return result

        is_wrigley = venue == "CHC"

        if "out" in direction:
            # Wind blowing out: ball carries, more HRs, more runs
            if wind_speed_mph < 10:
                adj = 0.3
            elif wind_speed_mph < 15:
                adj = 0.5 + (wind_speed_mph - 10) * 0.15
            elif wind_speed_mph < 20:
                adj = 1.25 + (wind_speed_mph - 15) * 0.25
            else:
                adj = 2.5 + min((wind_speed_mph - 20) * 0.2, 1.5)

            # Wrigley wind-out is legendary — amplify
            if is_wrigley:
                adj *= 1.4
                result["notes"].append(f"WRIGLEY WIND OUT ({wind_speed_mph} mph). "
                                       "Historic run-scoring conditions. Over is heavily favored.")
            else:
                result["notes"].append(f"Wind blowing out ({wind_speed_mph} mph). "
                                       "Ball carries. Over lean.")

            result["total_adjustment"] = adj

        elif "in" in direction:
            # Wind blowing in: suppresses fly balls, fewer HRs
            if wind_speed_mph < 10:
                adj = -0.2
            elif wind_speed_mph < 15:
                adj = -0.4 - (wind_speed_mph - 10) * 0.1
            elif wind_speed_mph < 20:
                adj = -0.9 - (wind_speed_mph - 15) * 0.2
            else:
                adj = -1.9 - min((wind_speed_mph - 20) * 0.15, 1.0)

            if is_wrigley:
                adj *= 1.3
                result["notes"].append(f"Wrigley wind in ({wind_speed_mph} mph). "
                                       "Fly balls die. Pitcher's conditions.")
            else:
                result["notes"].append(f"Wind blowing in ({wind_speed_mph} mph). "
                                       "Suppresses power. Under lean.")

            result["total_adjustment"] = adj

        elif "cross" in direction:
            # Crosswind: moderate effect, mostly on control
            if wind_speed_mph < 15:
                adj = 0.1
            else:
                adj = 0.2 + (wind_speed_mph - 15) * 0.05
            result["total_adjustment"] = adj
            result["notes"].append(f"Crosswind ({wind_speed_mph} mph). "
                                   "Modest effect. May affect pitcher control.")

        else:
            # Unknown direction: use generic model
            if wind_speed_mph < 10:
                result["notes"].append("Light wind, direction unknown. Minimal impact.")
            elif wind_speed_mph < 20:
                result["total_adjustment"] = 0.2
                result["notes"].append(f"Moderate wind ({wind_speed_mph} mph), direction unknown. "
                                       "Slight unpredictability factor.")
            else:
                result["total_adjustment"] = 0.4
                result["notes"].append(f"Strong wind ({wind_speed_mph} mph), direction unknown. "
                                       "Chaotic conditions likely.")

    else:
        # Other outdoor sports: generic wind model
        if wind_speed_mph >= 20:
            result["total_adjustment"] = -1.0
            result["notes"].append(f"Strong wind ({wind_speed_mph} mph). May affect play.")
        elif wind_speed_mph >= 15:
            result["total_adjustment"] = -0.5
            result["notes"].append(f"Moderate wind ({wind_speed_mph} mph).")

    # Round everything
    result["total_adjustment"] = round(result["total_adjustment"], 2)
    result["spread_adjustment"] = round(result["spread_adjustment"], 2)
    result["passing_adjustment"] = round(result["passing_adjustment"], 2)

    return result


# =============================================================================
# TEMPERATURE IMPACT MODEL
# =============================================================================

def temperature_impact(temp_f: float, sport: str = "NFL") -> dict:
    """
    Calculate temperature impact on game totals.

    Args:
        temp_f: Temperature in Fahrenheit.
        sport: Sport code.

    Returns:
        dict with total_adjustment and notes.
    """
    result = {"total_adjustment": 0.0, "notes": []}
    sport = sport.upper()

    if sport == "NFL":
        # Cold weather impact on NFL:
        # 40-50°F: negligible
        # 30-40°F: slight grip issues, -0.5 to -1 total
        # 20-30°F: fumble risk rises, passing efficiency drops, -1 to -2 total
        # 10-20°F: significant impact, -2 to -3 total
        # Below 10°F: extreme cold, -3 to -4 total
        # Above 85°F: heat/humidity can cause fatigue, slight total suppression

        if temp_f < 10:
            result["total_adjustment"] = -3.5 - min((10 - temp_f) * 0.1, 1.0)
            result["notes"].append(f"Extreme cold ({temp_f}°F). Ball is rock hard. "
                                   "Grip is terrible. Fumble rate spikes. Kicking accuracy drops.")
        elif temp_f < 20:
            result["total_adjustment"] = -2.0 - (20 - temp_f) * 0.15
            result["notes"].append(f"Severe cold ({temp_f}°F). Significant grip issues. "
                                   "Passing and kicking compromised. Fumble risk elevated.")
        elif temp_f < 30:
            result["total_adjustment"] = -1.0 - (30 - temp_f) * 0.1
            result["notes"].append(f"Cold ({temp_f}°F). Grip affected. Slight fumble "
                                   "risk increase. Passing efficiency reduced.")
        elif temp_f < 40:
            result["total_adjustment"] = -0.3 - (40 - temp_f) * 0.07
            result["notes"].append(f"Cool ({temp_f}°F). Marginal cold effect.")
        elif temp_f > 90:
            result["total_adjustment"] = -0.5 - min((temp_f - 90) * 0.05, 1.0)
            result["notes"].append(f"Extreme heat ({temp_f}°F). Fatigue factor for "
                                   "both teams. Slight suppression.")
        elif temp_f > 85:
            result["total_adjustment"] = -0.3
            result["notes"].append(f"Hot ({temp_f}°F). Minor fatigue factor.")
        else:
            result["notes"].append(f"Comfortable temperature ({temp_f}°F). No impact.")

    elif sport == "MLB":
        # MLB: cold reduces ball elasticity, warm helps carry
        if temp_f < 50:
            result["total_adjustment"] = -0.4 - (50 - temp_f) * 0.02
            result["notes"].append(f"Cold ({temp_f}°F). Ball doesn't carry as well. "
                                   "Slight under lean.")
        elif temp_f > 85:
            result["total_adjustment"] = 0.2 + min((temp_f - 85) * 0.02, 0.3)
            result["notes"].append(f"Hot ({temp_f}°F). Thinner air, ball carries. "
                                   "Slight over lean.")
        else:
            result["notes"].append(f"Normal temperature ({temp_f}°F). No significant impact.")

    else:
        # Generic
        if temp_f < 30 or temp_f > 95:
            result["total_adjustment"] = -0.5
            result["notes"].append(f"Extreme temperature ({temp_f}°F). May affect performance.")
        else:
            result["notes"].append(f"Normal temperature ({temp_f}°F). No impact.")

    result["total_adjustment"] = round(result["total_adjustment"], 2)
    return result



# =============================================================================
# ALTITUDE IMPACT MODEL
# =============================================================================

def altitude_impact(venue: str, sport: str) -> dict:
    """
    Calculate altitude impact for a venue.

    Altitude thins the air, reducing drag on balls and affecting player stamina.
    Significant above 3000 ft, massive above 5000 ft.

    Args:
        venue: Team abbreviation.
        sport: Sport code.

    Returns:
        dict with total_adjustment, notes, and altitude_ft.
    """
    result = {"total_adjustment": 0.0, "notes": [], "altitude_ft": 0}
    sport = sport.upper()

    # Look up altitude
    altitude_ft = 0
    if sport == "NFL" and venue in NFL_VENUES:
        altitude_ft = NFL_VENUES[venue]["altitude_ft"]
    elif sport == "NBA" and venue in NBA_VENUES:
        altitude_ft = NBA_VENUES[venue]["altitude_ft"]
    elif sport == "MLB" and venue in MLB_VENUES:
        altitude_ft = MLB_VENUES[venue]["altitude_ft"]
    else:
        result["notes"].append(f"Venue '{venue}' not found for {sport}.")
        return result

    result["altitude_ft"] = altitude_ft

    if sport == "NBA":
        # Denver is the big one: visiting teams fatigue, pace is faster,
        # totals run 3-4 points higher. Utah is second at 4226 ft.
        if altitude_ft >= 5000:
            result["total_adjustment"] = 3.5
            result["notes"].append(f"Mile-high altitude ({altitude_ft} ft). Visiting teams fatigue. "
                                   "Pace is faster. Totals historically run 3-4 points higher. "
                                   "Home team has significant conditioning advantage.")
        elif altitude_ft >= 4000:
            result["total_adjustment"] = 2.0
            result["notes"].append(f"High altitude ({altitude_ft} ft). Noticeable stamina impact "
                                   "on visitors. Totals run ~2 points higher.")
        elif altitude_ft >= 2000:
            result["total_adjustment"] = 0.5
            result["notes"].append(f"Moderate altitude ({altitude_ft} ft). Slight effect.")
        else:
            result["notes"].append(f"Low altitude ({altitude_ft} ft). No altitude impact.")

    elif sport == "NFL":
        # Denver: thinner air helps kicking range (+3-5 yards on FG),
        # slightly higher totals, visiting team fatigue in 4th quarter
        if altitude_ft >= 5000:
            result["total_adjustment"] = 1.5
            result["notes"].append(f"Mile-high altitude ({altitude_ft} ft). FG range extended ~5 yards. "
                                   "Ball carries further on deep throws. Visiting teams may fade late. "
                                   "Punts carry further.")
        elif altitude_ft >= 3000:
            result["total_adjustment"] = 0.5
            result["notes"].append(f"Elevated altitude ({altitude_ft} ft). Marginal kicking benefit.")
        else:
            result["notes"].append(f"Low altitude ({altitude_ft} ft). No altitude impact.")

    elif sport == "MLB":
        # Coors Field is the crown jewel: ~5200 ft altitude
        # Ball travels ~9% further. Sliders don't break as much.
        # Humidor mitigates but doesn't eliminate.
        venue_data = MLB_VENUES.get(venue, {})
        park_factor = venue_data.get("park_factor", 1.0)

        if altitude_ft >= 5000:
            # Coors: park factor already captures most of this, but altitude
            # is the root cause. We report the park_factor-derived adjustment.
            # Neutral park = ~8.5 runs/game average. Coors factor of 1.38
            # means 8.5 * 0.38 = ~3.2 extra runs.
            pf_adj = (park_factor - 1.0) * 8.5
            result["total_adjustment"] = round(pf_adj, 1)
            result["notes"].append(f"Extreme altitude ({altitude_ft} ft). Coors Field effect. "
                                   f"Park factor {park_factor}. Ball travels ~9% further. "
                                   "Breaking balls don't break. Humidor helps but doesn't fix it.")
        elif altitude_ft >= 1000:
            pf_adj = (park_factor - 1.0) * 8.5
            result["total_adjustment"] = round(pf_adj, 1)
            result["notes"].append(f"Elevated altitude ({altitude_ft} ft). Park factor {park_factor}.")
        else:
            pf_adj = (park_factor - 1.0) * 8.5
            result["total_adjustment"] = round(pf_adj, 1)
            if park_factor != 1.0:
                result["notes"].append(f"Sea-level altitude ({altitude_ft} ft). "
                                       f"Park factor {park_factor} (non-altitude factors).")
            else:
                result["notes"].append(f"Sea-level altitude ({altitude_ft} ft). Neutral.")

    result["total_adjustment"] = round(result["total_adjustment"], 2)
    return result



# =============================================================================
# HUMIDITY IMPACT MODEL
# =============================================================================

def humidity_impact(humidity_pct: float, temp_f: float, sport: str = "NFL") -> float:
    """
    Calculate humidity impact on game total.

    High humidity + heat creates heavy air that suppresses ball flight (MLB)
    and causes player fatigue (all sports). Very low humidity at altitude
    has the opposite effect.

    Contrary to popular belief, humid air is actually LESS dense than dry air
    (water vapor is lighter than N2/O2), so humid conditions slightly help
    ball carry. However, the fatigue effect usually dominates.

    Args:
        humidity_pct: Relative humidity as percentage (0-100).
        temp_f: Temperature in Fahrenheit.
        sport: Sport code.

    Returns:
        Total adjustment as float (points).
    """
    sport = sport.upper()
    adj = 0.0

    # Heat index effect: high humidity + high temp = more fatigue
    if temp_f > 80 and humidity_pct > 70:
        # Compute a rough heat stress factor
        heat_stress = ((temp_f - 80) / 20) * ((humidity_pct - 70) / 30)
        heat_stress = min(heat_stress, 1.0)

        if sport == "NFL":
            # NFL: fatigue suppresses late-game scoring
            adj = -0.5 * heat_stress
        elif sport == "MLB":
            # MLB: humid air is slightly less dense (ball carries marginally more)
            # but pitcher fatigue is a factor
            adj = 0.1 * heat_stress  # Net slight over from carry
        else:
            adj = -0.3 * heat_stress

    elif humidity_pct < 20 and temp_f > 60:
        # Very dry air: ball carries well (relevant for baseball)
        if sport == "MLB":
            adj = 0.15
        # Dry conditions don't significantly affect other sports

    return round(adj, 2)


# =============================================================================
# VENUE FACTORS LOOKUP
# =============================================================================

def get_venue_factors(team: str, sport: str) -> dict:
    """
    Get venue characteristics for a team.

    Args:
        team: Team abbreviation (e.g. "DEN", "CHC", "GB").
        sport: Sport code ("NFL", "NBA", "MLB").

    Returns:
        dict with altitude_ft, dome, surface, park_factor, and venue name.
    """
    sport = sport.upper()

    if sport == "NFL" and team in NFL_VENUES:
        v = NFL_VENUES[team]
        return {
            "team": team,
            "sport": sport,
            "venue_name": v["name"],
            "city": v["city"],
            "altitude_ft": v["altitude_ft"],
            "dome": v["dome"],
            "surface": v["surface"],
            "wind_exposure": v["wind_exposure"],
            "park_factor": None,
        }

    elif sport == "NBA" and team in NBA_VENUES:
        v = NBA_VENUES[team]
        return {
            "team": team,
            "sport": sport,
            "venue_name": v["name"],
            "city": v["city"],
            "altitude_ft": v["altitude_ft"],
            "dome": True,  # All NBA arenas are indoor
            "surface": "hardwood",
            "wind_exposure": 0,
            "park_factor": None,
        }

    elif sport == "MLB" and team in MLB_VENUES:
        v = MLB_VENUES[team]
        return {
            "team": team,
            "sport": sport,
            "venue_name": v["name"],
            "city": v["city"],
            "altitude_ft": v["altitude_ft"],
            "dome": v["dome"],
            "surface": "grass",
            "wind_exposure": v["wind_exposure"],
            "park_factor": v["park_factor"],
        }

    return {
        "team": team,
        "sport": sport,
        "venue_name": "Unknown",
        "city": "Unknown",
        "altitude_ft": 0,
        "dome": False,
        "surface": "unknown",
        "wind_exposure": 5,
        "park_factor": None,
        "error": f"Venue not found for {team} in {sport}",
    }


# =============================================================================
# WEATHER INTEGRATION (COMBINED WEATHER ADJUSTMENT)
# =============================================================================

def get_weather_adjustment(
    venue: str,
    sport: str,
    weather_data: Optional[dict] = None,
) -> dict:
    """
    Compute total weather-based adjustment for a game.

    If weather_data is None, returns only venue-based factors (altitude, dome).
    If weather_data is provided, it should contain any/all of:
        - wind_speed_mph: float
        - wind_direction: str (e.g. "out_to_CF", "in_from_CF", "cross", "N")
        - temp_f: float
        - humidity_pct: float
        - precipitation: str ("none", "rain", "snow", "drizzle")

    Args:
        venue: Team abbreviation.
        sport: Sport code.
        weather_data: Optional weather conditions dict.

    Returns:
        dict with total_adj, spread_adj, and factors breakdown.
    """
    sport = sport.upper()
    factors = []
    total_adj = 0.0
    spread_adj = 0.0

    # Always include altitude
    alt = altitude_impact(venue, sport)
    if alt["total_adjustment"] != 0:
        total_adj += alt["total_adjustment"]
        factors.append({
            "factor": "altitude",
            "adjustment": alt["total_adjustment"],
            "notes": alt["notes"],
        })

    # Check if venue is a dome — short-circuit weather effects
    venue_info = get_venue_factors(venue, sport)
    is_dome = venue_info.get("dome", False)

    if is_dome:
        factors.append({
            "factor": "dome",
            "adjustment": 0.0,
            "notes": ["Dome venue. Weather factors (wind, temp, humidity, precip) do not apply."],
        })
        return {
            "total_adj": round(total_adj, 2),
            "spread_adj": round(spread_adj, 2),
            "factors": factors,
            "venue": venue_info,
        }

    # If no weather data, return venue-only factors
    if weather_data is None:
        factors.append({
            "factor": "weather",
            "adjustment": 0.0,
            "notes": ["No weather data provided. Only venue-based factors applied."],
        })
        return {
            "total_adj": round(total_adj, 2),
            "spread_adj": round(spread_adj, 2),
            "factors": factors,
            "venue": venue_info,
        }

    # Wind
    ws = weather_data.get("wind_speed_mph")
    if ws is not None and ws > 0:
        wd = weather_data.get("wind_direction")
        w = wind_impact(ws, wd, venue, sport)
        total_adj += w["total_adjustment"]
        spread_adj += w["spread_adjustment"]
        factors.append({
            "factor": "wind",
            "adjustment": w["total_adjustment"],
            "passing_adjustment": w["passing_adjustment"],
            "notes": w["notes"],
        })

    # Temperature
    temp = weather_data.get("temp_f")
    if temp is not None:
        t = temperature_impact(temp, sport)
        total_adj += t["total_adjustment"]
        factors.append({
            "factor": "temperature",
            "adjustment": t["total_adjustment"],
            "notes": t["notes"],
        })

    # Humidity
    hum = weather_data.get("humidity_pct")
    if hum is not None and temp is not None:
        h = humidity_impact(hum, temp, sport)
        if h != 0:
            total_adj += h
            factors.append({
                "factor": "humidity",
                "adjustment": h,
                "notes": [f"Humidity {hum}% at {temp}°F."],
            })

    # Precipitation
    precip = weather_data.get("precipitation", "none").lower()
    if precip not in ("none", "", "clear"):
        if sport == "NFL":
            if precip in ("rain", "drizzle"):
                precip_adj = -1.5
                total_adj += precip_adj
                factors.append({
                    "factor": "precipitation",
                    "adjustment": precip_adj,
                    "notes": [f"Rain/drizzle. Wet ball = more fumbles, fewer deep passes. "
                              "Run game emphasized. Totals suppressed."],
                })
            elif precip == "snow":
                precip_adj = -2.5
                total_adj += precip_adj
                factors.append({
                    "factor": "precipitation",
                    "adjustment": precip_adj,
                    "notes": ["Snow. Significant footing and visibility issues. "
                              "Heavy run game expected. Totals heavily suppressed."],
                })
        elif sport == "MLB":
            # Rain in MLB usually means delay or cancellation, not play-through.
            # But drizzle or light rain can affect play if game continues.
            if precip in ("drizzle",):
                precip_adj = 0.2
                total_adj += precip_adj
                factors.append({
                    "factor": "precipitation",
                    "adjustment": precip_adj,
                    "notes": ["Light drizzle. Slightly slick ball may affect pitcher grip. "
                              "Marginal over lean from control issues."],
                })

    return {
        "total_adj": round(total_adj, 2),
        "spread_adj": round(spread_adj, 2),
        "factors": factors,
        "venue": venue_info,
    }
