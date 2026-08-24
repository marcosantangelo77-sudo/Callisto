"""Attach CME ZQ market-implied probabilities to retrodiction questions.

The deliverable beyond the adapter: RetrodictionQuestion.market_implied is
the benchmark tools/simulation.py and batch.magnitude_score already consume
(edge = model_prob − market_implied). This script fills it for macro/rate
questions whose FOMC meeting falls inside an available ZQ contract month,
refusing any attachment where the settlement's trade date is not strictly
before the question's claim_date (W5 leakage guard).

LIVE-ONLY (network opt-in): never call from tests.
  CALLISTO_ENABLE_NETWORK=1 python3 scripts/attach_market_implied.py \
      data/retro_questions.json [--trade-date YYYYMMDD] [--rate 4.25]

Questions without a covering contract month keep market_implied=None —
honest absence, not a fabricated benchmark. Provenance for every attached
value (source URL + sha256 + trade date) prints with the summary and should
be archived next to the question file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.retrodiction.questions import load_questions  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question_file")
    ap.add_argument("--trade-date", required=True,
                    help="CME trade date of the settlements, YYYYMMDD")
    ap.add_argument("--current-rate", type=float, required=True,
                    help="current target-range UPPER bound in percent")
    args = ap.parse_args()

    from tools.sources.cmefedfut import (CmeFedFutAdapter, SPEC,
                                         attach_from_derived, make_adapter)

    adapter: CmeFedFutAdapter = make_adapter()   # opt-in gated
    curve = adapter.zq_curve(args.trade_date)
    qs = load_questions(args.question_file)

    derived = {}
    for q in qs:
        text = q.text.lower()
        if not any(w in text for w in ("fed", "fomc", "rate", "cut", "hike")):
            continue
        if q.claim_date is None:
            continue
        meeting = q.resolution_date or q.claim_date
        d = adapter.implied_probability(
            meeting.isoformat(), args.current_rate, curve=curve)
        if d is not None:
            derived[q.question_id] = d

    skipped = attach_from_derived(qs, derived)
    attached = [q for q in qs if q.market_implied is not None]

    print(json.dumps({
        "questions_total": len(qs),
        "benchmarks_attached": len(attached),
        "skipped": skipped,
        "provenance": {
            "class": "INFERRED from PRIMARY",
            "source": SPEC.name,
            "trade_date": args.trade_date,
            "url": curve["_fetch"]["url"],
            "sha256": curve["_fetch"]["sha256"],
        },
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
