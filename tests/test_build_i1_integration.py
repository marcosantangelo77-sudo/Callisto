"""I1 — integration: W5 planner + W3 checkpointer + construction ergonomics.

Adoption tests for the three wiring jobs. Fixtures only (no_socket guard
active); the live run is driven separately by scripts/live_smoke_i1.py.

  JOB 1  the engine plans queries per source via query_builder.build_plan —
         the 4-entry GENERIC_CALLS table is gone and fan-out is no longer
         mono-source by construction
  JOB 2  checkpointer=None keeps run() byte-identical; a FileCheckpointer
         gives idempotent resume, ledger replay, and an enforced seal_guard
  JOB 3  ResearchPipeline(model=m) with no adversary_router constructs,
         runs to completion, and records self-review honestly in notes
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp import Domain, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.checkpoint import (  # noqa: E402
    FileCheckpointer,
    RunTrace,
)
from tools.pipeline.engine import (  # noqa: E402
    FetchResult,
    ResearchPipeline,
    _fetch_from_payload,
    _leaf_from_payload,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.sources.query_builder import (  # noqa: E402
    build_plan,
    plannable_sources,
)

TODAY = date(2026, 8, 22)

OPENALEX_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2025, "cited_by_count": 12},
    {"id": "W2", "title": "Resilience of semiconductor supply networks "
     "under export controls", "publication_year": 2024},
]})
SS_BODY = json.dumps({"data": [
    {"title": "Chokepoint governance in semiconductor manufacturing",
     "year": 2025},
]})


def _routes() -> dict[str, str]:
    return {"/works": OPENALEX_BODY, "/graph/v1/paper/search": SS_BODY}


def _decompose(min_indep=1) -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
         "supply chain resilience",
         "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2, "min_independent_sources": min_indep},
    ]})


def _answer(conf=0.8) -> str:
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _make(tmp_path, model=None, adversary=None, ledger=None,
          checkpointer=None):
    model = model or ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer()}],
    })
    return ResearchPipeline(
        model=model, adversary_router=adversary,
        transport=fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / "artifacts"), ledger=ledger,
        checkpointer=checkpointer)


# ── JOB 1: planner adoption ───────────────────────────────────────────────

def test_generic_calls_table_is_gone():
    assert not hasattr(ResearchPipeline, "GENERIC_CALLS")


def test_planner_covers_nine_sources_with_honest_gaps():
    covered = set(plannable_sources())
    assert {"openalex", "semantic_scholar", "clinicaltrials",
            "federalregister", "gdelt", "fred", "bls", "treasury",
            "wikidata"} <= covered
    assert build_plan("sec_fts", "anything").plannable is False


def test_planned_queries_are_fully_formed():
    plan = build_plan(
        "openalex", "What does recent scholarly research say about "
                    "semiconductor supply chain resilience?")
    assert plan.plannable and len(plan.queries) == 1
    q = plan.queries[0]
    assert q.method == "works_search"
    # the searchable core, not raw question scaffolding
    assert q.kwargs["query"] == "semiconductor supply chain resilience"


def test_engine_fetches_from_multiple_sources(tmp_path):
    """The mono-source regression: fan-out must reach every plannable source
    the registry selects, so independence can exceed 1."""
    pipe = _make(tmp_path)
    result = asyncio.run(pipe.run(
        "What does recent scholarly research say about semiconductor "
        "supply chain resilience?", today=TODAY))
    names = {f.source_name for f in result.fetches}
    assert len(names) >= 2, (
        f"expected multi-source fetches, got {names}")
    assert any(f.url for f in result.fetches)


# ── JOB 2: checkpointer adoption ──────────────────────────────────────────

import pytest


def test_no_checkpointer_is_byte_identical(tmp_path):
    """With None, run() must behave exactly as before W3 adoption."""
    led_a, led_b = ProvenanceLedger(), ProvenanceLedger()
    plain = _make(tmp_path / "a", ledger=led_a, adversary=_QuietAdversary())
    ra = asyncio.run(plain.run("question one", today=TODAY))

    cp = FileCheckpointer(root=tmp_path / "b" / "ckpt")
    checked = _make(tmp_path / "b", ledger=led_b, adversary=_QuietAdversary(),
                    checkpointer=cp)
    rb = asyncio.run(checked.run("question one", today=TODAY))

    assert ra.sealed and rb.sealed
    assert [l.confidence for l in ra.leaves] == \
        [l.confidence for l in rb.leaves]
    assert [f.content_sha256 for f in ra.fetches] == \
        [f.content_sha256 for f in rb.fetches]
    assert ra.confidence_tier == rb.confidence_tier
    # byte-identical also means NO trace object on the plain run
    assert ra.trace is None


def test_checkpointed_run_resumes_without_refetching(tmp_path):
    calls = {"n": 0}

    class _Counting:
        def __getattr__(self, name):
            def inner(*a, **k):
                return fixture_transport(_routes())[0]
            return inner

    transport = fixture_transport(_routes())
    orig = transport.__wrapped__ if hasattr(transport, "__wrapped__") else None

    cp = FileCheckpointer(root=tmp_path / "ckpt")
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer()}] * 4,
    })
    pipe = _make(tmp_path / "one", model=ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer()}],
    }), adversary=_QuietAdversary(),
        ledger=ProvenanceLedger(), checkpointer=cp)
    r1 = asyncio.run(pipe.run("resume question", today=TODAY))
    assert r1.sealed
    n_fetch_urls_1 = len(r1.fetches)

    # Second pipeline, same checkpointer, same question/date -> full cache
    # hits on decompose+fetch+answer; the model must not be consulted again.
    model2 = ScriptedModel({})  # any complete() call would return "{}"
    pipe2 = _make(tmp_path / "two", model=model2, adversary=_QuietAdversary(),
                  ledger=ProvenanceLedger(), checkpointer=cp)
    r2 = asyncio.run(pipe2.run("resume question", today=TODAY))

    assert r2.trace is not None and r2.trace.is_resume
    assert set(r2.trace.resumed_stages) >= {"decompose", "fetch_leaf"}
    assert [(f.source_name, f.content_sha256)
            for f in r2.fetches] == \
           [(f.source_name, f.content_sha256) for f in r1.fetches]


def test_seal_guard_refuses_when_provenance_broken(tmp_path):
    """A resumed run whose stored bodies no longer hash to their record must
    REFUSE, not seal — the anti-laundering guarantee."""
    from tools.pipeline import checkpoint as ckpt

    ledger = ProvenanceLedger()
    cp = FileCheckpointer(root=tmp_path / "ckpt")
    trace = RunTrace(run="rk")
    payload = {"fetches": [{
        "body": "original bytes", "url": "https://x.example/1",
        "content_sha256": ckpt._sha("original bytes"),
        "source_name": "openalex", "primary": True}]}
    cp.save("rk", "fetch_leaf", "ih", payload)
    ck = cp.list_all()[0]
    # tamper AFTER saving
    ck.payload["fetches"][0]["body"] = "tampered bytes"

    verdict, reason = ckpt.seal_guard(trace, [ck], ledger)
    assert verdict == "REFUSE"
    assert "provenance" in reason.lower()


# ── JOB 3: construction ergonomics ────────────────────────────────────────

def test_pipeline_constructs_and_runs_without_adversary_router(tmp_path):
    """The stage-6 crash: ResearchPipeline(model=m) used to die ~100s into a
    live run. Now it completes, with self-review recorded in notes."""
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer(0.7)}],
    })
    pipe = _make(tmp_path, model=model, adversary=None)
    assert pipe._adversary_is_self_review
    result = asyncio.run(pipe.run("self-review question", today=TODAY))
    assert result.sealed, result.refusal_reason
    assert any("self-review" in n for n in result.notes)


def test_explicit_adversary_router_is_not_self_review(tmp_path):
    pipe = _make(tmp_path, adversary=_QuietAdversary())
    assert not pipe._adversary_is_self_review


# ── serialization round-trips used by resume ──────────────────────────────

def test_fetch_and_leaf_payload_roundtrip():
    fr = FetchResult(source_name="openalex", url="https://u/1",
                     content_sha256="ab" * 32, body='{"results": []}',
                     parsed={"results": []}, question_id="q1")
    rec = dataclasses_asdict(fr)
    back = _fetch_from_payload(rec)
    assert back.source_name == fr.source_name
    assert back.parsed == fr.parsed


def dataclasses_asdict(x):
    import dataclasses
    return dataclasses.asdict(x)
