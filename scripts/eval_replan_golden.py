"""Golden-run proof for gap-triggered re-planning.

Runs every scripts.golden_corpus case through the FULL pipeline twice —
re-plan ENABLED vs DISABLED (the off switch: never call _maybe_replan_leaf)
— with a scripted model whose re-plan turn produces a replacement
sub-question, and compares:

  - conclusion quality per leaf (answered? which tier/stance inputs?)
  - fetch count and elapsed time

The model is scripted identically for both arms except that the disabled
arm never consumes the re-plan response, so any difference is attributable
to the structural change alone. Writes JSON to findings/replan_golden.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ssl  # noqa: F401 — before the socket guard patches socket.socket

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()
logging.disable(logging.CRITICAL)

from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from scripts.golden_corpus import build_cases  # noqa: E402


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _answer_for(titles):
    if not titles:
        return {"content": "{}"}
    return {"content": json.dumps({
        "answer": f"evidence from {len(titles)} source item(s) supports "
                  "a bounded answer",
        "proposed_confidence": 0.55})}


def run_case(case, replan_enabled: bool) -> dict:
    """One golden case, one arm. The scripted model answers leaves with a
    deterministic function of how many admissible items it saw, so an arm
    that reaches better evidence writes a better answer."""
    routes = case["routes"]

    def transport(url, headers):
        for pattern, body in routes.items():
            if pattern in url:
                return 200, body
        return 404, '{"error": "no fixture route"}'

    class CountingModel(ScriptedModel):
        """Answer quality tracks admitted evidence size; re-plan turn emits
        a replacement question_type aimed at whichever route exists."""
        def __init__(self):
            super().__init__()
            self.n_answers = 0
            self.n_replans = 0

        async def complete(self, role, messages, **_ig):
            prompt = "\n".join(m.get("content", "") for m in messages)
            if role == "Architect":
                if "RE-PLAN" in prompt or "Re-plan" in prompt:
                    self.n_replans += 1
                    # Re-aim at evidence this fixture HAS: agency rules.
                    return {"content": json.dumps({"sub_questions": [{
                        "text": case["qtext"] + " (re-aimed)",
                        "kind": "descriptive",
                        "question_type": "final/proposed agency rules with "
                                         "dates and docket refs",
                        "min_source_tier": 2,
                        "min_independent_sources":
                            case.get("min_ind", 2)}]})}
                from tools.pipeline.model import decompose_messages
                base = decompose_messages(case["qtext"])
                spec = {"text": case["qtext"], "kind": "descriptive",
                        "question_type": case["qtype"],
                        "min_source_tier": 2,
                        "min_independent_sources": case.get("min_ind", 2)}
                return {"content": json.dumps(
                    {"sub_questions": [spec]})} if prompt == \
                    "\n".join(m["content"] for m in base) else \
                    {"content": "{}"}
            if role == "Manager":
                # Count admissible evidence lines handed to the answerer.
                n = sum(1 for line in prompt.splitlines()
                        if line.startswith("- ["))
                self.n_answers += 1
                return _answer_for(range(n))
            return {"content": "{}"}

    model = CountingModel()
    pipe = ResearchPipeline(model=model, adversary_router=_QuietAdversary(),
                            transport=transport)
    t0 = time.perf_counter()

    original = ResearchPipeline._maybe_replan_leaf
    if not replan_enabled:
        async def _disabled(self, *a, **k):
            return None, None, None
        ResearchPipeline._maybe_replan_leaf = _disabled
    try:
        result = asyncio.new_event_loop().run_until_complete(
            pipe.run(case["qtext"], today=date(2026, 8, 22)))
    finally:
        ResearchPipeline._maybe_replan_leaf = original
    elapsed = time.perf_counter() - t0

    answered = [l for l in result.leaves if l.answer]
    return {
        "case": case["name"],
        "sealed": result.sealed,
        "refusal_reason": result.refusal_reason[:120],
        "n_leaves": len(result.leaves),
        "n_answered": len(answered),
        "n_fetches": len(result.fetches),
        "distinct_sources": sorted({f.source_name for f in result.fetches}),
        "best_tier": max((l.tier for l in result.leaves),
                         key=lambda t: {"SPECULATIVE": 0,
                                        "LOW": 1}.get(t, 1)) if
                    result.leaves else "",
        "gap_kinds": dict(result.gap_kinds),
        "n_replan_consults": model.n_replans,
        "elapsed_s": round(elapsed, 3),
        "conclusion_head": result.conclusion[:200],
    }


def main() -> int:
    cases = build_cases()
    # Add one conversion case: the decomposed leaf's question text is
    # deliberately source-ambiguous (no topical core any planner can route),
    # so round 1 reaches nothing and classifies as an actionable retrieval
    # failure; the re-plan re-aims at agency rules, which /documents serves.
    cases.append(dict(
        name="R1 replan-converts-failure",
        # Question text with a real topical core (routes the planner) but
        # a qtype whose selected sources this fixture does NOT serve, so
        # round 1 reaches nothing actionable; the re-plan re-aims at
        # agency rules, which /documents serves.
        qtext="CPIAUCSL annual inflation trend",
        qtype="scholarly work search",
        min_ind=1,
        routes={"/documents.json": json.dumps({"documents": [
            {"title": "Consumer Price Index observations series data with "
                      "final agency rule published by the government",
             "document_number": "2024-99999",
             "published_at": "2024-12-18"}]})},
    ))
    out = []
    print(f"{'case':44s} {'OFF f/a':>9s} {'ON f/a':>9s} "
          f"{'sealed':>13s} {'replans':>7s}")
    for case in cases:
        off = run_case(case, replan_enabled=False)
        on = run_case(case, replan_enabled=True)
        out.append({"off": off, "on": on})
        print(f"{off['case']:44s} "
              f"{off['n_fetches']}/{off['n_answered']:>2d}   "
              f"{on['n_fetches']}/{on['n_answered']:>2d}   "
              f"{str(off['sealed'])[:5]:>5s}->{str(on['sealed'])[:5]:<5s} "
              f"{on['n_replan_consults']:>7d}")
    dest = Path(__file__).resolve().parents[1] / "findings" / \
        "replan_golden.json"
    dest.write_text(json.dumps(out, indent=2))
    print("\nwrote", dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
