
import sys, json, ssl
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()
from agp.provenance import ProvenanceLedger
from agp.research_program import EvidenceRequirement, QuestionKind, ResearchQuestion, SourceClassRank
from tools.pipeline.engine import fixture_transport
from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate
from tools.sources.registry import SourceRegistry, SourceAdapter, SourceSpec

GOOD = json.dumps({"results": [{"id": "W1", "title": "Semiconductor supply chain resilience review"}]})
JUNK = json.dumps({"results": [{"id": "X9", "title": "Mating habits of deep-sea isopods"}]})

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
rq.evidence_requirements = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY, min_independent_sources=3)

def run(entries, routes, order=None, label="", maxr=2):
    reg = build(entries)
    r = IterativeRetriever(registry=reg, ledger=ProvenanceLedger(),
                           transport=fixture_transport(routes),
                           max_rounds=maxr, adaptive_gain=True,
                           source_order=order, gate=RelevanceGate(min_coverage=0.25),
                           max_sources_per_leaf=1,
                           generic_calls={n: ("works_search", ("term",), {"limit": 3}) for n in reg.names()})
    tr = r.retrieve(rq, rq.text, min_independent=3)
    print(label, "| stop:", tr.stop_reason[:55])
    for rd in tr.rounds: print("   round", rd["round"], [(s["name"], list(s)) for s in rd["sources"]])
    print("   keys:", sorted(tr.independent_keys))

E2 = [("alpha","https://api.openalex.org"), ("beta","https://b.example")]
R2 = {"/fetch_alpha": GOOD, "/fetch_beta": JUNK}
run(E2, R2, None, "A REG (alpha first):")
run(E2, R2, lambda s: sorted(s, key=lambda x: x.name!="beta"), "B junk beta FIRST:")
run(E2, R2, lambda s: sorted(s, key=lambda x: x.name=="beta"), "C junk beta LAST:")
