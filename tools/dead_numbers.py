"""
Dead number theory and margin-of-victory analysis — facade module.

This module is a thin re-export facade. The implementation lives in the
``tools.dead`` package:

- ``tools.dead.data``       — static frequency/key/push probability tables
- ``tools.dead.valuation``  — core valuation functions
- ``tools.dead.shading``    — public shading detection
- ``tools.dead.analysis``   — composite analysis functions

All public names are re-exported here so that existing imports of
``tools.dead_numbers`` continue to work unchanged.
"""

from tools.dead.analysis import (
    analyze_spread,
    buy_points_analysis,
    find_dead_number_steals,
    rank_line_shopping_opportunities,
)
from tools.dead.data import (
    DEAD_THRESHOLD,
    HALF_QUARTER_KEY,
    KEY_NUMBERS,
    MARGIN_FREQ,
    MLB_KEY_NUMBERS,
    MLB_MARGIN_FREQ,
    MLB_PUSH_PROB,
    NBA_FIRST_HALF_KEY,
    NBA_FIRST_QUARTER_KEY,
    NBA_KEY_NUMBERS,
    NBA_MARGIN_FREQ,
    NBA_PUSH_PROB,
    NCAAB_KEY_NUMBERS,
    NCAAB_MARGIN_FREQ,
    NCAAB_PUSH_PROB,
    NCAAF_KEY_NUMBERS,
    NCAAF_MARGIN_FREQ,
    NCAAF_PUSH_PROB,
    NHL_KEY_NUMBERS,
    NHL_MARGIN_FREQ,
    NHL_PUSH_PROB,
    NFL_FIRST_HALF_KEY,
    NFL_FIRST_QUARTER_KEY,
    NFL_KEY_NUMBERS,
    NFL_MARGIN_FREQ,
    NFL_PUSH_PROB,
    PUSH_PROB,
    SPORT_ALIASES,
)
from tools.dead.shading import SHADE_GRAVITY, detect_public_shading
from tools.dead.valuation import (
    get_margin_distribution,
    half_quarter_key_value,
    is_dead_number,
    key_number_value,
    line_shopping_value,
    push_probability,
)

__all__ = [
    # data tables
    "DEAD_THRESHOLD",
    "HALF_QUARTER_KEY",
    "KEY_NUMBERS",
    "MARGIN_FREQ",
    "MLB_KEY_NUMBERS",
    "MLB_MARGIN_FREQ",
    "MLB_PUSH_PROB",
    "NBA_FIRST_HALF_KEY",
    "NBA_FIRST_QUARTER_KEY",
    "NBA_KEY_NUMBERS",
    "NBA_MARGIN_FREQ",
    "NBA_PUSH_PROB",
    "NCAAB_KEY_NUMBERS",
    "NCAAB_MARGIN_FREQ",
    "NCAAB_PUSH_PROB",
    "NCAAF_KEY_NUMBERS",
    "NCAAF_MARGIN_FREQ",
    "NCAAF_PUSH_PROB",
    "NHL_KEY_NUMBERS",
    "NHL_MARGIN_FREQ",
    "NHL_PUSH_PROB",
    "NFL_FIRST_HALF_KEY",
    "NFL_FIRST_QUARTER_KEY",
    "NFL_KEY_NUMBERS",
    "NFL_MARGIN_FREQ",
    "NFL_PUSH_PROB",
    "PUSH_PROB",
    "SHADE_GRAVITY",
    "SPORT_ALIASES",
    # valuation functions
    "get_margin_distribution",
    "half_quarter_key_value",
    "is_dead_number",
    "key_number_value",
    "line_shopping_value",
    "push_probability",
    # shading
    "detect_public_shading",
    # analysis
    "analyze_spread",
    "buy_points_analysis",
    "find_dead_number_steals",
    "rank_line_shopping_opportunities",
]
