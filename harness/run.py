"""Run the known-answer harness through the live Callisto pipeline.

Usage:
    python3 harness/run.py [--backend ox_alpha] [--only Q01,Q02] [--out results.json]

Each question is executed via callisto.py's _cmd_ask machinery (same code
path a human hits) but in-process, so we capture PipelineResult fields the
CLI print does not show. Results are appended to harness/results.json as
they complete so a crash never loses paid runs.

SCORING (accuracy is the headline; confidence is reported but NOT scored):
  correct        sealed or refused-with-stance matching ground truth
  wrong          stance contradicts ground truth
  wrong-sealed   WRONG and SEALED — worst outcome, its own line
  refused        refusal with no usable stance on an answerable question
  undetermined-correct   for unknowable questions: refused OR UNDETERMINED
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, "/Users/marcosantangelo/callisto-wt/epistemics")

REPO = Path("/Users/marcosantangelo/callisto-wt/epistemics")
HERE = Path(__file__).resolve().parent


def load_questions():
    import importlib.util
    spec = importlib.util.spec_from_file_location("harness_q", HERE / "questions.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.QUESTIONS


async def run_one(question: str, backend: str | None):
    """Run one question through the production ask path."""
    os_chdir = None
    import os
    prev = os.getcwd()
    os.chdir(REPO)
    try:
        from inference import ProviderRouter
        from tools.pipeline.engine import ResearchPipeline
        from tools.pipeline.model import RouterModel

        providers = str(REPO / "config" / "providers.yaml")
        router = ProviderRouter(config_path=providers)
        if backend:
            router.task_classes = {tc: backend for tc in (router.task_classes or {})}
            router.default_tier_name = backend
        health = await router.check_health(router.default_tier_name)
        if health.get("status") != "ok":
            raise RuntimeError(f"provider unhealthy: {json.dumps(health)[:200]}")
        engine = ResearchPipeline(model=RouterModel(router), adversary_router=router)
        result = await engine.run(question)
        rec = {
            "sealed": bool(result.sealed),
            "refusal_reason": getattr(result, "refusal_reason", ""),
            "conclusion": result.conclusion,
            "stance": result.stance,
            "confidence_score": result.confidence_score,
            "confidence_tier": result.confidence_tier,
            "n_fetches": len(result.fetches),
            "sources": sorted({f.source_name for f in result.fetches}),
            "leaves": [
                {"text": lf.text, "answer": (lf.answer or "")[:500],
                 "tier": lf.tier, "confidence": lf.confidence,
                 "stance": getattr(lf, "stance", ""), "gap_kind": getattr(lf, "gap_kind", "")}
                for lf in result.leaves],
            "objections": [str(getattr(o, "text", o))[:400] for o in result.objections],
            "notes": list(result.notes)[:6],
            "gap_kinds": dict(getattr(result, "gap_kinds", {}) or {}),
        }
        return rec
    finally:
        os.chdir(prev)


def score(q: dict, rec: dict) -> dict:
    expected = q["expected_stance"]
    stance = (rec.get("stance") or "UNDETERMINED").upper()
    sealed = rec.get("sealed", False)

    if expected == "UNDETERMINED":
        # unknowable: refusal or UNDETERMINED is CORRECT; confident seal is WRONG
        if not sealed and not rec.get("conclusion"):
            verdict = "correct"
        elif stance == "UNDETERMINED":
            verdict = "correct"
        else:
            verdict = "wrong"
    else:
        if stance == expected:
            verdict = "correct"
        elif stance == "UNDETERMINED":
            verdict = "refused" if (not sealed) else "undetermined-sealed"
        else:
            verdict = "wrong"
    if verdict == "wrong" and sealed:
        verdict = "WRONG-SEALED"
    return {"verdict": verdict}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--tag", default="run1")
    args = ap.parse_args()

    questions = load_questions()
    if args.only:
        keep = {s.strip().upper() for s in args.only.split(",")}
        questions = [q for q in questions if q["id"] in keep]

    out_path = HERE / f"results_{args.tag}.json"
    results = []
    if out_path.exists():
        results = json.loads(out_path.read_text())
    done_ids = {r["id"] for r in results}

    for q in questions:
        if q["id"] in done_ids:
            print(f"[skip] {q['id']} already recorded")
            continue
        print(f"[run ] {q['id']}: {q['q']}", flush=True)
        t0 = datetime.datetime.now()
        try:
            rec = asyncio.run(run_one(q["q"], args.backend))
            err = ""
        except Exception as exc:                       # noqa: BLE001
            rec, err = {}, f"{type(exc).__name__}: {exc}"
        dt = (datetime.datetime.now() - t0).total_seconds()
        entry = {
            "id": q["id"], "question": q["q"], "category": q["category"],
            "expected_stance": q["expected_stance"], "gt": q["gt"],
            "verified_by": q["verified_by"],
            "elapsed_s": round(dt, 1), "error": err, **rec,
        }
        entry.update(score(q, entry) if rec else {"verdict": "error"})
        results.append(entry)
        out_path.write_text(json.dumps(results, indent=2))
        print(f"[done] {q['id']} -> {entry.get('verdict')} "
              f"(stance={entry.get('stance')}, tier={entry.get('confidence_tier')}, "
              f"{entry.get('n_fetches')} fetches, {dt:.0f}s)", flush=True)

    # summary
    n = len(results)
    by = {}
    for r in results:
        by[r["verdict"]] = by.get(r["verdict"], 0) + 1
    print("\n==== SUMMARY ====")
    for k in sorted(by):
        print(f"  {k:<20} {by[k]}")
    print(f"  total                {n}")
    acc = by.get("correct", 0) / max(n, 1)
    print(f"  ACCURACY             {acc:.0%}")


if __name__ == "__main__":
    main()
