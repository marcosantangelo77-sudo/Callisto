"""One-shot backtest runner for tools.arbitrage_scanner.

Runs against a read-only handle to the live DB so we don't interfere with
Callisto's writes. Prints a JSON summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure the repo root is on sys.path so `tools.` imports work.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.arbitrage_scanner import backtest_arbs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.getenv(
        "CALLISTO_DB_PATH",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "memory", "callisto.db"),
    ))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--epsilon", type=float, default=0.002)
    ap.add_argument("--stale-seconds", type=float, default=86400.0)
    ap.add_argument("--budget", type=float, default=1000.0)
    args = ap.parse_args()

    if not os.path.exists(args.db):
        print(f"DB not found at {args.db}", file=sys.stderr)
        return 2

    res = backtest_arbs(
        db_path=args.db,
        days=args.days,
        epsilon=args.epsilon,
        stale_seconds=args.stale_seconds,
        budget=args.budget,
    )
    print(json.dumps(res, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
