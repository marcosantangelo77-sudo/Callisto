"""tools.injury — quantitative injury impact model (data + computation).

Split out of tools/injury_model.py; that module re-exports this package's
public API for backwards compatibility.
"""

from tools.injury.data import (
    MARKET_ADJUSTMENT_CURVE,
    MLB_PITCHER_TIERS,
    MLB_POSITION_IMPACT_CENTS,
    MLB_POSITION_TIERS,
    NBA_MATCHUP_MODIFIERS,
    NBA_POSITION_IMPACT,
    NBA_TIER_THRESHOLDS,
    NFL_MATCHUP_MODIFIERS,
    NFL_POSITION_IMPACT,
    NFL_TARGET_REDISTRIBUTION,
    SIGNIFICANCE_TIERS,
)
from tools.injury.model import (
    MarketAdjustmentEstimate,
    MatchupAdjustedImpact,
    PlayerImpactResult,
    UsageRedistribution,
    estimate_market_adjustment,
    full_injury_analysis,
    lookup_position_impact,
    matchup_adjusted_impact,
    player_impact,
    redistribute_usage,
)

__all__ = [
    "MARKET_ADJUSTMENT_CURVE",
    "MLB_PITCHER_TIERS",
    "MLB_POSITION_IMPACT_CENTS",
    "MLB_POSITION_TIERS",
    "NBA_MATCHUP_MODIFIERS",
    "NBA_POSITION_IMPACT",
    "NBA_TIER_THRESHOLDS",
    "NFL_MATCHUP_MODIFIERS",
    "NFL_POSITION_IMPACT",
    "NFL_TARGET_REDISTRIBUTION",
    "SIGNIFICANCE_TIERS",
    "MarketAdjustmentEstimate",
    "MatchupAdjustedImpact",
    "PlayerImpactResult",
    "UsageRedistribution",
    "estimate_market_adjustment",
    "full_injury_analysis",
    "lookup_position_impact",
    "matchup_adjusted_impact",
    "player_impact",
    "redistribute_usage",
]
