"""Player prop simulation."""

import logging

import numpy as np

from tools.sim.constants import DEFAULT_ITERATIONS
from tools.sim.models import PropSimResult

logger = logging.getLogger("callisto.simulation")


def simulate_prop(
    player_avg: float,
    matchup_factor: float = 1.0,
    pace_factor: float = 1.0,
    minutes: float = 32.0,
    n_sims: int = DEFAULT_ITERATIONS,
    player_name: str = "Unknown",
    stat: str = "points",
    std_ratio: float = 0.35,
) -> PropSimResult:
    """
    Simulate a player prop stat line using usage/pace context.

    The model:
      adjusted_avg = player_avg * matchup_factor * pace_factor * (minutes / baseline_minutes)
      per-game variance modeled as normal with std = adjusted_avg * std_ratio

    For counting stats (rebounds, assists, threes), we use a modified Poisson
    because these are discrete low-count events. For points, normal is fine
    because the central limit theorem holds at NBA scoring rates.

    Args:
        player_avg: Player's season average for this stat.
        matchup_factor: Multiplier for matchup difficulty (>1 = favorable, <1 = tough).
                        Example: 1.15 if opponent allows 15% more of this stat vs league avg.
        pace_factor: Multiplier for game pace context (>1 = faster pace expected).
        minutes: Expected minutes for this game.
        n_sims: Number of simulations.
        player_name: Player name for labeling.
        stat: Stat type (points, rebounds, assists, threes, etc.).
        std_ratio: Standard deviation as a fraction of the mean. Defaults to 0.35.
                   Higher for more volatile stats (threes ~0.50, assists ~0.40).

    Returns:
        PropSimResult with full distribution.
    """
    rng = np.random.default_rng()

    # Baseline minutes assumption (NBA default)
    baseline_minutes = 32.0
    minutes_factor = minutes / baseline_minutes if baseline_minutes > 0 else 1.0

    # Adjusted expected value
    adjusted_avg = player_avg * matchup_factor * pace_factor * minutes_factor
    adjusted_avg = max(0.1, adjusted_avg)

    # Choose distribution based on stat type and magnitude
    is_counting = stat.lower() in {"rebounds", "assists", "threes", "steals", "blocks",
                                    "turnovers", "fouls", "three_pointers"}
    is_low_count = adjusted_avg < 5.0

    if is_counting and is_low_count:
        # Modified Poisson for low-count discrete stats
        # Add overdispersion: sample lambda from gamma, then draw Poisson
        # This is a negative binomial, which handles the extra variance
        # in real player stat distributions
        shape = adjusted_avg / max(std_ratio, 0.1)  # controls overdispersion
        scale = std_ratio
        lambdas = rng.gamma(shape=shape, scale=scale, size=n_sims)
        values = rng.poisson(lam=np.maximum(0.01, lambdas))
    else:
        # Normal distribution for higher-count stats
        std = adjusted_avg * std_ratio
        values = rng.normal(loc=adjusted_avg, scale=max(std, 0.5), size=n_sims)
        values = np.maximum(0, np.round(values)).astype(int)

    values_list = values.tolist()

    # Percentiles
    pcts = {
        5: float(np.percentile(values, 5)),
        10: float(np.percentile(values, 10)),
        25: float(np.percentile(values, 25)),
        50: float(np.percentile(values, 50)),
        75: float(np.percentile(values, 75)),
        90: float(np.percentile(values, 90)),
        95: float(np.percentile(values, 95)),
    }

    # Over probabilities at common lines around the adjusted average
    over_probs = {}
    center = round(adjusted_avg * 2) / 2  # nearest 0.5
    for offset in np.arange(-8, 8.5, 0.5):
        line = center + offset
        if line < 0:
            continue
        over_count = int(np.sum(values > line))
        over_probs[float(line)] = round(over_count / n_sims, 4)

    result = PropSimResult(
        player=player_name,
        stat=stat,
        iterations=n_sims,
        mean=round(float(np.mean(values)), 2),
        median=round(float(np.median(values)), 2),
        std=round(float(np.std(values, ddof=1)), 2),
        values=values_list,
        percentiles=pcts,
        over_probs=over_probs,
    )

    logger.info(
        f"Prop sim: {player_name} {stat} | avg={player_avg}, adj={adjusted_avg:.1f}, "
        f"matchup={matchup_factor:.2f}, pace={pace_factor:.2f}, mins={minutes} | "
        f"sim mean={result.mean}, median={result.median}"
    )

    return result
