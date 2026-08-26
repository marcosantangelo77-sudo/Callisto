"""Core Monte Carlo loop for portfolio bankroll simulation."""

from __future__ import annotations

import math
from typing import Optional

import numpy as np

from tools.bankrollsim.config import (
    DEFAULT_KILL_SWITCH_DRAWDOWN,
    SIM_MIN_BET_AMOUNT,
)
from tools.bankrollsim.result import PortfolioSimResult, degenerate_result
from tools.bankrollsim.signals import _load_signals, _group_signals_by_day
from tools.bankrollsim.sizing import _size_slate, _resolve_bets


def simulate_portfolio(
    hypothesis_ids: list[str],
    n_sims: int = 1000,
    horizon_days: int = 90,
    starting_bankroll: float = 10000.0,
    kelly_fraction: float = 0.25,
    correlation_matrix: Optional[dict[tuple[str, str], float]] = None,
    seed: int = 42,
    db_path: Optional[str] = None,
    kill_switch_drawdown: float = DEFAULT_KILL_SWITCH_DRAWDOWN,
    keep_paths: bool = False,
    signals_override: Optional[list[dict]] = None,
) -> PortfolioSimResult:
    """Monte Carlo a portfolio of hypotheses across ``n_sims`` bankroll paths.

    Each sim bootstraps ``horizon_days`` distinct-days sampled with replacement
    from the historical pool. For each sampled day, ALL signals that fired on
    that day are loaded jointly — this preserves same-event correlation (two
    hyps that bet the same game both resolve together, matching history).

    Args:
        hypothesis_ids: Portfolio members.
        n_sims: Number of parallel bankroll paths.
        horizon_days: Trading days per path.
        starting_bankroll: Each path starts with this.
        kelly_fraction: Kelly multiplier. 0.25 = quarter-Kelly (default sharp).
        correlation_matrix: Optional {(hyp_a, hyp_b): rho}. Overrides the
            per-bet default of 0.1. Same-event correlation is ALREADY modeled
            by the joint bootstrap — this matrix layers on top for signal
            co-firing across events.
        seed: RNG seed for reproducibility.
        db_path: Override DB path (defaults to CALLISTO_DB_PATH).
        kill_switch_drawdown: Fraction below rolling peak that triggers the
            sim's drawdown kill switch. Mirrors MAX_DRAWDOWN_PCT.
        keep_paths: If True, include full bankroll paths in the result
            (expensive for n_sims >= 1000).
        signals_override: Test hook — inject synthetic rows instead of
            loading from DB. When non-None, ``hypothesis_ids`` and ``db_path``
            are ignored for data loading.

    Returns:
        PortfolioSimResult with ruin probs, ROI distribution, drawdown dist,
        Sharpe/Sortino, and provenance counts.
    """
    rng = np.random.default_rng(seed=seed)

    if signals_override is not None:
        rows = list(signals_override)
        exc = {"total_considered": len(rows), "no_signal": 0, "unresolved": 0, "lookahead": 0}
    else:
        rows, exc = _load_signals(hypothesis_ids, db_path=db_path)

    by_day = _group_signals_by_day(rows)
    distinct_days = sorted(by_day.keys())
    distinct_hyps_with_data = len({r["hypothesis_id"] for r in rows})

    # Precount signals per hyp for variance dampener use (optional)
    hyp_signal_counts: dict[str, int] = {}
    for r in rows:
        hyp_signal_counts[r["hypothesis_id"]] = hyp_signal_counts.get(r["hypothesis_id"], 0) + 1

    # Edge case: no data at all — return degenerate result with zero bets
    if not distinct_days:
        return degenerate_result(
            hypothesis_ids=hypothesis_ids,
            n_sims=n_sims,
            horizon_days=horizon_days,
            starting_bankroll=starting_bankroll,
            kelly_fraction=kelly_fraction,
            seed=seed,
        )

    final_bankrolls = _run_paths(
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
        correlation_matrix=correlation_matrix,
        kill_switch_drawdown=kill_switch_drawdown,
        keep_paths=keep_paths,
        by_day=by_day,
        distinct_days=distinct_days,
        hyp_signal_counts=hyp_signal_counts,
        seed=seed,
    )
    (
        final_bankrolls_arr,
        max_drawdowns,
        daily_returns_all,
        kill_triggered,
        days_to_ruin,
        bets_placed,
        paths_storage,
    ) = final_bankrolls

    return _aggregate(
        hypothesis_ids=hypothesis_ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
        seed=seed,
        exc=exc,
        rows=rows,
        distinct_days=distinct_days,
        distinct_hyps_with_data=distinct_hyps_with_data,
        final_bankrolls=final_bankrolls_arr,
        max_drawdowns=max_drawdowns,
        daily_returns_all=daily_returns_all,
        kill_triggered=kill_triggered,
        days_to_ruin=days_to_ruin,
        bets_placed=bets_placed,
        paths_storage=paths_storage,
    )


def _run_paths(
    *,
    n_sims: int,
    horizon_days: int,
    starting_bankroll: float,
    kelly_fraction: float,
    correlation_matrix,
    kill_switch_drawdown: float,
    keep_paths: bool,
    by_day: dict[str, list[dict]],
    distinct_days: list[str],
    hyp_signal_counts: dict[str, int],
    seed: int,
):
    """Run all bankroll paths; return raw per-path arrays."""
    rng = np.random.default_rng(seed=seed)

    final_bankrolls = np.empty(n_sims, dtype=np.float64)
    max_drawdowns = np.empty(n_sims, dtype=np.float64)
    daily_returns_all = np.zeros((n_sims, horizon_days), dtype=np.float64)
    kill_triggered = np.zeros(n_sims, dtype=bool)
    days_to_ruin = np.full(n_sims, np.nan, dtype=np.float64)
    bets_placed = np.zeros(n_sims, dtype=np.int32)
    paths_storage = np.empty((n_sims, horizon_days + 1), dtype=np.float64) if keep_paths else None

    for s in range(n_sims):
        bankroll = starting_bankroll
        peak = bankroll
        current_max_dd = 0.0
        killed = False
        kill_day = None
        if paths_storage is not None:
            paths_storage[s, 0] = bankroll

        # Sample distinct game-days with replacement; horizon_days is the
        # calendar horizon we're simulating. Each sampled historical day
        # becomes ONE simulated day — we don't stretch or shrink.
        day_indices = rng.integers(0, len(distinct_days), size=horizon_days)

        for d_idx in range(horizon_days):
            if killed:
                # Kill-switch engaged: no more bets for the rest of the horizon
                if paths_storage is not None:
                    paths_storage[s, d_idx + 1] = bankroll
                continue

            day_key = distinct_days[day_indices[d_idx]]
            signals_today = by_day[day_key]

            # De-duplicate: same (hypothesis_id, event_id) → keep only one row
            seen = set()
            dedup = []
            for row in signals_today:
                key = (row["hypothesis_id"], row["event_id"])
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(row)

            sized = _size_slate(
                dedup, bankroll, kelly_fraction,
                hyp_signal_counts, correlation_matrix,
            )
            active = [b for b in sized if b["stake"] > 0]
            bets_placed[s] += len(active)

            pnl = _resolve_bets(active)
            new_bankroll = bankroll + pnl
            if new_bankroll < 0:
                new_bankroll = 0.0
            daily_returns_all[s, d_idx] = pnl / bankroll if bankroll > 0 else 0.0
            bankroll = new_bankroll

            if bankroll > peak:
                peak = bankroll
            dd = (peak - bankroll) / peak if peak > 0 else 0.0
            if dd > current_max_dd:
                current_max_dd = dd

            if paths_storage is not None:
                paths_storage[s, d_idx + 1] = bankroll

            # Drawdown kill-switch trips the first time dd crosses the threshold.
            if not killed and dd >= kill_switch_drawdown:
                killed = True
                kill_day = d_idx + 1

            # Ruin (bankroll effectively zero) — freeze path
            if bankroll <= max(SIM_MIN_BET_AMOUNT, starting_bankroll * 0.01):
                killed = True
                if kill_day is None:
                    kill_day = d_idx + 1

        final_bankrolls[s] = bankroll
        max_drawdowns[s] = current_max_dd
        kill_triggered[s] = killed
        if killed and kill_day is not None and current_max_dd >= 0.30:
            days_to_ruin[s] = float(kill_day)

    return (
        final_bankrolls,
        max_drawdowns,
        daily_returns_all,
        kill_triggered,
        days_to_ruin,
        bets_placed,
        paths_storage,
    )


def _aggregate(
    *,
    hypothesis_ids: list[str],
    n_sims: int,
    horizon_days: int,
    starting_bankroll: float,
    kelly_fraction: float,
    seed: int,
    exc: dict[str, int],
    rows: list[dict],
    distinct_days: list[str],
    distinct_hyps_with_data: int,
    final_bankrolls: np.ndarray,
    max_drawdowns: np.ndarray,
    daily_returns_all: np.ndarray,
    kill_triggered: np.ndarray,
    days_to_ruin: np.ndarray,
    bets_placed: np.ndarray,
    paths_storage,
) -> PortfolioSimResult:
    """Aggregate per-path arrays into a PortfolioSimResult."""
    start = starting_bankroll

    def _pct(a: np.ndarray, p: float) -> float:
        return float(np.percentile(a, p))

    total_roi = (final_bankrolls - start) / start
    # Monthly ROI: scale by (30 / horizon_days)
    monthly_factor = 30.0 / max(1, horizon_days)
    monthly_roi = total_roi * monthly_factor

    # Daily return stats for Sharpe/Sortino
    flat_returns = daily_returns_all.flatten()
    mean_ret = float(np.mean(flat_returns))
    std_ret = float(np.std(flat_returns)) if flat_returns.size > 1 else 0.0
    # Sharpe: annualized assuming 365 trading days/year (betting every day)
    sharpe = (mean_ret / std_ret) * math.sqrt(365) if std_ret > 0 else 0.0
    # Sortino: only downside stddev
    neg = flat_returns[flat_returns < 0]
    downside_std = float(np.std(neg)) if neg.size > 1 else 0.0
    sortino = (mean_ret / downside_std) * math.sqrt(365) if downside_std > 0 else 0.0

    ruin_5 = float(np.mean(max_drawdowns >= 0.05))
    ruin_15 = float(np.mean(max_drawdowns >= 0.15))
    ruin_30 = float(np.mean(max_drawdowns >= 0.30))
    days_to_ruin_valid = days_to_ruin[~np.isnan(days_to_ruin)]
    dtr_median = float(np.median(days_to_ruin_valid)) if days_to_ruin_valid.size else None

    return PortfolioSimResult(
        hypothesis_ids=hypothesis_ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
        seed=seed,
        total_rows_considered=exc["total_considered"],
        rows_excluded_no_signal=exc["no_signal"],
        rows_excluded_unresolved=exc["unresolved"],
        rows_excluded_lookahead=exc["lookahead"],
        rows_used=len(rows),
        distinct_days=len(distinct_days),
        distinct_hyps_with_data=distinct_hyps_with_data,
        final_bankroll_p10=_pct(final_bankrolls, 10),
        final_bankroll_p50=_pct(final_bankrolls, 50),
        final_bankroll_p90=_pct(final_bankrolls, 90),
        mean_final_bankroll=float(np.mean(final_bankrolls)),
        expected_total_roi=float(np.mean(total_roi)),
        median_total_roi=float(np.median(total_roi)),
        p10_total_roi=_pct(total_roi, 10),
        p90_total_roi=_pct(total_roi, 90),
        expected_monthly_roi=float(np.mean(monthly_roi)),
        median_monthly_roi=float(np.median(monthly_roi)),
        p10_monthly_roi=_pct(monthly_roi, 10),
        p90_monthly_roi=_pct(monthly_roi, 90),
        max_drawdown_median=float(np.median(max_drawdowns)),
        max_drawdown_p90=_pct(max_drawdowns, 90),
        max_drawdown_p99=_pct(max_drawdowns, 99),
        ruin_prob_5pct=ruin_5,
        ruin_prob_15pct=ruin_15,
        ruin_prob_30pct=ruin_30,
        days_to_ruin_median=dtr_median,
        pct_paths_kill_switch_triggered=float(np.mean(kill_triggered)),
        sharpe=sharpe,
        sortino=sortino,
        avg_bets_per_path=float(np.mean(bets_placed)),
        avg_bets_per_day=float(np.mean(bets_placed) / max(1, horizon_days)),
        paths=paths_storage,
    )
