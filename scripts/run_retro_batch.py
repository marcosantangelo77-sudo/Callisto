"""I4 — run the retrodiction batch. Resumable; Ctrl-C costs one question.

Usage:
  python3 scripts/run_retro_batch.py --questions data/retro_questions.json \
      [--limit 5] [--label smoke5] [--results data/retro_results_smoke.jsonl]

The live model is HermesCliModel (tools/pipeline/hermes_cli.py). Evidence
acquisition goes through the pipeline's real source registry; the Wayback
adapter is the intended proof path for cutoff verification.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.pipeline.checkpoint import FileCheckpointer          # noqa: E402
from tools.pipeline.hermes_cli import HermesCliModel, hermes_available  # noqa: E402
from tools.pipeline.retro import PipelineResearcher             # noqa: E402
from tools.retrodiction.batch import (                          # noqa: E402
    BatchConfig,
    RetrodictionBatch,
    build_report,
    render_report,
    write_routing_scores,
)
from tools.routing.scores import ModelScoreStore                # noqa: E402


def make_researcher_factory():
    assert hermes_available(), "hermes CLI not found"
    model = HermesCliModel(timeout_s=300.0)

    def factory():
        return PipelineResearcher(
            model=model,
            routes={},   # no fixture routes — real transport via registry
            adversary_router=model,
        )
    return factory


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True)
    ap.add_argument("--results", default="data/retro_batch/results.jsonl")
    ap.add_argument("--report", default="data/retro_batch/report.json")
    ap.add_argument("--checkpoints", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--label", default="batch")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    from retro_questions_i4 import load_set

    questions = load_set(args.questions)
    print(f"loaded {len(questions)} questions")

    cp = FileCheckpointer(root=Path(args.checkpoints)) \
        if args.checkpoints else FileCheckpointer()
    results_path = Path(args.results)

    batch = RetrodictionBatch(
        questions=questions,
        researcher_factory=make_researcher_factory(),
        checkpointer=cp,
        results_path=results_path,
        config=BatchConfig(label=args.label, limit=args.limit,
                           model_name=HermesCliModel.name))

    done_before = len(batch.load_completed())
    print(f"resume state: {done_before} already complete")

    try:
        results = asyncio.run(batch.run())
    except KeyboardInterrupt:
        print("\ninterrupted — progress saved; rerun the same command to resume")
        results = batch.results

    report = build_report(results)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=2))
    n = write_routing_scores(results, ModelScoreStore())
    print(f"routing store: +{n} observations")
    print(render_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
