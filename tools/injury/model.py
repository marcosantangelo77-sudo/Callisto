"""Quantitative injury impact and usage projection model — computation layer.

Every injury is a market event. The question is never "is this team worse
without Player X?" — the question is "by HOW MUCH, and has the market
fully priced it?"

Five capabilities:
1. Player impact quantification — marginal value over replacement in points/cents
2. Usage redistribution — where do the absent player's touches/targets/shots go?
3. Matchup-dependent adjustment — missing a rim protector matters more vs Jokic
4. Position impact database — see tools.injury.data for the lookup tables
5. Market adjustment speed — how fast do books reprice after injury news?

No external API calls. Pure computation against hardcoded research baselines.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

from tools.injury.data import (
    NBA_POSITION_IMPACT,
    NBA_TIER_THRESHOLDS,
)

logger = logging.getLogger("callisto.injury_model")


# ---------------------------------------------------------------------------
# Dataclasses for structured results
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Section 5: Dataclasses for structured results
# ---------------------------------------------------------------------------

@dataclass
class PlayerImpactResult:
    """Quantified impact of a player being absent."""
    player_name: str
    team: str
    sport: str
    position: str
    tier: str
    marginal_value_over_replacement: float  # points (NBA/NFL) or cents (MLB)
    spread_impact: float                    # how much the spread should move
    total_impact: float                     # impact on game total (points)
    prop_redistribution: dict               # which teammates benefit for props
    confidence: float                       # 0-1, how certain we are in the estimate
    notes: list[str] = field(default_factory=list)


@dataclass
class UsageRedistribution:
    """How a single teammate's workload changes when a player is out."""
    player: str
    role: str
    usage_increase: float           # percentage points of usage gained
    projected_stat_change: dict     # e.g., {"points": +4.2, "rebounds": +1.1}


@dataclass
class MatchupAdjustedImpact:
    """Injury impact adjusted for the specific opponent."""
    base_impact: float
    matchup_multiplier: float
    adjusted_spread_impact: float
    reasoning: list[str]


@dataclass
class MarketAdjustmentEstimate:
    """How much of the injury impact has been priced in by markets."""
    pct_adjusted: float             # 0-1, how much of the move has happened
    window_remaining_minutes: float # estimated minutes until 95% adjusted
    edge_remaining: float           # estimated remaining mispricing
    significance_tier: str          # star / starter / role_player
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------

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


def player_impact(
    player_name: str,
    team: str,
    sport: str,
    role: Optional[str] = None,
    position: Optional[str] = None,
    ppg: float = 0.0,
    bpm: float = 0.0,
    is_starter: bool = True,
    era: Optional[float] = None,
    war: Optional[float] = None,
    backup_info: Optional[dict] = None,
    teammates: Optional[list[dict]] = None,
) -> PlayerImpactResult:
    """
    Quantify the impact of a player being OUT.

    This is the central function. Given a player's identity and stats,
    it returns:
    - marginal_value_over_replacement: how many points/cents worse the team is
    - spread_impact: how much the spread should move toward the opponent
    - total_impact: how much the game total should decrease (usually negative)
    - prop_redistribution: which teammates see increased usage/stats

    Args:
        player_name: Player's name.
        team: Team name/abbreviation.
        sport: One of "NBA", "NFL", "MLB".
        role: Optional role descriptor (e.g., "rim_protector", "floor_spacer").
        position: Position code (e.g., "PG", "QB", "SP").
        ppg: Points per game (NBA).
        bpm: Box Plus/Minus (NBA).
        is_starter: Whether the player is a starter.
        era: Earned Run Average (MLB pitchers).
        war: Wins Above Replacement (MLB position players).
        backup_info: Dict with "name" and "quality" keys for the replacement.
        teammates: List of teammate dicts for usage redistribution.

    Returns:
        PlayerImpactResult with all quantified impacts.
    """
    sport = sport.upper()
    notes = []

    from tools.injury.impact_nfl_mlb import (
        _mlb_player_impact,
        _nfl_player_impact,
    )

    if sport == "NBA":
        return _nba_player_impact(
            player_name, team, position, role, ppg, bpm,
            is_starter, teammates, notes,
        )
    elif sport == "NFL":
        return _nfl_player_impact(
            player_name, team, position, role, backup_info,
            teammates, notes,
        )
    elif sport == "MLB":
        return _mlb_player_impact(
            player_name, team, position, era, war,
            teammates, notes,
        )
    else:
        logger.warning(f"Unsupported sport: {sport}")
        return PlayerImpactResult(
            player_name=player_name, team=team, sport=sport,
            position=position or "UNK", tier="unknown",
            marginal_value_over_replacement=0.0, spread_impact=0.0,
            total_impact=0.0, prop_redistribution={}, confidence=0.1,
            notes=[f"Unsupported sport: {sport}"],
        )


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


# ---------------------------------------------------------------------------
# Usage Redistribution (cross-sport entry point)
# ---------------------------------------------------------------------------

def redistribute_usage(
    absent_player: str,
    team_roster: list[dict],
    sport: str,
    absent_player_stats: Optional[dict] = None,
) -> list[UsageRedistribution]:
    """
    Project how usage/touches/targets redistribute when a player is OUT.

    This is where prop betting edge lives. When the 30 PPG scorer sits,
    someone absorbs those shots. Whoever gets the biggest share is likely
    underpriced in their player props.

    Args:
        absent_player: Name of the player who is out.
        team_roster: List of teammate dicts. Expected keys vary by sport:
            NBA: {"name", "ppg", "rpg", "apg", "usage_rate", "position"}
            NFL: {"name", "role", "targets_per_game", "carries_per_game"}
            MLB: {"name", "position", "pa_per_game", "avg", "obp"}
        sport: One of "NBA", "NFL", "MLB".
        absent_player_stats: Stats of the absent player for calibration.

    Returns:
        List of UsageRedistribution objects, sorted by usage_increase descending.
    """
    sport = sport.upper()

    from tools.injury.impact_nfl_mlb import (
        _mlb_usage_redistribution,
        _nfl_usage_redistribution,
    )

    if sport == "NBA":
        return _nba_usage_redistribution(absent_player, team_roster, absent_player_stats)
    elif sport == "NFL":
        return _nfl_usage_redistribution(absent_player, team_roster, absent_player_stats)
    elif sport == "MLB":
        return _mlb_usage_redistribution(absent_player, team_roster, absent_player_stats)
    else:
        logger.warning(f"Unsupported sport for redistribution: {sport}")
        return []


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


def _determine_nfl_backup_quality(backup_info: Optional[dict] = None) -> int:
    from tools.injury.impact_nfl_mlb import _determine_nfl_backup_quality as _impl
    return _impl(backup_info)


def _determine_mlb_tier(
    position: str,
    era: Optional[float] = None,
    war: Optional[float] = None,
) -> int:
    from tools.injury.impact_nfl_mlb import _determine_mlb_tier as _impl
    return _impl(position, era, war)


def _nfl_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    role: Optional[str],
    backup_info: Optional[dict],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    from tools.injury.impact_nfl_mlb import _nfl_player_impact as _impl
    return _impl(
        player_name, team, position, role, backup_info, teammates, notes,
    )


def _mlb_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    era: Optional[float],
    war: Optional[float],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    from tools.injury.impact_nfl_mlb import _mlb_player_impact as _impl
    return _impl(player_name, team, position, era, war, teammates, notes)


# Re-export NFL/MLB impact helpers (defined in tools.injury.impact_nfl_mlb).
from tools.injury.impact_nfl_mlb import (  # noqa: F401
    _determine_mlb_tier,
    _determine_nfl_backup_quality,
    _mlb_player_impact,
    _mlb_usage_redistribution,
    _nfl_player_impact,
    _nfl_redistribute_targets,
    _nfl_usage_redistribution,
)

# Re-export high-level analysis helpers (defined in tools.injury.analysis).
from tools.injury.analysis import (  # noqa: F401
    estimate_market_adjustment,
    full_injury_analysis,
    lookup_position_impact,
    matchup_adjusted_impact,
)
