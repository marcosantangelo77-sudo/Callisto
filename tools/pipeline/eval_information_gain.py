"""Golden-run evaluation for expected-information-gain retrieval.

For each golden scenario: run the full pipeline twice over the SAME
fixtures — adaptive_gain=False (plan-then-fetch baseline) vs True.
Success = identical leaf answers/stances/gap verdicts and parent
conclusion, with fewer fetches issued.

Run: python3 tools/pipeline/eval_information_gain.py
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import sys
from datetime import date

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402

GOOD = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2024},
    {"id": "W2", "title": "Chips act industrial policy",
     "publication_year": 2023},
]})
IRRELEVANT = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})


class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _model(decompose: str):
    return ScriptedModel({
        "Architect": [{"content": decompose}],
        "Manager": [{"content": json.dumps(
            {"answer": "evidence reviewed",
             "proposed_confidence": 0.8})}],
    })


DECOMPOSE_ONE = json.dumps({"sub_questions": [{
    "text": "what does scholarly research say about semiconductor supply "
            "chain resilience",
    "kind": "descriptive",
    "question_type": "scholarly literature about semiconductor supply "
                     "chains",
    "min_source_tier": 2, "min_independent_sources": 2}]})

DECOMPOSE_TWO = json.dumps({"sub_questions": [
    {"text": "what does scholarly research say about semiconductor supply "
             "chain resilience",
     "kind": "descriptive",
     "question_type": "scholarly literature about semiconductor supply "
                      "chains",
     "min_source_tier": 2, "min_independent_sources": 1},
    {"text": "what do news events say about semiconductor supply chains",
     "kind": "descriptive",
     "question_type": "news events about semiconductor supply chains",
     "min_source_tier": 2, "min_independent_sources": 2},
]})


def _run(decompose, routes, adaptive_gain):
    """Run the pipeline; return a comparable summary + fetch count."""
    from tools.sources.registry import SourceRegistry, SourceAdapter
    from tools.sources.base import SourceSpec

    reg = SourceRegistry()
    specs = {
        "alpha": ("scholarly works on semiconductor supply chain "
                  "resilience", "https://api.openalex.org"),
        "beta": ("news events about semiconductor supply chains",
                 "https://api.gdeltproject.org"),
        "gamma": ("agency rules about supply chains", "https://c.example"),
    }
    calls = {}

    def make_adapter(source):
        path = "/fetch_" + source.spec.name

        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    term = next((a for a in args if isinstance(a, str)),
                                kwargs.get("query_term", "q"))
                    url = source.build_url(
                        path, {"search": term.replace(" ", "+")})
                    return source.get_json(url)[0]
                return call
        return _Ad()

    for name, (answers, url) in specs.items():
        spec = SourceSpec(name=name, base_url=url, description="",
                          answers=(answers,), tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
        calls[name] = ("works_search", ("term",), {"limit": 3})

    pipe = ResearchPipeline(
        model=_model(decompose), adversary_router=_Quiet(),
        transport=fixture_transport(routes), store=None,
        ledger=ProvenanceLedger(), registry=reg)

    from tools.pipeline import retrieval as R
    orig_init = R.IterativeRetriever.__init__

    def patched(self, *a, **kw):
        kw.pop("adaptive_gain", None)
        orig_init(self, *a, adaptive_gain=adaptive_gain,
                  generic_calls=calls, **{k: v for k, v in kw.items()
                                          if k != "generic_calls"})
    R.IterativeRetriever.__init__ = patched
    try:
        result = asyncio.run(pipe.run(
            "What does research say about semiconductor supply chains?",
            today=date(2026, 8, 22)))
    finally:
        R.IterativeRetriever.__init__ = orig_init

    # sort by TEXT (question_id is a fresh uuid per run)
    leaves = sorted(
        (l.text[:50], l.answer, l.stance, l.confidence,
         l.tier, l.gap_kind or "") for l in result.leaves)
    return {
        "n_fetches": len(result.fetches),
        "n_rounds_total": sum(len(getattr(r, "rounds", []) or [])
                              for r in [result.trace] if r is not None),
        "leaves": leaves,
        "conclusion_tail": (result.conclusion or "").split("\n")[-3:] if
        result.conclusion else [],
        "sealed": result.sealed,
    }


SCENARIOS = {
    # one leaf needing 2 independent voices; only alpha relevant ->
    # baseline burns the round budget re-fetching; gain gate stops when
    # nothing new can be satisfied... (alpha keeps its voice counted)
    "single-leaf-duplicate-rounds": dict(
        decompose=DECOMPOSE_ONE,
        routes={"/fetch_alpha": GOOD}),
    # two leaves, second leaf's sources keep returning junk
    "two-leaf-junk-second": dict(
        decompose=DECOMPOSE_TWO,
        routes={"/fetch_alpha": GOOD, "/fetch_beta": IRRELEVANT,
                "/fetch_gamma": IRRELEVANT}),
}


def main():
    print(f"{'scenario':32s} {'baseline':>9s} {'gain-gated':>11s} "
          f"conclusion")
    total_base = total_gain = 0
    changed = 0
    for name, sc in SCENARIOS.items():
        base = _run(sc["decompose"], sc["routes"], adaptive_gain=False)
        gain = _run(sc["decompose"], sc["routes"], adaptive_gain=True)
        same_conclusion = (
            base["leaves"] == gain["leaves"]
            and base["sealed"] == gain["sealed"])
        if not same_conclusion:
            changed += 1
        total_base += base["n_fetches"]
        total_gain += gain["n_fetches"]
        verdict = "IDENTICAL" if same_conclusion else "!! CHANGED"
        print(f"{name:32s} {base['n_fetches']:>9d} "
              f"{gain['n_fetches']:>11d}  {verdict}")
        if not same_conclusion:
            print("  BASE:", json.dumps(base, default=str)[:600])
            print("  GAIN:", json.dumps(gain, default=str)[:600])
    print(f"\nTOTAL fetches: baseline={total_base} gain-gated="
          f"{total_gain} saved={total_base - total_gain}")
    print(f"Conclusions changed: {changed} (must be 0)")
    return 0 if changed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
