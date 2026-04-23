"""Dry-run: compare OLD per-signal sizing vs NEW batched portfolio sizing
on the current 22 LIVE hypotheses against a simulated 15-game MLB slate.

feat/portfolio-kelly-live-loop (audit 2026-04-22).

Read-only against the live Callisto DB (memory/callisto.db on the master
worktree). Does NOT mutate state. Reports the key metric: old path vs new
path on MAXIMUM single-game exposure.

Usage:
    python scripts/dry_run_portfolio_kelly.py
"""

import os
import sqlite3
import sys
from pathlib import Path
from statistics import mean

# Make sure we import from the worktree root
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools.bet_executor import (  # noqa: E402
    BetExecutor,
    MAX_BET_PCT,
    MAX_GAME_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
    KELLY_FRACTION,
)

# Read-only against the MASTER worktree DB (where live Callisto state lives).
LIVE_DB = os.getenv(
    "CALLISTO_DB_PATH",
    str(REPO.parent / "Callisto" / "memory" / "callisto.db"),
)
BANKROLL = float(os.getenv("DRY_RUN_BANKROLL", "10000"))
SLATE_SIZE = int(os.getenv("DRY_RUN_SLATE", "15"))


def load_live_hyps():
    """Pull (hypothesis_id, market_type, edge_threshold) + signals_n for LIVE hyps."""
    conn = sqlite3.connect(LIVE_DB)
    cur = conn.execute(
        "SELECT hypothesis_id, sport, market_type, edge_threshold FROM hypotheses "
        "WHERE status = 'live'"
    )
    rows = cur.fetchall()
    hyps = []
    for hid, sport, market, thresh in rows:
        cur2 = conn.execute(
            "SELECT signals_n, avg_edge FROM hypothesis_stats "
            "WHERE hypothesis_id = ? ORDER BY computed_at DESC LIMIT 1",
            (hid,),
        )
        stat = cur2.fetchone()
        signals_n = int(stat[0]) if stat else 0
        avg_edge = float(stat[1]) if stat and stat[1] else max(thresh or 0.02, 0.03)
        hyps.append({
            "hypothesis_id": hid,
            "sport": sport,
            "market_type": market,
            "edge_threshold": thresh or 0.02,
            "signals_n": signals_n,
            "avg_edge": avg_edge,
        })
    conn.close()
    return hyps


def build_correlation_from_history(hypothesis_ids, lookback_days=30):
    conn = sqlite3.connect(LIVE_DB)
    placeholders = ",".join("?" * len(hypothesis_ids))
    cur = conn.execute(
        f"SELECT hypothesis_id, event_id FROM backtest_events "
        f"WHERE signal_generated = 1 AND hypothesis_id IN ({placeholders}) "
        f"AND created_at >= datetime('now', '-{lookback_days} days')",
        tuple(hypothesis_ids),
    )
    rows = cur.fetchall()
    conn.close()

    fired = {}
    for hid, eid in rows:
        if not eid:
            continue
        fired.setdefault(hid, set()).add(eid)

    matrix = {}
    ids = sorted(hypothesis_ids)
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            sa, sb = fired.get(a, set()), fired.get(b, set())
            union = len(sa | sb)
            corr = (len(sa & sb) / union) if union else 0.0
            matrix[(a, b)] = corr
    return matrix


def synthesize_slate(hyps, slate_size=15):
    """Build a hypothetical batch: every MLB hyp fires on every MLB game.

    This is the pessimistic (and observed in practice) case that the audit
    is worried about: 22 LIVE hyps all pointing at tonight's MLB board.
    """
    mlb_hyps = [h for h in hyps if h["sport"] == "baseball_mlb"]
    if not mlb_hyps:
        # Fall back: treat every LIVE hyp as MLB for the simulation.
        mlb_hyps = hyps

    batch = []
    for game_i in range(slate_size):
        event_id = f"mlb_slate_{game_i}"
        for h in mlb_hyps:
            batch.append({
                "edge": h["avg_edge"],
                "odds": -110,
                "confidence": 0.78,
                "event_id": event_id,
                "sport": "baseball_mlb",
                "market_type": h.get("market_type", "h2h"),
                "hypothesis_id": h["hypothesis_id"],
                "description": f"{h['hypothesis_id'][:8]} on {event_id}",
                "signals_n": h["signals_n"],
            })
    return batch, mlb_hyps


def old_per_signal_sizing(executor, batch, bankroll):
    """Replay the pre-patch per-signal path: each signal gets compute_stake."""
    stakes = []
    for b in batch:
        stake = executor.compute_stake(
            edge=b["edge"], odds=b["odds"], bankroll=bankroll,
            confidence=b["confidence"],
        )
        stakes.append({**b, "stake": stake})
    return stakes


def summarize_by_game(sized_rows):
    by_game = {}
    for r in sized_rows:
        eid = r.get("event_id", "")
        by_game.setdefault(eid, []).append(r.get("stake", 0))
    return {eid: sum(s) for eid, s in by_game.items()}


def pct(x, y):
    return (x / y * 100.0) if y else 0.0


def main():
    if not Path(LIVE_DB).exists():
        print(f"ERROR: DB not found at {LIVE_DB}")
        sys.exit(1)

    print("=" * 78)
    print("DRY-RUN: old per-signal sizing  vs  new batched portfolio sizing")
    print("=" * 78)
    print(f"DB: {LIVE_DB}")
    print(f"Bankroll: ${BANKROLL:,.2f}  |  Slate: {SLATE_SIZE} MLB games")
    print(f"Caps:  per-bet {MAX_BET_PCT:.0%}  |  per-game {MAX_GAME_EXPOSURE_PCT:.0%}  "
          f"|  per-sport {MAX_SPORT_EXPOSURE_PCT:.0%}")
    print()

    hyps = load_live_hyps()
    print(f"LIVE hypotheses: {len(hyps)}")
    if not hyps:
        print("No LIVE hyps — aborting dry-run.")
        return

    ids = [h["hypothesis_id"] for h in hyps]
    corr = build_correlation_from_history(ids)
    nonzero_pairs = sum(1 for v in corr.values() if v > 0)
    avg_corr = mean(corr.values()) if corr else 0.0
    print(
        f"Correlation matrix: {len(corr)} pairs, {nonzero_pairs} with nonzero co-firing, "
        f"mean={avg_corr:.3f}"
    )

    batch, mlb_hyps = synthesize_slate(hyps, SLATE_SIZE)
    print(f"Synthetic batch: {len(batch)} signals "
          f"({len(mlb_hyps)} hyps x {SLATE_SIZE} games)")
    print()

    executor = BetExecutor()
    # OLD path
    old = old_per_signal_sizing(executor, batch, BANKROLL)
    old_by_game = summarize_by_game(old)
    old_total = sum(old_by_game.values())
    old_max_game = max(old_by_game.values()) if old_by_game else 0.0

    # NEW path
    new = executor.compute_portfolio_stakes(
        bets=batch, bankroll=BANKROLL, correlation_matrix=corr,
    )
    new_by_game = summarize_by_game(new)
    new_total = sum(new_by_game.values())
    new_max_game = max(new_by_game.values()) if new_by_game else 0.0

    print("-" * 78)
    print("RESULT: exposure per game (old vs new)")
    print("-" * 78)
    print(f"{'event_id':<24}{'OLD $':>14}{'OLD %':>8}  {'NEW $':>14}{'NEW %':>8}")
    for eid in sorted(old_by_game):
        o = old_by_game.get(eid, 0)
        n = new_by_game.get(eid, 0)
        print(f"{eid:<24}{o:>14.2f}{pct(o, BANKROLL):>7.2f}%  "
              f"{n:>14.2f}{pct(n, BANKROLL):>7.2f}%")
    print("-" * 78)
    print(f"{'TOTAL across slate':<24}"
          f"{old_total:>14.2f}{pct(old_total, BANKROLL):>7.2f}%  "
          f"{new_total:>14.2f}{pct(new_total, BANKROLL):>7.2f}%")
    print(f"{'MAX single game':<24}"
          f"{old_max_game:>14.2f}{pct(old_max_game, BANKROLL):>7.2f}%  "
          f"{new_max_game:>14.2f}{pct(new_max_game, BANKROLL):>7.2f}%")
    print()
    print("KEY METRIC: max single-game exposure reduction")
    if old_max_game > 0:
        reduction = (1 - new_max_game / old_max_game) * 100
        print(f"  OLD: ${old_max_game:,.2f} ({pct(old_max_game, BANKROLL):.2f}% of bankroll)")
        print(f"  NEW: ${new_max_game:,.2f} ({pct(new_max_game, BANKROLL):.2f}% of bankroll)")
        print(f"  Reduction: {reduction:.1f}%")
    else:
        print("  OLD had zero exposure (unexpected); new path:", new_max_game)


if __name__ == "__main__":
    main()
