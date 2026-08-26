"""
tools.envimpact — environmental impact models for Callisto.

Split out of tools/environment.py (kept as a facade). Submodules:

- venues:    NFL stadiums, NBA arenas, MLB parks (altitude, domes, park factors)
- referees:  NBA/MLB/NFL referee & umpire tendency databases
- weather:   wind / temperature / altitude / humidity models + venue lookups
             and combined weather adjustment
- combined:  referee tendency lookup + total_environment_adjustment entry point
"""

from tools.envimpact.combined import (
    _build_summary,
    ref_tendency,
    total_environment_adjustment,
)
from tools.envimpact.referees import MLB_UMPIRES, NBA_REFEREES, NFL_REFEREES
from tools.envimpact.venues import MLB_VENUES, NBA_VENUES, NFL_VENUES
from tools.envimpact.weather import (
    altitude_impact,
    get_venue_factors,
    get_weather_adjustment,
    humidity_impact,
    temperature_impact,
    wind_impact,
)

__all__ = [
    "NFL_VENUES",
    "NBA_VENUES",
    "MLB_VENUES",
    "NBA_REFEREES",
    "MLB_UMPIRES",
    "NFL_REFEREES",
    "wind_impact",
    "temperature_impact",
    "altitude_impact",
    "humidity_impact",
    "get_venue_factors",
    "get_weather_adjustment",
    "ref_tendency",
    "total_environment_adjustment",
    "_build_summary",
]
