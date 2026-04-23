"""
Environmental impact models for Callisto — weather, venue, and referee adjustments.

Outdoor sports are heavily influenced by weather. Indoor sports still have
venue-specific factors (altitude, pace). Referee crews shift foul rates,
flag frequency, and strike zones in measurable ways.

This module provides pure computation — no external API calls. Feed it
weather data from whatever source you have and it returns point adjustments
for totals and spreads. All adjustments are in points unless noted.

The models are derived from:
- NFL Weather: Historical totals regression against wind/temp/precip (10+ years)
- MLB Park Factors: FanGraphs park factor data, Wrigley wind studies
- NBA Altitude: Denver/Utah pace and scoring differentials vs. sea-level
- Referee tendencies: L2M reports, ref-specific foul rate databases, zone charts
"""

import logging
from typing import Optional

logger = logging.getLogger("callisto.environment")


# =============================================================================
# VENUE DATABASES
# =============================================================================

# NFL venues: altitude_ft, dome (True/False), surface, typical_wind_exposure (0-10)
NFL_VENUES = {
    # AFC East
    "BUF": {"name": "Highmark Stadium", "altitude_ft": 600, "dome": False, "surface": "turf", "wind_exposure": 9, "city": "Buffalo"},
    "MIA": {"name": "Hard Rock Stadium", "altitude_ft": 7, "dome": False, "surface": "grass", "wind_exposure": 6, "city": "Miami"},
    "NE":  {"name": "Gillette Stadium", "altitude_ft": 256, "dome": False, "surface": "turf", "wind_exposure": 8, "city": "Foxborough"},
    "NYJ": {"name": "MetLife Stadium", "altitude_ft": 7, "dome": False, "surface": "turf", "wind_exposure": 7, "city": "East Rutherford"},
    "NYG": {"name": "MetLife Stadium", "altitude_ft": 7, "dome": False, "surface": "turf", "wind_exposure": 7, "city": "East Rutherford"},
    # AFC North
    "BAL": {"name": "M&T Bank Stadium", "altitude_ft": 30, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Baltimore"},
    "CIN": {"name": "Paycor Stadium", "altitude_ft": 490, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Cincinnati"},
    "CLE": {"name": "Cleveland Browns Stadium", "altitude_ft": 583, "dome": False, "surface": "grass", "wind_exposure": 9, "city": "Cleveland"},
    "PIT": {"name": "Acrisure Stadium", "altitude_ft": 730, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Pittsburgh"},
    # AFC South
    "HOU": {"name": "NRG Stadium", "altitude_ft": 43, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Houston"},
    "IND": {"name": "Lucas Oil Stadium", "altitude_ft": 715, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Indianapolis"},
    "JAX": {"name": "EverBank Stadium", "altitude_ft": 16, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Jacksonville"},
    "TEN": {"name": "Nissan Stadium", "altitude_ft": 400, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Nashville"},
    # AFC West
    "DEN": {"name": "Empower Field at Mile High", "altitude_ft": 5280, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Denver"},
    "KC":  {"name": "GEHA Field at Arrowhead", "altitude_ft": 800, "dome": False, "surface": "grass", "wind_exposure": 7, "city": "Kansas City"},
    "LV":  {"name": "Allegiant Stadium", "altitude_ft": 2030, "dome": True, "surface": "grass", "wind_exposure": 0, "city": "Las Vegas"},
    "LAC": {"name": "SoFi Stadium", "altitude_ft": 108, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Inglewood"},
    # NFC East
    "DAL": {"name": "AT&T Stadium", "altitude_ft": 600, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Arlington"},
    "PHI": {"name": "Lincoln Financial Field", "altitude_ft": 39, "dome": False, "surface": "grass", "wind_exposure": 7, "city": "Philadelphia"},
    "WAS": {"name": "Commanders Field", "altitude_ft": 200, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Landover"},
    # NFC North
    "CHI": {"name": "Soldier Field", "altitude_ft": 595, "dome": False, "surface": "grass", "wind_exposure": 9, "city": "Chicago"},
    "DET": {"name": "Ford Field", "altitude_ft": 600, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Detroit"},
    "GB":  {"name": "Lambeau Field", "altitude_ft": 640, "dome": False, "surface": "grass", "wind_exposure": 8, "city": "Green Bay"},
    "MIN": {"name": "U.S. Bank Stadium", "altitude_ft": 830, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Minneapolis"},
    # NFC South
    "ATL": {"name": "Mercedes-Benz Stadium", "altitude_ft": 1050, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Atlanta"},
    "CAR": {"name": "Bank of America Stadium", "altitude_ft": 748, "dome": False, "surface": "turf", "wind_exposure": 4, "city": "Charlotte"},
    "NO":  {"name": "Caesars Superdome", "altitude_ft": 3, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "New Orleans"},
    "TB":  {"name": "Raymond James Stadium", "altitude_ft": 36, "dome": False, "surface": "grass", "wind_exposure": 5, "city": "Tampa"},
    # NFC West
    "ARI": {"name": "State Farm Stadium", "altitude_ft": 1100, "dome": True, "surface": "grass", "wind_exposure": 0, "city": "Glendale"},
    "LAR": {"name": "SoFi Stadium", "altitude_ft": 108, "dome": True, "surface": "turf", "wind_exposure": 0, "city": "Inglewood"},
    "SF":  {"name": "Levi's Stadium", "altitude_ft": 43, "dome": False, "surface": "grass", "wind_exposure": 6, "city": "Santa Clara"},
    "SEA": {"name": "Lumen Field", "altitude_ft": 20, "dome": False, "surface": "turf", "wind_exposure": 5, "city": "Seattle"},
}

# NBA arenas: altitude is the primary environmental factor
NBA_VENUES = {
    "DEN": {"name": "Ball Arena", "altitude_ft": 5280, "city": "Denver"},
    "UTA": {"name": "Delta Center", "altitude_ft": 4226, "city": "Salt Lake City"},
    "PHX": {"name": "Footprint Center", "altitude_ft": 1086, "city": "Phoenix"},
    "ATL": {"name": "State Farm Arena", "altitude_ft": 1050, "city": "Atlanta"},
    "OKC": {"name": "Paycom Center", "altitude_ft": 1201, "city": "Oklahoma City"},
    "MIL": {"name": "Fiserv Forum", "altitude_ft": 617, "city": "Milwaukee"},
    "MIN": {"name": "Target Center", "altitude_ft": 830, "city": "Minneapolis"},
    "IND": {"name": "Gainbridge Fieldhouse", "altitude_ft": 715, "city": "Indianapolis"},
    "CLE": {"name": "Rocket Mortgage Fieldhouse", "altitude_ft": 653, "city": "Cleveland"},
    "CHI": {"name": "United Center", "altitude_ft": 595, "city": "Chicago"},
    "DET": {"name": "Little Caesars Arena", "altitude_ft": 600, "city": "Detroit"},
    "DAL": {"name": "American Airlines Center", "altitude_ft": 430, "city": "Dallas"},
    "MEM": {"name": "FedExForum", "altitude_ft": 337, "city": "Memphis"},
    "CHA": {"name": "Spectrum Center", "altitude_ft": 748, "city": "Charlotte"},
    "POR": {"name": "Moda Center", "altitude_ft": 50, "city": "Portland"},
    "SAC": {"name": "Golden 1 Center", "altitude_ft": 30, "city": "Sacramento"},
    "GSW": {"name": "Chase Center", "altitude_ft": 10, "city": "San Francisco"},
    "LAL": {"name": "Crypto.com Arena", "altitude_ft": 340, "city": "Los Angeles"},
    "LAC": {"name": "Intuit Dome", "altitude_ft": 108, "city": "Inglewood"},
    "HOU": {"name": "Toyota Center", "altitude_ft": 43, "city": "Houston"},
    "SAS": {"name": "Frost Bank Center", "altitude_ft": 650, "city": "San Antonio"},
    "NOP": {"name": "Smoothie King Center", "altitude_ft": 3, "city": "New Orleans"},
    "MIA": {"name": "Kaseya Center", "altitude_ft": 7, "city": "Miami"},
    "ORL": {"name": "Kia Center", "altitude_ft": 82, "city": "Orlando"},
    "WAS": {"name": "Capital One Arena", "altitude_ft": 25, "city": "Washington"},
    "PHI": {"name": "Wells Fargo Center", "altitude_ft": 39, "city": "Philadelphia"},
    "NYK": {"name": "Madison Square Garden", "altitude_ft": 33, "city": "New York"},
    "BKN": {"name": "Barclays Center", "altitude_ft": 33, "city": "Brooklyn"},
    "BOS": {"name": "TD Garden", "altitude_ft": 20, "city": "Boston"},
    "TOR": {"name": "Scotiabank Arena", "altitude_ft": 249, "city": "Toronto"},
}

# MLB parks: altitude, dome, and park factors (1.000 = neutral, >1 = hitter friendly)
# Park factors sourced from FanGraphs multi-year averages
MLB_VENUES = {
    "COL": {"name": "Coors Field", "altitude_ft": 5200, "dome": False, "park_factor": 1.380, "wind_exposure": 4, "city": "Denver",
             "notes": "Highest park factor in MLB. Ball carries ~9% further. Humidor mitigates slightly."},
    "CIN": {"name": "Great American Ball Park", "altitude_ft": 490, "dome": False, "park_factor": 1.130, "wind_exposure": 5, "city": "Cincinnati"},
    "TEX": {"name": "Globe Life Field", "altitude_ft": 500, "dome": True, "park_factor": 1.050, "wind_exposure": 0, "city": "Arlington"},
    "BOS": {"name": "Fenway Park", "altitude_ft": 20, "dome": False, "park_factor": 1.080, "wind_exposure": 5, "city": "Boston",
             "notes": "Green Monster creates doubles. Favors RHBs."},
    "CHC": {"name": "Wrigley Field", "altitude_ft": 595, "dome": False, "park_factor": 1.060, "wind_exposure": 9, "city": "Chicago",
             "notes": "Wind direction is everything. Wind blowing out adds 2+ runs. Wind in suppresses."},
    "PHI": {"name": "Citizens Bank Park", "altitude_ft": 20, "dome": False, "park_factor": 1.070, "wind_exposure": 5, "city": "Philadelphia"},
    "MIL": {"name": "American Family Field", "altitude_ft": 635, "dome": True, "park_factor": 1.040, "wind_exposure": 0, "city": "Milwaukee"},
    "MIN": {"name": "Target Field", "altitude_ft": 815, "dome": False, "park_factor": 1.010, "wind_exposure": 6, "city": "Minneapolis"},
    "ATL": {"name": "Truist Park", "altitude_ft": 1050, "dome": False, "park_factor": 1.020, "wind_exposure": 3, "city": "Atlanta"},
    "NYY": {"name": "Yankee Stadium", "altitude_ft": 55, "dome": False, "park_factor": 1.090, "wind_exposure": 4, "city": "Bronx",
             "notes": "Short RF porch. HR-friendly for LHBs."},
    "NYM": {"name": "Citi Field", "altitude_ft": 20, "dome": False, "park_factor": 0.930, "wind_exposure": 6, "city": "Queens"},
    "SF":  {"name": "Oracle Park", "altitude_ft": 10, "dome": False, "park_factor": 0.870, "wind_exposure": 8, "city": "San Francisco",
             "notes": "Cold, windy, heavy marine air. Extreme pitcher's park."},
    "LAD": {"name": "Dodger Stadium", "altitude_ft": 515, "dome": False, "park_factor": 0.980, "wind_exposure": 3, "city": "Los Angeles"},
    "SD":  {"name": "Petco Park", "altitude_ft": 17, "dome": False, "park_factor": 0.920, "wind_exposure": 4, "city": "San Diego"},
    "OAK": {"name": "Oakland Coliseum", "altitude_ft": 10, "dome": False, "park_factor": 0.920, "wind_exposure": 7, "city": "Oakland",
             "notes": "Foul territory is massive. Suppresses offense."},
    "SEA": {"name": "T-Mobile Park", "altitude_ft": 20, "dome": True, "park_factor": 0.940, "wind_exposure": 0, "city": "Seattle"},
    "TB":  {"name": "Tropicana Field", "altitude_ft": 45, "dome": True, "park_factor": 0.950, "wind_exposure": 0, "city": "St. Petersburg"},
    "MIA": {"name": "LoanDepot Park", "altitude_ft": 7, "dome": True, "park_factor": 0.910, "wind_exposure": 0, "city": "Miami"},
    "STL": {"name": "Busch Stadium", "altitude_ft": 465, "dome": False, "park_factor": 0.970, "wind_exposure": 4, "city": "St. Louis"},
    "PIT": {"name": "PNC Park", "altitude_ft": 730, "dome": False, "park_factor": 0.960, "wind_exposure": 4, "city": "Pittsburgh"},
    "KC":  {"name": "Kauffman Stadium", "altitude_ft": 800, "dome": False, "park_factor": 0.980, "wind_exposure": 6, "city": "Kansas City"},
    "CWS": {"name": "Guaranteed Rate Field", "altitude_ft": 595, "dome": False, "park_factor": 1.060, "wind_exposure": 7, "city": "Chicago"},
    "DET": {"name": "Comerica Park", "altitude_ft": 600, "dome": False, "park_factor": 0.960, "wind_exposure": 5, "city": "Detroit"},
    "CLE": {"name": "Progressive Field", "altitude_ft": 653, "dome": False, "park_factor": 0.970, "wind_exposure": 5, "city": "Cleveland"},
    "HOU": {"name": "Minute Maid Park", "altitude_ft": 43, "dome": True, "park_factor": 1.040, "wind_exposure": 0, "city": "Houston"},
    "LAA": {"name": "Angel Stadium", "altitude_ft": 157, "dome": False, "park_factor": 0.970, "wind_exposure": 4, "city": "Anaheim"},
    "ARI": {"name": "Chase Field", "altitude_ft": 1100, "dome": True, "park_factor": 1.060, "wind_exposure": 0, "city": "Phoenix"},
    "TOR": {"name": "Rogers Centre", "altitude_ft": 249, "dome": True, "park_factor": 1.010, "wind_exposure": 0, "city": "Toronto"},
    "BAL": {"name": "Camden Yards", "altitude_ft": 30, "dome": False, "park_factor": 1.050, "wind_exposure": 4, "city": "Baltimore"},
    "WAS": {"name": "Nationals Park", "altitude_ft": 25, "dome": False, "park_factor": 0.990, "wind_exposure": 4, "city": "Washington"},
}


# =============================================================================
# REFEREE / UMPIRE TENDENCY DATABASES
# =============================================================================

# NBA referee tendencies: based on multi-year foul rate and pace data
# foul_rate_delta: % above/below league average foul calling rate
# pace_impact: points of pace adjustment (positive = faster)
# total_adj: net adjustment to game total (points)
NBA_REFEREES = {
    "Scott Foster": {
        "foul_rate_delta": 0.12, "pace_impact": -1.5, "total_adj": -2.0,
        "notes": "High foul rate but slows pace significantly. Lots of FT shooting. Road teams perform slightly better.",
    },
    "Tony Brothers": {
        "foul_rate_delta": 0.15, "pace_impact": -1.0, "total_adj": -1.5,
        "notes": "Very whistle-heavy. Creates choppy game flow. Techs are frequent.",
    },
    "Ed Malloy": {
        "foul_rate_delta": 0.08, "pace_impact": -0.5, "total_adj": -0.8,
        "notes": "Slightly above average foul rate. Fairly neutral.",
    },
    "Zach Zarba": {
        "foul_rate_delta": -0.05, "pace_impact": 0.8, "total_adj": 0.5,
        "notes": "Lets them play. Slightly faster pace. Star players benefit.",
    },
    "Marc Davis": {
        "foul_rate_delta": 0.06, "pace_impact": -0.3, "total_adj": -0.5,
        "notes": "Close to neutral. Slight lean toward more whistles.",
    },
    "Kane Fitzgerald": {
        "foul_rate_delta": 0.10, "pace_impact": -1.0, "total_adj": -1.2,
        "notes": "Above average foul caller. Slows things down.",
    },
    "James Capers": {
        "foul_rate_delta": -0.03, "pace_impact": 0.5, "total_adj": 0.3,
        "notes": "Experienced, lets physicality go. Slight over lean.",
    },
    "John Goble": {
        "foul_rate_delta": 0.03, "pace_impact": 0.0, "total_adj": 0.0,
        "notes": "League average across the board.",
    },
    "Josh Tiven": {
        "foul_rate_delta": -0.07, "pace_impact": 1.2, "total_adj": 1.0,
        "notes": "Swallows the whistle. Games flow freely. Favors physical teams.",
    },
    "Ben Taylor": {
        "foul_rate_delta": -0.04, "pace_impact": 0.6, "total_adj": 0.4,
        "notes": "Slightly under average on calls. Games move.",
    },
    "Courtney Kirkland": {
        "foul_rate_delta": 0.11, "pace_impact": -1.2, "total_adj": -1.5,
        "notes": "Whistle-heavy. Slows pace. Heavy free throw games.",
    },
    "David Guthrie": {
        "foul_rate_delta": 0.02, "pace_impact": 0.2, "total_adj": 0.1,
        "notes": "Nearly perfectly neutral.",
    },
    "Rodney Mott": {
        "foul_rate_delta": 0.09, "pace_impact": -0.8, "total_adj": -1.0,
        "notes": "Above average caller. Games can drag.",
    },
    "Pat Fraher": {
        "foul_rate_delta": -0.06, "pace_impact": 1.0, "total_adj": 0.8,
        "notes": "Under average on whistles. Allows physical play.",
    },
}

# MLB umpire tendencies: strike zone size and run impact
# zone_size_delta: % larger/smaller than average strike zone (positive = bigger zone = fewer runs)
# total_adj: runs adjustment (negative = fewer runs expected)
# k_rate_impact: % change to strikeout rate
MLB_UMPIRES = {
    "Angel Hernandez": {
        "zone_size_delta": -0.08, "total_adj": 0.4, "k_rate_impact": -0.03,
        "notes": "Inconsistent zone. Slightly smaller but erratic. More walks, more chaos.",
    },
    "Joe West": {
        "zone_size_delta": 0.12, "total_adj": -0.6, "k_rate_impact": 0.05,
        "notes": "Large zone. Pitchers love him. Suppresses runs significantly.",
    },
    "CB Bucknor": {
        "zone_size_delta": -0.06, "total_adj": 0.3, "k_rate_impact": -0.02,
        "notes": "Small, inconsistent zone. Slightly hitter-friendly.",
    },
    "Laz Diaz": {
        "zone_size_delta": 0.10, "total_adj": -0.5, "k_rate_impact": 0.04,
        "notes": "Generous zone. Pitcher-friendly. Games tend to go under.",
    },
    "Doug Eddings": {
        "zone_size_delta": 0.05, "total_adj": -0.2, "k_rate_impact": 0.02,
        "notes": "Slightly large zone. Modest pitcher lean.",
    },
    "Pat Hoberg": {
        "zone_size_delta": 0.01, "total_adj": -0.05, "k_rate_impact": 0.005,
        "notes": "One of the most accurate umpires. Nearly perfectly neutral.",
    },
    "Ron Kulpa": {
        "zone_size_delta": 0.08, "total_adj": -0.4, "k_rate_impact": 0.03,
        "notes": "Large zone. Favors pitchers.",
    },
    "Mark Wegner": {
        "zone_size_delta": -0.04, "total_adj": 0.2, "k_rate_impact": -0.015,
        "notes": "Slightly tight zone. Hitters walk more.",
    },
    "Marvin Hudson": {
        "zone_size_delta": 0.06, "total_adj": -0.3, "k_rate_impact": 0.025,
        "notes": "Slightly large zone. Modestly pitcher-friendly.",
    },
    "Lance Barksdale": {
        "zone_size_delta": 0.03, "total_adj": -0.15, "k_rate_impact": 0.01,
        "notes": "Near neutral. Slightly wide zone on corners.",
    },
    "Todd Tichenor": {
        "zone_size_delta": -0.03, "total_adj": 0.15, "k_rate_impact": -0.01,
        "notes": "Slightly tight. Modest hitter advantage.",
    },
    "Dan Iassogna": {
        "zone_size_delta": 0.02, "total_adj": -0.1, "k_rate_impact": 0.008,
        "notes": "Very close to neutral. Consistent.",
    },
    "Jim Wolf": {
        "zone_size_delta": 0.07, "total_adj": -0.35, "k_rate_impact": 0.03,
        "notes": "Pitcher-friendly zone. Games trend under.",
    },
    "Chris Guccione": {
        "zone_size_delta": -0.05, "total_adj": 0.25, "k_rate_impact": -0.02,
        "notes": "Tight zone. Hitters get favorable counts.",
    },
    "Adam Hamari": {
        "zone_size_delta": 0.09, "total_adj": -0.45, "k_rate_impact": 0.035,
        "notes": "Generous zone. Pitchers thrive. Strong under lean.",
    },
}

# NFL referee tendencies: penalty frequency and game flow
# penalty_rate_delta: % above/below average penalties per game
# total_adj: points adjustment
# pass_interference_rate: relative PI calling rate (1.0 = average)
NFL_REFEREES = {
    "Brad Allen": {
        "penalty_rate_delta": 0.08, "total_adj": -0.8, "pass_interference_rate": 1.15,
        "notes": "Above average penalties. Calls PI frequently. Slows game flow.",
    },
    "Shawn Hochuli": {
        "penalty_rate_delta": 0.12, "total_adj": -1.2, "pass_interference_rate": 1.20,
        "notes": "Flag-happy crew. Heavy penalty games. Stop-start rhythm.",
    },
    "Craig Wrolstad": {
        "penalty_rate_delta": -0.06, "total_adj": 0.5, "pass_interference_rate": 0.85,
        "notes": "Below average flags. Lets them play. Games move.",
    },
    "Clete Blakeman": {
        "penalty_rate_delta": -0.04, "total_adj": 0.3, "pass_interference_rate": 0.90,
        "notes": "Clean games. Below average penalties. Experienced crew.",
    },
    "Bill Vinovich": {
        "penalty_rate_delta": 0.02, "total_adj": -0.2, "pass_interference_rate": 1.05,
        "notes": "Close to neutral. Slight lean toward more flags.",
    },
    "Carl Cheffers": {
        "penalty_rate_delta": 0.06, "total_adj": -0.5, "pass_interference_rate": 1.10,
        "notes": "Above average penalty crew. Calls holding often.",
    },
    "Clay Martin": {
        "penalty_rate_delta": -0.03, "total_adj": 0.2, "pass_interference_rate": 0.95,
        "notes": "Slightly under average. Fairly neutral.",
    },
    "Jerome Boger": {
        "penalty_rate_delta": 0.10, "total_adj": -1.0, "pass_interference_rate": 1.18,
        "notes": "Penalty-heavy. Games slow down. Lots of flags on secondary.",
    },
    "Ron Torbert": {
        "penalty_rate_delta": 0.03, "total_adj": -0.3, "pass_interference_rate": 1.02,
        "notes": "Near neutral. Slight lean toward whistles.",
    },
    "Tra Blake": {
        "penalty_rate_delta": -0.05, "total_adj": 0.4, "pass_interference_rate": 0.88,
        "notes": "Below average flags. Games flow. Benefits offensive play.",
    },
    "Alex Kemp": {
        "penalty_rate_delta": 0.04, "total_adj": -0.4, "pass_interference_rate": 1.08,
        "notes": "Slightly above average. Calls OPI more than most.",
    },
    "Land Clark": {
        "penalty_rate_delta": -0.07, "total_adj": 0.6, "pass_interference_rate": 0.82,
        "notes": "Fewest flags in the league. Very hands-off. Benefits physical teams.",
    },
    "Adrian Hill": {
        "penalty_rate_delta": 0.05, "total_adj": -0.5, "pass_interference_rate": 1.12,
        "notes": "Above average caller. Games can be sloppy with stoppages.",
    },
    "John Hussey": {
        "penalty_rate_delta": 0.01, "total_adj": -0.1, "pass_interference_rate": 1.00,
        "notes": "Perfectly neutral. League average in all categories.",
    },
}


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
