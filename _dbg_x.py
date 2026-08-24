
import sys, json, ssl, asyncio, tempfile, os, dataclasses
sys.path.insert(0, "."); sys.path.insert(0, "tests")
from tests.helpers.no_socket import NoSocket; NoSocket().install()
from tools.pipeline.engine import _trace_from_payload

# F6 FINAL-B: mixed trace (beta error + alpha REJECTED) — live honest_null with
# coverage note; restored: does the ERROR detail vanish from the explanation?
# (Kind stays honest_null because rejected is restored, but the "some sources
# also errored" disclosure is lost -> partial coverage laundered into silence.)
from tools.pipeline.retrieval import RetrievalTrace
from types import SimpleNamespace

tr = RetrievalTrace(question_id="q1")
tr.rejected.append(SimpleNamespace(source_name="alpha", url="u", reason="irrelevant",
                                   relevance_score=0.1, content_sha256="x"))
tr.rounds.append({"round": 1, "query": "q", "sources": [
    {"name": "beta", "error": "connection refused"},
    {"name": "alpha", "rejected": "irrelevant"}], "admitted": 0})
cls = __import__("tools.gaps", fromlist=["classify_null_kind"]).classify_null_kind
k1, e1 = cls(tr)
print("LIVE :", k1)
print("  ", e1)

payload = {"fetches": [], "rejections": [{"source_name": "alpha", "url": "u",
    "reason": "irrelevant", "relevance_score": 0.1, "content_sha256": "x"}],
    "independent_keys": [], "queries": ["q"], "stop_reason": "budget"}
tr2 = _trace_from_payload("q1", payload)
k2, e2 = cls(tr2)
print("RESTORED:", k2)
print("  ", e2)
