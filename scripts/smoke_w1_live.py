"""W1 live smoke: one real sub-question through the iterative retriever
against the live OpenAlex API. No key needed. Run directly, not under the
no-socket guard."""
import asyncio
import json
from datetime import date

from agp.research_program import (EvidenceRequirement, QuestionKind,
                                  ResearchQuestion, SourceClassRank)
from tools.pipeline.engine import ResearchPipeline

QUESTION = ("What does recent scholarly research say about semiconductor "
            "supply chain resilience?")


async def main():
    from agp.provenance import ProvenanceLedger
    from tools.artifacts import ArtifactStore
    from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate

    # Direct retriever run first — this is the honest look at what comes back.
    reg_mod = __import__("tools.sources.registry", fromlist=["x"])
    reg = reg_mod.get_source_registry()
    rq = ResearchQuestion(text=QUESTION, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY, min_independent_sources=2)

    ret = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(), transport=None,
        gate=RelevanceGate(min_coverage=0.25), max_rounds=3)
    trace = ret.retrieve(rq, "", min_independent=2)
    print("=== LIVE OPENALEX SMOKE ===")
    print("queries:", trace.queries)
    for r in trace.rounds:
        for s in r["sources"]:
            print(f"  round {r['round']} {s['name']}: "
                  f"{('ADMITTED rel=' + str(s.get('relevance'))) if s.get('admitted') else s}")
    print("stop:", trace.stop_reason)
    print("independent keys:", trace.independent_keys)
    for f in trace.admitted:
        titles = []
        if isinstance(f.parsed, dict):
            for item in f.parsed.get("results", [])[:3]:
                t = item.get("title") or item.get("display_name")
                if t:
                    titles.append(t[:90])
        print(f"  {f.source_name} -> {f.url[:100]}")
        for t in titles:
            print(f"     hit: {t}")
    for rej in trace.rejected:
        print(f"  REJECTED [{rej.source_name}] {rej.reason[:140]}")


if __name__ == "__main__":
    asyncio.run(main())
