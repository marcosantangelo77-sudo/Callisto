"""Shared constants and sport classification for the simulation engine."""

# Default iterations — balance accuracy vs speed
DEFAULT_ITERATIONS = 10000

# Sport classification
HIGH_SCORING_SPORTS = {
    "basketball", "football",
    "basketball_nba", "basketball_ncaab", "basketball_euroleague",
    "americanfootball_nfl", "americanfootball_ncaaf",
}
LOW_SCORING_SPORTS = {
    "soccer", "hockey", "baseball",
    "soccer_epl", "soccer_germany_bundesliga", "soccer_spain_la_liga",
    "soccer_italy_serie_a", "soccer_france_ligue_one", "soccer_usa_mls",
    "icehockey_nhl", "baseball_mlb",
}

# Sport-specific scoring parameters (mean total, std dev of team score)
SPORT_DEFAULTS = {
    "basketball":      {"mean_total": 220, "team_std": 12.0, "home_adv": 3.0},
    "basketball_nba":  {"mean_total": 224, "team_std": 12.0, "home_adv": 3.0},
    "basketball_ncaab":{"mean_total": 140, "team_std": 10.0, "home_adv": 3.5},
    "football":        {"mean_total": 44,  "team_std": 10.0, "home_adv": 2.5},
    "americanfootball_nfl":  {"mean_total": 44, "team_std": 10.0, "home_adv": 2.5},
    "americanfootball_ncaaf":{"mean_total": 50, "team_std": 12.0, "home_adv": 3.0},
    "soccer":          {"mean_total": 2.6, "home_lambda": 1.45, "away_lambda": 1.15},
    "soccer_epl":      {"mean_total": 2.7, "home_lambda": 1.50, "away_lambda": 1.20},
    "hockey":          {"mean_total": 5.8, "home_lambda": 3.05, "away_lambda": 2.75},
    "icehockey_nhl":   {"mean_total": 6.1, "home_lambda": 3.20, "away_lambda": 2.90},
    "baseball":        {"mean_total": 8.6, "home_lambda": 4.50, "away_lambda": 4.10},
    "baseball_mlb":    {"mean_total": 8.6, "home_lambda": 4.50, "away_lambda": 4.10},
}


def classify_sport(sport: str) -> str:
    """Classify a sport key into 'high_scoring' or 'low_scoring'."""
    s = sport.lower().strip()
    if s in LOW_SCORING_SPORTS or any(s.startswith(p) for p in ("soccer", "ice", "baseball")):
        return "low_scoring"
    return "high_scoring"


# Backward-compatible private alias (imported as _classify_sport elsewhere)
_classify_sport = classify_sport
