"""
Backtest engine I/O & filter helpers — extracted from tools/backtest.py.

Slice 2 of the god-module diet. This module owns the logic that used to live
as BacktestEngine members:

  - context-factor registries (FILTERABLE / UNFILTERABLE, keyword map)
  - hypothesis filter parsing (line filters, side, home/away, dog/fav)
  - structured-filter detection and context-coverage scoring
  - game-vs-context matching (fail closed) and needs-context checks
  - schedule-context computation (DB-backed)
  - bet resolution (_resolve_line)
  - team-name normalization/matching (alias map)

BacktestEngine keeps thin delegating wrappers so existing callers
(tools/autonomous.py, tests) are unaffected.
"""

import re
from datetime import timedelta
from typing import Optional

logger = __import__("logging").getLogger("callisto.backtest")


# ── HYPOTHESIS-AWARE FILTERING ──
# Tier 1: Line-based filters (spread range, side, home/away)
# Tier 2: Contextual filters (weather, travel, etc.) — logged as unavailable

# Factors that CANNOT be applied as game-level filters during backtesting.
# Split into two groups:
#   - No data source exists (weather, pitcher, etc.)
#   - Data exists but filter code is NOT yet implemented (rest, pace, etc.)
# Both groups are treated as unfilterable. When filter code is written for
# a factor, remove it from this set and add to _matches_hypothesis_conditions.
UNFILTERABLE_CONTEXT_FACTORS = {
    # ── Derivable but NOT YET IMPLEMENTED ──
    # These have data in game_contexts / game_results / player_stats,
    # but no code maps them to event-level filters yet.
    # NOTE: days_rest, back_to_back, playoff_standing, revenge_game_flag,
    # schedule_context, etc. are NOW implemented — see FILTERABLE_CONTEXT_FACTORS
    # and _build_schedule_context() / _game_matches_context_filter().
    "starter_4q_minutes_prev",
    "home_pace_rank", "away_pace_rank", "pace_differential",
    "home_team_pace_rank", "away_team_pace_rank",
    "head_to_head_record", "opponent_record",
    "conference_tier",
    "team_identity", "school_identity",
    "seed_number",
    "hours_before_tip",
    "foul_rates", "foul_rate", "personal_fouls_per_game",
    "defensive_efficiency", "adjusted_defensive_efficiency",
    "tempo", "pace", "offensive_efficiency",
    "overtime_history", "prior_game_overtime",
    # schedule_context (sandwich/trap/letdown) — NOW FILTERABLE via _game_matches_context_filter
    "tournament_round",   # Sweet 16, Elite 8, etc. — no round detection from dates alone
    # ── No data source ──
    "weather", "temperature", "wind", "wind_speed", "wind_direction",
    "travel_distance", "timezone_crossing", "altitude",
    "venue_type",  # dome, indoor, outdoor, retractable roof
    "pitcher_history", "pitcher_velocity", "pitcher_workload",
    "pitcher_pitch_type",  # sinkerball, breaking ball, pitch mix
    "player_trade_recency", "player_impact_rating",
    "bullpen_status", "battery_composition",
    "spring_training_stats", "roster_composition",
    "first_inning_stats",
    "umpire_tendencies",  # hp umpire, zone width
    "coaching_staff",  # manager, coach, scheme changes
    "referee_crew", "referee_foul_tendency", "public_betting_pct",
    "handle_estimate", "line_movement_velocity", "line_movement_direction",
    "bye_week_flag", "bye_week_return", "primetime_flag", "game_slot",
    "thursday_game", "national_tv_flag",
    "last_10_possessions_per_game", "defensive_rating_slow_team",
    "season_avg_total", "pre_bye_scoring_trend_last_3",
    "first_half_total", "defensive_rank_both_teams",
    "postseason_stage",  # playoff round, series length, elimination game
    "pitcher_identity",  # starting pitcher name/matchup
    "schedule_type",  # interleague, opening day, etc.
}

# Keywords in thesis/name that imply game-level context filtering is needed.
# Maps keyword patterns → the unfilterable factor they represent.
_CONTEXT_KEYWORD_MAP = {
    r"\bdome\b": "venue_type",
    r"\bretractable.roof\b": "venue_type",
    r"\bindoor\b": "venue_type",
    r"\boutdoor\b": "venue_type",
    r"\bweather\b": "weather",
    r"\btemperature\b": "temperature",
    r"\bcold.weather\b": "temperature",
    r"\bwind\b": "wind",
    r"\bfastball.velo": "pitcher_velocity",
    r"\bvelo(city)?\b.*(drop|gain)": "pitcher_velocity",
    r"\bmph\b": "pitcher_velocity",
    r"\bpitch.count": "pitcher_workload",
    r"\bbullpen\b": "bullpen_status",
    r"\bthin.bullpen\b": "bullpen_status",
    r"\bopener\b.*inning": "bullpen_status",
    r"\bcatcher\b": "battery_composition",
    r"\bbattery\b": "battery_composition",
    r"\btravel\b": "travel_distance",
    r"\bwest.coast.*east|east.*west.coast": "timezone_crossing",
    r"\btimezone\b": "timezone_crossing",
    r"\bspring.training\b": "spring_training_stats",
    r"\bspring.era\b": "spring_training_stats",
    r"\bspring.*k/9\b": "spring_training_stats",
    r"\bspring.*\bip\b": "spring_training_stats",
    r"\bwhiff.rate\b": "spring_training_stats",
    r"\broster.turnover\b": "roster_composition",
    r"\bnew.lineup\b": "roster_composition",
    r"\bnew.team\b": "roster_composition",
    r"\b\d\+.new\b.*starter": "roster_composition",
    r"\boffseason\b.*acqui": "roster_composition",
    r"\bfree.agency\b": "roster_composition",
    r"\btrade\b": "roster_composition",
    r"\bnrfi\b": "first_inning_stats",
    r"\bfirst.inning\b": "first_inning_stats",
    r"\bdivision\b": "head_to_head_record",
    r"familiarity\b": "head_to_head_record",
    r"\brevenge\b": "head_to_head_record",
    r"\bformer.team\b": "head_to_head_record",
    r"\bpitcher\b": "pitcher_identity",
    r"\baces?\b.*first.*start": "pitcher_history",
    r"\bace\b.*starter": "pitcher_history",
    r"\bseason.debut\b": "pitcher_history",
    r"\bfirst.*career.*start": "pitcher_history",
    r"\bcareer.*debut": "pitcher_history",
    r"\bk/9\b": "pitcher_history",
    r"\bera\b.*under|under.*\bera\b": "pitcher_history",
    r"\bstrikeout": "pitcher_history",
    r"\bsinkerball\b": "pitcher_pitch_type",
    r"\bbreaking.ball\b": "pitcher_pitch_type",
    r"\bpitch.mix\b": "pitcher_pitch_type",
    r"\bcurveball\b": "pitcher_pitch_type",
    r"\bslider\b": "pitcher_pitch_type",
    r"\bchangeup\b": "pitcher_pitch_type",
    r"\bumpire\b": "umpire_tendencies",
    r"\bhp.umpire\b": "umpire_tendencies",
    r"\bwide.zone\b": "umpire_tendencies",
    r"\bstrike.zone\b": "umpire_tendencies",
    r"\bmanager\b": "coaching_staff",
    r"\bcoach\b": "coaching_staff",
    r"\bscheme\b": "coaching_staff",
    r"\bhbcu\b": "school_identity",
    r"\breligious\b": "school_identity",
    r"\bcohesion\b": "team_identity",
    r"\bidentity\b": "team_identity",
    r"\bcultural\b": "team_identity",
    # Playoff standing / motivation factors (NBA, NHL, MLB)
    r"\beliminated\b": "playoff_standing",
    r"\btanking\b": "playoff_standing",
    r"\bclinch": "playoff_standing",
    r"\bplayoff.race\b": "playoff_standing",
    r"\bplay.in\b": "playoff_standing",
    r"\bseed.locked\b": "playoff_standing",
    r"\bmagic.number\b": "playoff_standing",
    r"\bdesperate\b": "playoff_standing",
    r"\bmust.win\b": "playoff_standing",
    r"\bletdown\b": "schedule_context",
    r"\bsandwich\b": "schedule_context",
    r"\btrap.game\b": "schedule_context",
    r"\blook.ahead\b": "schedule_context",
    r"\boverlay\b": "schedule_context",
    # Rest / schedule factors
    r"\brest\b": "days_rest",
    r"\bb2b\b": "days_rest",
    r"\bback.to.back\b": "days_rest",
    r"\bdays.rest\b": "days_rest",
    r"\brest.mismatch\b": "days_rest",
    r"\bshort.rest\b": "days_rest",
    r"\bextra.rest\b": "extra_rest_days",
    r"\bbye\b": "bye_week_flag",
    # Closer / reliever patterns
    r"\bcloser\b": "bullpen_status",
    r"\breliever\b": "bullpen_status",
    r"\bsetup.man\b": "bullpen_status",
    # Venue / park factors
    r"\bpark.dim": "venue_type",
    r"\bpark.factor": "venue_type",
    r"\bfence.distance\b": "venue_type",
    # Pace / tempo factors
    r"\bpace\b": "pace",
    r"\btempo\b": "tempo",
    # Foul rate / defensive efficiency factors
    r"\bfoul.rat": "foul_rates",
    r"\bpersonal.foul": "foul_rates",
    r"\bfoul.prone\b": "foul_rates",
    r"\bhigh.foul": "foul_rates",
    r"\bdefensive.efficiency\b": "defensive_efficiency",
    r"\badjusted.defensive\b": "adjusted_defensive_efficiency",
    r"\bdefensive.rating\b": "defensive_efficiency",
    # Overtime / fatigue from prior game
    r"\bovertime\b.*fatigue\b": "overtime_history",
    r"\bovertime\b.*prior\b": "prior_game_overtime",
    r"\bovertime\b.*previous\b": "prior_game_overtime",
    r"\bclose.game.*intensity\b": "overtime_history",
    # Schedule / matchup type factors — season timing, date-based filters
    r"\binterleague\b": "schedule_type",
    r"\bopening.day\b": "schedule_type",
    r"\bopener\b": "schedule_type",
    r"\bopening.week\b": "schedule_type",
    r"\bopening.series\b": "schedule_type",
    r"\bearly.season\b": "schedule_type",
    r"\bfirst.week\b": "schedule_type",
    r"\bfirst.series\b": "schedule_type",
    r"\bfirst.\d+.games?\b": "schedule_type",
    r"\bseason.open": "schedule_type",
    r"\bday.game.*night|night.*day.game\b": "schedule_type",
    r"\bday.after.night\b": "schedule_type",
    r"\bapril\b": "schedule_type",
    r"\bsp.rust\b": "schedule_type",
    r"\bpitcher.rust\b": "schedule_type",
    # Specific venue names
    r"\bcoors\b": "venue_type",
    r"\bfenway\b": "venue_type",
    r"\bwrigley\b": "venue_type",
    r"\byankee.stadium\b": "venue_type",
    # Lineup / starter identity
    r"\bstarting.pitcher\b": "pitcher_identity",
    r"\b(?:sp|ace)\b.*\bpitcher\b": "pitcher_identity",
    # Tournament round / stage (NCAA, playoffs)
    r"\bsweet.16\b": "tournament_round",
    r"\belite.8\b": "tournament_round",
    r"\bfinal.four\b": "tournament_round",
    r"\bround.of.\d+\b": "tournament_round",
    r"\btournament\b": "tournament_round",
    r"\bmarch.madness\b": "tournament_round",
    r"\bncaa.*round\b": "tournament_round",
    r"\bsurviv\w*\b.*seed": "tournament_round",
    # Seed matchups
    r"\b\d+.seed\b": "seed_number",
    r"\blower.seed\b": "seed_number",
    r"\bhigher.seed\b": "seed_number",
    r"\bseed.matchup\b": "seed_number",
    r"\bseed.*vs\b": "seed_number",
    r"\bcinderella\b": "seed_number",
    # Postseason / playoff stage
    r"\bplayoff.round\b": "postseason_stage",
    r"\bfirst.round.*playoff": "postseason_stage",
    r"\bsecond.round.*playoff": "postseason_stage",
    r"\belimination.game\b": "postseason_stage",
    r"\bgame.[567]\b": "postseason_stage",
    r"\bseries.length\b": "postseason_stage",
}

def has_structured_filters(config: dict) -> bool:
    """Check if hypothesis has line_filters or game_filters that differentiate event sets.

    When structured filters are present, the hypothesis can meaningfully
    differentiate games even without context factor filtering. This prevents
    the keyword-based context inference from blocking otherwise testable hypotheses.
    """
    lf = config.get("line_filters") or {}
    gf = config.get("game_filters") or {}
    return bool(
        any(v for v in lf.values() if v)
        or any(v is not None for v in gf.values())
    )

def _infer_context_needs(thesis: str, name: str) -> list[str]:
    """Infer unfilterable context factors from thesis/name when context_factors is empty.

    Returns list of inferred context needs, or empty list if the hypothesis
    appears to be purely line-based (no game-context filtering needed).

    Only returns factors that are in UNFILTERABLE_CONTEXT_FACTORS — keywords
    that map to derivable/filterable factors should not block backtesting.
    """
    # Replace underscores/hyphens with spaces so \b word boundaries match
    # hypothesis names like "sp_dome_to_cold" (where _ is a word char in regex)
    text = f"{thesis} {name}".lower().replace("_", " ").replace("-", " ")
    inferred = set()
    for pattern, factor in _CONTEXT_KEYWORD_MAP.items():
        if re.search(pattern, text):
            # Only block if the factor is truly unfilterable
            if factor in UNFILTERABLE_CONTEXT_FACTORS:
                inferred.add(factor)
    return sorted(inferred)

def _parse_hypothesis_filters(thesis: str, config: dict, hypothesis_id: str = "") -> dict:
    """Parse hypothesis thesis text, model_config, and name to extract line-based filters.

    Returns a dict of filters that can be applied to game lines:
        side_filter: "Over", "Under", or None — only evaluate this side
        spread_range: (min, max) or None — only test spreads in this range
        spread_min: float or None — only test spreads >= this value
        home_away_filter: "home", "away", or None — only test this side
        dog_fav_filter: "underdog", "favorite", or None — only test this role
    """
    filters = {}
    thesis_lower = thesis.lower() if thesis else ""
    h_id_lower = hypothesis_id.lower() if hypothesis_id else ""

    # 0. STRUCTURED LINE FILTERS from model_config (highest priority)
    # These are machine-readable specs generated alongside the hypothesis.
    # When present, use them directly and skip all regex parsing.
    lf = config.get("line_filters") or {}
    if lf:
        if lf.get("home_away") in ("home", "away"):
            filters["home_away_filter"] = lf["home_away"]
        if lf.get("dog_fav") in ("underdog", "favorite"):
            filters["dog_fav_filter"] = lf["dog_fav"]
        if lf.get("side") in ("Over", "Under"):
            filters["side_filter"] = lf["side"]
        if lf.get("spread_range") and isinstance(lf["spread_range"], (list, tuple)):
            lo, hi = float(lf["spread_range"][0]), float(lf["spread_range"][1])
            if lo > hi:
                lo, hi = hi, lo
            filters["spread_range"] = (lo, hi)
        if lf.get("spread_min") is not None:
            filters["spread_min"] = float(lf["spread_min"])
        if filters:
            logger.info(
                f"Line filters from structured spec for {hypothesis_id}: {filters}"
            )
            return filters  # Structured filters are authoritative

    # 1. Side filter from model_config (most reliable — explicitly set)
    side_filter = config.get("side_filter")
    if side_filter:
        filters["side_filter"] = side_filter  # "Over" or "Under"
    elif config.get("market_type") == "totals" or "total" in thesis_lower:
        # Parse from thesis: if thesis is about "under" or "over" specifically
        # Be careful: "underdog" contains "under" but is not a side filter
        # Look for "under" as a side-of-total context, not "underdog"
        thesis_words = re.split(r'[\s,;.!?()]+', thesis_lower)
        has_under_side = ("under" in thesis_words or "unders" in thesis_words
                         or "under-" in thesis_lower
                         or re.search(r'\bunder\b(?!dog)', thesis_lower))
        has_over_side = ("over" in thesis_words or "overs" in thesis_words
                        or "over-" in thesis_lower
                        or re.search(r'\bover\b(?!reaction|priced|weight|all|val)', thesis_lower))
        # Only set side filter if thesis is clearly about ONE side
        if has_under_side and not has_over_side:
            filters["side_filter"] = "Under"
        elif has_over_side and not has_under_side:
            filters["side_filter"] = "Over"

    # 1b. Fallback: extract side from hypothesis NAME if thesis parsing missed it.
    # Names like "mlb_opener_bullpen_game_total_over" or "mlb_new_manager_total_under"
    # encode the predicted direction. Only use as fallback when thesis didn't yield a side.
    if "side_filter" not in filters and h_id_lower:
        is_totals = config.get("market_type") == "totals" or "total" in h_id_lower
        if is_totals:
            # Check name suffix/segments for over/under direction
            name_parts = h_id_lower.replace("-", "_").split("_")
            name_has_over = "over" in name_parts or "overs" in name_parts
            name_has_under = "under" in name_parts or "unders" in name_parts
            if name_has_over and not name_has_under:
                filters["side_filter"] = "Over"
                logger.info(f"Side filter 'Over' inferred from hypothesis name: {hypothesis_id}")
            elif name_has_under and not name_has_over:
                filters["side_filter"] = "Under"
                logger.info(f"Side filter 'Under' inferred from hypothesis name: {hypothesis_id}")

    # 2. Spread range from model_config or thesis
    spread_range = config.get("spread_range")
    if spread_range and isinstance(spread_range, (list, tuple)) and len(spread_range) == 2:
        filters["spread_range"] = (float(spread_range[0]), float(spread_range[1]))
    else:
        # Parse "X-Y points" from thesis — only when describing game selection criteria
        # e.g., "underdogs of 3-7 points" or "favorites by 3-7 points"
        # NOT "moving the line 0.5-1.5 points past fair value" (line movement context)
        range_match = re.search(
            r'(?:of|by|between|within|spread(?:s)?[\s:]+)'
            r'\s*(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*point',
            thesis_lower,
        )
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low > high:
                low, high = high, low
            # Sanity: spread ranges should be in a reasonable range (1-20)
            if 1 <= low and high <= 25:
                filters["spread_range"] = (low, high)

    # 3. Spread minimum from thesis ("X+ points", "of X+ points")
    if "spread_range" not in filters:
        # Only match when in a game-selection context, not line movement
        min_match = re.search(
            r'(?:of|by|at least|minimum|spread(?:s)?[\s:]+)'
            r'\s*(\d+(?:\.\d+)?)\+?\s*point',
            thesis_lower,
        )
        if min_match:
            val = float(min_match.group(1))
            # Sanity: only treat as spread minimum if it looks like a spread value (1-20)
            if 1 <= val <= 20:
                filters["spread_min"] = val

    # 4. Home/away filter — from thesis text
    if re.search(r'\bhome\s+(underdog|dog|team|favorite|side|advantage)', thesis_lower):
        filters["home_away_filter"] = "home"
    elif re.search(r'\b(?:road|away|visitor|visiting)\s+(underdog|dog|team|favorite|side|value)', thesis_lower):
        filters["home_away_filter"] = "away"
    elif re.search(r'\baway\s+(underdog|dog|team|favorite)', thesis_lower):
        filters["home_away_filter"] = "away"

    # 4b. Fallback: extract home/away from hypothesis NAME.
    # Names like "mlb_opening_week_road_favorites_h2h" or "nba_home_underdog_ats"
    if "home_away_filter" not in filters and h_id_lower:
        name_parts = h_id_lower.replace("-", "_").split("_")
        if any(kw in name_parts for kw in ("road", "away", "visitor", "visiting")):
            filters["home_away_filter"] = "away"
            logger.info(f"home_away_filter 'away' inferred from hypothesis name: {hypothesis_id}")
        elif "home" in name_parts:
            filters["home_away_filter"] = "home"
            logger.info(f"home_away_filter 'home' inferred from hypothesis name: {hypothesis_id}")

    # 5. Underdog/favorite filter — from thesis text
    if re.search(r'\bunderdog', thesis_lower) and not re.search(r'\bfavorite', thesis_lower):
        filters["dog_fav_filter"] = "underdog"
    elif re.search(r'\bfavorite', thesis_lower) and not re.search(r'\bunderdog', thesis_lower):
        filters["dog_fav_filter"] = "favorite"

    # 5b. Fallback: extract dog/fav from hypothesis NAME if thesis didn't yield it.
    # Names like "mlb_opening_week_underdog_ml" or "mlb_road_favorite_mispricing"
    # encode the predicted direction.
    if "dog_fav_filter" not in filters and h_id_lower:
        name_parts = set(h_id_lower.replace("-", "_").split("_"))
        has_dog = bool(name_parts & {
            "underdog", "dog", "underdogs", "undervalued", "upset",
        })
        has_fav = bool(name_parts & {
            "favorite", "favorites", "fav", "chalk",
        })
        if has_dog and not has_fav:
            filters["dog_fav_filter"] = "underdog"
            logger.info(f"dog_fav_filter 'underdog' inferred from hypothesis name: {hypothesis_id}")
        elif has_fav and not has_dog:
            filters["dog_fav_filter"] = "favorite"
            logger.info(f"dog_fav_filter 'favorite' inferred from hypothesis name: {hypothesis_id}")

    # 6. Thesis direction — detect bearish hypotheses (bet AGAINST filtered side)
    # E.g., "nba_heavy_favorite_ml_overpriced" → bet the underdog, not the favorite.
    # When bearish + a directional filter exists, flip the filter so we
    # evaluate and record the CORRECT bet side.
    #
    # IMPORTANT: Only use the hypothesis NAME for bearish detection, not the
    # thesis text. The name encodes intent (what we bet on), while the thesis
    # explains the phenomenon. A thesis like "underdogs have value because
    # favorites are overpriced" is BULLISH on underdogs — detecting "overpriced"
    # in the thesis would falsely trigger a bearish flip.
    bearish_name = False
    if h_id_lower:
        name_parts = set(h_id_lower.replace("-", "_").split("_"))
        bearish_name = bool(name_parts & {
            "overpriced", "overvalued", "inflated",
            "fade", "fading",
        })

    if bearish_name:
        flipped = False
        if "dog_fav_filter" in filters:
            old = filters["dog_fav_filter"]
            filters["dog_fav_filter"] = (
                "underdog" if old == "favorite" else "favorite"
            )
            flipped = True
            logger.info(
                "Bearish thesis detected for %s: flipped dog_fav_filter "
                "%s → %s", hypothesis_id, old, filters["dog_fav_filter"],
            )
        if "home_away_filter" in filters:
            old = filters["home_away_filter"]
            filters["home_away_filter"] = (
                "away" if old == "home" else "home"
            )
            flipped = True
            logger.info(
                "Bearish thesis detected for %s: flipped home_away_filter "
                "%s → %s", hypothesis_id, old, filters["home_away_filter"],
            )
        if flipped:
            filters["_bearish_flip"] = True

    return filters

def matches_hypothesis_conditions(
    side_name: str,
    market_type: str,
    point: Optional[float],
    home_team: str,
    away_team: str,
    filters: dict,
    fair_prob: Optional[float] = None,
) -> bool:
    """Check if a specific line/side matches the hypothesis conditions.

    Args:
        side_name: The side being evaluated (team name, "Over", "Under")
        market_type: "spreads", "totals", "h2h"
        point: The line value (spread number, total number)
        home_team: Home team name
        away_team: Away team name
        filters: Pre-parsed filters from _parse_hypothesis_filters()
        fair_prob: Devigged fair probability for this side (used for
            h2h favorite/underdog detection where no spread line exists)

    Returns True if this line should be processed, False to skip.
    """
    # 1. Side filter (Over/Under for totals) — check even when filters dict
    # is empty, because totals should default to single-side if possible
    side_filter = filters.get("side_filter") if filters else None
    if market_type == "totals":
        if side_filter:
            if side_name.lower() != side_filter.lower():
                return False
        # No side filter on a totals hypothesis = both sides processed.
        # This is acceptable for generic edge detection but will be flagged
        # in backtest metadata as "unfiltered_totals_side".

    if not filters:
        # No line-based filters parsed — process all lines for this game.
        # WARNING: This means the hypothesis has no directional filtering.
        # For generic cross-book edge detection this is acceptable, but for
        # directional hypotheses (favorite/underdog/home/away) this is a bug
        # in _parse_hypothesis_filters that causes identical event sets.
        # We still return True here but callers should check filter coverage.
        return True

    # 2. Spread range filter
    spread_range = filters.get("spread_range")
    if spread_range and market_type == "spreads" and point is not None:
        abs_spread = abs(point)
        low, high = spread_range
        if abs_spread < low or abs_spread > high:
            return False

    # 3. Spread minimum filter
    spread_min = filters.get("spread_min")
    if spread_min is not None and market_type == "spreads" and point is not None:
        if abs(point) < spread_min:
            return False

    # 4. Home/away filter
    home_away = filters.get("home_away_filter")
    if home_away and market_type in ("spreads", "h2h"):
        is_home_side = _team_matches(side_name, home_team)
        is_away_side = _team_matches(side_name, away_team)
        if home_away == "home" and not is_home_side:
            return False
        if home_away == "away" and not is_away_side:
            return False

    # 5. Underdog/favorite filter
    # For spreads: negative line = favorite, positive = underdog
    # For h2h: fair_prob > 0.5 = favorite, < 0.5 = underdog
    dog_fav = filters.get("dog_fav_filter")
    if dog_fav:
        if market_type == "spreads" and point is not None:
            is_underdog = point > 0
            is_favorite = point < 0
            if dog_fav == "underdog" and not is_underdog:
                return False
            if dog_fav == "favorite" and not is_favorite:
                return False
        elif market_type == "h2h" and fair_prob is not None:
            is_favorite = fair_prob > 0.5
            is_underdog = fair_prob < 0.5
            if dog_fav == "underdog" and not is_underdog:
                return False
            if dog_fav == "favorite" and not is_favorite:
                return False

    return True

def _log_unfilterable_context_factors(hypothesis_id: str, config: dict) -> list[str]:
    """Check for context factors we cannot filter on and log them.

    Returns list of unfilterable factors for inclusion in backtest metadata.
    """
    context_factors = config.get("context_factors", [])
    if not context_factors:
        return []

    unfilterable = [
        f for f in context_factors
        if f.lower().replace(" ", "_") in UNFILTERABLE_CONTEXT_FACTORS
        or f.lower() in UNFILTERABLE_CONTEXT_FACTORS
    ]

    if unfilterable:
        logger.warning(
            f"Hypothesis {hypothesis_id} requires context_factors "
            f"{unfilterable} which are not yet available — running "
            f"unfiltered backtest for those conditions (results will be noisy)."
        )

    return unfilterable

def compute_context_coverage(config: dict) -> float:
    """Calculate what fraction of a hypothesis's context conditions can be filtered.

    Returns 1.0 if no context factors needed (pure line-based hypothesis),
    0.0 if ALL context factors are unfilterable (backtest is meaningless),
    or a value between 0-1 indicating partial coverage.

    Hypotheses with context_coverage < 0.5 should NOT be backtested — the
    results will be indistinguishable from random since most game-selection
    conditions cannot be applied.
    """
    context_factors = config.get("context_factors", [])
    if not context_factors:
        return 1.0  # No context needed — pure line-based, fully filterable

    # WHITELIST logic: only factors with actual filtering code count as filterable.
    # Previously used blacklist (UNFILTERABLE), but unknown factors like
    # "season_week", "park_type" slipped through as falsely "filterable".
    filterable_count = sum(
        1 for f in context_factors
        if f.lower().replace(" ", "_") in FILTERABLE_CONTEXT_FACTORS
        or f.lower() in FILTERABLE_CONTEXT_FACTORS
    )

    return filterable_count / len(context_factors)

# ── SCHEDULE CONTEXT COMPUTATION ──
# Derive game-level context from game_results so contextual filters
# (b2b, days_rest, road_trip, sandwich, clinched, revenge) can actually
# filter games instead of being no-ops.

# Factors that ARE now filterable via schedule context.
# When adding a new derivable factor: implement it in _build_schedule_context,
# add matching logic in _game_matches_context_filter, and list it here.
FILTERABLE_CONTEXT_FACTORS = {
    "days_rest", "days_since_last_game", "extra_rest_days",
    "back_to_back", "is_b2b_second_night", "back_to_back_second_night",
    "both_teams_short_rest", "opponent_days_rest",
    "consecutive_road_games", "road_trip_game_number",
    "schedule_density", "games_in_last_4_days", "schedule_context",
    "revenge_game_flag", "is_revenge_game",
    "prev_game_margin", "divisional_matchup",
    "playoff_standing",
}

async def build_schedule_context(
    db,
    sport: str, start_date: str, end_date: str,
    live_games: list[tuple[str, str, str]] | None = None,
) -> dict:
    """Pre-compute schedule context for all games in a date range.

    Args:
        live_games: Optional list of (game_date, home_team, away_team) for
            upcoming games not yet in game_results (e.g. today's live odds).
            Context will be computed for these using historical team data.

    Returns dict keyed by (game_date, home_team, away_team) with context:
        home_days_rest / away_days_rest: int
        home_b2b / away_b2b: bool — team played yesterday
        home_road_streak / away_road_streak: int — consecutive away games
        home_games_in_4 / away_games_in_4: int — schedule density
        home_prev_margin / away_prev_margin: float
        is_revenge: bool — teams played recently
        home_sandwich / away_sandwich: bool — game squeezed between two others
        home_win_pct / away_win_pct: float — season record approximation
    """
    from datetime import datetime as dt

    buffer_start = dt.strptime(start_date, "%Y-%m-%d") - timedelta(days=30)
    buffer_start_str = buffer_start.strftime("%Y-%m-%d")

    rows = await db.execute_fetchall(
        """SELECT game_date, home_team, away_team, home_score, away_score,
                  total_score, spread_result, winner
           FROM game_results
           WHERE sport = ? AND game_date >= ? AND game_date <= ?
           ORDER BY game_date""",
        (sport, buffer_start_str, end_date),
    )

    if not rows:
        return {}

    # Build per-team game lists
    team_games: dict[str, list] = {}
    for r in rows:
        gd, home, away, hs, as_, ts, sr, winner = r
        hs = hs or 0
        as_ = as_ or 0
        home_margin = hs - as_
        team_games.setdefault(home, []).append(
            (gd, away, True, home_margin, winner)
        )
        team_games.setdefault(away, []).append(
            (gd, home, False, -home_margin, winner)
        )

    for t in team_games:
        team_games[t].sort(key=lambda x: x[0])

    context = {}
    for r in rows:
        gd, home, away = r[0], r[1], r[2]
        if gd < start_date:
            continue

        ctx: dict = {}
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            opp = away if prefix == "home" else home
            is_home_side = prefix == "home"
            idx = None
            for i, g in enumerate(tg):
                if g[0] == gd and g[2] == is_home_side and g[1] == opp:
                    idx = i
                    break
            if idx is None:
                ctx[f"{prefix}_days_rest"] = 99
                ctx[f"{prefix}_b2b"] = False
                ctx[f"{prefix}_road_streak"] = 0
                ctx[f"{prefix}_games_in_4"] = 1
                ctx[f"{prefix}_prev_margin"] = 0.0
                continue

            # Days rest
            if idx > 0:
                prev_date = tg[idx - 1][0]
                d1 = dt.strptime(gd, "%Y-%m-%d")
                d0 = dt.strptime(prev_date, "%Y-%m-%d")
                days_rest = (d1 - d0).days
                prev_margin = tg[idx - 1][3]
            else:
                days_rest = 99
                prev_margin = 0.0

            ctx[f"{prefix}_days_rest"] = days_rest
            ctx[f"{prefix}_b2b"] = (days_rest == 1)
            ctx[f"{prefix}_prev_margin"] = prev_margin

            # Road streak
            road_streak = 0
            if not is_home_side:
                for j in range(idx, -1, -1):
                    if not tg[j][2]:
                        road_streak += 1
                    else:
                        break
            else:
                for j in range(idx - 1, -1, -1):
                    if not tg[j][2]:
                        road_streak += 1
                    else:
                        break
            ctx[f"{prefix}_road_streak"] = road_streak

            # Games in last 4 days (schedule density)
            game_dt = dt.strptime(gd, "%Y-%m-%d")
            four_days_ago = (game_dt - timedelta(days=4)).strftime("%Y-%m-%d")
            games_in_4 = sum(1 for g in tg if four_days_ago < g[0] <= gd)
            ctx[f"{prefix}_games_in_4"] = games_in_4

        # Revenge game: teams played in last 30 days
        home_games = team_games.get(home, [])
        ctx["is_revenge"] = any(
            g[1] == away and g[0] < gd and g[0] >= buffer_start_str
            for g in home_games
        )

        # Sandwich game: game within 2 days before AND within 2 days after
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            game_dt = dt.strptime(gd, "%Y-%m-%d")
            has_prev_close = any(
                0 < (game_dt - dt.strptime(g[0], "%Y-%m-%d")).days <= 2
                for g in tg if g[0] < gd
            )
            has_next_close = any(
                0 < (dt.strptime(g[0], "%Y-%m-%d") - game_dt).days <= 2
                for g in tg if g[0] > gd
            )
            ctx[f"{prefix}_sandwich"] = has_prev_close and has_next_close

        # Team records for playoff standing approximation
        for team, prefix in [(home, "home"), (away, "away")]:
            tg = team_games.get(team, [])
            wins = sum(1 for g in tg if g[0] < gd and g[4] == team)
            losses = sum(1 for g in tg if g[0] < gd and g[4] and g[4] != team)
            ctx[f"{prefix}_wins"] = wins
            ctx[f"{prefix}_losses"] = losses
            total = wins + losses
            ctx[f"{prefix}_win_pct"] = wins / total if total > 0 else 0.5

        context[(gd, home, away)] = ctx

    # ── Augment with live/upcoming games not yet in game_results ──
    # Paper trading needs context for today's games, which haven't been
    # played yet and so aren't in game_results.  Compute their schedule
    # factors from the same team_games history.
    if live_games:
        added = 0
        for lg_date, lg_home, lg_away in live_games:
            key = (lg_date, lg_home, lg_away)
            if key in context:
                continue  # already computed from game_results
            ctx = {}
            for team, prefix, is_home_side in [
                (lg_home, "home", True),
                (lg_away, "away", False),
            ]:
                tg = team_games.get(team, [])
                # Find most recent game before lg_date
                prev = [g for g in tg if g[0] < lg_date]
                if prev:
                    last = prev[-1]
                    d1 = dt.strptime(lg_date, "%Y-%m-%d")
                    d0 = dt.strptime(last[0], "%Y-%m-%d")
                    days_rest = (d1 - d0).days
                    prev_margin = last[3]
                else:
                    days_rest = 99
                    prev_margin = 0.0
                ctx[f"{prefix}_days_rest"] = days_rest
                ctx[f"{prefix}_b2b"] = (days_rest == 1)
                ctx[f"{prefix}_prev_margin"] = prev_margin

                # Road streak
                road_streak = 0
                for g in reversed(prev):
                    if not g[2]:  # away game
                        road_streak += 1
                    else:
                        break
                ctx[f"{prefix}_road_streak"] = road_streak

                # Games in last 4 days
                game_dt_live = dt.strptime(lg_date, "%Y-%m-%d")
                four_days_ago = (game_dt_live - timedelta(days=4)).strftime("%Y-%m-%d")
                games_in_4 = sum(1 for g in tg if four_days_ago < g[0] <= lg_date)
                ctx[f"{prefix}_games_in_4"] = max(games_in_4, 1)

                # Win pct from all prior games
                wins = sum(1 for g in tg if g[0] < lg_date and g[4] == team)
                losses = sum(1 for g in tg if g[0] < lg_date and g[4] and g[4] != team)
                ctx[f"{prefix}_wins"] = wins
                ctx[f"{prefix}_losses"] = losses
                total = wins + losses
                ctx[f"{prefix}_win_pct"] = wins / total if total > 0 else 0.5

            # Revenge game
            home_prev = [g for g in team_games.get(lg_home, []) if g[0] < lg_date]
            ctx["is_revenge"] = any(
                g[1] == lg_away and g[0] >= buffer_start_str for g in home_prev
            )

            # Sandwich game
            for team, prefix in [(lg_home, "home"), (lg_away, "away")]:
                tg = team_games.get(team, [])
                game_dt_live = dt.strptime(lg_date, "%Y-%m-%d")
                has_prev_close = any(
                    0 < (game_dt_live - dt.strptime(g[0], "%Y-%m-%d")).days <= 2
                    for g in tg if g[0] < lg_date
                )
                has_next_close = any(
                    0 < (dt.strptime(g[0], "%Y-%m-%d") - game_dt_live).days <= 2
                    for g in tg if g[0] > lg_date
                )
                ctx[f"{prefix}_sandwich"] = has_prev_close and has_next_close

            context[key] = ctx
            added += 1
        if added:
            logger.info(
                f"Schedule context: augmented with {added} live games "
                f"(total now {len(context)})"
            )

    logger.info(
        f"Schedule context: computed for {len(context)} games "
        f"({sport}, {start_date} to {end_date})"
    )
    return context

def _game_matches_context_filter(
    game_context: dict,
    hypothesis_name: str,
    thesis: str,
    config: dict,
) -> bool:
    """Check if a game matches the hypothesis's contextual requirements.

    Uses hypothesis name, thesis text, and config.context_factors to determine
    what context conditions are needed, then checks them against the pre-computed
    game context.

    Returns True if the game should be processed, False to skip.
    """
    name_lower = hypothesis_name.lower().replace("-", " ").replace("_", " ")
    thesis_lower = (thesis or "").lower()
    text = f"{name_lower} {thesis_lower}"
    context_factors = config.get("context_factors", [])
    cf_set = {f.lower().replace(" ", "_") for f in context_factors}

    if not game_context:
        return False  # Context filtering expected but no data — fail closed

    # ── STRUCTURED GAME FILTERS (from model_config — highest priority) ──
    # These are machine-readable specs generated alongside the hypothesis,
    # not reverse-engineered from natural language.  When present they are
    # authoritative; the regex fallbacks below only fire for legacy
    # hypotheses that lack structured filters.
    gf = config.get("game_filters") or {}
    if gf:
        gf_side = gf.get("side")  # "home", "away", or None

        # ── Game-level filters (not team-specific) ──
        if "min_rest_mismatch" in gf:
            hr = game_context.get("home_days_rest", 1)
            ar = game_context.get("away_days_rest", 1)
            if abs(hr - ar) < gf["min_rest_mismatch"]:
                return False

        if gf.get("require_revenge"):
            if not game_context.get("is_revenge"):
                return False

        # ── Team-specific filters: conjunctive per-team ──
        # When gf_side is set, only that side is checked.
        # When gf_side is None, ALL team-specific conditions must be
        # satisfied by the SAME team.  Previous OR-per-condition logic
        # allowed home to pass one filter and away another, producing
        # identical event sets across hypotheses with different theses.
        candidates = {gf_side} if gf_side else {"home", "away"}

        if gf.get("require_b2b"):
            candidates = {s for s in candidates if game_context.get(f"{s}_b2b")}
            if not candidates:
                return False

        if "max_rest_days" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_days_rest", 99) <= gf["max_rest_days"]}
            if not candidates:
                return False

        if "min_games_in_4" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_games_in_4", 1) >= gf["min_games_in_4"]}
            if not candidates:
                return False

        if "require_road_streak" in gf:
            threshold = gf["require_road_streak"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_road_streak", 0) >= threshold}
            if not candidates:
                return False

        if gf.get("require_sandwich"):
            candidates = {s for s in candidates if game_context.get(f"{s}_sandwich")}
            if not candidates:
                return False

        if "min_win_pct" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_win_pct", 0.5) >= gf["min_win_pct"]}
            if not candidates:
                return False

        if "max_win_pct" in gf:
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_win_pct", 0.5) <= gf["max_win_pct"]}
            if not candidates:
                return False

        if "win_pct_range" in gf:
            lo, hi = gf["win_pct_range"]
            candidates = {s for s in candidates
                          if lo <= game_context.get(f"{s}_win_pct", 0.5) <= hi}
            if not candidates:
                return False

        if "max_prev_margin" in gf:
            threshold = gf["max_prev_margin"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_prev_margin", 0) <= threshold}
            if not candidates:
                return False

        if "min_prev_margin" in gf:
            threshold = gf["min_prev_margin"]
            candidates = {s for s in candidates
                          if game_context.get(f"{s}_prev_margin", 0) >= threshold}
            if not candidates:
                return False

        # Structured filters are authoritative — skip regex fallbacks
        return True

    # ── REGEX FALLBACKS (for hypotheses without structured filters) ──
    # Regex matching infers context filters from hypothesis keywords
    # (sandwich, revenge, blowout, etc.). However, two hypotheses sharing
    # the same keyword (e.g., both containing "revenge") will match the
    # SAME game_context field and produce identical event sets.
    #
    # Guard: require explicit context_factors to use regex fallbacks.
    # Hypotheses without context_factors get 0 events and are rejected
    # for insufficient data — better than corrupted identical event sets.
    if not cf_set:
        return False

    _any_filter_matched = False

    # ── Back-to-back filter ──
    if ("back_to_back" in cf_set or "is_b2b_second_night" in cf_set
            or "back_to_back_second_night" in cf_set
            or re.search(r"\bb2b\b|\bback.to.back\b", text)):
        _any_filter_matched = True
        if not game_context.get("home_b2b") and not game_context.get("away_b2b"):
            return False

    # ── Days rest filter ──
    if ("days_rest" in cf_set or "days_since_last_game" in cf_set
            or re.search(r"\bshort.rest\b|\brest.mismatch\b|\bdays?.rest\b", text)):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 99)
        away_rest = game_context.get("away_days_rest", 99)
        if home_rest > 2 and away_rest > 2:
            return False

    # ── Extra rest filter ──
    if "extra_rest_days" in cf_set or re.search(r"\bextra.rest\b", text):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 1)
        away_rest = game_context.get("away_days_rest", 1)
        if home_rest < 3 and away_rest < 3:
            return False

    # ── Road trip filter ──
    if ("consecutive_road_games" in cf_set or "road_trip_game_number" in cf_set
            or re.search(r"\broad.trip\b|\b\d\+?\s*(?:road|away)\b|\bconsecutive.(?:road|away)\b", text)):
        _any_filter_matched = True
        threshold = 3
        m = re.search(r"(\d)\+?\s*(?:road|away)", text)
        if m:
            threshold = int(m.group(1))
        away_streak = game_context.get("away_road_streak", 0)
        home_road_before = game_context.get("home_road_streak", 0)
        if away_streak < threshold and home_road_before < threshold:
            return False

    # ── Schedule density (3in4, 4in5) filter ──
    if ("schedule_density" in cf_set or "games_in_last_4_days" in cf_set
            or re.search(r"\b3.?in.?4\b|\b4.?in.?5\b|\bschedule.compress\b|\bschedule.density\b", text)):
        _any_filter_matched = True
        home_g4 = game_context.get("home_games_in_4", 1)
        away_g4 = game_context.get("away_games_in_4", 1)
        if home_g4 < 3 and away_g4 < 3:
            return False

    # ── Sandwich game filter ──
    if ("schedule_context" in cf_set
            or re.search(r"\bsandwich\b|\btrap.game\b|\bletdown\b", text)):
        _any_filter_matched = True
        if not game_context.get("home_sandwich") and not game_context.get("away_sandwich"):
            return False

    # ── Revenge game filter ──
    if ("revenge_game_flag" in cf_set or "is_revenge_game" in cf_set
            or re.search(r"\brevenge\b|\bformer.team\b", text)):
        _any_filter_matched = True
        if not game_context.get("is_revenge"):
            return False

    # ── Playoff standing / clinched / eliminated / bubble filter ──
    if ("playoff_standing" in cf_set
            or re.search(r"\bclinch|\beliminated\b|\btanking\b|\bplayoff.(?:race|bubble)\b|\bdesperate\b|\bbubble\b|\bmust.win\b", text)):
        _any_filter_matched = True
        home_wp = game_context.get("home_win_pct", 0.5)
        away_wp = game_context.get("away_win_pct", 0.5)

        if re.search(r"\bclinch", text):
            # 65%+ win pct = likely clinched (top ~6 teams per conference)
            # Previous 60% was too loose — captured mid-tier teams
            if home_wp < 0.65 and away_wp < 0.65:
                return False
        elif re.search(r"\beliminated\b|\btanking\b", text):
            # 35%- win pct = likely eliminated/tanking
            # Previous 40% was too loose — captured mediocre teams
            if home_wp > 0.35 and away_wp > 0.35:
                return False
        elif re.search(r"\bbubble\b|\bdesperate\b|\bmust.win\b|\bplayoff.race\b", text):
            # Bubble/desperate = at least one team in tight playoff fight
            # Narrowed from 40-60% to 43-57% to exclude comfortable mid-table
            if not (0.43 <= home_wp <= 0.57 or 0.43 <= away_wp <= 0.57):
                return False

    # ── Both teams short rest filter ──
    if "both_teams_short_rest" in cf_set:
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 99)
        away_rest = game_context.get("away_days_rest", 99)
        if home_rest > 1 or away_rest > 1:
            return False

    # ── Rest mismatch filter ──
    if ("rest_mismatch" in cf_set
            or re.search(r"\brest.(?:mismatch|differential|advantage|edge)\b|\bfresh.vs.tired\b", text)):
        _any_filter_matched = True
        home_rest = game_context.get("home_days_rest", 1)
        away_rest = game_context.get("away_days_rest", 1)
        # Extract mismatch threshold from text (e.g., "2+ day rest mismatch")
        mm = re.search(r"(\d)\+?\s*(?:day)?\s*rest", text)
        threshold = int(mm.group(1)) if mm else 2
        if abs(home_rest - away_rest) < threshold:
            return False

    # ── Bad loss / blowout / bounce filter (using prev_margin) ──
    if (re.search(r"\bbad.loss\b|\bblowout(?!.win)\b|\bblown.(?:out|lead)\b|\bbounce\b"
                   r"|\bhangover\b|\bafter.(?:bad|ugly|blowout)", text)):
        _any_filter_matched = True
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team lost their previous game badly (margin < -10)
        if hpm > -10 and apm > -10:
            return False

    # ── Winning streak / dominant win filter (using prev_margin) ──
    if re.search(r"\bwinning.streak\b|\bblowout.win\b|\bdomin\w+.win\b|\bmomentum\b", text):
        _any_filter_matched = True
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team won their previous game convincingly (margin > 10)
        if hpm < 10 and apm < 10:
            return False

    # ── Losing team / struggling team filter ──
    if re.search(r"\blosing.streak\b|\bstruggling\b|\bslumping\b|\bskid\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        hpm = game_context.get("home_prev_margin", 0)
        apm = game_context.get("away_prev_margin", 0)
        # At least one team is losing AND lost their previous game
        if not ((hwp < 0.45 and hpm < 0) or (awp < 0.45 and apm < 0)):
            return False

    # ── Generic streak filter (bare "streak" without winning/losing qualifier) ──
    if not _any_filter_matched and re.search(r"\bstreak\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        # At least one team has a notably non-average record (on a streak)
        if not (hwp >= 0.58 or hwp <= 0.42 or awp >= 0.58 or awp <= 0.42):
            return False

    # ── Home stand filter ──
    if not _any_filter_matched and re.search(r"\bhome.?stand\b", text):
        _any_filter_matched = True
        # Home stand = home team not on road trip + playing frequently
        home_road = game_context.get("home_road_streak", 0)
        home_g4 = game_context.get("home_games_in_4", 1)
        # Home team must have 0 consecutive road games and 2+ games in 4 days
        if home_road > 0 or home_g4 < 2:
            return False

    # ── Favorite/underdog/dominant/narrative filters ──
    # These patterns exist in _needs_context_filter but previously had no
    # corresponding game-level filter, causing fail-closed (0 events).
    # Use win_pct as proxy: favorites ~55%+, underdogs ~45%-, dominant ~60%+.
    if not _any_filter_matched and re.search(r"\bfavorite\b", text):
        _any_filter_matched = True
        # At least one team must be a clear favorite (high win%)
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp < 0.55 and awp < 0.55:
            return False

    if not _any_filter_matched and re.search(r"\bunderdog\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp > 0.45 and awp > 0.45:
            return False

    if not _any_filter_matched and re.search(r"\bdominant\b", text):
        _any_filter_matched = True
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if hwp < 0.60 and awp < 0.60:
            return False

    if not _any_filter_matched and re.search(r"\bnarrative\b", text):
        _any_filter_matched = True
        # Narrative games = high-profile matchups with extreme records
        hwp = game_context.get("home_win_pct", 0.5)
        awp = game_context.get("away_win_pct", 0.5)
        if not (hwp >= 0.58 or hwp <= 0.42 or awp >= 0.58 or awp <= 0.42):
            return False

    # ── Non-game-filterable conditions (fail-closed) ──
    # These patterns describe market-level or venue-level conditions that
    # cannot be evaluated from schedule context. Previously these set
    # _any_filter_matched=True and passed ALL games through, causing the
    # "164 identical events" bug (DW#230). Now they return False so
    # hypotheses with these conditions get 0 events and are rejected for
    # insufficient data. The gate in autonomous.py _phase_evaluate() also
    # prevents new hypotheses with these conditions from entering backtesting
    # without structured game_filters.
    _unfilterable_patterns = [
        r"\baltitud|\belev\w+|\bdenver\b|\bmile.high\b|\bcoors\b",        # venue
        r"\bpacific\b|\beastern\b|\bcentral\b|\btime.?zone\b|\bearly.tip\b|\blate.tip\b",  # timezone
        r"\bclosing.line\b|\bline.?value\b|\bclv\b|\bline.?move\b",       # market dynamics
        r"\bsharp\b|\bsteam\b|\breversal\b|\brlm\b",                      # market signals
        r"\bsecond.half\b|\bfirst.half\b|\bhalf.time\b",                  # market segment
    ]
    for pat in _unfilterable_patterns:
        if not _any_filter_matched and re.search(pat, text):
            # Can't filter at game level — fail closed to prevent identical event sets
            return False

    if not _any_filter_matched:
        # No regex pattern matched the hypothesis text — we can't verify the
        # hypothesis condition for this game.  Fail closed to prevent all
        # games leaking through unfiltered (the "149 identical events" bug).
        return False

    return True

def _needs_context_filter(hypothesis_name: str, thesis: str, config: dict) -> bool:
    """Quick check: does this hypothesis need game-level context filtering?

    Returns True if the hypothesis references any schedule-derivable context
    factor in its name, thesis, or context_factors config, OR has structured
    game_filters that require schedule context to evaluate.
    """
    # Structured game_filters are authoritative — always require context
    if config.get("game_filters"):
        return True

    context_factors = config.get("context_factors", [])
    cf_set = {f.lower().replace(" ", "_") for f in context_factors}
    if cf_set & FILTERABLE_CONTEXT_FACTORS:
        return True

    text = f"{hypothesis_name} {thesis or ''}".lower().replace("_", " ").replace("-", " ")
    schedule_patterns = [
        r"\bb2b\b", r"\bback.to.back\b", r"\bdays?.rest\b", r"\bshort.rest\b",
        r"\broad.trip\b", r"\bconsecutive.(?:road|away)\b",
        r"\b3.?in.?4\b", r"\b4.?in.?5\b", r"\bschedule.(?:compress|density)\b",
        r"\bsandwich\b", r"\btrap.game\b",
        r"\brevenge\b", r"\bformer.team\b",
        r"\bclinch", r"\beliminated\b", r"\btanking\b", r"\bplayoff.(?:race|bubble)\b",
        r"\bdesperate\b", r"\bmust.win\b",
        r"\bextra.rest\b", r"\brest.mismatch\b",
        r"\bhomestand\b", r"\bhome.stand\b", r"\bwinning.streak\b", r"\blosing.streak\b",
        r"\bwin.pct\b", r"\bwin.rate\b",
        # Venue/environment context
        r"\baltitud", r"\belev\w+", r"\bdenver\b", r"\bmile.high\b",
        # Time/timezone context
        r"\bpacific\b", r"\beastern\b", r"\bcentral\b", r"\btime.?zone\b", r"\bearly.tip\b", r"\blate.tip\b",
        # Line movement / closing line context
        r"\bclosing.line\b", r"\bline.?value\b", r"\bclv\b", r"\bline.?move\b",
        # Sharp money / steam context
        r"\bsharp\b", r"\bsteam\b", r"\breversal\b", r"\brlm\b",
        # Half-specific context
        r"\bsecond.half\b", r"\bfirst.half\b", r"\bhalf.time\b",
    ]
    return any(re.search(p, text) for p in schedule_patterns)

def resolve_line(
    market: str,
    side: str,
    line: Optional[float],
    home_score: int,
    away_score: int,
    home_team: str,
    away_team: str,
) -> Optional[str]:
    """Determine if a bet won, lost, or pushed."""
    total = home_score + away_score
    margin = home_score - away_score

    # Use fuzzy matching for side identification — side names from Odds API
    # may differ from game_results team names
    is_home = _team_matches(side, home_team)
    is_away = _team_matches(side, away_team)

    if market == "h2h":
        if is_home:
            return "won" if margin > 0 else "lost" if margin < 0 else "push"
        elif is_away:
            return "won" if margin < 0 else "lost" if margin > 0 else "push"
        return None

    if market == "spreads" and line is not None:
        # side is the team name, line is their spread
        if is_home:
            adjusted = margin + line
        else:
            adjusted = -margin + line

        if adjusted > 0:
            return "won"
        elif adjusted < 0:
            return "lost"
        return "push"

    if market == "totals" and line is not None:
        if side.lower() == "over":
            if total > line:
                return "won"
            elif total < line:
                return "lost"
            return "push"
        elif side.lower() == "under":
            if total < line:
                return "won"
            elif total > line:
                return "lost"
            return "push"

    return None

# Canonical team alias map — maps any known variation to a single key.
# Covers MLB, NBA, NFL, NHL. Keys are lowercase.
_TEAM_ALIASES: dict[str, str] = {}

def _build_alias_map() -> dict[str, str]:
    """Build a comprehensive alias -> canonical name mapping."""
    # Each entry: canonical name -> list of known aliases
    teams = {
        # ── MLB ──
        "arizona diamondbacks": ["az diamondbacks", "ari diamondbacks", "d-backs", "dbacks"],
        "atlanta braves": ["atl braves"],
        "baltimore orioles": ["bal orioles", "balt orioles"],
        "boston red sox": ["bos red sox", "redsox"],
        "chicago cubs": ["chi cubs", "chc cubs"],
        "chicago white sox": ["chi white sox", "chw white sox", "chi sox", "whitesox"],
        "cincinnati reds": ["cin reds", "cincy reds"],
        "cleveland guardians": ["cle guardians", "cleveland indians", "cle indians"],
        "colorado rockies": ["col rockies", "colo rockies"],
        "detroit tigers": ["det tigers"],
        "houston astros": ["hou astros"],
        "kansas city royals": ["kc royals"],
        "los angeles angels": ["la angels", "anaheim angels", "laa angels", "angels"],
        "los angeles dodgers": ["la dodgers", "lad dodgers"],
        "miami marlins": ["mia marlins", "fla marlins", "florida marlins"],
        "milwaukee brewers": ["mil brewers"],
        "minnesota twins": ["min twins"],
        "new york mets": ["ny mets", "nym mets"],
        "new york yankees": ["ny yankees", "nyy yankees"],
        "athletics": ["oakland athletics", "oakland a's", "oak athletics", "a's", "as"],
        "philadelphia phillies": ["phi phillies", "philly phillies", "phl phillies"],
        "pittsburgh pirates": ["pit pirates", "pitt pirates"],
        "san diego padres": ["sd padres"],
        "san francisco giants": ["sf giants"],
        "seattle mariners": ["sea mariners"],
        "st. louis cardinals": ["stl cardinals", "st louis cardinals", "saint louis cardinals"],
        "tampa bay rays": ["tb rays"],
        "texas rangers": ["tex rangers"],
        "toronto blue jays": ["tor blue jays", "blue jays"],
        "washington nationals": ["was nationals", "wsh nationals", "nats"],
        # ── NBA ──
        "atlanta hawks": ["atl hawks"],
        "boston celtics": ["bos celtics"],
        "brooklyn nets": ["bkn nets", "bk nets"],
        "charlotte hornets": ["cha hornets", "char hornets"],
        "chicago bulls": ["chi bulls"],
        "cleveland cavaliers": ["cle cavaliers", "cle cavs", "cavs"],
        "dallas mavericks": ["dal mavericks", "dal mavs", "mavs"],
        "denver nuggets": ["den nuggets"],
        "detroit pistons": ["det pistons"],
        "golden state warriors": ["gs warriors", "gsw warriors"],
        "houston rockets": ["hou rockets"],
        "indiana pacers": ["ind pacers"],
        "los angeles clippers": ["la clippers", "lac clippers"],
        "los angeles lakers": ["la lakers", "lal lakers"],
        "memphis grizzlies": ["mem grizzlies"],
        "miami heat": ["mia heat"],
        "milwaukee bucks": ["mil bucks"],
        "minnesota timberwolves": ["min timberwolves", "min wolves", "t-wolves"],
        "new orleans pelicans": ["no pelicans", "nop pelicans", "nola pelicans"],
        "new york knicks": ["ny knicks", "nyk knicks"],
        "oklahoma city thunder": ["okc thunder"],
        "orlando magic": ["orl magic"],
        "philadelphia 76ers": ["phi 76ers", "philly 76ers", "philadelphia sixers", "phi sixers", "sixers"],
        "phoenix suns": ["phx suns"],
        "portland trail blazers": ["por trail blazers", "portland blazers", "por blazers", "blazers"],
        "sacramento kings": ["sac kings"],
        "san antonio spurs": ["sa spurs"],
        "toronto raptors": ["tor raptors"],
        "utah jazz": ["uta jazz"],
        "washington wizards": ["was wizards", "wsh wizards"],
        # ── NFL ──
        "arizona cardinals": ["az cardinals", "ari cardinals"],
        "atlanta falcons": ["atl falcons"],
        "baltimore ravens": ["bal ravens", "balt ravens"],
        "buffalo bills": ["buf bills"],
        "carolina panthers": ["car panthers"],
        "chicago bears": ["chi bears"],
        "cincinnati bengals": ["cin bengals", "cincy bengals"],
        "cleveland browns": ["cle browns"],
        "dallas cowboys": ["dal cowboys"],
        "denver broncos": ["den broncos"],
        "detroit lions": ["det lions"],
        "green bay packers": ["gb packers"],
        "houston texans": ["hou texans"],
        "indianapolis colts": ["ind colts", "indy colts"],
        "jacksonville jaguars": ["jax jaguars", "jac jaguars"],
        "kansas city chiefs": ["kc chiefs"],
        "las vegas raiders": ["lv raiders", "oakland raiders", "oak raiders"],
        "los angeles chargers": ["la chargers", "lac chargers", "san diego chargers", "sd chargers"],
        "los angeles rams": ["la rams", "lar rams", "st. louis rams", "stl rams"],
        "miami dolphins": ["mia dolphins"],
        "minnesota vikings": ["min vikings"],
        "new england patriots": ["ne patriots", "nep patriots", "pats"],
        "new orleans saints": ["no saints", "nola saints"],
        "new york giants": ["ny giants", "nyg giants"],
        "new york jets": ["ny jets", "nyj jets"],
        "philadelphia eagles": ["phi eagles", "philly eagles"],
        "pittsburgh steelers": ["pit steelers", "pitt steelers"],
        "san francisco 49ers": ["sf 49ers", "niners"],
        "seattle seahawks": ["sea seahawks"],
        "tampa bay buccaneers": ["tb buccaneers", "tb bucs", "bucs"],
        "tennessee titans": ["ten titans"],
        "washington commanders": ["was commanders", "wsh commanders", "washington football team"],
        # ── NHL ──
        "anaheim ducks": ["ana ducks"],
        "boston bruins": ["bos bruins"],
        "buffalo sabres": ["buf sabres"],
        "calgary flames": ["cgy flames", "cal flames"],
        "carolina hurricanes": ["car hurricanes", "canes"],
        "chicago blackhawks": ["chi blackhawks"],
        "colorado avalanche": ["col avalanche", "avs"],
        "columbus blue jackets": ["cbj blue jackets", "blue jackets"],
        "dallas stars": ["dal stars"],
        "detroit red wings": ["det red wings"],
        "edmonton oilers": ["edm oilers"],
        "florida panthers": ["fla panthers"],
        "los angeles kings": ["la kings", "lak kings"],
        "minnesota wild": ["min wild"],
        "montreal canadiens": ["mtl canadiens", "canadiens", "habs"],
        "nashville predators": ["nsh predators", "nas predators", "preds"],
        "new jersey devils": ["nj devils", "njd devils"],
        "new york islanders": ["ny islanders", "nyi islanders"],
        "new york rangers": ["ny rangers", "nyr rangers"],
        "ottawa senators": ["ott senators", "sens"],
        "philadelphia flyers": ["phi flyers", "philly flyers"],
        "pittsburgh penguins": ["pit penguins", "pitt penguins", "pens"],
        "san jose sharks": ["sj sharks"],
        "seattle kraken": ["sea kraken"],
        "st. louis blues": ["stl blues", "st louis blues", "saint louis blues"],
        "tampa bay lightning": ["tb lightning", "tbl lightning", "bolts"],
        "toronto maple leafs": ["tor maple leafs", "leafs"],
        "utah mammoth": ["uta mammoth", "utah hockey club", "utah hc"],
        "vancouver canucks": ["van canucks"],
        "vegas golden knights": ["vgk golden knights", "vegas knights", "golden knights"],
        "washington capitals": ["was capitals", "wsh capitals", "caps"],
        "winnipeg jets": ["wpg jets"],
    }

    alias_map: dict[str, str] = {}
    for canonical, aliases in teams.items():
        alias_map[canonical] = canonical
        for alias in aliases:
            alias_map[alias] = canonical
    return alias_map

def _normalize_team(name: str) -> str:
    """Normalize team name for fuzzy matching across data sources.

    Uses a canonical alias map for exact lookups, then falls back to
    city-abbreviation replacement for unknown names.

    Handles differences between Odds API names (e.g. "Los Angeles Dodgers")
    and ESPN names (e.g. "LA Dodgers", "Athletics", etc.).
    """
    if not name:
        return ""
    n = name.strip().lower()
    # Remove trailing periods from abbreviations (e.g. "St." -> "st")
    n = " ".join(n.split())

    # Build alias map once (lazy singleton).
    # Build into a local first, then atomically assign — this guarantees
    # readers never see a partially-built dict (race-safe with GIL).
    alias_map = _TEAM_ALIASES
    if not alias_map:
        alias_map = _build_alias_map()
        _TEAM_ALIASES.clear()
        _TEAM_ALIASES.update(alias_map)

    # Direct alias lookup
    if n in alias_map:
        return alias_map[n]

    # Fallback: city abbreviation replacement for unknown names
    city_replacements = {
        "los angeles": "la",
        "new york": "ny",
        "san francisco": "sf",
        "san antonio": "sa",
        "san diego": "sd",
        "golden state": "gs",
        "oklahoma city": "okc",
        "portland trail blazers": "portland blazers",
        "brooklyn": "bkn",
        "saint louis": "st. louis",
        "st louis": "st. louis",
    }
    for full, abbrev in city_replacements.items():
        if n.startswith(full):
            n = abbrev + n[len(full):]
            break
    n = " ".join(n.split())
    return n

def _team_matches(name_a: str, name_b: str) -> bool:
    """Check if two team names refer to the same team.

    Uses canonical alias resolution first, then falls back to
    mascot matching and substring containment.
    """
    if not name_a or not name_b:
        return False
    if name_a == name_b:
        return True

    a = _normalize_team(name_a)
    b = _normalize_team(name_b)

    if a == b:
        return True

    # Last word (mascot) match — "LA Dodgers" vs "Los Angeles Dodgers"
    # Only match if mascot has 4+ chars to avoid false positives
    a_last = a.rsplit(None, 1)[-1] if a else ""
    b_last = b.rsplit(None, 1)[-1] if b else ""
    if a_last == b_last and len(a_last) > 3:
        return True

    # Substring: "Athletics" matches "Oakland Athletics" or "Athletics"
    if len(a) > 3 and len(b) > 3 and (a in b or b in a):
        return True

    return False
