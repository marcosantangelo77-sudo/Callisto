"""
Learned correlations — online Welford algorithm for empirical correlation estimates.

Replaces hardcoded Pearson values with data-driven estimates as observations
accumulate. Uses Bayesian shrinkage: starts with hardcoded priors, converges
to observed correlations as sample size grows.

Welford's algorithm provides numerically stable online updates for mean,
variance, and covariance — no need to store all observations.

Blending formula:
    weight = min(1.0, n / 100)  # Full data weight at n=100 observations
    rho = (1 - weight) * prior + weight * learned
    If learned CI width > 0.3 (too uncertain), fall back entirely to prior.
"""

import logging
import math
import os
from dataclasses import dataclass
from typing import Optional

import aiosqlite

logger = logging.getLogger("callisto.learned_correlations")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Minimum observations before learned estimate is used in blending
MIN_OBSERVATIONS_FOR_BLEND = 10
# Full data weight achieved at this many observations
FULL_WEIGHT_AT_N = 100
# Max CI width before falling back to prior
MAX_CI_WIDTH = 0.3


@dataclass
class CorrelationEstimate:
    """Running correlation estimate between two markets."""
    sport: str
    market_a: str
    market_b: str
    n: int
    mean_a: float
    mean_b: float
    m2_a: float       # sum of squared deviations for a
    m2_b: float       # sum of squared deviations for b
    co_moment: float   # sum of cross-deviations
    pearson_r: float
    ci_low: float
    ci_high: float


class LearnedCorrelationStore:
    """SQLite-backed online correlation learner."""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        self._cache: dict[tuple[str, str, str], CorrelationEstimate] = {}

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        await self._db.execute("PRAGMA busy_timeout = 10000")
        # Load existing estimates into cache
        cursor = await self._db.execute(
            "SELECT sport, market_a, market_b, n, mean_a, mean_b, "
            "m2_a, m2_b, co_moment, pearson_r, ci_low, ci_high "
            "FROM learned_correlations"
        )
        for row in await cursor.fetchall():
            est = CorrelationEstimate(*row)
            key = (est.sport, est.market_a, est.market_b)
            self._cache[key] = est
        logger.info(f"Loaded {len(self._cache)} learned correlation estimates")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    async def update(
        self,
        sport: str,
        market_a: str,
        market_b: str,
        value_a: float,
        value_b: float,
    ) -> CorrelationEstimate:
        """
        Update running correlation estimate with a new observation.

        Uses Welford's online algorithm for numerically stable computation
        of mean, variance, and covariance in a single pass.
        """
        key = (sport, market_a, market_b)
        est = self._cache.get(key)

        if est is None:
            est = CorrelationEstimate(
                sport=sport, market_a=market_a, market_b=market_b,
                n=0, mean_a=0, mean_b=0, m2_a=0, m2_b=0,
                co_moment=0, pearson_r=0, ci_low=-1, ci_high=1,
            )

        # Welford's online update
        est.n += 1
        n = est.n
        delta_a = value_a - est.mean_a
        delta_b = value_b - est.mean_b
        est.mean_a += delta_a / n
        est.mean_b += delta_b / n
        delta_a2 = value_a - est.mean_a  # updated delta
        delta_b2 = value_b - est.mean_b
        est.m2_a += delta_a * delta_a2
        est.m2_b += delta_b * delta_b2
        est.co_moment += delta_a * delta_b2

        # Compute Pearson r
        if n >= 2:
            var_a = est.m2_a / (n - 1)
            var_b = est.m2_b / (n - 1)
            if var_a > 1e-12 and var_b > 1e-12:
                cov = est.co_moment / (n - 1)
                est.pearson_r = cov / math.sqrt(var_a * var_b)
                est.pearson_r = max(-1.0, min(1.0, est.pearson_r))  # clamp

                # Fisher z-transform for confidence interval
                if abs(est.pearson_r) < 0.9999 and n >= 4:
                    z = 0.5 * math.log((1 + est.pearson_r) / (1 - est.pearson_r))
                    se = 1.0 / math.sqrt(n - 3)
                    z_low = z - 1.96 * se
                    z_high = z + 1.96 * se
                    est.ci_low = (math.exp(2 * z_low) - 1) / (math.exp(2 * z_low) + 1)
                    est.ci_high = (math.exp(2 * z_high) - 1) / (math.exp(2 * z_high) + 1)

        self._cache[key] = est

        # Persist to SQLite
        await self._db.execute(
            "INSERT OR REPLACE INTO learned_correlations "
            "(sport, market_a, market_b, n, mean_a, mean_b, m2_a, m2_b, "
            "co_moment, pearson_r, ci_low, ci_high, last_updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))",
            (
                sport, market_a, market_b, est.n,
                est.mean_a, est.mean_b, est.m2_a, est.m2_b,
                est.co_moment, round(est.pearson_r, 6),
                round(est.ci_low, 6), round(est.ci_high, 6),
            ),
        )
        await self._db.commit()
        return est

    async def get(
        self, sport: str, market_a: str, market_b: str
    ) -> Optional[CorrelationEstimate]:
        """Get learned correlation estimate from cache."""
        return self._cache.get((sport, market_a, market_b))

    def get_blended(
        self,
        sport: str,
        market_a: str,
        market_b: str,
        prior: float,
    ) -> float:
        """
        Return blended correlation: learned estimate weighted by sample size,
        falling back to prior when data is insufficient.

        Args:
            sport: sport key
            market_a, market_b: canonical market names
            prior: hardcoded Pearson value from correlation.py

        Returns:
            Blended correlation coefficient.
        """
        est = self._cache.get((sport, market_a, market_b))
        if est is None or est.n < MIN_OBSERVATIONS_FOR_BLEND:
            return prior

        # Check CI width — too uncertain means fall back to prior
        ci_width = est.ci_high - est.ci_low
        if ci_width > MAX_CI_WIDTH:
            return prior

        # Bayesian shrinkage: weight learned estimate by sample size
        weight = min(1.0, est.n / FULL_WEIGHT_AT_N)
        return (1 - weight) * prior + weight * est.pearson_r

    async def get_all_learned(self) -> list[dict]:
        """Get all learned correlation estimates for API/debugging."""
        return [
            {
                "sport": est.sport,
                "market_a": est.market_a,
                "market_b": est.market_b,
                "n": est.n,
                "pearson_r": round(est.pearson_r, 4),
                "ci_low": round(est.ci_low, 4),
                "ci_high": round(est.ci_high, 4),
                "ci_width": round(est.ci_high - est.ci_low, 4),
            }
            for est in sorted(
                self._cache.values(), key=lambda e: e.n, reverse=True
            )
        ]

    def get_stats(self) -> dict:
        """Return learned correlation statistics."""
        estimates = list(self._cache.values())
        return {
            "total_pairs": len(estimates),
            "pairs_with_30_plus_obs": sum(1 for e in estimates if e.n >= 30),
            "pairs_with_100_plus_obs": sum(1 for e in estimates if e.n >= 100),
            "avg_observations": (
                round(sum(e.n for e in estimates) / len(estimates), 1)
                if estimates else 0
            ),
        }
