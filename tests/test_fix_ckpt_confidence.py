"""FIX — checkpoint resume must never inflate confidence.

The defect (fixed here): the engine's resume branch replayed stored fetches
with `rejected = []` and `trace_q = None`, so a checkpointed run carried
evidence the relevance gate would have rejected AND reported zero rejections
AND lost the independence-family collapse — scoring 0.80 where the identical
plain run scored 0.54. Confidence rose from a mechanism unrelated to evidence
quality.

The fix stores the gate's full verdict set alongside each fetch and restores
the complete RetrievalTrace on resume.

  1. byte-identical confidence: a fully-resumed run scores exactly what the
     equivalent live run scored — including the rejection notes.
  2. PROPERTY: for random evidence sets, resumed confidence NEVER EXCEEDS
     live confidence. Lower is acceptable; higher is the bug.
  3. rejections survive the boundary: a resumed run reports the same
     gate rejections as the live run, so it cannot look cleaner than it was.
  4. legacy checkpoints (payload without verdict fields) degrade safely:
     no trace is fabricated as 'everything was admitted'.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.checkpoint import FileCheckpointer, RunTrace  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    _trace_from_payload,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.sources.query_builder import build_plan  # noqa: E402

TODAY = date(2026, 8, 22)

QUESTION = ("What does recent scholarly research say about semiconductor "
            "supply chain resilience?")


def _openalex_body(n_titles=2, topic="semiconductor supply chain"):
    return json.dumps({"results": [
        {"id": f"W{i}", "title": f"{topic} study {i}",
         "publication_year": 2025, "cited_by_count": 12}
        for i in range(1, n_titles + 1)
    ]})


def _ss_body(topic="semiconductor supply chain"):
    return json.dumps({"data": [
        {"title": f"Resilience of {topic} networks", "year": 2025}]})


def _routes(openalex=None, ss=None):
    return {
        "/works": openalex if openalex is not None else _openalex_body(),
        "/graph/v1/paper/search": ss if ss is not None else _ss_body(),
    }


def _decompose(min_indep=1) -> str:
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


def _make(tmp_path, *, conf=0.8, ledger=None, checkpointer=None,
          routes=None):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer(conf)}],
    })
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes or _routes()),
        store=ArtifactStore(root=tmp_path / "artifacts"), ledger=ledger,
        checkpointer=checkpointer)


def _run(pipe, question=QUESTION):
    return asyncio.run(pipe.run(question, today=TODAY))


# ── 1+3: a fully-cached run is confidence-identical to the live run ───────

def test_resumed_run_matches_live_confidence_and_rejections(tmp_path):
    led_a = ProvenanceLedger()
    plain = _make(tmp_path / "a", ledger=led_a)
    ra = _run(plain)

    cp = FileCheckpointer(root=tmp_path / "b" / "ckpt")
    first = _make(tmp_path / "b", ledger=ProvenanceLedger(), checkpointer=cp)
    _run(first)  # populate every stage checkpoint

    # Fully cached resume: any complete() call would return "{}".
    led_c = ProvenanceLedger()
    resumed = _make(tmp_path / "c", ledger=led_c, checkpointer=cp,
                    routes=_routes())  # fresh transport, must not be hit
    rb = _run(resumed)

    assert rb.trace is not None and rb.trace.is_resume
    assert [l.confidence for l in ra.leaves] == \
        [l.confidence for l in rb.leaves]
    assert ra.confidence_score == rb.confidence_score, (
        f"live={ra.confidence_score} resumed={rb.confidence_score}")
    assert ra.confidence_tier == rb.confidence_tier

    # Rejection notes survive: a resumed run may not report zero rejections
    # where the live run reported some.
    rej_a = [n for n in ra.notes if "rejected at ingestion" in n]
    rej_b = [n for n in rb.notes if "rejected at ingestion" in n]
    assert rej_a == rej_b


# ── 2: property — resume never exceeds live ────────────────────────────────

@st.composite
def _evidence_set(draw):
    """Random evidence sets: per-source title topics drawn from a mix of
    on-topic and off-topic vocabularies, so the gate admits/rejects
    differently across draws."""
    n_openalex = draw(st.integers(min_value=0, max_value=4))
    n_ss = draw(st.integers(min_value=0, max_value=3))
    topics = st.sampled_from([
        "semiconductor supply chain resilience",
        "quantum error correction thresholds",
        "deep sea sediment cores",
        "medieval wool trade routes",
        "protein folding kinetics",
    ])
    t_oa = draw(topics)
    t_ss = draw(topics)
    oa = json.dumps({"results": [
        {"id": f"W{i}", "title": f"{t_oa} review {i}",
         "publication_year": 2025} for i in range(1, n_openalex + 1)]})
    ss = json.dumps({"data": [
        {"title": f"{t_ss} analysis", "year": 2024} for _ in range(max(1, n_ss))]})
    conf = draw(st.floats(min_value=0.1, max_value=0.95))
    return oa, ss, conf


@given(data=_evidence_set())
@settings(max_examples=25, deadline=None, database=None)
def test_property_resume_never_exceeds_live(data, tmp_path_factory):
    oa, ss, conf = data
    routes = _routes(oa, ss)

    led_a = ProvenanceLedger()
    plain = _make(tmp_path_factory.mktemp("prop_a"), conf=conf,
                  ledger=led_a, routes=routes)
    ra = asyncio.run(plain.run(QUESTION, today=TODAY))

    cp = FileCheckpointer(root=tmp_path_factory.mktemp("prop_ckpt") / "ckpt")
    warm = _make(tmp_path_factory.mktemp("prop_warm"), conf=conf,
                 ledger=ProvenanceLedger(), checkpointer=cp, routes=routes)
    asyncio.run(warm.run(QUESTION, today=TODAY))

    led_c = ProvenanceLedger()
    resumed = _make(tmp_path_factory.mktemp("prop_c"), conf=conf,
                    ledger=led_c, checkpointer=cp, routes=routes)
    rb = asyncio.run(resumed.run(QUESTION, today=TODAY))

    assert rb.confidence_score <= ra.confidence_score + 1e-9, (
        "resumed run scored HIGHER than the identical live run: "
        f"resume={rb.confidence_score} live={ra.confidence_score}")


# ── 4: legacy payloads degrade safely ──────────────────────────────────────

def test_legacy_payload_without_verdicts_degrades_to_empty_not_admit_all():
    """An OLD checkpoint (pre-fix payload: fetches only) must not be read as
    'the gate admitted everything'. No trace is fabricated; nothing claims
    zero rejections or full independence."""
    tr = _trace_from_payload("q1", {"fetches": [
        {"source_name": "openalex", "url": "https://u/1"}]})
    assert tr.rejected == []            # unknown, not asserted-admitted...
    assert tr.independent_keys == set()   # ...and independence NOT granted


def test_trace_roundtrip_preserves_rejections():
    payload = {
        "rejections": [
            {"source_name": "gdelt", "url": "https://g/1",
             "reason": "content covers 8% of the question's topical words "
                       "(need 25%); missing: semiconductor, resilience",
             "relevance_score": 0.08,
             "content_sha256": "cd" * 32},
        ],
        "independent_keys": ["api.openalex.org"],
        "queries": ["semiconductor supply chain resilience"],
        "stop_reason": "round budget exhausted",
    }
    tr = _trace_from_payload("qX", payload)
    assert len(tr.rejected) == 1
    r = tr.rejected[0]
    assert r.source_name == "gdelt"
    assert r.content_sha256 == "cd" * 32
    assert abs(r.relevance_score - 0.08) < 1e-9
    assert tr.independent_keys == {"api.openalex.org"}
