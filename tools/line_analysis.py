"""
Line movement analysis and public betting module — facade.

The implementation lives in the ``tools.lanalysis`` package:

- ``tools.lanalysis.constants``     — brand tiers, key numbers, ROI tables
- ``tools.lanalysis.decomposition`` — HP-filter-inspired trend/noise separation
- ``tools.lanalysis.rlm``           — reverse line movement detection
- ``tools.lanalysis.steam``         — steam move detection
- ``tools.lanalysis.timing``        — sport-specific bet timing windows
- ``tools.lanalysis.public``        — public side estimation + contrarian value
- ``tools.lanalysis.priority``      — EV of further analysis
- ``tools.lanalysis.composite``     — full_line_analysis composite report

This module re-exports the public API for backwards compatibility.
"""

from tools.lanalysis._util import _parse_timestamp
from tools.lanalysis.composite import full_line_analysis
from tools.lanalysis.constants import (
    CONTRARIAN_ROI_TABLE,
    NFL_KEY_NUMBERS,
    TEAM_BRAND_TIERS,
    _DEFAULT_ROI_TABLE,
)
from tools.lanalysis.decomposition import decompose_movement
from tools.lanalysis.priority import ev_of_analysis
from tools.lanalysis.public import (
    _public_estimation_confidence,
    contrarian_value,
    estimate_public_side,
)
from tools.lanalysis.rlm import detect_rlm
from tools.lanalysis.steam import detect_steam
from tools.lanalysis.timing import (
    _generic_timing,
    _mlb_timing,
    _nba_timing,
    _ncaab_timing,
    _ncaaf_timing,
    _nfl_timing,
    _nhl_timing,
    optimal_bet_timing,
)

__all__ = [
    "CONTRARIAN_ROI_TABLE",
    "NFL_KEY_NUMBERS",
    "TEAM_BRAND_TIERS",
    "_DEFAULT_ROI_TABLE",
    "_parse_timestamp",
    "_public_estimation_confidence",
    "contrarian_value",
    "decompose_movement",
    "detect_rlm",
    "detect_steam",
    "estimate_public_side",
    "ev_of_analysis",
    "full_line_analysis",
    "optimal_bet_timing",
]
