"""Sport-specific correlation matrices and market alias tables.

Extracted verbatim from tools/correlation.py.
"""

# ---------------------------------------------------------------------------
# Sport-specific correlation matrices
# ---------------------------------------------------------------------------
# Keys are tuples (market_a, market_b). Order doesn't matter — lookups check
# both directions. Values are Pearson correlation coefficients.
#
# Positive = both tend to move in the same direction
# Negative = one goes up when the other goes down
# Zero     = independent (book assumption is correct)

NFL_CORRELATIONS: dict[tuple[str, str], float] = {
    # QB passing volume correlates strongly with team scoring
    ("qb_passing_yards", "team_total"): 0.65,
    ("qb_passing_yards", "game_total"): 0.45,
    ("qb_passing_tds", "team_total"): 0.72,
    ("qb_passing_tds", "game_total"): 0.40,
    ("qb_passing_attempts", "team_total"): 0.35,
    ("qb_passing_yards", "qb_passing_tds"): 0.60,
    ("qb_passing_yards", "qb_completions"): 0.78,

    # WR/TE receiving flows through the QB
    ("wr_receiving_yards", "qb_passing_yards"): 0.55,
    ("wr_receiving_yards", "team_total"): 0.45,
    ("wr_receiving_yards", "game_total"): 0.30,
    ("wr_receptions", "qb_completions"): 0.50,
    ("wr_receptions", "qb_passing_yards"): 0.45,
    ("wr_receiving_tds", "qb_passing_tds"): 0.40,
    ("wr_receiving_tds", "team_total"): 0.38,
    ("te_receiving_yards", "qb_passing_yards"): 0.40,

    # RB rushing depends on game script — favorites run more when ahead
    ("rb_rushing_yards", "team_spread"): 0.45,  # positive when favored (negative spread)
    ("rb_rushing_yards", "team_total"): 0.30,
    ("rb_rushing_yards", "team_ml"): 0.40,
    ("rb_rushing_tds", "team_total"): 0.35,
    ("rb_rushing_tds", "team_spread"): 0.38,
    ("rb_rushing_attempts", "team_spread"): 0.50,  # favorites run clock
    ("rb_rushing_attempts", "rb_rushing_yards"): 0.72,

    # Game script correlations
    ("team_spread", "team_total"): 0.30,  # favorites in high-total games score more
    ("team_spread", "game_total"): 0.15,
    ("team_ml", "team_total"): 0.35,

    # Defensive / turnover correlations
    ("qb_interceptions", "opposing_team_total"): 0.25,  # picks lead to opponent points
    ("qb_interceptions", "team_total"): -0.30,  # picks reduce own team scoring
    ("def_sacks", "qb_passing_yards"): -0.35,  # more sacks = fewer passing yards
    ("def_sacks", "opposing_qb_passing_yards"): -0.35,

    # Kicker correlations
    ("kicker_points", "team_total"): 0.40,
    ("kicker_fg_made", "team_total"): 0.15,  # FGs inversely correlated with TDs

    # Anytime TD scorer correlations
    ("anytime_td", "team_total"): 0.42,
    ("anytime_td", "game_total"): 0.25,

    # Anti-correlations — legs that fight each other
    ("qb_passing_yards", "rb_rushing_yards"): -0.15,  # pass-heavy vs run-heavy scripts
    ("team_total", "opposing_team_total"): -0.10,  # blowouts suppress loser scoring
    ("qb_passing_yards", "opposing_qb_passing_yards"): 0.10,  # slight positive (game pace)
}

NBA_CORRELATIONS: dict[tuple[str, str], float] = {
    # Player scoring and team totals
    ("player_points", "team_total"): 0.50,
    ("player_points", "game_total"): 0.35,
    ("player_points", "team_ml"): 0.20,
    ("player_points", "team_spread"): 0.18,

    # Assists correlate with overall scoring environment
    ("player_assists", "game_total"): 0.40,
    ("player_assists", "team_total"): 0.45,
    ("player_assists", "player_points"): 0.35,  # high-usage players do both

    # Rebounds correlate with pace — more possessions = more misses = more boards
    ("player_rebounds", "game_pace"): 0.35,
    ("player_rebounds", "game_total"): 0.25,
    ("player_rebounds", "player_points"): 0.20,  # stars get both

    # Three-pointers
    ("player_threes", "player_points"): 0.55,
    ("player_threes", "team_total"): 0.30,
    ("player_threes", "game_total"): 0.20,

    # PRA (points + rebounds + assists) combos
    ("player_pra", "game_total"): 0.45,
    ("player_pra", "team_total"): 0.55,
    ("player_pra", "player_points"): 0.85,  # points dominate PRA
    ("player_pra", "player_assists"): 0.60,
    ("player_pra", "player_rebounds"): 0.55,

    # Game-level correlations
    ("team_spread", "team_total"): 0.35,
    ("team_ml", "team_total"): 0.40,
    ("game_total", "game_pace"): 0.60,

    # Blowout effects — starters sit in garbage time
    ("player_points", "game_spread_margin"): -0.15,  # blowout = less star minutes
    ("player_minutes", "game_spread_margin"): -0.25,

    # Same-team player correlations (roster context matters)
    ("teammate_a_points", "teammate_b_points"): 0.15,  # slightly positive (team scoring)
    ("player_points", "opposing_player_points"): 0.10,  # game pace effect

    # Steals / blocks (defensive stats)
    ("player_steals", "game_total"): 0.10,
    ("player_blocks", "game_total"): 0.05,
    ("player_steals", "player_points"): 0.15,  # active players do everything

    # Anti-correlations
    ("player_turnovers", "player_assists"): 0.30,  # high usage = both
    ("player_turnovers", "team_total"): -0.15,  # turnovers hurt scoring
}

MLB_CORRELATIONS: dict[tuple[str, str], float] = {
    # Batter performance and team totals
    ("batter_hits", "team_total"): 0.30,
    ("batter_total_bases", "team_total"): 0.40,
    ("batter_rbi", "team_total"): 0.55,
    ("batter_runs_scored", "team_total"): 0.50,
    ("batter_home_runs", "team_total"): 0.45,
    ("batter_home_runs", "game_total"): 0.25,
    ("batter_hits", "game_total"): 0.20,

    # Pitcher correlations
    ("pitcher_strikeouts", "pitcher_outs"): 0.65,  # deeper outings = more Ks
    ("pitcher_strikeouts", "opposing_team_total"): -0.25,  # more Ks = fewer runs
    ("pitcher_earned_runs", "opposing_team_total"): 0.60,  # direct relationship
    ("pitcher_earned_runs", "game_total"): 0.35,

    # Game-level
    ("team_spread", "team_total"): 0.35,
    ("team_ml", "team_total"): 0.40,
    ("game_total", "wind_speed"): 0.15,  # wind out = more runs (park-dependent)
    ("team_total", "opposing_pitcher_era"): 0.30,  # bad pitching = more runs

    # First 5 innings (F5) correlations
    ("f5_team_total", "team_total"): 0.65,
    ("f5_game_total", "game_total"): 0.60,
    ("f5_ml", "team_ml"): 0.80,

    # Anti-correlations
    ("pitcher_strikeouts", "batter_hits"): -0.20,  # same matchup opposition
    ("batter_stolen_bases", "team_total"): 0.10,  # weak correlation, small samples
}

NHL_CORRELATIONS: dict[tuple[str, str], float] = {
    # Skater correlations
    ("player_points_nhl", "team_total"): 0.40,
    ("player_goals", "team_total"): 0.45,
    ("player_assists_nhl", "team_total"): 0.35,
    ("player_shots_on_goal", "team_total"): 0.30,
    ("player_shots_on_goal", "player_goals"): 0.40,
    ("player_shots_on_goal", "game_total"): 0.20,

    # Goalie correlations
    ("goalie_saves", "opposing_team_total"): 0.15,  # more shots against = more saves but also more goals
    ("goalie_saves", "game_total"): 0.20,
    ("goalie_saves", "opposing_shots_on_goal"): 0.85,

    # Game-level
    ("team_spread", "team_total"): 0.30,
    ("team_ml", "team_total"): 0.35,
    ("game_total", "team_total"): 0.65,

    # Power play correlations
    ("power_play_points", "team_total"): 0.30,
    ("player_goals", "power_play_points"): 0.25,

    # Anti-correlations
    ("goalie_saves", "team_total"): -0.15,  # own team scoring less related to saves
    ("player_blocked_shots", "team_total"): -0.10,  # blocking = defending
}

# Registry for lookup
SPORT_CORRELATIONS: dict[str, dict[tuple[str, str], float]] = {
    "nfl": NFL_CORRELATIONS,
    "ncaaf": NFL_CORRELATIONS,  # same sport structure
    "nba": NBA_CORRELATIONS,
    "ncaab": NBA_CORRELATIONS,  # same sport structure
    "wnba": NBA_CORRELATIONS,
    "mlb": MLB_CORRELATIONS,
    "nhl": NHL_CORRELATIONS,
}

# Market aliases — normalize various naming conventions to canonical form
MARKET_ALIASES: dict[str, str] = {
    # NFL
    "passing_yards": "qb_passing_yards",
    "pass_yards": "qb_passing_yards",
    "passing_tds": "qb_passing_tds",
    "pass_tds": "qb_passing_tds",
    "pass_touchdowns": "qb_passing_tds",
    "passing_attempts": "qb_passing_attempts",
    "pass_attempts": "qb_passing_attempts",
    "completions": "qb_completions",
    "interceptions": "qb_interceptions",
    "rushing_yards": "rb_rushing_yards",
    "rush_yards": "rb_rushing_yards",
    "rushing_tds": "rb_rushing_tds",
    "rush_tds": "rb_rushing_tds",
    "rushing_attempts": "rb_rushing_attempts",
    "rush_attempts": "rb_rushing_attempts",
    "receiving_yards": "wr_receiving_yards",
    "rec_yards": "wr_receiving_yards",
    "receptions": "wr_receptions",
    "receiving_tds": "wr_receiving_tds",
    "rec_tds": "wr_receiving_tds",
    "sacks": "def_sacks",
    "fg_made": "kicker_fg_made",
    "field_goals": "kicker_fg_made",
    "kicker_pts": "kicker_points",
    "td_scorer": "anytime_td",
    "anytime_touchdown": "anytime_td",
    # NBA / basketball
    "points": "player_points",
    "rebounds": "player_rebounds",
    "assists": "player_assists",
    "threes": "player_threes",
    "three_pointers": "player_threes",
    "three_pointers_made": "player_threes",
    "pts_rebs_asts": "player_pra",
    "pra": "player_pra",
    "steals": "player_steals",
    "blocks": "player_blocks",
    "turnovers": "player_turnovers",
    "minutes": "player_minutes",
    # MLB
    "hits": "batter_hits",
    "total_bases": "batter_total_bases",
    "rbi": "batter_rbi",
    "rbis": "batter_rbi",
    "runs": "batter_runs_scored",
    "home_runs": "batter_home_runs",
    "hrs": "batter_home_runs",
    "strikeouts": "pitcher_strikeouts",
    "earned_runs": "pitcher_earned_runs",
    "pitcher_ks": "pitcher_strikeouts",
    "stolen_bases": "batter_stolen_bases",
    # NHL
    "goals": "player_goals",
    "shots": "player_shots_on_goal",
    "shots_on_goal": "player_shots_on_goal",
    "saves": "goalie_saves",
    "hockey_points": "player_points_nhl",
    "hockey_assists": "player_assists_nhl",
    "blocked_shots": "player_blocked_shots",
    # Game-level
    "spread": "team_spread",
    "moneyline": "team_ml",
    "ml": "team_ml",
    "total": "game_total",
    "over_under": "game_total",
    "team_over_under": "team_total",
    "pace": "game_pace",
}
