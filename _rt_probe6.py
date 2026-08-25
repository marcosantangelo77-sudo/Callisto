import sys, json
sys.path.insert(0, '.')
from tools.sources.registry import SourceRegistry, SourceAdapter
from tools.pipeline.retrieval import IterativeRetriever
from agp.provenance import ProvenanceLedger
from tools.sources.base import SourceSpec

BODY = {"error": "your query 'unemployment rate among teenagers in Spain' "
                 "was malformed; nothing was searched"}


class Q:
    text = "unemployment rate among teenagers in Spain"
    question_id = "q1"
    evidence_requirements = None


class FakeRest:
    """Mimics the RestSource surface _fetch_one relies on."""
    def __init__(self):
        self.last_record = type("R", (), {
            "status": 200, "url": "https://example.com/works?x=1",
            "content_sha256": "0" * 64})()

    def build_url(self, path="", params=None):
        return "https://example.com/works"


def make_adapter(rest_source):
    class A:
        def works_search(self, query, limit=10):
            return BODY
    return A()


reg = SourceRegistry()
spec = SourceSpec(name="openalex", base_url="https://example.com",
                  description="d",
                  answers=("macroeconomic unemployment series",))
reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))

led = ProvenanceLedger()
ret = IterativeRetriever(registry=reg, ledger=led,
                         transport=lambda url, h: (200, json.dumps(BODY)))
trace = ret.retrieve(Q(), "unemployment rate", min_independent=2)
print("admitted:", len(trace.admitted), "rejected:", len(trace.rejected),
      "stop:", trace.stop_reason)
print("indep keys:", trace.independent_keys)
for f in trace.admitted:
    print("ADMITTED body:", f.body[:80])
    print("ledger minted PRIMARY?", led.is_primary_bytes(f.body))
