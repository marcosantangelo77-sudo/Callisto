from tests.test_build_w1_retrieval import (  # noqa: F401
    _registry, _retriever, _q, _openalex_body, IRRELEVANT_BODY,
    _ALPHA_ANSWERS, _BETA_ANSWERS)
from tools.pipeline.engine import fixture_transport


def test_debug_urls():
    reg = _registry(("alpha", _ALPHA_ANSWERS, "https://api.openalex.org"),
                    ("beta", ["agency rules about supply chains"],
                     "https://c.org"))
    seen = []

    def transport(url, headers):
        seen.append(url)
        return 200, IRRELEVANT_BODY

    from agp.provenance import ProvenanceLedger
    from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate
    ret = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(), transport=transport,
        gate=RelevanceGate(min_coverage=0.25), max_rounds=1,
        generic_calls={"alpha": ("works_search", ("term",), {"limit": 3}),
                       "beta": ("works_search", ("term",), {"limit": 3})})
    trace = ret.retrieve(_q(), "", min_independent=1)
    print("URLS:", seen)
    assert True
