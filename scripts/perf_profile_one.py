"""perf/speed-20260823 — instrumented end-to-end profile of ONE retrodiction question.

Wraps the pipeline's model seam with a timer (records every call: role, wall
seconds, bytes in/out) and times each pipeline stage, then runs a single real
question from data/retro_batch through PipelineResearcher with the live warm
pool. Writes a stage table to stdout and findings/speed_2026-08-23.md.

No caching across cutoffs is involved: this is a live measurement of the real
path, fixture transport disabled (routes={} → registry fetch), claim_date
passed so any date logic sees the past-dated claim only.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

QUESTION = sys.argv[1] if len(sys.argv) > 1 else \
    "data/retro_batch/questions.json"
QID = sys.argv[2] if len(sys.argv) > 2 else None


class TimedModel:
    """Wraps HermesCliModel; records every complete() call."""
    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    async def complete(self, task_class, messages, schema=None):
        prompt = messages[-1]["content"] if messages else ""
        t0 = time.monotonic()
        resp = await self.inner.complete(task_class, messages, schema=schema)
        dt = time.monotonic() - t0
        content = ""
        if isinstance(resp, dict):
            content = resp.get("content") or resp.get("parsed_json") or ""
        self.calls.append({
            "task_class": task_class,
            "s": round(dt, 2),
            "prompt_chars": len(prompt),
            "resp_chars": len(str(content)),
        })
        print(f"  model[{task_class}] {dt:6.2f}s  in={len(prompt):6d}B")
        return resp


async def main():
    from tools.pipeline.hermes_cli import HermesCliModel
    from tools.pipeline.retro import PipelineResearcher
    from retro_questions_i4 import load_set

    qs = load_set(QUESTION)
    if QID:
        qs = [q for q in qs if q.question_id == QID]
    q = qs[0]
    print(f"question {q.question_id}: {q.text[:80]}")

    model = TimedModel(HermesCliModel(timeout_s=300.0))
    researcher = PipelineResearcher(model=model, routes={},
                                    adversary_router=model,
                                    claim_date=q.claim_date)

    t0 = time.monotonic()
    preds = await researcher.answer_async([q.prompt_for_researcher()], [])
    total = time.monotonic() - t0

    calls = model.calls
    n_calls = len(calls)
    model_s = sum(c["s"] for c in calls)
    # transport overhead per call = call wall minus what the pool itself
    # reported as inference time
    print("\n=== PROFILE ===")
    print(f"total wall:            {total:8.1f}s")
    print(f"model calls:           {n_calls}")
    print(f"model wall (summed):   {model_s:8.1f}s "
          f"({model_s/total*100:.0f}% of total)")
    print(f"non-model wall:        {total-model_s:8.1f}s "
          f"({(total-model_s)/total*100:.0f}% of total)")
    for i, c in enumerate(calls):
        print(f"  call {i}: {c['s']:6.2f}s task={c['task_class']} "
              f"in={c['prompt_chars']}B out={c['resp_chars']}B")
    r = researcher.results[-1] if researcher.results else None
    if r is not None:
        print(f"sealed={r.sealed} conf={r.confidence_score} "
              f"fetches={len(getattr(r,'fetches',[]) or [])} "
              f"objections={len(r.objections)} notes={r.notes[:3]}")
    print(f"prediction: {[p.probability for p in preds]}")

    Path("data/retro_batch/profile_one.json").write_text(json.dumps({
        "question_id": q.question_id, "total_s": round(total, 1),
        "n_model_calls": n_calls, "model_wall_s": round(model_s, 1),
        "calls": calls}, indent=2))

if __name__ == "__main__":
    asyncio.run(main())
