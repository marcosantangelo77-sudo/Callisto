from tests.helpers.no_socket import NoSocket
NoSocket().install()
import json
from tools.sources.registry import SourceRegistry, SourceAdapter
from tools.sources.base import SourceSpec, RestSource
from agp.provenance import ProvenanceLedger
from tools.pipeline.retrieval import IterativeRetriever
from tools.pipeline.engine import fixture_transport
from agp.research_program import ResearchQuestion, QuestionKind, EvidenceRequirement, SourceClassRank


def test_debug():
    reg = SourceRegistry()

    def make_adapter(source):
        class Ad:
            def works_search(self, query, limit=10):
                data, rec = source.get_json(
                    self.source_url(query))
                return data
            source_url = staticmethod(lambda q: (
                "https://x.example/works?search=" + q.replace(" ", "+")))
        return Ad()
    for name, answers, url in [
        ("alpha", ("semiconductor supply chain resilience scholarly works",), "https://a.example"),
        ("beta", ("news events about semiconductor supply chains",), "https://b.example"),
    ]:
        spec = SourceSpec(name=name, base_url=url, description="",
                          answers=tuple(answers), cannot_answer=("x",),
                          tier=1, min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    text = "What does research say about semiconductor supply chain resilience?"
    body = json.dumps({"results": [{"id": "W1", "title": "Semiconductor supply chain resilience review"}]})
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY, min_independent_sources=2)
    ret = IterativeRetriever(registry=reg, ledger=ProvenanceLedger(),
                             transport=fixture_transport({"/works": body}),
                             max_rounds=3,
                             generic_calls={"alpha": ("works_search", ("term",), {"limit": 3}),
                                            "beta": ("works_search", ("term",), {"limit": 3})})
    trace = ret.retrieve(rq, "", min_independent=2)
    print("ROUNDS:", json.dumps(trace.rounds)[:800])
    print("STOP:", trace.stop_reason, "KEYS:", trace.independent_keys,
          "ADMITTED:", len(trace.admitted))
    assert True
