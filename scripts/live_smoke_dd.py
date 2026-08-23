"""DD — live end-to-end run reporting DISTINCT INDEPENDENCE FAMILIES.

The metric three consecutive runs pinned at 1. Real question, real model
(Ox Alpha via Hermes CLI), real fetches. No SEC, no ClinicalTrials.gov
(both 403 this machine).

Reports BOTH:
  * planned families — what the decomposition could reach (decompose.py)
  * achieved families — independence keys of the fetches that actually
    returned (retrieval.independence_key)
"""
import asyncio
import sys
from datetime import date

sys.path.insert(0, ".")

from tools.pipeline.decompose import assess_program_diversity
from tools.pipeline.engine import ResearchPipeline
from tools.pipeline.hermes_cli import HermesCliModel, hermes_available
from tools.pipeline.retrieval import independence_key
from tools.sources.registry import get_source_registry


async def main():
    assert hermes_available(), "hermes CLI not found"
    model = HermesCliModel(timeout_s=240.0)
    pipe = ResearchPipeline(model=model)  # self-review adversary path

    q = ("What does recent scholarly research say about semiconductor "
         "supply chain resilience?")
    result = await pipe.run(q, today=date.today())

    reg = get_source_registry()
    planned = assess_program_diversity(
        reg, result.program, dict(getattr(pipe, "_question_types", {})))

    fetches = result.fetches
    achieved = sorted({independence_key(f.source_name, f.url)
                       for f in fetches})

    print("=== LIVE RUN — DECOMPOSITION DIVERSITY ===")
    print("question:", q)
    print("leaves:", len(result.leaves))
    for l in result.leaves:
        print(f"  - [{l.tier} {l.confidence}] {l.text[:80]}")
    print()
    print("PLANNED distinct independence families:",
          planned.n_families, sorted(planned.families))
    for fam, srcs in sorted(planned.family_sources.items()):
        print(f"    {fam}: {sorted(srcs)}")
    print("planned note:", planned.note)
    print("weak:", planned.weak)
    print()
    print("fetches:", len(fetches))
    print("distinct sources:", sorted({f.source_name for f in fetches}))
    print("ACHIEVED distinct independence families:",
          len(achieved), achieved)
    print()
    print("sealed:", result.sealed)
    print("refusal:", result.refusal_reason[:300])
    print("confidence:", result.confidence_score, result.confidence_tier)
    print("notes:")
    for n in result.notes:
        print("  *", n[:200])


if __name__ == "__main__":
    asyncio.run(main())
