from tests.helpers.no_socket import NoSocket
NoSocket().install()
import json
from tools.sources.registry import SourceRegistry, SourceAdapter
from tools.sources.base import SourceSpec
from tools.pipeline.retrieval import translate_question_type


def test_debug():
    reg = SourceRegistry()
    spec = SourceSpec(name="alpha", base_url="https://a.example",
                      description="", answers=("scholarly work search",),
                      cannot_answer=("x",), tier=1, min_interval_s=0.0)
    reg.register(SourceAdapter(spec=spec,
                               make_adapter=lambda s: type("Ad", (), {})()))
    tr = translate_question_type(
        reg, "What does research say about semiconductor supply chain "
             "resilience?", "")
    print("TRANSLATED:", tr)
    sel = [(d.name, d.included, round(d.score, 2))
           for d in reg.select_explained(tr[0])]
    print("SELECT:", sel)
    assert True
