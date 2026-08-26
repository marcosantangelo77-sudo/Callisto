"""Dead number theory package: data tables, valuation, shading, analysis."""

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
    "DEAD_THRESHOLD",
    "HALF_QUARTER_KEY",
    "KEY_NUMBERS",
    "MARGIN_FREQ",
    "PUSH_PROB",
    "SHADE_GRAVITY",
    "SPORT_ALIASES",
    "get_margin_distribution",
    "half_quarter_key_value",
    "is_dead_number",
    "key_number_value",
    "line_shopping_value",
    "push_probability",
    "detect_public_shading",
    "analyze_spread",
    "buy_points_analysis",
    "find_dead_number_steals",
    "rank_line_shopping_opportunities",
]
