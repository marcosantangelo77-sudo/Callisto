"""NFL/MLB player-impact quantification and usage redistribution.

Extracted from tools.injury.model. NBA impact helpers and the
player_impact / redistribute_usage dispatchers stay in the computation layer.
"""

from __future__ import annotations

import logging
from typing import Optional

from tools.injury.data import (
    MLB_PITCHER_TIERS,
    MLB_POSITION_IMPACT_CENTS,
    MLB_POSITION_TIERS,
    NFL_POSITION_IMPACT,
    NFL_TARGET_REDISTRIBUTION,
)

logger = logging.getLogger("callisto.injury_model")


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


# Imported last: tools.injury.model re-exports these names, so a top-level
# import from model would cycle while this module is still defining them.
from tools.injury.model import (  # noqa: E402
    PlayerImpactResult,
    UsageRedistribution,
)
