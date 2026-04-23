"""Run a pre-LIVE bankroll Monte Carlo simulation from the CLI.

feat/bankroll-montecarlo-sim (2026-04-22).

Examples:
    # Simulate the full current 22-hyp LIVE roster, 90d, 200 paths
    python scripts/run_portfolio_sim.py --all-live --horizon 90 --n-sims 200

    # Simulate a specific trio, 30d, 500 paths
    python scripts/run_portfolio_sim.py --ids a,b,c --horizon 30 --n-sims 500

    # Kelly-fraction sensitivity table (runs the same portfolio at 1/4, 1/2, full)
    python scripts/run_portfolio_sim.py --all-live --sensitivity

The script is READ-ONLY against the live DB. It never writes or mutates state.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# Use the master-worktree DB by default so CLI runs reflect true LIVE state.
DEFAULT_DB = os.getenv(
    "CALLISTO_DB_PATH",
    str(REPO.parent / "Callisto" / "memory" / "callisto.db"),
)


def _load_live_ids(db_path: str) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT hypothesis_id, name FROM hypotheses WHERE status = 'live' ORDER BY hypothesis_id"
        ).fetchall()
    finally:
        conn.close()
    if rows:
        print(f"Found {len(rows)} LIVE hypotheses:")
        for hid, name in rows:
            print(f"  {hid}  {name}")
        print()
    return [r[0] for r in rows]


def _fmt_money(v: float) -> str:
    return f"${v:,.0f}"


def _fmt_pct(v: float, decimals: int = 2) -> str:
    return f"{v*100:+.{decimals}f}%"


def _print_result(result, starting_bankroll: float, kelly_label: str = "") -> None:
    from tools.bankroll_sim import ascii_bankroll_histogram
    import numpy as np

    header = f"  PORTFOLIO SIM RESULT"
    if kelly_label:
        header += f"  ({kelly_label})"
    print("=" * 80)
    print(header)
    print("=" * 80)
    print()
    print(f"  Hypotheses:          {len(result.hypothesis_ids)}")
    print(f"  n_sims:              {result.n_sims}")
    print(f"  Horizon:             {result.horizon_days} days")
    print(f"  Starting bankroll:   {_fmt_money(starting_bankroll)}")
    print(f"  Kelly fraction:      {result.kelly_fraction}")
    print()
    print("  Data provenance:")
    print(f"    Rows considered:   {result.total_rows_considered:,}")
    print(f"    Excluded (no signal): {result.rows_excluded_no_signal:,}")
    print(f"    Excluded (unresolved): {result.rows_excluded_unresolved:,}")
    print(f"    Excluded (lookahead): {result.rows_excluded_lookahead:,}")
    print(f"    Rows used:         {result.rows_used:,}")
    print(f"    Distinct days:     {result.distinct_days:,}")
    print(f"    Hyps with data:    {result.distinct_hyps_with_data}/{len(result.hypothesis_ids)}")
    print()
    print("  Final bankroll distribution:")
    print(f"    p10:               {_fmt_money(result.final_bankroll_p10)}")
    print(f"    p50 (median):      {_fmt_money(result.final_bankroll_p50)}")
    print(f"    p90:               {_fmt_money(result.final_bankroll_p90)}")
    print(f"    mean:              {_fmt_money(result.mean_final_bankroll)}")
    print()
    print("  Total ROI over horizon:")
    print(f"    p10 / median / mean / p90: "
          f"{_fmt_pct(result.p10_total_roi)} / "
          f"{_fmt_pct(result.median_total_roi)} / "
          f"{_fmt_pct(result.expected_total_roi)} / "
          f"{_fmt_pct(result.p90_total_roi)}")
    print()
    print("  Monthly ROI (scaled to 30d):")
    print(f"    p10 / median / mean / p90: "
          f"{_fmt_pct(result.p10_monthly_roi)} / "
          f"{_fmt_pct(result.median_monthly_roi)} / "
          f"{_fmt_pct(result.expected_monthly_roi)} / "
          f"{_fmt_pct(result.p90_monthly_roi)}")
    print()
    print("  Max drawdown distribution:")
    print(f"    p50:  {result.max_drawdown_median*100:5.2f}%")
    print(f"    p90:  {result.max_drawdown_p90*100:5.2f}%")
    print(f"    p99:  {result.max_drawdown_p99*100:5.2f}%")
    print()
    print("  RUIN PROBABILITIES (pct of paths that crossed each drawdown):")
    print(f"    >=  5% drawdown:    {result.ruin_prob_5pct*100:5.2f}%")
    print(f"    >= 15% drawdown:    {result.ruin_prob_15pct*100:5.2f}%  <-- kill-switch trigger")
    print(f"    >= 30% drawdown:    {result.ruin_prob_30pct*100:5.2f}%")
    print()
    print("  Kill-switch / ruin timing:")
    print(f"    Paths that tripped kill switch: {result.pct_paths_kill_switch_triggered*100:5.2f}%")
    if result.days_to_ruin_median is not None:
        print(f"    Median days to 30% drawdown:    {result.days_to_ruin_median:.0f}")
    else:
        print(f"    Median days to 30% drawdown:    -- (no paths hit 30% DD)")
    print()
    print("  Risk-adjusted returns (annualized):")
    print(f"    Sharpe:            {result.sharpe:6.3f}")
    print(f"    Sortino:           {result.sortino:6.3f}")
    print()
    print("  Bet volume:")
    print(f"    Avg bets / path:   {result.avg_bets_per_path:7.1f}")
    print(f"    Avg bets / day:    {result.avg_bets_per_day:7.2f}")
    print()


def _run_sensitivity_table(
    ids: list[str],
    n_sims: int,
    horizon: int,
    bankroll: float,
    db_path: str,
) -> None:
    """Run the same portfolio at 1/4, 1/2, and full Kelly. Print comparison."""
    from tools.bankroll_sim import simulate_portfolio
    fractions = [
        (0.25, "Quarter Kelly (default)"),
        (0.50, "Half Kelly"),
        (1.00, "Full Kelly"),
    ]
    print()
    print("=" * 100)
    print("  KELLY-FRACTION SENSITIVITY TABLE")
    print("=" * 100)
    print()
    fmt = "{:<25} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10} {:>8}"
    print(fmt.format(
        "Kelly fraction",
        "ExpROI/mo", "P10 ROI/mo", "P90 ROI/mo",
        "MedDD", "P90 DD", "Ruin15", "Sharpe",
    ))
    print("-" * 100)
    for frac, label in fractions:
        res = simulate_portfolio(
            hypothesis_ids=ids,
            n_sims=n_sims,
            horizon_days=horizon,
            starting_bankroll=bankroll,
            kelly_fraction=frac,
            db_path=db_path,
        )
        print(fmt.format(
            label,
            f"{res.expected_monthly_roi*100:+.2f}%",
            f"{res.p10_monthly_roi*100:+.2f}%",
            f"{res.p90_monthly_roi*100:+.2f}%",
            f"{res.max_drawdown_median*100:.1f}%",
            f"{res.max_drawdown_p90*100:.1f}%",
            f"{res.ruin_prob_15pct*100:.1f}%",
            f"{res.sharpe:.2f}",
        ))
    print()


def main():
    ap = argparse.ArgumentParser(description="Pre-LIVE portfolio bankroll sim (read-only)")
    ap.add_argument("--ids", type=str, default="", help="CSV of hypothesis IDs")
    ap.add_argument("--all-live", action="store_true", help="Use all current LIVE hyps")
    ap.add_argument("--n-sims", type=int, default=500)
    ap.add_argument("--horizon", type=int, default=90, help="Horizon days per path")
    ap.add_argument("--bankroll", type=float, default=10000.0)
    ap.add_argument("--kelly-fraction", type=float, default=0.25)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--db", type=str, default=DEFAULT_DB, help="DB path (READ-ONLY)")
    ap.add_argument("--sensitivity", action="store_true",
                    help="Also print Quarter / Half / Full Kelly comparison table")
    ap.add_argument("--histogram", action="store_true",
                    help="Print ASCII bankroll histogram (requires --keep-paths)")
    ap.add_argument("--keep-paths", action="store_true",
                    help="Retain per-path bankroll arrays (slower, RAM heavy)")
    args = ap.parse_args()

    # Route CALLISTO_DB_PATH before importing bankroll_sim so _load_signals uses it
    os.environ["CALLISTO_DB_PATH"] = args.db

    if args.all_live:
        ids = _load_live_ids(args.db)
    else:
        ids = [x.strip() for x in args.ids.split(",") if x.strip()]

    if not ids:
        print("ERROR: no hypothesis IDs supplied (pass --ids a,b,c or --all-live)", file=sys.stderr)
        sys.exit(2)

    from tools.bankroll_sim import simulate_portfolio, ascii_bankroll_histogram
    result = simulate_portfolio(
        hypothesis_ids=ids,
        n_sims=args.n_sims,
        horizon_days=args.horizon,
        starting_bankroll=args.bankroll,
        kelly_fraction=args.kelly_fraction,
        seed=args.seed,
        db_path=args.db,
        keep_paths=args.keep_paths or args.histogram,
    )

    kelly_label = {0.25: "Quarter Kelly", 0.5: "Half Kelly", 1.0: "Full Kelly"}.get(
        args.kelly_fraction, f"Kelly x{args.kelly_fraction}"
    )
    _print_result(result, args.bankroll, kelly_label=kelly_label)

    if args.histogram and result.paths is not None:
        import numpy as _np
        finals = result.paths[:, -1]
        print(ascii_bankroll_histogram(finals, args.bankroll))
        print()

    if args.sensitivity:
        _run_sensitivity_table(ids, args.n_sims, args.horizon, args.bankroll, args.db)


if __name__ == "__main__":
    main()
