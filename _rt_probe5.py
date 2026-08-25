import sys; sys.path.insert(0, '.')
from tools.sources.registry import get_source_registry
from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate
from agp.provenance import ProvenanceLedger


class Q:
    def __init__(self, text):
        self.text = text
        self.question_id = "q1"
        self.evidence_requirements = None


reg = get_source_registry()
led = ProvenanceLedger()
ret = IterativeRetriever(registry=reg, ledger=led, transport=None)
q = Q("Which company has the most patents in battery technology?")
try:
    trace = ret.retrieve(q, "entity lookup", min_independent=2)
    print("survived; stop_reason:", trace.stop_reason)
    print("skipped:", trace.skipped_sources[:5])
except Exception as e:
    print("RETRIEVE CRASHED:", type(e).__name__, e)
