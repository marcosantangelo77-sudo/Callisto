"""Pace-model & environment enhanced simulation."""

import logging

from tools.sim.constants import DEFAULT_ITERATIONS, classify_sport
from tools.sim.game import simulate_game
from tools.sim.models import SimulationResult

logger = logging.getLogger("callisto.simulation")

# Sport key -> pace_model.Sport mapping
_PACE_SPORT_MAP = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "soccer",
    "soccer_germany_bundesliga": "soccer",
    "soccer_spain_la_liga": "soccer",
    "soccer_italy_serie_a": "soccer",
    "soccer_france_ligue_one": "soccer",
    "soccer_usa_mls": "soccer",
}


def simulate_game_with_pace_env(
    home_power: float,
    away_power: float,
    sport: str = "basketball_nba",
    n_sims: int = DEFAULT_ITERATIONS,
    home_advantage: float = None,
    venue_team: str = None,
    weather_data: dict = None,
    refs: list[str] = None,
    home_pace: float = None,
    away_pace: float = None,
    home_off_eff: float = None,
    away_off_eff: float = None,
    home_def_eff: float = None,
    away_def_eff: float = None,
) -> SimulationResult:
    """
    Enhanced game simulation that integrates pace model projections and
    environment adjustments into the Monte Carlo engine.

    When pace model data is available, it uses pace x efficiency to derive
    more accurate power ratings. When environment data is available (venue,
    weather, refs), it adjusts the simulation parameters accordingly.

    Falls back to the standard simulate_game() when pace/env data is absent.

    Args:
        home_power: Base home team power rating.
        away_power: Base away team power rating.
        sport: Sport key.
        n_sims: Monte Carlo iterations.
        home_advantage: Override home advantage.
        venue_team: Home team abbreviation for venue/environment lookup.
        weather_data: Weather conditions dict.
        refs: Referee names for tendency adjustments.
        home_pace / away_pace: Team pace values for pace model.
        home_off_eff / away_off_eff: Offensive efficiency values.
        home_def_eff / away_def_eff: Defensive efficiency values.

    Returns:
        SimulationResult with environment metadata attached.
    """
    env_adjustment = 0.0
    env_detail = None
    pace_projection = None

    # --- Environment adjustment ---
    if venue_team:
        pace_sport = _PACE_SPORT_MAP.get(sport.lower())
        env_sport_code = (pace_sport or "").upper()
        if env_sport_code:
            try:
                from tools.environment import total_environment_adjustment
                env_result = total_environment_adjustment(
                    venue=venue_team,
                    sport=env_sport_code,
                    weather=weather_data,
                    refs=refs,
                )
                env_adjustment = env_result.get("total_adj", 0.0)
                env_detail = env_result
                logger.info(
                    f"Simulation env adjustment for {venue_team} ({env_sport_code}): "
                    f"{env_adjustment:+.1f} pts"
                )
            except Exception as e:
                logger.debug(f"Environment adjustment failed in simulation: {e}")

    # --- Pace model power rating override ---
    pace_sport = _PACE_SPORT_MAP.get(sport.lower())
    if (pace_sport and home_pace is not None and away_pace is not None
            and home_off_eff is not None and away_off_eff is not None
            and home_def_eff is not None and away_def_eff is not None):
        try:
            from tools.pace_model import project_game_total, LEAGUE_DEFAULTS, Sport
            sport_enum = Sport(pace_sport)
            defaults = LEAGUE_DEFAULTS.get(sport_enum, {})

            if sport_enum == Sport.NBA:
                league_avg_pace = defaults.get("pace", 100.0)
            elif sport_enum == Sport.NFL:
                league_avg_pace = defaults.get("plays_per_game", 64.0)
            elif sport_enum == Sport.MLB:
                league_avg_pace = defaults.get("runs_per_game", 4.5)
            elif sport_enum == Sport.NHL:
                league_avg_pace = defaults.get("shots_per_game", 30.0)
            elif sport_enum == Sport.SOCCER:
                league_avg_pace = defaults.get("shots_per_game", 12.0)
            else:
                league_avg_pace = home_pace  # fallback

            projection = project_game_total(
                home_pace=home_pace,
                away_pace=away_pace,
                home_off_eff=home_off_eff,
                away_off_eff=away_off_eff,
                home_def_eff=home_def_eff,
                away_def_eff=away_def_eff,
                league_avg_pace=league_avg_pace,
                sport=pace_sport,
            )
            pace_projection = projection

            # Override power ratings with pace model projections
            home_power = projection.home_projected
            away_power = projection.away_projected
            # Set home_advantage to 0 since pace model already includes it
            home_advantage = 0.0

            logger.info(
                f"Pace model overriding sim powers: home={home_power:.1f}, "
                f"away={away_power:.1f}, total={projection.projected_total:.1f}"
            )
        except Exception as e:
            logger.debug(f"Pace model projection failed in simulation: {e}")

    # Apply environment adjustment to power ratings (split evenly)
    if env_adjustment != 0:
        classification = classify_sport(sport)
        if classification == "low_scoring":
            # For low-scoring: split proportionally
            total_base = home_power + away_power
            if total_base > 0:
                home_power += env_adjustment * (home_power / total_base)
                away_power += env_adjustment * (away_power / total_base)
        else:
            # For high-scoring: split evenly
            home_power += env_adjustment / 2.0
            away_power += env_adjustment / 2.0

    # Run the base simulation with adjusted parameters
    sim = simulate_game(
        home_power=home_power,
        away_power=away_power,
        sport=sport,
        n_sims=n_sims,
        home_advantage=home_advantage,
    )

    # Attach pace/env metadata to the result for downstream consumers
    # (Using a simple dict attribute since SimulationResult is a dataclass)
    if not hasattr(sim, '_pace_env_meta'):
        object.__setattr__(sim, '_pace_env_meta', {})
    sim._pace_env_meta = {
        "environment_adjustment": round(env_adjustment, 2),
        "environment_detail": env_detail,
        "pace_projection": {
            "projected_total": pace_projection.projected_total,
            "pace_factor": pace_projection.pace_factor,
            "methodology": pace_projection.methodology,
        } if pace_projection else None,
    }

    return sim
