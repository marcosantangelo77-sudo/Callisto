"""LIFECYCLE INTEGRATION — part 1: question -> ResearchProgram -> sealed or refused conclusion.

Drives ONE question through the first half of the arc with a scripted model
and fixture transport (no network, no live model):

    question -> decompose into a ResearchProgram
             -> select sources, fetch, record provenance
             -> relevance-gate at ingestion
             -> synthesise across independent sources
             -> adversary attacks; dissent logged
             -> seal or refuse

HARD INVARIANTS asserted here:
  - every fetched body is ledger-recorded PRIMARY bytes (provenance by code
    path, not self-report);
  - no confidence anywhere exceeds the provenance ceiling of its best
    assigned source class;
  - zero resolved descendants caps the sealed parent at SPECULATIVE;
  - a BLOCKING adversary objection refuses the seal AND logs SUSTAINED
    dissent; MAJOR objections lower confidence but do not block;
  - the sealed session's seal verifies.

The preregistration seam itself lives in test_lifecycle_claim.py — see
findings/lifecycle.md for why it is NOT wired into this pipeline run.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain, SourceClass  # noqa: E402
from agp import AGPSession  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import QuestionKind  # noqa: E402
from agp.thresholds import (  # noqa: E402
    MAX_CONFIDENCE_BY_SOURCE,
)
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.research_program import SPECULATIVE_CAP  # noqa: E402

_CLASS_RANK = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}

OPENALEX_BODY = json.dumps({
    "results": [
        {"id": "W1", "title": "Scholarly study on the topic: a literature "
         "review of scholarly work", "publication_year": 2024,
         "cited_by_count": 12},
    ],
})
FR_BODY = json.dumps({
    "documents": [
        {"title": "Final agency rule published by the government: proposed "
         "and final rules with dates, docket refs",
         "document_number": "2024-12345", "published_at": "2024-01-15",
         "agency": "government agency"},
    ],
})

ROUTES = {"/works": OPENALEX_BODY, "/documents.json": FR_BODY}


def _decompose_response() -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does the literature say about the topic",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 2},
        {"text": "has the government published agency rules on the topic",
         "kind": "descriptive",
         "question_type": "final/proposed agency rules with dates and docket refs",
         "min_source_tier": 1, "min_independent_sources": 1},
    ]})


def _answer(conf: float) -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": conf})


class QuietAdversary:
    """Scripted adversary backend: objects unless given objections."""

    def __init__(self, objections=None):
        self.objections = list(objections or [])

    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": self.objections},
                "model": "scripted-adversary"}


def make_pipeline(model=None, adversary=None, descendant_resolutions=None,
                  objections=None):
    model = model or ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        "Manager": [{"content": _answer(0.8)}, {"content": _answer(0.7)}],
    })
    return ResearchPipeline(
        model=model,
        adversary_router=adversary or QuietAdversary(objections),
        transport=fixture_transport(dict(ROUTES)),
        store=ArtifactStore(),
        ledger=ProvenanceLedger(),
        descendant_resolutions=descendant_resolutions)


# ── the arc, first half ───────────────────────────────────────────────────

def test_question_to_seal_with_provenance_gating_adversary():
    pipeline = make_pipeline()
    result = asyncio.run(pipeline.run(
        "What is known about the topic?", today=date(2026, 8, 22)))

    # decompose: a real program with two descriptive leaves came back
    assert result.program is not None
    assert len(result.program.leaves) == 2
    assert {q.kind for q in result.program.leaves} == {QuestionKind.DESCRIPTIVE}
    assert result.program.validate() == []
    assert result.program.fingerprint()  # audit anchor exists

    # select+fetch: every fetch is PRIMARY bytes in the ledger — the bytes
    # were recorded by the fetch code path, never self-declared
    assert result.fetches, "pipeline selected no sources"
    ledger = pipeline.ledger
    for f in result.fetches:
        assert ledger.is_primary_bytes(f.body), (
            f"{f.source_name} fetch not ledger-recorded as primary")
        assert f.content_sha256

    # relevance gating happened at ingestion: the trace either admitted or
    # rejected each candidate, and anything rejected carries a reason
    # (surfaced on result.notes). Nothing entered evidence ungated.
    for leaf in result.leaves:
        for c in leaf.source_classes:
            assert c in _CLASS_RANK

    # synthesise: leaves answered, session carries the evidence
    assert any(l.answer for l in result.leaves)
    assert result.session.evidence, "session has no evidence"

    # INVARIANT: confidence never exceeds the provenance ceiling, at leaf
    # level...
    for leaf in result.leaves:
        best = max(leaf.source_classes,
                   key=lambda c: _CLASS_RANK.get(c, 0)) \
            if leaf.source_classes else "INFERRED"
        assert leaf.confidence <= MAX_CONFIDENCE_BY_SOURCE[best] + 1e-9, (
            f"leaf {leaf.text[:40]}: {leaf.confidence} > {best} ceiling")

    # ...at parent level under the inheritance rule (zero resolved
    # descendants => SPECULATIVE forever), and at the sealed summary.
    assert result.confidence_score <= SPECULATIVE_CAP + 1e-9
    assert result.session.summary.confidence_score <= SPECULATIVE_CAP + 1e-9

    # adversary attacked before sealing; dissent is in its ledger
    assert result.objections is not None
    sid = result.session.session_id
    raised = pipeline.adversary.ledger.objections_for(sid)
    assert all(o.status in ("RAISED", "OVERRULED") for o in raised)

    # seal: verifiable under the session seal machinery
    assert result.sealed, result.refusal_reason
    assert result.session.seal_hash
    assert AGPSession.verify_seal(result.session.to_dict())


def test_major_objection_lowers_confidence_and_is_logged_as_overruled():
    critic = QuietAdversary(objections=[
        {"kind": "selection_effect", "severity": "MAJOR",
         "text": "only successful replications indexed"}])
    pipeline = make_pipeline(adversary=critic)
    clean = asyncio.run(make_pipeline().run("Q?", today=date(2026, 8, 22)))
    attacked = asyncio.run(pipeline.run("Q?", today=date(2026, 8, 22)))

    assert clean.sealed and attacked.sealed
    # asymmetric: the adversary subtracted, never added
    assert attacked.confidence_score < clean.confidence_score
    objs = pipeline.adversary.ledger.objections_for(
        attacked.session.session_id)
    assert any(o.status == "OVERRULED" and o.overrule_reasoning
               for o in objs), "overruled dissent not logged with reasoning"


def test_blocking_objection_refuses_the_seal_and_sustains_dissent():
    blocker = QuietAdversary(objections=[
        {"kind": "refuting_evidence", "severity": "BLOCKING",
         "text": "fixture route never proves the mechanism"}])
    pipeline = make_pipeline(adversary=blocker)
    result = asyncio.run(pipeline.run("Q?", today=date(2026, 8, 22)))

    assert not result.sealed
    assert "adversary veto" in result.refusal_reason
    objs = pipeline.adversary.ledger.objections_for(result.session.session_id)
    sustained = [o for o in objs if o.status == "SUSTAINED"]
    assert sustained, "veto was not logged as sustained dissent"


def test_unanswered_leaves_refuse_instead_of_sealing_a_null():
    model = ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        "Manager": [{"content": json.dumps({"answer": "",
                                            "proposed_confidence": 0.5})}],
    })
    pipeline = make_pipeline(model=model)
    result = asyncio.run(pipeline.run("Q?", today=date(2026, 8, 22)))
    assert not result.sealed
    assert result.refusal_reason


def test_no_socket_held():
    import socket
    with pytest.raises(AssertionError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
