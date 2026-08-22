"""I1 — live end-to-end run through the wired pipeline.

Real question, real model (Ox Alpha via the Hermes CLI), real fetches.
No SEC, no ClinicalTrials.gov (both 403 this machine).
"""
import asyncio
import json
import sys
from datetime import date

sys.path.insert(0, ".")

from tools.pipeline.engine import ResearchPipeline
from tools.pipeline.hermes_cli import HermesCliModel, hermes_available
from tools.pipeline.retrieval import independence_key


async def main():
    assert hermes_available(), "hermes CLI not found"
    model = HermesCliModel(timeout_s=240.0)
    pipe = ResearchPipeline(model=model)  # NO adversary_router — JOB 3 path

    q = ("What does recent scholarly research say about semiconductor "
         "supply chain resilience?")
    result = await pipe.run(q, today=date.today())

    fetches = result.fetches
    sources = sorted({f.source_name for f in fetches})
    hosts = sorted({independence_key(f.source_name, f.url) for f in fetches})
    print("=== LIVE RUN RESULT ===")
    print("sealed:", result.sealed)
    print("refusal:", result.refusal_reason[:300])
    print("fetches:", len(fetches))
    print("distinct sources:", sources)
    print("independent keys:", hosts, "-> count:", len(hosts))
    print("confidence:", result.confidence_score, result.confidence_tier)
    print("leaves:", len(result.leaves))
    for l in result.leaves:
        print(f"  - [{l.tier} {l.confidence}] {l.text[:70]}")
    print("notes:")
    for n in result.notes:
        print("  *", n[:200])
    print("objections:")
    for o in result.objections:
        print("  -", getattr(o, "text", str(o))[:300])
    print("conclusion (first 600):")
    print(result.conclusion[:600])
    print("model calls:", len(model.calls))
    if result.trace is not None:
        print("trace resumed:", result.trace.resumed_stages,
              "fresh:", result.trace.fresh_stages)


if __name__ == "__main__":
    asyncio.run(main())
