"""
Static venue metadata lookup with fuzzy name matching.

Dimensions ESPN doesn't provide but critical for hypothesis testing:
altitude (feet), dome flag, timezone offset from ET, MLB park factors.
"""

from __future__ import annotations

import difflib


# ── Static venue metadata ──
# Dimensions that ESPN doesn't provide but are critical for hypothesis testing.
# Altitude in feet, timezone offset from ET.
VENUE_METADATA = {
    # NBA
    "Ball Arena": {"dome": True, "altitude_ft": 5280, "tz_offset": -2, "city": "Denver"},
    "Vivint Arena": {"dome": True, "altitude_ft": 4226, "tz_offset": -2, "city": "Salt Lake City"},
    "Footprint Center": {"dome": True, "altitude_ft": 1086, "tz_offset": -2, "city": "Phoenix"},
    "Chase Center": {"dome": True, "altitude_ft": 10, "tz_offset": -3, "city": "San Francisco"},
    "Crypto.com Arena": {"dome": True, "altitude_ft": 300, "tz_offset": -3, "city": "Los Angeles"},
    "Intuit Dome": {"dome": True, "altitude_ft": 100, "tz_offset": -3, "city": "Inglewood"},
    "Moda Center": {"dome": True, "altitude_ft": 50, "tz_offset": -3, "city": "Portland"},
    "Climate Pledge Arena": {"dome": True, "altitude_ft": 20, "tz_offset": -3, "city": "Seattle"},
    "Target Center": {"dome": True, "altitude_ft": 830, "tz_offset": -1, "city": "Minneapolis"},
    "United Center": {"dome": True, "altitude_ft": 594, "tz_offset": -1, "city": "Chicago"},
    "Madison Square Garden": {"dome": True, "altitude_ft": 33, "tz_offset": 0, "city": "New York"},
    "TD Garden": {"dome": True, "altitude_ft": 20, "tz_offset": 0, "city": "Boston"},
    # NFL outdoor stadiums
    "Empower Field at Mile High": {"dome": False, "altitude_ft": 5280, "tz_offset": -2, "city": "Denver"},
    "Highmark Stadium": {"dome": False, "altitude_ft": 600, "tz_offset": 0, "city": "Buffalo"},
    "Lambeau Field": {"dome": False, "altitude_ft": 640, "tz_offset": -1, "city": "Green Bay"},
    "Soldier Field": {"dome": False, "altitude_ft": 594, "tz_offset": -1, "city": "Chicago"},
    "Arrowhead Stadium": {"dome": False, "altitude_ft": 800, "tz_offset": -1, "city": "Kansas City"},
    "MetLife Stadium": {"dome": False, "altitude_ft": 10, "tz_offset": 0, "city": "East Rutherford"},
    "SoFi Stadium": {"dome": True, "altitude_ft": 100, "tz_offset": -3, "city": "Inglewood"},
    "Allegiant Stadium": {"dome": True, "altitude_ft": 2001, "tz_offset": -3, "city": "Las Vegas"},
    "Mercedes-Benz Stadium": {"dome": True, "altitude_ft": 1050, "tz_offset": 0, "city": "Atlanta"},
    "AT&T Stadium": {"dome": True, "altitude_ft": 600, "tz_offset": -1, "city": "Arlington"},
    "Caesars Superdome": {"dome": True, "altitude_ft": 3, "tz_offset": -1, "city": "New Orleans"},
    "Lucas Oil Stadium": {"dome": True, "altitude_ft": 720, "tz_offset": 0, "city": "Indianapolis"},
    "U.S. Bank Stadium": {"dome": True, "altitude_ft": 830, "tz_offset": -1, "city": "Minneapolis"},
    "State Farm Stadium": {"dome": True, "altitude_ft": 1100, "tz_offset": -2, "city": "Glendale"},
    "NRG Stadium": {"dome": True, "altitude_ft": 43, "tz_offset": -1, "city": "Houston"},
    # MLB outdoor
    "Coors Field": {"dome": False, "altitude_ft": 5200, "tz_offset": -2, "city": "Denver", "park_factor": 1.35},
    "Fenway Park": {"dome": False, "altitude_ft": 20, "tz_offset": 0, "city": "Boston", "park_factor": 1.07},
    "Oracle Park": {"dome": False, "altitude_ft": 0, "tz_offset": -3, "city": "San Francisco", "park_factor": 0.83},
    "Petco Park": {"dome": False, "altitude_ft": 15, "tz_offset": -3, "city": "San Diego", "park_factor": 0.90},
    "Yankee Stadium": {"dome": False, "altitude_ft": 10, "tz_offset": 0, "city": "New York", "park_factor": 1.11},
    "Wrigley Field": {"dome": False, "altitude_ft": 600, "tz_offset": -1, "city": "Chicago", "park_factor": 1.05},
    "Great American Ball Park": {"dome": False, "altitude_ft": 480, "tz_offset": 0, "city": "Cincinnati", "park_factor": 1.13},
    "Dodger Stadium": {"dome": False, "altitude_ft": 510, "tz_offset": -3, "city": "Los Angeles", "park_factor": 0.96},
    "T-Mobile Park": {"dome": True, "altitude_ft": 2, "tz_offset": -3, "city": "Seattle", "park_factor": 0.93},
    "Tropicana Field": {"dome": True, "altitude_ft": 10, "tz_offset": 0, "city": "St. Petersburg", "park_factor": 0.90},
    "Minute Maid Park": {"dome": True, "altitude_ft": 43, "tz_offset": -1, "city": "Houston", "park_factor": 1.04},
    "Globe Life Field": {"dome": True, "altitude_ft": 540, "tz_offset": -1, "city": "Arlington", "park_factor": 0.98},
    "Chase Field": {"dome": True, "altitude_ft": 1082, "tz_offset": -2, "city": "Phoenix", "park_factor": 1.04},
    "Rogers Centre": {"dome": True, "altitude_ft": 250, "tz_offset": 0, "city": "Toronto", "park_factor": 1.00},
    "loanDepot park": {"dome": True, "altitude_ft": 5, "tz_offset": 0, "city": "Miami", "park_factor": 0.88},
    "American Family Field": {"dome": True, "altitude_ft": 635, "tz_offset": -1, "city": "Milwaukee", "park_factor": 1.05},
    # NHL arenas — all indoor (dome=True)
    # Can be extended as needed
}

# Fuzzy match threshold for venue name lookups
_VENUE_MATCH_THRESHOLD = 0.6


def _get_venue_metadata(venue_name: str, sport: str = "") -> dict:
    """Look up static venue metadata by name with fuzzy matching."""
    if not venue_name:
        return {}

    # Direct match first
    if venue_name in VENUE_METADATA:
        return {f"venue_{k}": v for k, v in VENUE_METADATA[venue_name].items()}

    # Fuzzy match
    matches = difflib.get_close_matches(
        venue_name, VENUE_METADATA.keys(), n=1, cutoff=_VENUE_MATCH_THRESHOLD
    )
    if matches:
        meta = VENUE_METADATA[matches[0]]
        return {f"venue_{k}": v for k, v in meta.items()}

    return {}
