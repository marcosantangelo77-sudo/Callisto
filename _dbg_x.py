
import sys, json, ssl, asyncio, tempfile, os, dataclasses
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()
from agp.provenance import ProvenanceLedger
from tools.pipeline.engine import ResearchPipeline, fixture_transport
from tools.pipeline.model import ScriptedModel

GOOD = json.dumps({"results": [{"id": "W1", "title": "Semiconductor supply chain resilience review"}]})
JUNK = json.dumps({"results": [{"id": "X9", "title": "Mating habits of deep-sea isopods"}]})

DECOMPOSE = json.dumps({"sub_questions": [
    {"text": "what does scholarly research say about semiconductor supply chain resilience",
     "kind": "descriptive", "question_type": "scholarly literature about semiconductor supply chains",
     "min_source_tier": 2, "min_independent_sources": 2}]})

class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}

def build_reg():
    from tools.sources.registry import SourceRegistry, SourceAdapter, SourceSpec
    reg = SourceRegistry()
    specs = {
        "alpha": ("scholarly works on semiconductor supply chain resilience", "https://api.openalex.org"),
        "beta": ("news events about semiconductor supply chains", "https://api.gdeltproject.org"),
    }
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
    for name,(ans,url) in specs.items():
        kw = dict(name=name, base_url=url, description="", answers=(ans,), tier=1, min_interval_s=0.0)
        if name == "beta":
            # beta CANNOT answer the scholarly question — but the registry still selects it
            kw["cannot_answer"] = ("scholarly literature about semiconductor supply chains",)
        reg.register(SourceAdapter(spec=SourceSpec(**kw), make_adapter=make_adapter))
    calls = {n: ("works_search", ("term",), {"limit": 3}) for n in specs}
    return reg, calls

reg, calls = build_reg()
mdl = ScriptedModel({
    "Architect": [{"content": DECOMPOSE}],
    "Manager": [{"content": json.dumps({"answer": "", "proposed_confidence": 0.2})}]})
pipe = ResearchPipeline(model=mdl, adversary_router=_Quiet(),
    transport=fixture_transport({"/fetch_alpha": GOOD}),
    store=None, ledger=ProvenanceLedger(), registry=reg)
from tools.pipeline import retrieval as R
orig_init = R.IterativeRetriever.__init__
def patched(self, *a, **kw):
    kw["generic_calls"] = calls; kw["adaptive_gain"] = True
    orig_init(self, *a, **kw)
R.IterativeRetriever.__init__ = patched
try:
    result = asyncio.run(pipe.run("What does research say about semiconductor supply chains?", today=__import__("datetime").date(2026,8,22)))
finally:
    R.IterativeRetriever.__init__ = orig_init
print("sealed:", result.sealed)
for l in result.leaves:
    print("leaf gap:", repr(l.gap_kind))
    print("expl:", (l.gap_explanation or "")[:400])
