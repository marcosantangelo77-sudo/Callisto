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
import math
from dataclasses import dataclass, field
from typing import Optional

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


def _determine_nfl_backup_quality(backup_info: Optional[dict] = None) -> int:
    """
    Determine NFL backup quality index.

    Returns 0 (high quality), 1 (average), or 2 (low quality backup).
    Default to average (1) if no info provided.
    """
    if backup_info is None:
        return 1  # assume average backup

    quality = backup_info.get("quality", "average")
    mapping = {"high": 0, "good": 0, "average": 1, "low": 2, "poor": 2}
    return mapping.get(quality.lower(), 1)


def _determine_mlb_tier(
    position: str,
    era: Optional[float] = None,
    war: Optional[float] = None,
) -> int:
    """
    Determine MLB player tier.

    Returns tier index into MLB_POSITION_IMPACT_CENTS tuples.
    """
    if position == "SP" and era is not None:
        for tier_idx in range(3, -1, -1):
            low, high = MLB_PITCHER_TIERS[tier_idx]
            if low <= era <= high:
                return tier_idx
        return 1  # default avg

    if war is not None:
        for tier_idx in range(3, -1, -1):
            low, high = MLB_POSITION_TIERS[tier_idx]
            if low <= war <= high:
                return tier_idx
        if war > 4.0:
            return 3
        return 1

    # Default: avg starter
    return 1


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


def _nfl_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    role: Optional[str],
    backup_info: Optional[dict],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    """
    NFL player impact — position-dependent with backup quality adjustment.

    NFL is more position-dependent than any other sport:
    - QB out is catastrophic (3-7 pts depending on replacement)
    - RB is nearly fungible (0.3-1.5 pts)
    - OL is underrated by public but books know (0.5-1 pt each)
    - Elite pass rushers can swing games (1-2 pts)
    """
    pos = (position or role or "WR").upper()
    if pos not in NFL_POSITION_IMPACT:
        # Try to map common roles
        role_map = {
            "QUARTERBACK": "QB", "RUNNING BACK": "RB", "WIDE RECEIVER": "WR",
            "TIGHT END": "TE", "OFFENSIVE LINE": "OL", "OFFENSIVE TACKLE": "OL",
            "GUARD": "OL", "CENTER": "OL",
            "DEFENSIVE END": "EDGE", "OUTSIDE LINEBACKER": "EDGE",
            "PASS RUSHER": "EDGE", "DEFENSIVE TACKLE": "DT", "NOSE TACKLE": "DT",
            "LINEBACKER": "LB", "INSIDE LINEBACKER": "LB", "MIDDLE LINEBACKER": "LB",
            "CORNERBACK": "CB", "SAFETY": "S", "FREE SAFETY": "S",
            "STRONG SAFETY": "S", "KICKER": "K", "PUNTER": "P",
            "DE": "EDGE", "OLB": "EDGE", "ILB": "LB", "MLB": "LB",
            "NT": "DT", "OT": "OL", "OG": "OL", "FS": "S", "SS": "S",
        }
        pos = role_map.get(pos, "WR")

    backup_idx = _determine_nfl_backup_quality(backup_info)
    impact_values = NFL_POSITION_IMPACT.get(pos, (0.5, 1.0, 1.5))
    base_impact = impact_values[backup_idx]

    backup_desc = ["high quality", "average", "low quality"][backup_idx]
    notes.append(f"Position: {pos}, Backup quality: {backup_desc}")
    notes.append(f"Impact range for {pos}: {impact_values[0]}-{impact_values[2]} pts")

    # QB-specific: further adjust based on passing environment
    if pos == "QB":
        notes.append("QB absence is the single largest injury impact in NFL")
        if backup_info and backup_info.get("games_started", 0) > 10:
            # Experienced backup is less damaging
            experience_discount = min(backup_info["games_started"] / 50, 0.25)
            base_impact *= (1 - experience_discount)
            notes.append(
                f"Backup has {backup_info['games_started']} starts — "
                f"discount: {experience_discount:.0%}"
            )

    # Total impact: NFL totals drop more directly than NBA
    # A QB out drops total by roughly 70-80% of the spread impact
    total_impact = -base_impact * 0.75

    # Prop redistribution
    prop_redist = {}
    if pos in ("WR", "TE", "RB"):
        # Use NFL target redistribution patterns
        key_map = {
            "WR": "WR1_out",  # default to WR1, could be more specific
            "TE": "TE1_out",
            "RB": "RB1_out",
        }
        pattern_key = key_map.get(pos, "WR1_out")
        if role and role.upper() in NFL_TARGET_REDISTRIBUTION:
            pattern_key = role.upper()

        pattern = NFL_TARGET_REDISTRIBUTION.get(pattern_key, {})
        if teammates:
            prop_redist = _nfl_redistribute_targets(pattern, teammates)
        else:
            prop_redist = {
                role_name: {"target_share_increase": round(share * 100, 1)}
                for role_name, share in pattern.items()
            }
            notes.append("No teammate data — using generic redistribution pattern")

    tier_name = "starter"
    if pos == "QB":
        tier_name = "franchise_qb"
    elif base_impact >= 1.5:
        tier_name = "impact_starter"

    confidence = 0.55
    if backup_info:
        confidence += 0.15
    if teammates:
        confidence += 0.10
    if pos == "QB":
        confidence += 0.10  # QB impact is most well-studied

    logger.info(
        f"NFL impact: {player_name} ({pos}) = {base_impact:.2f} pts spread, "
        f"backup={backup_desc}"
    )

    return PlayerImpactResult(
        player_name=player_name,
        team=team,
        sport="NFL",
        position=pos,
        tier=tier_name,
        marginal_value_over_replacement=round(base_impact, 2),
        spread_impact=round(base_impact, 2),
        total_impact=round(total_impact, 2),
        prop_redistribution=prop_redist,
        confidence=round(min(confidence, 0.95), 2),
        notes=notes,
    )


def _nfl_redistribute_targets(
    pattern: dict,
    teammates: list[dict],
) -> dict:
    """
    Map NFL target redistribution to actual teammate names.

    Each teammate dict should have: {"name", "role"} where role matches
    the pattern keys (e.g., "WR2", "TE1", "RB1").
    """
    result = {}
    role_to_player = {t.get("role", "").upper(): t.get("name", "Unknown") for t in teammates}

    for pattern_role, share in pattern.items():
        player_name = role_to_player.get(pattern_role.upper(), pattern_role)
        result[player_name] = {
            "role": pattern_role,
            "target_share_increase": round(share * 100, 1),
            "projected_target_increase_per_game": round(share * 8, 1),  # assume ~8 targets vacated
        }

    return result


def _mlb_player_impact(
    player_name: str,
    team: str,
    position: Optional[str],
    era: Optional[float],
    war: Optional[float],
    teammates: Optional[list[dict]],
    notes: list[str],
) -> PlayerImpactResult:
    """
    MLB player impact in moneyline cents.

    Baseball is dominated by pitching:
    - Replacing an ace (2.80 ERA) with a spot starter (5.50 ERA) moves the ML 40-55 cents
    - Replacing an average starter (4.20 ERA) with a spot starter moves it 15-25 cents
    - Position players are 5-20 cents at most, even stars

    These map directly to betting value because MLB is primarily priced on the ML.
    Spread (run line) impact is smaller but correlated.
    """
    pos = (position or "SP").upper()
    if pos not in MLB_POSITION_IMPACT_CENTS:
        # Map to nearest position
        pos_map = {
            "P": "SP", "PITCHER": "SP", "STARTER": "SP", "RELIEVER": "RP",
            "CLOSER": "RP", "SETUP": "RP", "CATCHER": "C",
            "FIRST BASE": "1B", "SECOND BASE": "2B", "THIRD BASE": "3B",
            "SHORTSTOP": "SS", "LEFT FIELD": "LF", "CENTER FIELD": "CF",
            "RIGHT FIELD": "RF", "DESIGNATED HITTER": "DH",
            "IF": "SS", "OF": "CF", "UTIL": "DH",
        }
        pos = pos_map.get(pos, "DH")

    tier_idx = _determine_mlb_tier(pos, era, war)
    impact_values = MLB_POSITION_IMPACT_CENTS[pos]
    base_impact_cents = impact_values[tier_idx]

    tier_names = ["replacement", "average", "above_average", "star"]
    tier_name = tier_names[tier_idx]

    # ERA-based fine-tuning for pitchers
    if pos == "SP" and era is not None:
        # Linear interpolation within tier
        era_range = MLB_PITCHER_TIERS[tier_idx]
        tier_width = era_range[1] - era_range[0]
        if tier_width > 0 and tier_width < 10:
            # Lower ERA = higher impact (better pitcher)
            era_pct = 1.0 - (era - era_range[0]) / tier_width
            era_pct = max(0, min(1, era_pct))
            low_impact = impact_values[tier_idx]
            high_impact = impact_values[min(tier_idx + 1, 3)] if tier_idx < 3 else low_impact * 1.15
            base_impact_cents = low_impact + (high_impact - low_impact) * era_pct

        notes.append(f"ERA: {era:.2f} — SP absence moves ML ~{base_impact_cents:.0f} cents")

    # Convert cents to spread points for consistency: ~10 cents ≈ 0.3 run line pts
    spread_impact = base_impact_cents * 0.03
    # Total impact: pitcher affects total significantly (run suppression/inflation)
    total_impact = base_impact_cents * 0.04 if pos in ("SP", "RP") else base_impact_cents * 0.01

    # Prop redistribution — less relevant in MLB but lineup slot matters
    prop_redist = {}
    if pos in ("SP", "RP"):
        prop_redist = {
            "replacement_pitcher": {
                "expected_era_increase": round((5.5 - (era or 4.2)), 1),
                "expected_ip_decrease": round(max(0, 6.0 - 4.5), 1),
                "bullpen_usage_increase": "High — expect 1-2 extra RP innings",
            }
        }
        notes.append("SP absence also increases bullpen workload for subsequent games")
    elif teammates:
        # Batting order shifts — later hitters get slightly better RBI opportunities
        for t in teammates[:3]:
            name = t.get("name", "Unknown")
            prop_redist[name] = {
                "lineup_protection_change": "Reduced — fewer baserunners ahead",
                "pa_change": "+0.2 PA/game if moved up in order",
            }

    confidence = 0.50
    if era is not None:
        confidence += 0.20
    if war is not None:
        confidence += 0.15
    if pos == "SP":
        confidence += 0.05  # SP impact is most well-understood

    logger.info(
        f"MLB impact: {player_name} ({pos}, {tier_name}) = {base_impact_cents:.0f} cents ML, "
        f"{spread_impact:.2f} run line"
    )

    return PlayerImpactResult(
        player_name=player_name,
        team=team,
        sport="MLB",
        position=pos,
        tier=tier_name,
        marginal_value_over_replacement=round(base_impact_cents, 1),
        spread_impact=round(spread_impact, 2),
        total_impact=round(total_impact, 2),
        prop_redistribution=prop_redist,
        confidence=round(min(confidence, 0.95), 2),
        notes=notes,
    )


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


def _nfl_usage_redistribution(
    absent_player: str,
    roster: list[dict],
    absent_stats: Optional[dict],
) -> list[UsageRedistribution]:
    """
    NFL target/carry redistribution.

    NFL redistribution is more structured than NBA:
    - WR1 out: targets flow WR2 > TE > WR3 > RB check-downs
    - RB1 out: carries go ~55% to RB2, rest is pass game
    - TE1 out: split between WR corps and TE2
    """
    if absent_stats is None:
        absent_stats = {
            "role": "WR1",
            "targets_per_game": 8.0,
            "receptions_per_game": 5.5,
            "yards_per_game": 70.0,
            "carries_per_game": 0.0,
        }

    role = absent_stats.get("role", "WR1").upper()
    targets = absent_stats.get("targets_per_game", 8.0)
    carries = absent_stats.get("carries_per_game", 0.0)

    # Get redistribution pattern
    pattern_key = f"{role}_out"
    if pattern_key not in NFL_TARGET_REDISTRIBUTION:
        # Try to find closest match
        if "WR" in role:
            pattern_key = "WR1_out"
        elif "TE" in role:
            pattern_key = "TE1_out"
        elif "RB" in role:
            pattern_key = "RB1_out"
        else:
            pattern_key = "WR1_out"

    pattern = NFL_TARGET_REDISTRIBUTION.get(pattern_key, {"WR2": 0.35, "WR3": 0.25, "TE1": 0.20, "RB1": 0.20})

    role_to_player = {t.get("role", "").upper(): t for t in roster}

    results = []
    for pattern_role, share in sorted(pattern.items(), key=lambda x: -x[1]):
        teammate = role_to_player.get(pattern_role.upper())
        if teammate is None:
            # Use generic name
            teammate = {"name": pattern_role, "role": pattern_role}

        name = teammate.get("name", pattern_role)
        their_targets = teammate.get("targets_per_game", 4.0)
        their_carries = teammate.get("carries_per_game", 0.0)

        target_gain = targets * share
        carry_gain = carries * share if "RB" in pattern_role else 0

        # Yard projection: additional targets are less efficient (coverage adjusts)
        yards_per_target = 7.5  # league average, declining with volume
        additional_yards = target_gain * yards_per_target * 0.85  # 15% efficiency drop
        additional_rush_yards = carry_gain * 3.8  # avg yards per carry

        results.append(UsageRedistribution(
            player=name,
            role=pattern_role,
            usage_increase=round(share * 100, 1),  # percentage of vacated usage
            projected_stat_change={
                "target_increase": round(target_gain, 1),
                "new_projected_targets": round(their_targets + target_gain, 1),
                "additional_yards": round(additional_yards + additional_rush_yards, 1),
                "carry_increase": round(carry_gain, 1),
                "new_projected_carries": round(their_carries + carry_gain, 1),
            },
        ))

    results.sort(key=lambda r: r.usage_increase, reverse=True)
    logger.info(
        f"NFL redistribution for {absent_player} ({role}) out: "
        f"top beneficiary = {results[0].player if results else 'N/A'}"
    )
    return results


def _mlb_usage_redistribution(
    absent_player: str,
    roster: list[dict],
    absent_stats: Optional[dict],
) -> list[UsageRedistribution]:
    """
    MLB usage redistribution is primarily about lineup order shifts.

    Unlike NBA/NFL, MLB redistribution is subtle:
    - A hitter out means the lineup shifts, affecting plate appearances
    - Protection matters: the hitter behind a star gets worse pitches
    - Run production in the slot is partially replaced by whoever fills it

    The biggest prop edge in MLB injuries is the REPLACEMENT player's
    over/under, not redistribution to existing teammates.
    """
    if absent_stats is None:
        absent_stats = {
            "position": "SS",
            "batting_order": 3,
            "pa_per_game": 4.2,
            "avg": 0.280,
            "obp": 0.360,
            "slg": 0.500,
            "hr_rate": 0.040,
        }

    batting_order = absent_stats.get("batting_order", 5)
    absent_obp = absent_stats.get("obp", 0.330)

    results = []
    for t in roster:
        name = t.get("name", "Unknown")
        if name.lower() == absent_player.lower():
            continue

        their_order = t.get("batting_order", 7)
        their_pa = t.get("pa_per_game", 3.8)
        their_avg = t.get("avg", 0.250)

        # Lineup protection effect: hitter BEHIND the absent player
        # sees worse pitches (fewer strikes in zone, more walks to guy before)
        protection_effect = 0.0
        if their_order == batting_order + 1:
            # Directly behind absent player — loses protection
            protection_effect = -0.010  # ~10 points of batting avg
            obp_penalty = -0.015
        elif their_order == batting_order - 1:
            # Directly ahead — now has less protection behind them
            protection_effect = -0.005
            obp_penalty = -0.008
        else:
            obp_penalty = 0.0

        # PA change from lineup shift
        pa_change = 0.0
        if their_order > batting_order:
            # Everyone below moves up one slot = slightly more PA
            pa_change = 0.15
        elif their_order == batting_order:
            pa_change = 0.10  # replacement gets roughly same PAs

        results.append(UsageRedistribution(
            player=name,
            role=t.get("position", "UTIL"),
            usage_increase=round(pa_change / max(their_pa, 1) * 100, 1),
            projected_stat_change={
                "pa_change": round(pa_change, 2),
                "avg_change": round(protection_effect, 3),
                "obp_change": round(obp_penalty, 3),
                "lineup_slot_change": "Moves up 1" if their_order > batting_order else "No change",
            },
        ))

    results.sort(key=lambda r: r.usage_increase, reverse=True)
    return results


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
