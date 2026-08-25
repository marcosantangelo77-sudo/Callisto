"""SPEED run 19 — measure serial vs bounded-concurrent batch wall time.

Offline: stub researcher with per-question latency (stands in for the
~25-30s of model-call time a real question costs through the warm proxy).
Prints a stage/wall table and the serial:parallel ratio.

  python3 scripts/measure_batch_concurrency.py [--latency 1.0] [--nq 5]
      [--mc 3]
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipeline.checkpoint import FileCheckpointer          # noqa: E402
from tools.retrodiction.batch import BatchConfig, RetrodictionBatch  # noqa: E402
from tools.retrodiction.questions import (                      # noqa: E402
    QuestionType, RetrodictionQuestion)
from tools.retrodiction.scoring import Prediction               # noqa: E402


class LatencyResearcher:
    name = "latency-stub"

    def __init__(self, latency: float):
        self.latency = latency
        self.calls = 0

    async def answer(self, prompts, evidence, loops=1):
        out = []
        for p in prompts:
            self.calls += 1
            await asyncio.sleep(self.latency)
            out.append(Prediction(question_id=p["question_id"],
                                  probability=0.5))
        return out


def _questions(n: int):
    from datetime import date
    return [RetrodictionQuestion(
        question_id=f"q{i}",
        text=f"retro latency question {i}",
        domain="FINANCIAL",
        question_type=QuestionType.BEAT_OR_MISS,
        claim_date=date(2024, 1, 1), resolution_date=date(2024, 3, 1),
        answer_binary=True, answer_confidence=1.0, market_implied=None,
    ) for i in range(n)]


def _run(qs, latency, mc, root: Path):
    r = LatencyResearcher(latency)
    b = RetrodictionBatch(
        questions=qs, researcher_factory=lambda: r,
        checkpointer=FileCheckpointer(root=root / "cp"),
        results_path=root / "results.jsonl",
        config=BatchConfig(label="measure", max_concurrency=mc))
    t0 = time.monotonic()
    results = asyncio.run(b.run())
    wall = time.monotonic() - t0
    return wall, r.calls, results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--latency", type=float, default=1.0)
    ap.add_argument("--nq", type=int, default=5)
    ap.add_argument("--mc", type=int, default=3)
    args = ap.parse_args()

    qs = _questions(args.nq)
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        t_ser, c1, r1 = _run(qs, args.latency, 1, root / "serial")
        t_par, c2, r2 = _run(qs, args.latency, args.mc, root / "par")

    assert {k: v.predicted_probability for k, v in r1.items()} == \
           {k: v.predicted_probability for k, v in r2.items()}, \
        "REGRESSION: answers changed under concurrency"
    assert all(v.status == "scored" for v in r2.values())

    print(f"questions={args.nq} per_question_latency={args.latency}s "
          f"max_concurrency={args.mc}")
    print(f"{'mode':<12} {'wall_s':>8} {'model_calls':>12}")
    print(f"{'serial':<12} {t_ser:>8.2f} {c1:>12}")
    print(f"{'concurrent':<12} {t_par:>8.2f} {c2:>12}")
    print(f"speedup: {t_ser / t_par:.2f}x   answers identical: yes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
