"""
Backtest engine I/O & filter helpers — extracted from tools/backtest.py.

Slice 3 of the god-module diet. The context-factor registries below remain
the canonical definitions (characterization-pinned here); the heavy logic
now lives in the tools.btio package:

  - tools.btio.filters       — hypothesis filter parsing, structured-filter
                               detection, context-coverage scoring
  - tools.btio.schedule      — schedule-context computation (DB-backed)
  - tools.btio.context_match — game-vs-context matching (fail closed),
                               needs-context checks
  - tools.btio.resolution    — bet resolution (resolve_line)
  - tools.btio.teams         — team-name normalization/matching (alias map)

This module re-exports every public name so existing callers
(tools/autonomous.py, tools/backtest.py, tests) are unaffected.
"""

logger = __import__("logging").getLogger("callisto.backtest")

from tools.btio.context_match import (
    _game_matches_context_filter,
    _needs_context_filter,
)
from tools.btio.filters import (
    _infer_context_needs,
    _log_unfilterable_context_factors,
    _parse_hypothesis_filters,
    compute_context_coverage,
    has_structured_filters,
    matches_hypothesis_conditions,
)
from tools.btio.resolution import resolve_line
from tools.btio.schedule import build_schedule_context
from tools.btio.teams import (
    _TEAM_ALIASES,
    _build_alias_map,
    _normalize_team,
    _team_matches,
)


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
