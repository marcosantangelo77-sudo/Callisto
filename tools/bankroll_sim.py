"""
Pre-LIVE bankroll Monte Carlo simulation framework.

feat/bankroll-montecarlo-sim (2026-04-22):

Before promoting a hypothesis (or portfolio of hypotheses) to real money, we
simulate N parallel bankroll paths through bootstrapped historical signal data
and quantify:
  * Ruin probability at multiple drawdown thresholds (5%, 15%, 30%)
  * Expected ROI / month (median, p10, p90)
  * Max drawdown distribution (p50, p90, p99)
  * Days-to-ruin (when it happens)
  * Sharpe / Sortino

Why this exists (the 2026-04-22 audit):
  The 16-of-22 LIVE-hypothesis correlation finding made clear that
  per-hypothesis Kelly + per-game/sport caps are not enough. Two hyps that
  fire on the same MLB game are NOT two independent edges. A 22-hyp portfolio
  of correlated MLB bets can route ~80% of bankroll into a single blowout
  evening. The only honest way to bound that tail risk is to bootstrap real
  historical signals jointly (preserving same-event correlation) and walk
  forward through the portfolio-Kelly sizer + drawdown kill switch.

Data source:
  `backtest_events` — 37k rows with resolved outcomes. Each row has
  ``hypothesis_id, event_id, game_date, side, book_odds_american, edge,
  ev_pct, actual_result`` (won/lost/push).

Lookahead filter:
  The schema does NOT (yet) have a ``snapshot_quality`` column. The audit
  wanting us to restrict to ``snapshot_quality='pre_commence'`` predicts a
  schema that doesn't exist. We defensively filter:
    * ``signal_generated = 1`` — only rows that actually triggered a bet
    * ``actual_result IN ('won', 'lost', 'push')`` — resolved rows only
    * Any row where ``snapshot_time > game_date + 1 day`` (post-commence
      snapshot, i.e., known-lookahead) is dropped.
  The sim logs counts of excluded rows so the operator can see what fraction
  of the pool was filtered out.

Correlation:
  Same-event correlation is preserved automatically by the bootstrap: when we
  sample a day, we pull ALL signals on that day for every hypothesis in the
  portfolio. If hyp A and hyp B both fire on event X on 2026-03-27, they both
  win or both lose jointly (the real historical outcome). This is the
  strongest possible correlation model — it matches reality exactly for the
  historical window.

Reproducibility:
  ``seed`` parameter (default 42). Same seed + same DB = deterministic result.
"""

from __future__ import annotations

import logging
import math
import os
import random
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np

logger = logging.getLogger("callisto.bankroll_sim")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Drawdown kill switch threshold — mirrors tools/bet_executor.MAX_DRAWDOWN_PCT.
# When a simulated bankroll path dips this far below its rolling peak, we
# freeze betting for the rest of the horizon (approximates the executor's
# real behavior where LIVE hyps get set to 'drawdown_paused').
DEFAULT_KILL_SWITCH_DRAWDOWN = float(
    os.getenv("CALLISTO_SIM_KILL_DRAWDOWN", "0.15")
)

# Default per-bet and portfolio caps mirror bet_executor defaults so sims
# reflect the same sizing the live path would produce.
SIM_MAX_BET_PCT = float(os.getenv("CALLISTO_SIM_MAX_BET_PCT", "0.05"))
SIM_MAX_GAME_EXPOSURE_PCT = float(os.getenv("CALLISTO_SIM_MAX_GAME_PCT", "0.08"))
SIM_MAX_SPORT_EXPOSURE_PCT = float(os.getenv("CALLISTO_SIM_MAX_SPORT_PCT", "0.15"))
SIM_MIN_BET_AMOUNT = 1.0


# =========================================================================
# Result dataclass
# =========================================================================
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


# =========================================================================
# Internal: load historical signal data joined for portfolio simulation
# =========================================================================
def _load_signals(
    hypothesis_ids: list[str],
    db_path: str = None,
) -> tuple[list[dict], dict[str, int]]:
    """Load signal-generated, resolved backtest events for the given hyps.

    Returns (rows, exclusion_counts). Each row is a dict:
        {hypothesis_id, event_id, game_date, sport, market, side,
         odds, edge, ev_pct, actual_result}

    Exclusion counts:
        {"no_signal": int, "unresolved": int, "lookahead": int, "total_considered": int}
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    if not hypothesis_ids:
        conn.close()
        return [], {"no_signal": 0, "unresolved": 0, "lookahead": 0, "total_considered": 0}

    placeholders = ",".join("?" * len(hypothesis_ids))
    cur = conn.execute(
        f"""
        SELECT hypothesis_id, event_id, game_date, snapshot_time, sport,
               market, side, book_odds_american, edge, ev_pct,
               signal_generated, actual_result
        FROM backtest_events
        WHERE hypothesis_id IN ({placeholders})
        """,
        tuple(hypothesis_ids),
    )
    raw = [dict(r) for r in cur.fetchall()]
    conn.close()

    exc = {
        "total_considered": len(raw),
        "no_signal": 0,
        "unresolved": 0,
        "lookahead": 0,
    }
    rows: list[dict] = []
    for r in raw:
        if not r.get("signal_generated"):
            exc["no_signal"] += 1
            continue
        if r.get("actual_result") not in ("won", "lost", "push"):
            exc["unresolved"] += 1
            continue
        # Lookahead defense: if snapshot was taken AFTER the game date,
        # this is a known-leaky row — drop it. We allow same-day snapshots
        # because the audit's pre_commence filter is about intra-day fetches
        # that don't exist as a column yet.
        try:
            gdate = datetime.fromisoformat(r["game_date"]).date()
            st_raw = r.get("snapshot_time") or ""
            # Replace Z for parseability
            st = st_raw.replace("Z", "+00:00") if st_raw else ""
            if st:
                sdt = datetime.fromisoformat(st)
                # If snapshot is > 1 day AFTER the game started, treat as leaky
                if sdt.date() > gdate + timedelta(days=1):
                    exc["lookahead"] += 1
                    continue
        except (ValueError, TypeError):
            pass  # best-effort only — don't exclude on parse failure

        rows.append({
            "hypothesis_id": r["hypothesis_id"],
            "event_id": str(r["event_id"]),
            "game_date": r["game_date"],
            "sport": r.get("sport") or "",
            "market": r.get("market") or "",
            "side": r.get("side") or "",
            "odds": int(r.get("book_odds_american") or -110),
            "edge": float(r.get("edge") or 0.0),
            "ev_pct": float(r.get("ev_pct") or 0.0),
            "actual_result": r["actual_result"],
        })
    return rows, exc


def _group_signals_by_day(
    rows: list[dict],
) -> dict[str, list[dict]]:
    """Group signals by game_date so we can sample jointly."""
    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["game_date"], []).append(r)
    return by_day


# =========================================================================
# Internal: size a slate of bets using the same logic as the live executor
# =========================================================================
def _size_slate(
    signals: list[dict],
    bankroll: float,
    kelly_fraction: float,
    hyp_signal_counts: dict[str, int],
    correlation_matrix: Optional[dict[tuple[str, str], float]] = None,
) -> list[dict]:
    """Size a list of signals using portfolio-Kelly + per-game/sport caps.

    Mirrors ``BetExecutor.compute_portfolio_stakes`` without the DB writes.
    Returns a list of {stake, edge, odds, actual_result, sport, event_id,
    hypothesis_id} dicts. Stakes below SIM_MIN_BET_AMOUNT are zeroed.
    """
    if not signals:
        return []

    # Import lazily so the sim can be unit-tested without pulling in aiosqlite
    from tools.kelly import kelly_portfolio, kelly_fractional, _confidence_tier_from_score, AGP_TIER_MULTIPLIERS

    # Default confidence: mirror the executor's 0.6 (PROBABLE tier) so sim
    # sizing matches what the live path would produce.
    default_conf = 0.6
    # Corr overrides from matrix
    corr_overrides: dict[int, float] = {}
    if correlation_matrix:
        n = len(signals)
        for i, si in enumerate(signals):
            hi = si["hypothesis_id"]
            pair_corrs = []
            for j, sj in enumerate(signals):
                if i == j:
                    continue
                hj = sj["hypothesis_id"]
                key = (hi, hj) if (hi, hj) in correlation_matrix else (hj, hi)
                if key in correlation_matrix:
                    pair_corrs.append(correlation_matrix[key])
            if pair_corrs:
                corr_overrides[i] = sum(pair_corrs) / len(pair_corrs)

    if len(signals) == 1:
        # Single bet: simple fractional Kelly + tier adjustment
        s = signals[0]
        frac = kelly_fractional(s["edge"], s["odds"], fraction=kelly_fraction)
        tier = _confidence_tier_from_score(default_conf)
        frac *= AGP_TIER_MULTIPLIERS.get(tier, 0.0)
        frac = min(frac, SIM_MAX_BET_PCT)
        stake = round(bankroll * frac, 2)
        if stake < SIM_MIN_BET_AMOUNT:
            stake = 0.0
        return [{**s, "stake": stake, "fraction": frac}]

    portfolio_bets = []
    for i, s in enumerate(signals):
        rho = corr_overrides.get(i, 0.1)
        portfolio_bets.append({
            "edge": s["edge"],
            "odds": s["odds"],
            "confidence_score": default_conf,
            "variance_estimate": abs(s["edge"]) * 0.5,
            "correlation_with_others": rho,
            "description": s["hypothesis_id"],
        })
    sized = kelly_portfolio(portfolio_bets)

    # Scale by kelly_fraction relative to default quarter-Kelly (0.25)
    # so sensitivity analysis actually moves the sim.
    scale = kelly_fraction / 0.25 if kelly_fraction != 0.25 else 1.0

    results = []
    for i, item in enumerate(sized):
        frac = float(item.get("final_fraction", 0.0)) * scale
        stake = round(bankroll * frac, 2)
        results.append({
            **signals[i],
            "stake": stake,
            "fraction": frac,
        })

    # Per-game cap
    game_cap = bankroll * SIM_MAX_GAME_EXPOSURE_PCT
    by_game: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        by_game.setdefault(r["event_id"], []).append(idx)
    for eid, idxs in by_game.items():
        total = sum(results[i]["stake"] for i in idxs)
        if total > game_cap and total > 0:
            ratio = game_cap / total
            for i in idxs:
                results[i]["stake"] = round(results[i]["stake"] * ratio, 2)

    # Per-sport cap
    sport_cap = bankroll * SIM_MAX_SPORT_EXPOSURE_PCT
    by_sport: dict[str, list[int]] = {}
    for idx, r in enumerate(results):
        sp = r.get("sport") or ""
        if not sp:
            continue
        by_sport.setdefault(sp, []).append(idx)
    for sp, idxs in by_sport.items():
        total = sum(results[i]["stake"] for i in idxs)
        if total > sport_cap and total > 0:
            ratio = sport_cap / total
            for i in idxs:
                results[i]["stake"] = round(results[i]["stake"] * ratio, 2)

    # Floor
    for r in results:
        if r["stake"] < SIM_MIN_BET_AMOUNT:
            r["stake"] = 0.0

    return results


def _resolve_bets(bets: list[dict]) -> float:
    """Given a list of sized bets with actual_result, return net P&L."""
    pnl = 0.0
    for b in bets:
        stake = b["stake"]
        if stake <= 0:
            continue
        odds = b["odds"]
        if b["actual_result"] == "won":
            if odds > 0:
                pnl += stake * (odds / 100.0)
            else:
                pnl += stake * (100.0 / abs(odds))
        elif b["actual_result"] == "lost":
            pnl -= stake
        # push: 0
    return pnl


# =========================================================================
# Public API: simulate_portfolio
# =========================================================================
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
    py_rng = random.Random(seed)

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

    # Pre-allocate result arrays
    final_bankrolls = np.empty(n_sims, dtype=np.float64)
    max_drawdowns = np.empty(n_sims, dtype=np.float64)
    daily_returns_all = np.zeros((n_sims, horizon_days), dtype=np.float64)
    kill_triggered = np.zeros(n_sims, dtype=bool)
    days_to_ruin = np.full(n_sims, np.nan, dtype=np.float64)
    bets_placed = np.zeros(n_sims, dtype=np.int32)
    paths_storage = np.empty((n_sims, horizon_days + 1), dtype=np.float64) if keep_paths else None

    # Main loop
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
            # (typical historical data has 5-10 rows per signal due to line
            # snapshots). The correlation check lives at the event level.
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

    # ── Aggregate ──
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


# =========================================================================
# Pre-LIVE gate: simulate_before_promote
# =========================================================================
def simulate_before_promote(
    hypothesis_id: str,
    db_path: Optional[str] = None,
    n_sims: int = 500,
    horizon_days: int = 30,
) -> dict:
    """Run a pre-promotion simulation for ``hypothesis_id`` + all current LIVE hyps.

    Intended to be called from the paper_trading → live gate. Returns a simple
    dict suitable for inclusion in a promotion-readiness report.

    Returns:
        {
          "ruin_prob_30d": float,        # 15% drawdown at 30-day horizon
          "ruin_prob_30pct_30d": float,  # 30% drawdown at 30-day horizon
          "expected_monthly_roi": float,
          "expected_drawdown": float,    # median max DD
          "n_sims": int,
          "hyp_count": int,
          "rows_used": int,
        }
    """
    path = db_path or DB_PATH
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'"
        ).fetchall()
    finally:
        conn.close()
    live_ids = [r[0] for r in rows]
    # De-dup the candidate in case it's already LIVE (idempotent call)
    portfolio = list(dict.fromkeys([hypothesis_id] + live_ids))

    result = simulate_portfolio(
        hypothesis_ids=portfolio,
        n_sims=n_sims,
        horizon_days=horizon_days,
        db_path=path,
    )

    return {
        "ruin_prob_30d": result.ruin_prob_15pct,
        "ruin_prob_30pct_30d": result.ruin_prob_30pct,
        "expected_monthly_roi": result.expected_monthly_roi,
        "expected_drawdown": result.max_drawdown_median,
        "n_sims": n_sims,
        "hyp_count": len(portfolio),
        "rows_used": result.rows_used,
    }


# =========================================================================
# ASCII bankroll-distribution plot (for CLI)
# =========================================================================
def ascii_bankroll_histogram(
    final_bankrolls: np.ndarray,
    starting_bankroll: float,
    width: int = 60,
    bins: int = 20,
) -> str:
    """Return a human-readable ASCII histogram of final bankrolls."""
    if final_bankrolls is None or len(final_bankrolls) == 0:
        return "(no data)"
    lo = float(np.min(final_bankrolls))
    hi = float(np.max(final_bankrolls))
    if hi <= lo:
        return f"(all paths ended at ${lo:,.0f})"
    hist, edges = np.histogram(final_bankrolls, bins=bins, range=(lo, hi))
    peak = int(np.max(hist))
    lines = []
    lines.append(f"Bankroll distribution (start=${starting_bankroll:,.0f}):")
    for i, count in enumerate(hist):
        left = edges[i]
        right = edges[i + 1]
        bar_len = int(round(width * count / peak)) if peak > 0 else 0
        bar = "#" * bar_len
        pct = 100.0 * count / len(final_bankrolls)
        lines.append(
            f"  ${left:>8,.0f} - ${right:>8,.0f} | {bar:<{width}} {count:>5} ({pct:4.1f}%)"
        )
    return "\n".join(lines)
