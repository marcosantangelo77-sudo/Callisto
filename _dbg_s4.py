
import sys, json, ssl
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()
from agp.provenance import ProvenanceLedger
from agp.research_program import EvidenceRequirement, QuestionKind, ResearchQuestion, SourceClassRank
from tools.pipeline.engine import fixture_transport
from tools.pipeline.retrieval import IterativeRetriever
from tools.pipeline.stasis_stop import StasisStop
from tools.gaps import classify_null_kind
from tools.sources.registry import SourceRegistry, SourceAdapter, SourceSpec

IRRELEVANT = json.dumps({"results": [{"id": "X9", "title": "Mating habits of deep-sea isopods"}]})

reg = SourceRegistry()
def make_adapter(source):
    path = "/fetch_" + source.spec.name
    class _Ad:
        def __getattr__(self, method_name):
            def call(*args, **kwargs):
                term = next((a for a in args if isinstance(a, str)), kwargs.get("query_term", "q"))
                url = source.build_url(path, {"search": term.replace(" ", "+")})
                return source.get_json(url)[0]
            return call
    return _Ad()

spec = SourceSpec(name="alpha", base_url="https://api.openalex.org", description="",
                  answers=("scholarly works on semiconductor supply chain resilience",), tier=1, min_interval_s=0.0)
reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
calls = {"alpha": ("works_search", ("term",), {"limit": 3})}

rq = ResearchQuestion(text="What does research say about semiconductor supply chain resilience?", kind=QuestionKind.DESCRIPTIVE)
rq.evidence_requirements = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY, min_independent_sources=2)

r = IterativeRetriever(registry=reg, ledger=ProvenanceLedger(),
                       transport=fixture_transport({"/fetch_alpha": IRRELEVANT}),
                       max_rounds=3, adaptive_gain=False, generic_calls=calls)
r.stasis_stop = StasisStop()
tr = r.retrieve(rq, rq.text, min_independent=2)
print("STOP:", tr.stop_reason)
print("FIRED_AT:", r.stasis_stop.fired_at)
for rd in tr.rounds:
    print(rd)
print(classify_null_kind(tr))
