"""PortfolioSimResult — full result record of a portfolio Monte Carlo sim."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

import numpy as np


@dataclass
class PortfolioSimResult:
    """Full result of a portfolio bankroll Monte Carlo simulation."""

    # Parameters
    hypothesis_ids: list[str]
    n_sims: int
    horizon_days: int
    starting_bankroll: float
    kelly_fraction: float
    seed: int

    # Data provenance
    total_rows_considered: int
    rows_excluded_no_signal: int
    rows_excluded_unresolved: int
    rows_excluded_lookahead: int
    rows_used: int
    distinct_days: int
    distinct_hyps_with_data: int

    # Bankroll statistics
    final_bankroll_p10: float
    final_bankroll_p50: float
    final_bankroll_p90: float
    mean_final_bankroll: float

    # ROI statistics
    expected_total_roi: float     # (final - start) / start, mean
    median_total_roi: float
    p10_total_roi: float
    p90_total_roi: float
    expected_monthly_roi: float   # total roi scaled to 30d
    median_monthly_roi: float
    p10_monthly_roi: float
    p90_monthly_roi: float

    # Drawdown distribution (fraction, 0.0 to 1.0)
    max_drawdown_median: float
    max_drawdown_p90: float
    max_drawdown_p99: float

    # Ruin probabilities at multiple drawdown thresholds
    ruin_prob_5pct: float
    ruin_prob_15pct: float
    ruin_prob_30pct: float

    # Days-to-ruin: median across paths that hit the 30% ruin threshold
    days_to_ruin_median: Optional[float]
    pct_paths_kill_switch_triggered: float

    # Risk-adjusted return metrics (daily units, annualized where meaningful)
    sharpe: float
    sortino: float

    # Bet statistics
    avg_bets_per_path: float
    avg_bets_per_day: float

    # Raw paths are optional — suppressed from JSON for size.
    paths: Optional[np.ndarray] = field(default=None, repr=False)

    def to_dict(self, include_paths: bool = False) -> dict:
        d = asdict(self)
        if not include_paths:
            d.pop("paths", None)
        else:
            # Convert to list of lists for JSON friendliness
            if self.paths is not None:
                d["paths"] = self.paths.tolist()
        return d


def degenerate_result(
    hypothesis_ids: list[str],
    n_sims: int,
    horizon_days: int,
    starting_bankroll: float,
    kelly_fraction: float,
    seed: int,
) -> PortfolioSimResult:
    """Zero-bet result used when the historical pool is empty."""
    return PortfolioSimResult(
        hypothesis_ids=hypothesis_ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
        seed=seed,
        total_rows_considered=0,
        rows_excluded_no_signal=0,
        rows_excluded_unresolved=0,
        rows_excluded_lookahead=0,
        rows_used=0,
        distinct_days=0,
        distinct_hyps_with_data=0,
        final_bankroll_p10=starting_bankroll,
        final_bankroll_p50=starting_bankroll,
        final_bankroll_p90=starting_bankroll,
        mean_final_bankroll=starting_bankroll,
        expected_total_roi=0.0,
        median_total_roi=0.0,
        p10_total_roi=0.0,
        p90_total_roi=0.0,
        expected_monthly_roi=0.0,
        median_monthly_roi=0.0,
        p10_monthly_roi=0.0,
        p90_monthly_roi=0.0,
        max_drawdown_median=0.0,
        max_drawdown_p90=0.0,
        max_drawdown_p99=0.0,
        ruin_prob_5pct=0.0,
        ruin_prob_15pct=0.0,
        ruin_prob_30pct=0.0,
        days_to_ruin_median=None,
        pct_paths_kill_switch_triggered=0.0,
        sharpe=0.0,
        sortino=0.0,
        avg_bets_per_path=0.0,
        avg_bets_per_day=0.0,
        paths=None,
    )
