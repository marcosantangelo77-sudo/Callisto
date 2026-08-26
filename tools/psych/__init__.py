"""Market psychology package — split from tools/market_psychology.py."""

from tools.psych.constants import (
    NFL_MARGIN_FREQ,
    NFL_SPREAD_SHADE,
    NBA_TOTAL_SHADE,
    NBA_SPREAD_SHADE,
    SCORING_DISTRIBUTION,
    HALF_QUARTER_EDGES,
    LINE_MOVEMENT_VELOCITY,
    ATTENTION_WEIGHTS,
)
from tools.psych.shading import detect_number_shading, _shading_explanation
from tools.psych.trap_lines import detect_trap_line
from tools.psych.futures import (
    futures_efficiency,
    _win_rate_from_implied,
    _bayesian_weight,
    _estimate_futures_vig,
    optimal_hedge_time,
    _expected_odds_improvement,
)
from tools.psych.half_markets import half_market_adjustment
from tools.psych.attention import attention_arbitrage
from tools.psych.closing_line import predict_closing_line, _clv_recommendation
from tools.psych._utils import _prob_to_american

__all__ = [
    "NFL_MARGIN_FREQ",
    "NFL_SPREAD_SHADE",
    "NBA_TOTAL_SHADE",
    "NBA_SPREAD_SHADE",
    "SCORING_DISTRIBUTION",
    "HALF_QUARTER_EDGES",
    "LINE_MOVEMENT_VELOCITY",
    "ATTENTION_WEIGHTS",
    "detect_number_shading",
    "_shading_explanation",
    "detect_trap_line",
    "futures_efficiency",
    "_win_rate_from_implied",
    "_bayesian_weight",
    "_estimate_futures_vig",
    "optimal_hedge_time",
    "_expected_odds_improvement",
    "half_market_adjustment",
    "attention_arbitrage",
    "predict_closing_line",
    "_clv_recommendation",
    "_prob_to_american",
]
