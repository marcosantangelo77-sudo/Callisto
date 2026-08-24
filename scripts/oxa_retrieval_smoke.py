"""One-off live retrieval smoke: what does the retriever actually fetch for
the unemployment-trend question, with keys loaded? Run by hand only."""
import sys, os, json, logging

sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO)

for line in open("/Users/marcosantangelo/callisto-wt/.env.local"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ[k] = v

from agp.provenance import ProvenanceLedger
from tools.pipeline.retrieval import (IterativeRetriever, RelevanceGate,
                                      translate_question_type)
from tools.sources.registry import get_source_registry

Q = "What has the trend in the US unemployment rate been since 2023?"

reg = get_source_registry()
t, _ = translate_question_type(reg, Q, "")
print("translated question type:", t[:200])

ret = IterativeRetriever(
    registry=reg, ledger=ProvenanceLedger(), transport=None,
    gate=RelevanceGate(min_coverage=0.25), max_rounds=3)

from agp.research_program import (EvidenceRequirement, QuestionKind,
                                  ResearchQuestion, SourceClassRank)
rq = ResearchQuestion(text=Q, kind=QuestionKind.DESCRIPTIVE)
rq.evidence_requirements = EvidenceRequirement(
    min_source_class=SourceClassRank.SECONDARY, min_independent_sources=2)

trace = ret.retrieve(rq, "", min_independent=2)
print("\n=== RETRIEVAL SMOKE ===")
print("queries:", trace.queries)
for r in trace.rounds:
    for s in r["sources"]:
        print(" round", r["round"], s)
print("skipped:", json.dumps(trace.skipped_sources, indent=1)[:800])
print("gain_skipped:", trace.gain_skipped)
print("stop:", trace.stop_reason)
print("admitted:", [(f.source_name, f.url[:80]) for f in trace.admitted])
print("rejected:", [(x.source_name, x.reason[:60]) for x in trace.rejected])
print("independent_keys:", sorted(trace.independent_keys))
