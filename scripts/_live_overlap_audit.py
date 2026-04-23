"""Read-only audit: for every pair of LIVE hypotheses, compute the
portfolio-correlation overlap% over the last N days. Prints any pair that
exceeds CALLISTO_MAX_LIVE_OVERLAP_PCT.

Uses the MAIN Callisto DB directly via a read-only URI — does NOT touch
state and does NOT restart the live process.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv(
    "CALLISTO_DB_PATH",
    "C:/Users/marco/OneDrive/Desktop/Callisto/memory/callisto.db",
)
WINDOW_DAYS = int(os.getenv("WINDOW_DAYS", "30"))
CAP = float(os.getenv("CALLISTO_MAX_LIVE_OVERLAP_PCT", "0.40"))


def main() -> None:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    cur = conn.cursor()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")

    cur.execute(
        "SELECT hypothesis_id, name, sport, market_type FROM hypotheses WHERE status='live'"
    )
    live = cur.fetchall()
    print(f"LIVE hyps: {len(live)}")
    print(f"Window: last {WINDOW_DAYS} days (>= {cutoff})")
    print(f"Cap:    {CAP:.0%}\n")

    # Preload each LIVE hyp's distinct signal event_ids in window.
    events_by_hyp: dict[str, set[str]] = {}
    for hid, *_ in live:
        cur.execute(
            "SELECT DISTINCT event_id FROM backtest_events "
            "WHERE hypothesis_id = ? AND signal_generated = 1 "
            "AND game_date >= ?",
            (hid, cutoff),
        )
        events_by_hyp[hid] = {r[0] for r in cur.fetchall()}

    names = {h[0]: h[1] for h in live}

    offenders = []
    for a in live:
        for b in live:
            if a[0] == b[0]:
                continue
            ea = events_by_hyp[a[0]]
            eb = events_by_hyp[b[0]]
            if not ea:
                continue
            # overlap% = |a ∩ b| / |a|  (directional: what fraction of A's
            # signals are also B's)
            shared = ea & eb
            pct = len(shared) / len(ea) if ea else 0.0
            if pct > CAP:
                offenders.append({
                    "a": a[0],
                    "a_name": names[a[0]],
                    "a_n": len(ea),
                    "b": b[0],
                    "b_name": names[b[0]],
                    "b_n": len(eb),
                    "shared": len(shared),
                    "overlap_pct": pct,
                })

    offenders.sort(key=lambda r: -r["overlap_pct"])

    print(f"{'A (would-fail)':40s} {'B (existing LIVE)':40s} {'A_n':>5s} {'B_n':>5s} {'shared':>7s} {'pct':>6s}")
    print("-" * 110)
    distinct_fail_ids = set()
    for r in offenders:
        distinct_fail_ids.add(r["a"])
        print(f"{r['a_name'][:39]:40s} {r['b_name'][:39]:40s} "
              f"{r['a_n']:5d} {r['b_n']:5d} {r['shared']:7d} {r['overlap_pct']*100:5.1f}%")

    print()
    print(f"Total pair-wise violations: {len(offenders)}")
    print(f"Distinct hyps that would fail the gate: {len(distinct_fail_ids)} / {len(live)}")

    # Per-hyp signal counts (diagnostic)
    print("\nPer-hyp signal counts in window:")
    for hid, ev in sorted(events_by_hyp.items(), key=lambda x: -len(x[1])):
        print(f"  {names[hid][:50]:50s} n_signals={len(ev)}")


if __name__ == "__main__":
    main()
