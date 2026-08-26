"""Pre-LIVE promotion gate: simulate a candidate hyp + current LIVE portfolio."""

from __future__ import annotations

import sqlite3
from typing import Optional

from tools.bankrollsim.config import DB_PATH
from tools.bankrollsim.simulator import simulate_portfolio


def simulate_before_promote(
    hypothesis_id: str,
    db_path: Optional[str] = None,
    n_sims: int = 500,
    horizon_days: int = 30,
) -> dict:
    """Run a pre-promotion simulation for ``hypothesis_id`` + all current LIVE hyps.

    Intended to be called from the paper_trading → live gate. Returns a simple
    dict suitable for inclusion in a promotion-readiness report.

    NOTE: this gate reads LIVE hypotheses for portfolio context only — it never
    places bets and never touches any live-betting path.
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
