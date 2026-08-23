#!/usr/bin/env python3
"""Source health check — run BY HAND, never from pytest.

Usage:
    CALLISTO_SOURCE_HEALTH_NET=1 python3 scripts/source_health.py [--json PATH]

Probes every registered source with a known-good live query, classifies
each as OK / DEGRADED / BROKEN / SKIPPED, prints a table, and (with
--json) writes machine-readable evidence. Refuses to run without the
CALLISTO_SOURCE_HEALTH_NET=1 gate; the normal suite is no-socket guarded
and must stay that way.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.sources.health import NET_GATE_ENV, run_all  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", metavar="PATH", help="also write JSON evidence")
    args = ap.parse_args()

    results = run_all()

    w = max(len(r.source) for r in results)
    print(f"{'SOURCE':<{w}}  VERDICT   TIME(s)  EVIDENCE")
    print("-" * 100)
    order = {"BROKEN": 0, "DEGRADED": 1, "OK": 2, "SKIPPED": 3}
    counts: dict[str, int] = {}
    for r in sorted(results, key=lambda r: (order[r.verdict], r.source)):
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
        print(f"{r.source:<{w}}  {r.verdict:<8}  {r.duration_s:6.1f}  "
              f"{r.evidence[:70]}")
        if r.url and r.verdict != "OK":
            print(f"{'':<{w}}  url: {r.url}")
    print("-" * 100)
    print(" | ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    healthy = counts.get("OK", 0)
    tested = sum(v for k, v in counts.items() if k != "SKIPPED")
    print(f"{healthy}/{tested} tested sources healthy "
          f"(SKIPPED = keyed or no probe)")

    if args.json:
        payload = {
            "gate": NET_GATE_ENV,
            "results": [r.__dict__ for r in results],
            "counts": counts,
        }
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=2)
        print(f"evidence written: {args.json}")

    # nonzero exit when any TESTED source is not OK — so this can be
    # cron'd or chained without parsing output
    return 0 if counts.get("OK", 0) == tested else 1


if __name__ == "__main__":
    sys.exit(main())
