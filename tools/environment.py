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

The implementation lives in ``tools.envimpact``; this module is a facade that
re-exports the public names so existing imports keep working.
"""

import logging

from tools.envimpact import (  # noqa: F401
    MLB_UMPIRES,
    MLB_VENUES,
    NBA_REFEREES,
    NBA_VENUES,
    NFL_REFEREES,
    NFL_VENUES,
    _build_summary,
    altitude_impact,
    get_venue_factors,
    get_weather_adjustment,
    humidity_impact,
    ref_tendency,
    temperature_impact,
    total_environment_adjustment,
    wind_impact,
)

logger = logging.getLogger("callisto.environment")
