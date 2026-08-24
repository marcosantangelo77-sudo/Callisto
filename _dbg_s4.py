
import sys, json, ssl
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()
from agp.provenance import ProvenanceLedger
from agp.research_program import EvidenceRequirement, QuestionKind, ResearchQuestion, SourceClassRank
from tools.pipeline.engine import fixture_transport
from tools.pipeline.retrieval import IterativeRetriever, estimate_gain, independence_key
from tools.pipeline.stasis_stop import StasisStop
from tools.sources.registry import SourceRegistry, SourceAdapter, SourceSpec

JUNK = json.dumps({"results": [{"id": "X9", "title": "Mating habits of deep-sea isopods"}]})
GOOD = json.dumps({"results": [{"id": "W1", "title": "Semiconductor supply chain resilience review"}]})

def build(entries):
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
    for name, url0 in entries:
        spec = SourceSpec(name=name, base_url=url0, description="",
                          answers=("scholarly works on semiconductor supply chain resilience",), tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg

rq = ResearchQuestion(text="What does research say about semiconductor supply chain resilience?", kind=QuestionKind.DESCRIPTIVE)
rq.evidence_requirements = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY, min_independent_sources=2)

def run(entries, routes, order=None, label=""):
    reg = build(entries)
    r = IterativeRetriever(registry=reg, ledger=ProvenanceLedger(),
                           transport=fixture_transport(routes),
                           max_rounds=3, adaptive_gain=True,
                           source_order=order,
                           generic_calls={n: ("works_search", ("term",), {"limit": 3}) for n in reg.names()})
    tr = r.retrieve(rq, rq.text, min_independent=3)
    print(label, "STOP:", tr.stop_reason[:70])
    print("  keys:", sorted(tr.independent_keys))
    for rd in tr.rounds: print("   round", rd["round"], [(s["name"], list(s)) for s in rd["sources"]])
    print("  gain_skipped:", tr.gain_skipped)

# alpha & semanticscholar same family (both openalex base? no - family from independence_key)
print("keys:", independence_key("alpha","https://api.openalex.org"), independence_key("scholarly","https://api.openalex.org"))

# ORDER 1: alpha first
run([("alpha","https://api.openalex.org"), ("beta","https://b.example")],
    {"/fetch_alpha": GOOD, "/fetch_beta": GOOD}, None, "PLAIN:")
run([("alpha","https://api.openalex.org"), ("beta","https://b.example")],
    {"/fetch_alpha": GOOD, "/fetch_beta": GOOD},
    lambda specs: sorted(specs, key=lambda s: s.name), "ORDERED(alpha,beta):")
