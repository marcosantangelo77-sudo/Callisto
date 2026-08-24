
import sys, json, ssl, asyncio, tempfile, os, dataclasses
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()

# F6 END-TO-END: leaf errors on beta (only alpha admitted) -> live gap honest_null;
# resume from checkpoint -> restored trace loses the error round -> gap flips to
# retrieval_failure. Use the engine with a checkpointer, two leaves.
from agp.provenance import ProvenanceLedger
from tools.pipeline.engine import ResearchPipeline, fixture_transport
from tools.pipeline.model import ScriptedModel
from tools.pipeline import checkpoint as ckpt

GOOD = json.dumps({"results": [{"id": "W1", "title": "Semiconductor supply chain resilience review"}]})
JUNK = json.dumps({"results": [{"id": "X9", "title": "Mating habits of deep-sea isopods"}]})

DECOMPOSE = json.dumps({"sub_questions": [
    {"text": "what does scholarly research say about semiconductor supply chain resilience",
     "kind": "descriptive", "question_type": "scholarly literature about semiconductor supply chains",
     "min_source_tier": 2, "min_independent_sources": 1},
    {"text": "what do news events say about semiconductor supply chains",
     "kind": "descriptive", "question_type": "news events about semiconductor supply chains",
     "min_source_tier": 2, "min_independent_sources": 1}]})

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
        reg.register(SourceAdapter(spec=SourceSpec(name=name, base_url=url, description="",
            answers=(ans,), tier=1, min_interval_s=0.0), make_adapter=make_adapter))
    calls = {n: ("works_search", ("term",), {"limit": 3}) for n in specs}
    return reg, calls

tmp = tempfile.mkdtemp()
cp = ckpt.FileCheckpointer(os.path.join(tmp, "cp"))

def run_with_cp(routes, label):
    reg, calls = build_reg()
    mdl = ScriptedModel({
        "Architect": [{"content": DECOMPOSE}],
        "Manager": [{"content": json.dumps({"answer": "", "proposed_confidence": 0.2})}]})
    pipe = ResearchPipeline(model=mdl, adversary_router=_Quiet(),
        transport=fixture_transport(routes),
        store=None, ledger=ProvenanceLedger(), registry=reg, checkpointer=cp)
    from tools.pipeline import retrieval as R
    orig_init = R.IterativeRetriever.__init__
    def patched(self, *a, **kw):
        kw["generic_calls"] = calls; kw["adaptive_gain"] = False
        orig_init(self, *a, **kw)
    R.IterativeRetriever.__init__ = patched
    try:
        result = asyncio.run(pipe.run("What does research say about semiconductor supply chains?", today=__import__("datetime").date(2026,8,22)))
    finally:
        R.IterativeRetriever.__init__ = orig_init
    print(label, "| refusal:", result.refusal_reason)
    for l in sorted(result.leaves, key=lambda x: x.text):
        print("   ", l.text[:40], "| gap:", repr(l.gap_kind))
    return result

run_with_cp({"/fetch_alpha": GOOD}, "LIVE (beta route missing -> skip not error?)")
run_with_cp({"/fetch_alpha": GOOD}, "RESUMED")
