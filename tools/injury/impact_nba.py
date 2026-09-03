"""NBA player-impact quantification and usage redistribution.

Extracted from tools.injury.model. NFL/MLB impact helpers live in
tools.injury.impact_nfl_mlb; player_impact / redistribute_usage
dispatchers stay in the computation layer.
"""

from __future__ import annotations

import logging
from typing import Optional

from tools.injury.data import (
    NBA_POSITION_IMPACT,
    NBA_TIER_THRESHOLDS,
)

logger = logging.getLogger("callisto.injury_model")


def _determine_nba_tier(
    ppg: float = 0.0,
    bpm: float = 0.0,
    is_starter: bool = True,
) -> tuple[int, str]:
    """
    Determine NBA player tier from available stats.

    Returns (tier_index, tier_name).
    Tier index maps into the NBA_POSITION_IMPACT tuples.
    """
    tier_names = ["bench", "avg_starter", "good_starter", "all_star", "mvp_candidate"]

    # Walk from highest tier downward
    for tier_idx in range(4, -1, -1):
        min_ppg, min_bpm = NBA_TIER_THRESHOLDS[tier_idx]
        if ppg >= min_ppg or bpm >= min_bpm:
            # Non-starters cap at good_starter unless stats are elite
            if not is_starter and tier_idx > 2 and ppg < 20:
                tier_idx = min(tier_idx, 2)
            return tier_idx, tier_names[tier_idx]

    return 0, "bench"


def _nba_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    role: Optional[str],
    ppg: float,
    bpm: float,
    is_starter: bool,
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    """
    NBA player impact using RAPTOR/EPM-style marginal value.

    The core insight: a player's on/off differential is the best measure of
    their value. RAPTOR and EPM estimate this with priors. We approximate it
    using PPG and BPM as inputs to our tier system, which maps to researched
    point-spread impacts.

    A top-5 NBA player (Jokic, Shai, Luka) being out moves a spread 4-5 points.
    An average starter moves it 1-2 points.
    A bench player moves it 0-0.5 points.
    """
    pos = (position or "SF").upper()
    if pos not in NBA_POSITION_IMPACT:
        # Try common aliases
        pos_map = {"POINT GUARD": "PG", "SHOOTING GUARD": "SG",
                   "SMALL FORWARD": "SF", "POWER FORWARD": "PF", "CENTER": "C",
                   "G": "PG", "F": "SF", "G-F": "SG", "F-C": "PF", "F-G": "SF",
                   "C-F": "C"}
        pos = pos_map.get(pos, "SF")

    tier_idx, tier_name = _determine_nba_tier(ppg, bpm, is_starter)
    impact_values = NBA_POSITION_IMPACT[pos]
    base_impact = impact_values[tier_idx]

    # Fine-tune within tier using PPG as a scaler.
    # A 25 PPG all-star has more impact than a 22 PPG all-star.
    if tier_idx >= 3 and ppg > 0:
        tier_floor_ppg = NBA_TIER_THRESHOLDS[tier_idx][0]
        next_tier_ppg = NBA_TIER_THRESHOLDS.get(tier_idx + 1, (35.0, 10.0))[0]
        ppg_range = max(next_tier_ppg - tier_floor_ppg, 1.0)
        ppg_pct = min((ppg - tier_floor_ppg) / ppg_range, 1.0)
        ppg_pct = max(ppg_pct, 0.0)
        # Scale within the tier range
        tier_low = impact_values[tier_idx]
        tier_high = impact_values[min(tier_idx + 1, 4)] if tier_idx < 4 else tier_low * 1.15
        base_impact = tier_low + (tier_high - tier_low) * ppg_pct

    # BPM adjustment: high-BPM players have outsized on/off impact
    if bpm > 5.0:
        bpm_bonus = (bpm - 5.0) * 0.15  # each BPM point above 5 adds ~0.15 spread pts
        base_impact += bpm_bonus
        notes.append(f"BPM bonus: +{bpm_bonus:.2f} pts (BPM={bpm:.1f})")

    # Total impact: when a scorer is out, the team scores fewer points
    # but also pace may slow. Net effect on total is usually 60-75% of spread impact.
    total_impact = -base_impact * 0.65

    # Prop redistribution
    prop_redist = {}
    if teammates and ppg > 0:
        prop_redist = _nba_redistribute_usage(ppg, teammates)
    elif ppg > 10:
        # Estimate without specific teammate data
        notes.append("No teammate data — prop redistribution is approximate")
        prop_redist = {
            "primary_beneficiary": {"usage_increase_pct": 3.5, "ppg_increase": ppg * 0.15},
            "secondary_beneficiary": {"usage_increase_pct": 2.5, "ppg_increase": ppg * 0.10},
            "tertiary_beneficiary": {"usage_increase_pct": 1.5, "ppg_increase": ppg * 0.07},
            "rest_of_team": {"usage_increase_pct": 5.0, "ppg_increase": ppg * 0.08},
        }

    # Confidence: higher with more stat info
    confidence = 0.50
    if ppg > 0:
        confidence += 0.15
    if bpm != 0:
        confidence += 0.15
    if is_starter:
        confidence += 0.05
    if teammates:
        confidence += 0.10

    notes.append(f"Tier: {tier_name} (idx={tier_idx})")
    notes.append(f"Position: {pos}, Base impact: {base_impact:.2f} pts")

    logger.info(
        f"NBA impact: {player_name} ({pos}, {tier_name}) = {base_impact:.2f} pts spread, "
        f"{total_impact:.2f} pts total"
    )

    return PlayerImpactResult(
        player_name=player_name,
        team=team,
        sport="NBA",
        position=pos,
        tier=tier_name,
        marginal_value_over_replacement=round(base_impact, 2),
        spread_impact=round(base_impact, 2),
        total_impact=round(total_impact, 2),
        prop_redistribution=prop_redist,
        confidence=round(min(confidence, 0.95), 2),
        notes=notes,
    )


def _nba_redistribute_usage(
    absent_ppg: float,
    teammates: list[dict],
) -> dict:
    """
    Redistribute an absent NBA player's usage to teammates.

    NBA usage redistribution follows a roughly proportional pattern:
    teammates with higher existing usage absorb more of the vacated touches.

    Each teammate dict should have: {"name", "ppg", "usage_rate"}.
    usage_rate is the percentage of team possessions used (0-40 typically).
    """
    total_teammate_usage = sum(t.get("usage_rate", 15.0) for t in teammates)
    if total_teammate_usage == 0:
        total_teammate_usage = 1.0

    # The absent player's shots don't all become scoring — efficiency drops.
    # Historically, ~70-80% of a star's PPG is redistributed, rest is lost.
    redistributable_ppg = absent_ppg * 0.75
    # Approximate shot attempts from PPG (assuming ~50% TS)
    absent_fga = absent_ppg / 1.1  # rough PPG-to-FGA conversion

    result = {}
    for t in teammates:
        name = t.get("name", "Unknown")
        usage = t.get("usage_rate", 15.0)
        their_ppg = t.get("ppg", 8.0)

        share = usage / total_teammate_usage
        ppg_gain = redistributable_ppg * share
        fga_gain = absent_fga * share
        # Each additional shot is less efficient — apply diminishing returns
        efficiency_penalty = 0.92 if usage < 25 else 0.85
        ppg_gain *= efficiency_penalty

        result[name] = {
            "usage_increase_pct": round(share * 15.0, 1),  # pct points of usage gained
            "projected_ppg_increase": round(ppg_gain, 1),
            "projected_fga_increase": round(fga_gain, 1),
            "new_projected_ppg": round(their_ppg + ppg_gain, 1),
        }

    return result


def _nba_usage_redistribution(
    absent_player: str,
    roster: list[dict],
    absent_stats: Optional[dict],
) -> list[UsageRedistribution]:
    """
    NBA usage redistribution.

    When a high-usage player sits, his possessions redistribute proportionally
    to remaining players' usage rates. Key empirical findings:

    - ~75% of a star's scoring is recovered by teammates
    - The primary ball-handler sees the biggest usage bump (~3-5% usage increase)
    - Efficiency drops across the board (more shots != more efficient shots)
    - Rebounds redistribute more evenly than scoring
    - Assists crater if the absent player was the primary playmaker
    """
    if absent_stats is None:
        absent_stats = {"ppg": 20.0, "rpg": 5.0, "apg": 4.0, "usage_rate": 28.0}

    absent_ppg = absent_stats.get("ppg", 20.0)
    absent_rpg = absent_stats.get("rpg", 5.0)
    absent_apg = absent_stats.get("apg", 4.0)
    absent_usage = absent_stats.get("usage_rate", 28.0)

    # Total redistributable stats (not all production is recovered)
    recovery_rate = 0.75  # ~75% of scoring is redistributed
    rebound_recovery = 0.90  # rebounds are more mechanical
    assist_recovery = 0.60  # playmaking is harder to replace

    redistributable_ppg = absent_ppg * recovery_rate
    redistributable_rpg = absent_rpg * rebound_recovery
    redistributable_apg = absent_apg * assist_recovery

    total_usage = sum(t.get("usage_rate", 15.0) for t in roster)
    if total_usage == 0:
        total_usage = 1.0

    results = []
    for t in roster:
        name = t.get("name", "Unknown")
        if name.lower() == absent_player.lower():
            continue  # skip the absent player

        usage = t.get("usage_rate", 15.0)
        their_ppg = t.get("ppg", 8.0)
        their_rpg = t.get("rpg", 4.0)
        their_apg = t.get("apg", 2.0)
        pos = t.get("position", "SF")

        share = usage / total_usage

        # Scoring redistribution with diminishing returns
        efficiency_factor = 0.92 if usage < 25 else 0.85 if usage < 30 else 0.78
        ppg_gain = redistributable_ppg * share * efficiency_factor

        # Rebound redistribution — bigs get more, guards get less
        rebound_multiplier = 1.3 if pos in ("C", "PF") else 0.8
        rpg_gain = redistributable_rpg * share * rebound_multiplier

        # Assist redistribution — guards and playmakers get more
        assist_multiplier = 1.4 if pos in ("PG",) else 1.0 if pos in ("SG", "SF") else 0.6
        apg_gain = redistributable_apg * share * assist_multiplier

        # Usage increase in percentage points
        usage_increase = absent_usage * share * 0.85  # not all usage is redistributed

        results.append(UsageRedistribution(
            player=name,
            role=pos,
            usage_increase=round(usage_increase, 1),
            projected_stat_change={
                "ppg_increase": round(ppg_gain, 1),
                "rpg_increase": round(rpg_gain, 1),
                "apg_increase": round(apg_gain, 1),
                "new_projected_ppg": round(their_ppg + ppg_gain, 1),
                "new_projected_rpg": round(their_rpg + rpg_gain, 1),
                "new_projected_apg": round(their_apg + apg_gain, 1),
            },
        ))

    results.sort(key=lambda r: r.usage_increase, reverse=True)
    logger.info(
        f"NBA redistribution for {absent_player} out: "
        f"top beneficiary = {results[0].player if results else 'N/A'}"
    )
    return results


# Imported last: tools.injury.model re-exports these names, so a top-level
# import from model would cycle while this module is still defining them.
from tools.injury.model import (  # noqa: E402
    PlayerImpactResult,
    UsageRedistribution,
)
