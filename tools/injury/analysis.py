"""High-level injury analysis — matchup, market timing, full pipeline.

Extracted from tools.injury.model. Player-impact quantification and usage
redistribution stay in the computation layer; this module composes them.
"""

from __future__ import annotations

import logging
import math
from typing import Optional

from tools.injury.data import (
    MARKET_ADJUSTMENT_CURVE,
    MLB_POSITION_IMPACT_CENTS,
    NBA_MATCHUP_MODIFIERS,
    NBA_POSITION_IMPACT,
    NFL_MATCHUP_MODIFIERS,
    NFL_POSITION_IMPACT,
    SIGNIFICANCE_TIERS,
)

logger = logging.getLogger("callisto.injury_model")


# ---------------------------------------------------------------------------
# Matchup-Dependent Injury Impact
# ---------------------------------------------------------------------------

def matchup_adjusted_impact(
    absent_player: str,
    opponent: str,
    sport: str,
    player_archetype: Optional[str] = None,
    opponent_style: Optional[str] = None,
    position: Optional[str] = None,
    base_impact: Optional[float] = None,
    ppg: float = 0.0,
    bpm: float = 0.0,
    era: Optional[float] = None,
    war: Optional[float] = None,
    backup_info: Optional[dict] = None,
) -> MatchupAdjustedImpact:
    """
    Adjust injury impact based on the specific opponent matchup.

    The core insight: not all absences are equal across opponents.
    Missing your rim protector vs Jokic is catastrophic.
    Missing your rim protector vs a 3-point shooting team barely matters.

    Args:
        absent_player: Player who is out.
        opponent: Opponent team name.
        sport: "NBA", "NFL", "MLB".
        player_archetype: The absent player's archetype/role for modifier lookup.
            NBA: "rim_protector", "perimeter_defender", "floor_spacer", "playmaker", "scorer"
            NFL: position code (QB, EDGE, CB, etc.)
        opponent_style: The opponent's relevant style for modifier lookup.
            NBA: "interior_dominant", "perimeter_dominant", "balanced", etc.
            NFL: "vs_strong_pass_rush", "vs_pocket_passer", etc.
        position: Player position.
        base_impact: Override base impact instead of calculating it.
        ppg, bpm, era, war, backup_info: Passed through to player_impact if needed.

    Returns:
        MatchupAdjustedImpact with adjusted spread impact and reasoning.
    """
    sport = sport.upper()
    reasoning = []

    # Calculate base impact if not provided
    if base_impact is None:
        result = player_impact(
            player_name=absent_player,
            team="",
            sport=sport,
            position=position,
            ppg=ppg,
            bpm=bpm,
            era=era,
            war=war,
            backup_info=backup_info,
        )
        base_impact = result.spread_impact
        reasoning.append(f"Base impact: {base_impact:.2f} (tier: {result.tier})")

    # Determine matchup modifier
    multiplier = 1.0
    opp_style = (opponent_style or "balanced").lower().replace(" ", "_")

    if sport == "NBA":
        archetype = (player_archetype or "scorer").lower().replace(" ", "_")
        modifiers = NBA_MATCHUP_MODIFIERS.get(archetype, {})
        multiplier = modifiers.get(opp_style, 1.0)

        if multiplier != 1.0:
            direction = "amplified" if multiplier > 1.0 else "mitigated"
            reasoning.append(
                f"Matchup modifier: {archetype} vs {opp_style} opponent = "
                f"{multiplier:.2f}x ({direction})"
            )
        else:
            reasoning.append(f"No specific matchup modifier for {archetype} vs {opp_style}")

        # Additional NBA matchup nuances
        if archetype == "rim_protector" and opp_style == "interior_dominant":
            reasoning.append(
                "HIGH IMPACT: Missing rim protection vs interior attack. "
                "Expect paint points surge (+6-10 pts for opponent in paint)."
            )
        elif archetype == "playmaker" and opp_style == "pressing_defense":
            reasoning.append(
                "HIGH IMPACT: Missing primary ball-handler vs full-court pressure. "
                "Expect turnover rate increase (+3-5 turnovers)."
            )

    elif sport == "NFL":
        pos = (position or player_archetype or "WR").upper()
        modifiers = NFL_MATCHUP_MODIFIERS.get(pos, {})
        multiplier = modifiers.get(opp_style, 1.0)

        if multiplier != 1.0:
            direction = "amplified" if multiplier > 1.0 else "mitigated"
            reasoning.append(
                f"Matchup modifier: {pos} out vs {opp_style} = "
                f"{multiplier:.2f}x ({direction})"
            )

        # NFL-specific matchup reasoning
        if pos == "QB" and opp_style == "vs_strong_pass_rush":
            reasoning.append(
                "CRITICAL: Backup QB behind a line facing elite pass rush. "
                "Expect sack rate to increase 30-50%, completion pct to drop 8-15%."
            )
        elif pos == "CB" and opp_style == "vs_elite_wr":
            reasoning.append(
                "HIGH IMPACT: Missing shadow corner vs opposing WR1. "
                "That WR's prop lines are significantly underpriced."
            )

    elif sport == "MLB":
        # MLB matchup adjustments are simpler: handedness is the main factor
        reasoning.append(
            "MLB matchup adjustment is primarily handedness-driven. "
            "SP matchup quality affects line more than position player matchups."
        )
        # Small default modifiers for MLB
        if opp_style in ("vs_strong_lineup", "vs_elite_offense"):
            multiplier = 1.15
            reasoning.append("Strong opposing lineup amplifies pitching absence.")
        elif opp_style in ("vs_weak_lineup", "vs_poor_offense"):
            multiplier = 0.85
            reasoning.append("Weak opposing lineup mitigates pitching absence.")

    adjusted_impact = base_impact * multiplier

    reasoning.append(
        f"Final adjusted impact: {base_impact:.2f} x {multiplier:.2f} = {adjusted_impact:.2f}"
    )

    logger.info(
        f"Matchup adjusted impact: {absent_player} out vs {opponent} = "
        f"{adjusted_impact:.2f} (base={base_impact:.2f}, mult={multiplier:.2f})"
    )

    return MatchupAdjustedImpact(
        base_impact=round(base_impact, 2),
        matchup_multiplier=round(multiplier, 2),
        adjusted_spread_impact=round(adjusted_impact, 2),
        reasoning=reasoning,
    )


# ---------------------------------------------------------------------------
# Market Adjustment Speed
# ---------------------------------------------------------------------------

def estimate_market_adjustment(
    injury_announced_minutes_ago: float,
    sport: str,
    position: Optional[str] = None,
    player_tier: Optional[str] = None,
    spread_impact: Optional[float] = None,
) -> MarketAdjustmentEstimate:
    """
    Estimate how much of an injury's line impact has been priced in.

    Markets are efficient but not instantaneous. The adjustment curve:
    - 0-2 minutes: Algorithmic bots and sharp bettors who follow injury feeds
    - 2-10 minutes: Sharp syndicates and quantitative bettors
    - 10-30 minutes: Books manually adjust, second-wave sharps
    - 30-60 minutes: Recreational bettors begin to notice
    - 60+ minutes: Fully efficient, no edge remaining

    The speed depends on injury significance:
    - Star player out (LeBron, Mahomes, deGrom) = adjusts in 5-15 minutes
    - Starter out = adjusts in 15-30 minutes
    - Role player out = adjusts in 30-60+ minutes (if at all for minor guys)

    THIS IS WHERE CALLISTO'S EDGE IS. If we detect injury news in <2 minutes,
    we have a window before the line fully adjusts.

    Args:
        injury_announced_minutes_ago: Minutes since injury news broke.
        sport: "NBA", "NFL", "MLB".
        position: Player position (affects significance tier).
        player_tier: Override tier ("star", "starter", "role_player").
        spread_impact: The total expected spread impact in points/cents.

    Returns:
        MarketAdjustmentEstimate with remaining edge.
    """
    sport = sport.upper()
    notes = []

    # Determine significance tier
    if player_tier and player_tier in MARKET_ADJUSTMENT_CURVE:
        tier = player_tier
    elif position:
        sport_tiers = SIGNIFICANCE_TIERS.get(sport, {})
        tier = sport_tiers.get(position.upper(), "starter")
    else:
        tier = "starter"  # default assumption

    curve = MARKET_ADJUSTMENT_CURVE[tier]
    minutes = injury_announced_minutes_ago

    # Interpolate the curve to find current adjustment percentage
    pct_adjusted = 0.0
    for i, (curve_mins, curve_pct) in enumerate(curve):
        if minutes <= curve_mins:
            if i == 0:
                # Before first data point — linear interpolation from 0
                pct_adjusted = (minutes / curve_mins) * curve_pct
            else:
                prev_mins, prev_pct = curve[i - 1]
                # Linear interpolation between points
                frac = (minutes - prev_mins) / (curve_mins - prev_mins)
                pct_adjusted = prev_pct + frac * (curve_pct - prev_pct)
            break
    else:
        # Past the last curve point
        pct_adjusted = curve[-1][1]
        if minutes > curve[-1][0]:
            # Asymptotic approach to 1.0
            excess_minutes = minutes - curve[-1][0]
            remaining = 1.0 - curve[-1][1]
            pct_adjusted = curve[-1][1] + remaining * (1 - math.exp(-excess_minutes / 60))

    pct_adjusted = min(pct_adjusted, 1.0)

    # Estimate window remaining to reach 95% adjustment
    target_pct = 0.95
    window_remaining = 0.0
    if pct_adjusted < target_pct:
        for curve_mins, curve_pct in curve:
            if curve_pct >= target_pct:
                # Interpolate to find exact time of 95%
                idx = curve.index((curve_mins, curve_pct))
                if idx > 0:
                    prev_mins, prev_pct = curve[idx - 1]
                    frac = (target_pct - prev_pct) / (curve_pct - prev_pct)
                    target_time = prev_mins + frac * (curve_mins - prev_mins)
                else:
                    target_time = curve_mins * (target_pct / curve_pct)
                window_remaining = max(0, target_time - minutes)
                break
        else:
            window_remaining = max(0, 120 - minutes)

    # Calculate remaining edge
    edge_remaining = 0.0
    if spread_impact is not None:
        edge_remaining = spread_impact * (1.0 - pct_adjusted)

    # Add actionability notes
    if pct_adjusted < 0.30:
        notes.append("URGENT: Market is barely adjusted. Maximum edge available NOW.")
        notes.append("Action within 2-3 minutes for best execution.")
    elif pct_adjusted < 0.60:
        notes.append("SIGNIFICANT: Substantial edge remains. Act quickly.")
        notes.append("Sharp money is moving but recreational books may still be stale.")
    elif pct_adjusted < 0.85:
        notes.append("MODERATE: Some edge remains, primarily on slower-moving books.")
        notes.append("Check soft books (DraftKings, FanDuel, BetMGM) for stale lines.")
    elif pct_adjusted < 0.95:
        notes.append("SMALL: Most of the adjustment has happened.")
        notes.append("Remaining edge is likely in props and secondary markets.")
    else:
        notes.append("NONE: Market is fully adjusted. No informational edge from this injury.")

    # Sport-specific notes
    if sport == "NBA" and tier == "star":
        notes.append(
            "NBA star injuries move fastest — sharp NBA bettors monitor "
            "injury feeds aggressively. Alt-spread and prop markets adjust slower."
        )
    elif sport == "NFL":
        notes.append(
            "NFL injury news often breaks via beat reporters on X/Twitter. "
            "Game-day inactives (90 min pre-game) are the primary window."
        )
    elif sport == "MLB":
        notes.append(
            "MLB lineup announcements (~2-4 hours pre-game) create regular windows. "
            "Monitor for late scratches — those create the best edges."
        )

    logger.info(
        f"Market adjustment: {tier} injury, {minutes:.1f} min ago = "
        f"{pct_adjusted:.1%} adjusted, {window_remaining:.1f} min remaining"
    )

    return MarketAdjustmentEstimate(
        pct_adjusted=round(pct_adjusted, 3),
        window_remaining_minutes=round(window_remaining, 1),
        edge_remaining=round(edge_remaining, 3),
        significance_tier=tier,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Convenience / aggregate functions
# ---------------------------------------------------------------------------

def full_injury_analysis(
    player_name: str,
    team: str,
    sport: str,
    opponent: str,
    position: Optional[str] = None,
    role: Optional[str] = None,
    ppg: float = 0.0,
    bpm: float = 0.0,
    era: Optional[float] = None,
    war: Optional[float] = None,
    backup_info: Optional[dict] = None,
    teammates: Optional[list[dict]] = None,
    player_archetype: Optional[str] = None,
    opponent_style: Optional[str] = None,
    minutes_since_announced: float = 0.0,
) -> dict:
    """
    Run the complete injury analysis pipeline in one call.

    Returns a dict with all four analysis components:
    1. impact: PlayerImpactResult
    2. redistribution: list[UsageRedistribution]
    3. matchup: MatchupAdjustedImpact
    4. market_timing: MarketAdjustmentEstimate

    This is the function the orchestrator should call when injury news hits.
    """
    logger.info(f"Full injury analysis: {player_name} ({team}) out vs {opponent}")

    # Step 1: Base impact
    impact = player_impact(
        player_name=player_name,
        team=team,
        sport=sport,
        role=role,
        position=position,
        ppg=ppg,
        bpm=bpm,
        era=era,
        war=war,
        backup_info=backup_info,
        teammates=teammates,
    )

    # Step 2: Usage redistribution
    absent_stats = {}
    if sport.upper() == "NBA":
        absent_stats = {"ppg": ppg, "usage_rate": ppg * 1.2}  # rough proxy
    elif sport.upper() == "NFL":
        absent_stats = {"role": role or position or "WR1"}

    redistribution = []
    if teammates:
        redistribution = redistribute_usage(
            absent_player=player_name,
            team_roster=teammates,
            sport=sport,
            absent_player_stats=absent_stats or None,
        )

    # Step 3: Matchup adjustment
    matchup = matchup_adjusted_impact(
        absent_player=player_name,
        opponent=opponent,
        sport=sport,
        player_archetype=player_archetype or role,
        opponent_style=opponent_style,
        position=position,
        base_impact=impact.spread_impact,
    )

    # Step 4: Market timing
    market_timing = estimate_market_adjustment(
        injury_announced_minutes_ago=minutes_since_announced,
        sport=sport,
        position=position,
        player_tier=None,  # let it infer from position
        spread_impact=matchup.adjusted_spread_impact,
    )

    summary = {
        "player": player_name,
        "team": team,
        "sport": sport,
        "opponent": opponent,
        "impact": impact,
        "redistribution": redistribution,
        "matchup_adjusted": matchup,
        "market_timing": market_timing,
        "actionable": market_timing.pct_adjusted < 0.85,
        "edge_points": market_timing.edge_remaining,
    }

    logger.info(
        f"Full analysis complete: {player_name} — "
        f"spread impact={matchup.adjusted_spread_impact:.2f}, "
        f"market {market_timing.pct_adjusted:.0%} adjusted, "
        f"edge remaining={market_timing.edge_remaining:.3f}, "
        f"actionable={summary['actionable']}"
    )

    return summary


def lookup_position_impact(
    sport: str,
    position: str,
) -> dict:
    """
    Quick lookup of position impact ranges without player-specific data.

    Useful for rough estimates when you only know the position, not the player.
    Returns the full range from bench/replacement to star/MVP.
    """
    sport = sport.upper()
    position = position.upper()

    if sport == "NBA":
        values = NBA_POSITION_IMPACT.get(position)
        if values:
            return {
                "sport": sport,
                "position": position,
                "unit": "spread points",
                "bench": values[0],
                "avg_starter": values[1],
                "good_starter": values[2],
                "all_star": values[3],
                "mvp_candidate": values[4],
            }

    elif sport == "NFL":
        values = NFL_POSITION_IMPACT.get(position)
        if values:
            return {
                "sport": sport,
                "position": position,
                "unit": "spread points",
                "high_quality_backup": values[0],
                "average_backup": values[1],
                "low_quality_backup": values[2],
            }

    elif sport == "MLB":
        values = MLB_POSITION_IMPACT_CENTS.get(position)
        if values:
            return {
                "sport": sport,
                "position": position,
                "unit": "moneyline cents",
                "replacement": values[0],
                "average": values[1],
                "above_average": values[2],
                "star": values[3],
            }

    return {"error": f"No impact data for {sport} {position}"}

# Imported last: tools.injury.model re-exports these names, so a top-level
# import from model would cycle while this module is still defining them.
from tools.injury.model import (  # noqa: E402
    MarketAdjustmentEstimate,
    MatchupAdjustedImpact,
    player_impact,
    redistribute_usage,
)
