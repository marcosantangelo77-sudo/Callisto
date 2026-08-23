"""RED TEAM — source registry & independence families.

Surface: every copy of "how many independent sources is this?" —
  0. tools/sources/base.py:independence_family      (declared families)
  1. tools/pipeline/retrieval.py:independence_key   (the canonical one)
  2. tools/pipeline/engine.py:_answer_leaf fallback (len({source_name}) + sandbox)
  3. tools/pipeline/synthesis.py EvidenceItem       (fabricated base_url)
  4. tools/why.py:independence_from_fetches         (fetch URL as base_url)

METHOD: cross-module duplication hunt (F) with differential sub-checks (B).
The rule exists in five places; the copies must agree or one of them is a
bug. synthesis.py claims (lines 13-15) that "no second notion of
independence is invented" — that claim is what we attack.

Prior passes: confidence inflation (property sweep + adversarial input),
retrodiction cutoff forgery, memory/wiki laundering, retry-after DoS.
This surface has never been attacked; the family-collapse machinery is the
newest load-bearing code in the repo (wave 4/5) and the morning report's own
headline defect was independence inflation ("nine fetches... independence
stayed at 1").
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp import Domain, AGPSession  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.checkpoint import FileCheckpointer  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, _trace_from_payload  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    IterativeRetriever,
    RetrievalTrace,
    independence_key,
)
from tools.sources.base import INDEPENDENCE_FAMILIES, independence_family  # noqa: E402

TODAY = date(2026, 8, 23)

QUESTION = ("What does recent scholarly research say about semiconductor "
            "supply chain resilience?")


def _openalex_body(n_titles=2):
    return json.dumps({"results": [
        {"id": f"W{i}",
         "title": f"semiconductor supply chain resilience study {i}",
         "publication_year": 2025} for i in range(1, n_titles + 1)]})


def _ss_body():
    return json.dumps({"data": [
        {"title": "Resilience of semiconductor supply chain networks",
         "year": 2025}]})


def _routes():
    return {
        "/works": _openalex_body(),
        "/graph/v1/paper/search": _ss_body(),
    }


def _decompose(min_indep=2) -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2,
         "min_independent_sources": min_indep},
    ]})


def _answer(conf) -> str:
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _pipe(tmp_path, *, conf=0.9, ledger=None, checkpointer=None):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer(conf)}],
    })
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=__import__("tools.pipeline.engine",
                             fromlist=["fixture_transport"]
                             ).fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / "artifacts"), ledger=ledger,
        checkpointer=checkpointer)


# ═══════════════════════════════════════════════════════════════════════
# BREAK 1 — the engine keeps a SECOND, un-collapsed independence counter
#
# engine._answer_leaf:397-401:
#     if trace is not None and trace.independent_keys:
#         n_indep = len(trace.independent_keys)
#     else:
#         n_indep = len({f.source_name for f in fetches}) + (sandbox...)
#
# The retrieval layer collapses openalex+semanticscholar into ONE voice
# (scholarly-aggregator). The fallback counts adapter NAMES — exactly the
# pre-W4 bug the collapse was built to close. It engages whenever keys are
# empty: legacy checkpoints (the prior fix's own test says missing keys
# "degrade safely"), hand-restored traces, any caller passing trace=None.
# ═══════════════════════════════════════════════════════════════════════


def _leaf_fetches(ledger):
    """Two fetches from ONE declared family, recorded the way retrieval
    does (engine.py:444), so provenance assigns SECONDARY."""
    bodies = {"openalex": _openalex_body(), "semanticscholar": _ss_body()}
    out = []
    for name, body in bodies.items():
        url = (f"https://api.openalex.org/works?search=x" if name ==
               "openalex" else
               "https://api.semanticscholar.org/graph/v1/paper/search?query=x")
        ledger.record_tool_result(f"{name}_fetch", body, primary=True,
                                  urls=[url])
        from tools.pipeline.engine import FetchResult, _sha
        out.append(FetchResult(
            source_name=name, url=url, content_sha256=_sha(body),
            body=body, parsed=json.loads(body), question_id="qR"))
    return out


def _run_leaf(tmp_path, *, trace, min_indep=2, proposal=None):
    ledger = ProvenanceLedger()
    fetches = _leaf_fetches(ledger)
    model = ScriptedModel({"Manager": [{"content": proposal or _answer(0.9)}]})
    pipe = ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=lambda u, h: (200, "{}"),
        store=ArtifactStore(root=tmp_path / "artifacts"), ledger=ledger)
    q = ResearchQuestion(
        text="scholarly research on semiconductor supply chains",
        kind=QuestionKind.DESCRIPTIVE,
        evidence_requirements=EvidenceRequirement(
            min_source_class=SourceClassRank.INFERRED,
            min_independent_sources=min_indep))
    session = AGPSession("redteam")
    session.domain = Domain.GENERAL
    return asyncio.run(
        pipe._answer_leaf(q, fetches, session, trace=trace))


class TestEngineFallbackCounter:
    def test_live_trace_collapses_the_family_to_one_voice(self):
        """PIN (passes): with restored keys, openalex+semanticscholar are
        ONE independent voice and min_independent_sources=2 stays unmet ->
        capped at SPECULATIVE 0.54."""
        trace = RetrievalTrace(question_id="qR",
                               independent_keys={"scholarly-aggregator"})
        out = _run_leaf(__import__("pathlib").Path("/tmp") / "rt-live-a",
                        trace=trace)
        assert out.confidence <= 0.54

    def test_empty_keys_silently_switch_to_uncollapsed_name_counting(self):
        """BREAK: same evidence, same requirement — but keys empty (what
        _trace_from_payload yields for ANY legacy/corrupted payload). The
        fallback counts two adapter NAMES, the family collapse evaporates,
        and the leaf scores the uncapped 0.75. Nothing flags the switch."""
        trace = RetrievalTrace(question_id="qR", independent_keys=set())
        out = _run_leaf(__import__("pathlib").Path("/tmp") / "rt-live-b",
                        trace=trace)
        assert out.confidence <= 0.54, (
            "empty independent_keys downgraded the gate to NAME counting: "
            f"{out.confidence} with reasons {out.requirement_reasons}")

    def test_resumed_leaf_must_not_beat_live_leaf(self):
        """Differential pin: live vs degraded-keys resume, identical
        evidence. Resumed must never exceed live."""
        tmp = __import__("pathlib").Path("/tmp")
        live = _run_leaf(tmp / "rt-diff-a", trace=RetrievalTrace(
            question_id="qR", independent_keys={"scholarly-aggregator"}))
        resumed = _run_leaf(tmp / "rt-diff-b", trace=RetrievalTrace(
            question_id="qR", independent_keys=set()))
        assert resumed.confidence <= live.confidence + 1e-9, (
            f"resume={resumed.confidence} beat live={live.confidence} "
            "through the engine's un-collapsed fallback counter")

    def test_sandbox_counts_as_an_independent_source(self):
        """BREAK: in the fallback, a successful SANDBOX run adds +1 to the
        independent-source count. A computation derived FROM the fetched
        evidence is maximally dependent on it; it cannot corroborate it.
        One fetch + arithmetic satisfies min_independent_sources=2."""
        proposal = json.dumps({
            "answer": "computed", "proposed_confidence": 0.9,
            "compute": {"code": "print(2 + 2)"}})
        trace = RetrievalTrace(question_id="qR", independent_keys=set())
        out = _run_leaf(__import__("pathlib").Path("/tmp") / "rt-sb",
                        trace=trace, min_indep=2, proposal=proposal)
        assert out.sandbox_status == "ok"
        assert out.confidence <= 0.54, (
            f"sandbox counted as an independent source: {out.confidence}")


# ═══════════════════════════════════════════════════════════════════════
# BREAK 2 — end-to-end: a legacy checkpoint store flips the gate, and the
# run SEALS at the inflated score.
# ═══════════════════════════════════════════════════════════════════════


class TestLegacyCheckpointEndToEnd:
    def test_resume_from_pre_w5_store_beats_live_and_seals(self, tmp_path):
        # live run, no checkpointing
        led_a = ProvenanceLedger()
        ra = asyncio.run(_pipe(tmp_path / "a", ledger=led_a).run(
            QUESTION, today=TODAY))
        live_leaf = ra.leaves[0].confidence

        # warm run WITH checkpointing
        cp = FileCheckpointer(root=tmp_path / "ckpt")
        warm_led = ProvenanceLedger()
        asyncio.run(_pipe(tmp_path / "b", ledger=warm_led,
                          checkpointer=cp).run(QUESTION, today=TODAY))

        # rewind the store to pre-W5 shape: fetch_leaf payloads without
        # independent_keys (plain JSON, no integrity seal on payloads),
        # answer_leaf stages absent (crash before they were written).
        run_dir = next((tmp_path / "ckpt").glob("*"))
        for p in run_dir.glob("fetch_leaf.*.json"):
            d = json.loads(p.read_text())
            d["payload"].pop("independent_keys", None)
            p.write_text(json.dumps(d))
        for p in run_dir.glob("answer_leaf.*.json"):
            p.unlink()

        # resume: decompose+fetch resumed, answer re-executed
        resumed_led = ProvenanceLedger()
        rb = asyncio.run(_pipe(tmp_path / "c", ledger=resumed_led,
                               checkpointer=cp).run(QUESTION, today=TODAY))
        resumed_leaf = rb.leaves[0].confidence

        assert resumed_leaf > live_leaf, (
            "legacy resume did not diverge (fix landed?) "
            f"live={live_leaf} resumed={resumed_leaf}")
        assert rb.sealed, rb.refusal_reason
        assert rb.confidence_score > ra.confidence_score, (
            f"sealed resumed run {rb.confidence_score} did not beat sealed "
            f"live run {ra.confidence_score}")
