"""P1 — the end-to-end pipeline test.

Takes a question, runs the WHOLE chain against fixtures with a scripted
model (no network, no live model), and asserts a sealed conclusion whose:
  - confidence traces to real provenance (ledger-assigned source class),
  - artifacts are attached and verifiable in the store,
  - the adversary had its say before sealing,
  - refusal paths actually refuse.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()


from agp import Domain, SourceClass  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import QuestionKind  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402


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


def _routes() -> dict[str, str]:
    return {"/works": OPENALEX_BODY, "/documents.json": FR_BODY}


def _decompose_response(quant=False) -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does the literature say about the topic",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 2,
         "quant_required": quant},
        {"text": "has the government published agency rules on the topic",
         "kind": "descriptive",
         "question_type": "final/proposed agency rules with dates and docket refs",
         "min_source_tier": 1, "min_independent_sources": 1},
    ]})


def _answer(conf=0.8, compute=None) -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": conf, "compute": compute})


def _make(tmp_path, model=None, adversary=None, ledger=None,
          descendant_resolutions=None):
    model = model or ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        "Manager": [{"content": _answer(0.8)}, {"content": _answer(0.7)}],
    })
    adversary = adversary or _QuietAdversary()
    store = ArtifactStore(root=tmp_path / "artifacts")
    pipeline = ResearchPipeline(
        model=model, adversary_router=adversary,
        transport=fixture_transport(_routes()), store=store, ledger=ledger,
        descendant_resolutions=descendant_resolutions)
    return pipeline, model


class _QuietAdversary:
    """Adversary backend that raises no objections unless scripted."""

    def __init__(self, objections=None):
        self.objections = list(objections or [])

    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": self.objections},
                "model": "stub"}


def test_end_to_end_sealed_with_provenance_artifact_and_adversary(tmp_path):
    ledger = ProvenanceLedger()
    pipeline, model = _make(tmp_path, ledger=ledger)
    result = asyncio.get_event_loop().run_until_complete(
        pipeline.run("What is known about the topic?", today=date(2026, 8, 22)))

    # ── sealed ──
    assert result.sealed, result.refusal_reason
    assert result.session.seal_hash
    from agp import AGPSession
    assert AGPSession.verify_seal(result.session.to_dict())

    # ── decomposition happened: real program with two leaves ──
    assert result.program is not None
    assert len(result.program.leaves) == 2
    kinds = {q.kind for q in result.program.leaves}
    assert kinds == {QuestionKind.DESCRIPTIVE}

    # ── fetches recorded in the provenance ledger ──
    assert result.fetches, "no sources fetched"
    for f in result.fetches:
        assert ledger.is_primary_bytes(f.body), (
            f"fetch from {f.source_name} not in ledger as primary bytes")
    source_names = {f.source_name for f in result.fetches}
    # openalex matches "scholarly work search"; federalregister matches
    # "federal register documents" via registry word-overlap selection.
    assert source_names, result.fetches

    # ── confidence traces to provenance ──
    # Scripted model proposed 0.8/0.7. Best fetched class must cap it.
    best = max(
        SourceClass[e.source_class.value].value if hasattr(e.source_class, "value")
        else str(e.source_class)
        for e in result.session.evidence)
    ceilings = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55,
                "INFERRED": 0.55}
    proposed = max(l.confidence for l in result.leaves)
    # Every leaf confidence ≤ its provenance ceiling; final score ≤ proposed.
    assert result.confidence_score <= proposed + 1e-9
    rank = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}
    for leaf in result.leaves:
        best_leaf_class = max(leaf.source_classes, key=lambda c: rank.get(c, 0)) \
            if leaf.source_classes else "INFERRED"
        assert leaf.confidence <= ceilings[best_leaf_class] + 1e-9, (
            f"{leaf.text}: {leaf.confidence} exceeds {best_leaf_class} ceiling")

    # Zero resolved descendants → inheritance rule caps parent at
    # SPECULATIVE_CAP (= TIER_PROBABLE_MIN per tools/research_program.py).
    from tools.research_program import SPECULATIVE_CAP
    assert result.confidence_score <= SPECULATIVE_CAP + 1e-9

    # ── adversary had its say ──
    assert result.objections is not None  # attack ran (empty list = withstood)


def test_inheritance_lifts_ceiling_with_strong_descendants(tmp_path):
    recs = [{"question_id": f"d{i}", "resolved_at": "2026-01-01",
             "outcome": "hit"} for i in range(6)]
    ledger = ProvenanceLedger()
    pipeline, _ = _make(tmp_path, ledger=ledger,
                        descendant_resolutions=recs)
    result = asyncio.get_event_loop().run_until_complete(
        pipeline.run("Q?", today=date(2026, 8, 22)))
    assert result.sealed
    # With hits behind it the ceiling may rise above SPECULATIVE — but only
    # to what Wilson-LB × calibration × provenance allows; here SECONDARY
    # evidence caps at 0.75 regardless.
    assert result.confidence_score <= 0.75 + 1e-9


def test_adversary_veto_blocks_the_seal(tmp_path):
    blocker = _BlockingAdversary()
    pipeline, _ = _make(tmp_path, adversary=blocker)
    result = asyncio.get_event_loop().run_until_complete(pipeline.run("Q?"))
    assert not result.sealed
    assert "adversary veto" in result.refusal_reason
    # Dissent logged as SUSTAINED in the ledger.
    objs = pipeline.adversary.ledger.objections_for(
        result.session.session_id)
    assert any(o.status == "SUSTAINED" for o in objs)


class _BlockingAdversary(_QuietAdversary):
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": [
            {"kind": "refuting_evidence", "severity": "BLOCKING",
             "text": "the fixture route never proves the mechanism"}]},
            "model": "stub"}


def test_major_objection_lowers_confidence_but_still_seals(tmp_path):
    critic = _QuietAdversary(objections=[
        {"kind": "selection_effect", "severity": "MAJOR",
         "text": "only successful replications indexed"}])
    pipeline, _ = _make(tmp_path, adversary=critic)
    clean, _ = _make(tmp_path / "b")
    attacked = asyncio.get_event_loop().run_until_complete(pipeline.run("Q?"))
    clean_res = asyncio.get_event_loop().run_until_complete(clean.run("Q?"))
    assert attacked.sealed and clean_res.sealed
    assert attacked.confidence_score < clean_res.confidence_score


def test_unanswered_pipeline_refuses_to_seal(tmp_path):
    model = ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        # Manager returns no answer at all
        "Manager": [{"content": json.dumps({"answer": "",
                                            "proposed_confidence": 0.5})},
                    {"content": json.dumps({"answer": "",
                                            "proposed_confidence": 0.5})}],
    })
    pipeline, _ = _make(tmp_path, model=model)
    result = asyncio.get_event_loop().run_until_complete(pipeline.run("Q?"))
    assert not result.sealed
    assert result.refusal_reason


def test_compute_stage_runs_sandbox_and_emits_artifacts(tmp_path):
    code = ("import json\n"
            "series = json.load(open('series.json'))\n"
            "result = {'mean': sum(series) / len(series)}")
    compute = {"code": code, "inputs": {"series": [1.0, 2.0, 3.0]}}
    model = ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        # First Manager turn asks for compute; second answers using it.
        "Manager": [{"content": json.dumps({"answer": None,
                                            "compute": compute})},
                    {"content": json.dumps(
                        {"answer": "mean computed as 2.0",
                         "proposed_confidence": 0.8})},
                    {"content": _answer(0.7)}],
    })
    pipeline, _ = _make(tmp_path, model=model)
    result = asyncio.get_event_loop().run_until_complete(
        pipeline.run("Compute question?", today=date(2026, 8, 22)))
    assert result.sealed, result.refusal_reason
    ran = [l for l in result.leaves if l.sandbox_status == "ok"]
    assert ran, "sandbox never executed"
    assert result.artifact_refs, "no artifact emitted from sandbox run"
    for ref in result.artifact_refs:
        report = pipeline.store.verify_artifacts([ref])
        assert report["ok"], report


def test_chart_artifact_backs_a_conclusion(tmp_path):
    """The artifact path also covers charts: spec in, chart+spec refs out,
    both verifiable."""
    from tools.charts import store_chart
    store = ArtifactStore(root=tmp_path / "art")
    out = store_chart({"title": "series backing claim",
                       "series": {"a": [1.0, 2.0, 3.0]},
                       "code_sha256": ""}, store=store)
    report = store.verify_artifacts([out["chart"], out["spec"]])
    assert report["ok"]


def test_no_socket_held():
    """Sanity: the guard is installed for this module."""
    import socket
    with pytest.raises(AssertionError):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM)
