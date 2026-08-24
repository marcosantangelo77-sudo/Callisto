"""One real question, end to end, live.

Question: "Has the US unemployment rate been lower in 2026 than it was in
January 2023?" — chosen because the working sources answer it directly:
  fred  (PRIMARY): UNRATE monthly observations, series id resolvable from
        the planner's curated concept table (unemployment -> UNRATE)
  bls   (PRIMARY): LNS14000000 same series from the second independent
        statistical agency
Both adapters are healthy (source-health OK), both are plannable without
model calls, and the question is a DESCRIPTIVE trend claim — no broken
source needed.

Run BY HAND only (opens sockets; the pytest no-socket guard forbids it).
Usage: python3 scripts/oxa_one_real_question.py [--question "..."]
"""
import asyncio
import json
import os
import sys
from datetime import date

sys.path.insert(0, ".")

# Load API keys from the dispatcher's env file. Never print or commit values.
_ENV = "/Users/marcosantangelo/callisto-wt/.env.local"
if os.path.exists(_ENV):
    for line in open(_ENV):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k, v)

from tools.pipeline.engine import ResearchPipeline
from tools.pipeline.hermes_cli import HermesCliModel, hermes_available
from tools.pipeline.retrieval import independence_key


def describe(result) -> str:
    lines = []
    lines.append(f"sealed: {result.sealed}")
    if result.refusal_reason:
        lines.append(f"refusal_reason: {result.refusal_reason}")
    fetches = result.fetches
    sources = sorted({f.source_name for f in fetches})
    hosts = sorted({independence_key(f.source_name, f.url)
                    for f in fetches})
    lines.append(f"fetches admitted: {len(fetches)} from sources {sources}")
    lines.append(f"independent keys ({len(hosts)}): {hosts}")
    lines.append(f"confidence: {result.confidence_score} "
                 f"({result.confidence_tier}); stance: {result.stance}")
    lines.append(f"gap_kinds: {result.gap_kinds}")
    lines.append("leaves:")
    for l in result.leaves:
        lines.append(f"  - [{l.tier} conf={l.confidence} "
                     f"est={l.confidence_estimate} ceil={l.confidence_ceiling}"
                     f"] stance={l.stance} n_sources={l.n_sources} "
                     f"classes={sorted(set(l.source_classes))} "
                     f"gap={l.gap_kind or '-'} :: {l.text[:90]}")
        if l.requirement_reasons:
            lines.append(f"      unmet requirements: {l.requirement_reasons}")
        lines.append(f"      answer: {l.answer[:300]}")
    lines.append("notes:")
    for n in result.notes:
        lines.append(f"  * {n[:250]}")
    lines.append("objections:")
    for o in result.objections:
        lines.append(f"  - [{getattr(o, 'severity', '?')}] "
                     f"{getattr(o, 'text', str(o))[:250]}")
    lines.append("conclusion:")
    lines.append(result.conclusion[:1500])
    return "\n".join(lines)


async def main():
    assert hermes_available(), "hermes CLI not found"
    model = HermesCliModel(timeout_s=540.0)
    pipe = ResearchPipeline(model=model)
    q = ("Has the US unemployment rate been lower in 2026 than it was in "
         "January 2023?")
    result = await pipe.run(q, today=date.today())
    print(describe(result))
    # Persist a machine-readable record for the findings doc.
    out = {"root_query": result.root_query,
           "sealed": result.sealed,
           "refusal_reason": result.refusal_reason,
           "confidence": result.confidence_score,
           "tier": result.confidence_tier,
           "stance": result.stance,
           "n_fetches": len(result.fetches),
           "sources": sorted({f.source_name for f in result.fetches}),
           "gap_kinds": result.gap_kinds,
           "conclusion": result.conclusion,
           "objections": [getattr(o, "text", str(o))
                          for o in result.objections]}
    path = "findings/one_real_question_run.json"
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2)
    print("\nsaved:", path)


if __name__ == "__main__":
    asyncio.run(main())
