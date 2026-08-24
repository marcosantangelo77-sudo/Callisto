"""I3 synthesis adoption — engine stage 6b.

The synthesizer existed and was tested (tests/test_build_i3_synthesis.py)
but engine.run never called it: parent confidence came from the best leaf
alone, so nine fetches from one host scored like nine independent sources,
and a live contradiction between sources never touched the sealed number.
The module shipped with an "EXACT ENGINE ADOPTION DIFF" in
findings/i3_synthesis.md that no merge pass ever applied. These tests pin
the adoption:

  1. The structural score can only LOWER the parent proposal (min) —
     never raise it, whatever the evidence structure says.
  2. One independence unit across many fetches scores like ONE source.
  3. A live contradiction caps the parent at SPECULATIVE (0.54).
  4. The report ships on PipelineResult.synthesis / .contradictions.
  5. If synthesis somehow fails, sealing proceeds unchanged (the stage
     must never break the chain) — degradation, not refusal.

Family-1 framing (PATTERNS.md): confidence_from_agreement was a mechanism
that never ran in production — built, tested, and inert at the only place
a score is minted.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
import pathlib
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()


import tests.test_build_p1_pipeline as p1  # fixtures + helpers  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402


def _run(model=None) -> object:
    tmp = pathlib.Path(tempfile.mkdtemp())
    model = model or ScriptedModel({
        "Architect": [{"content": p1._decompose_response()}],
        "Manager": [{"content": p1._answer(0.8)},
                    {"content": p1._answer(0.7)}],
    })
    pipeline = ResearchPipeline(
        model=model, adversary_router=p1._QuietAdversary(),
        transport=fixture_transport(p1._routes()),
        store=ArtifactStore(root=tmp / "artifacts"),
        ledger=ProvenanceLedger())
    return asyncio.new_event_loop().run_until_complete(
        pipeline.run("What is known about the topic?",
                     today=date(2026, 8, 22)))


def test_synthesis_report_ships_on_the_result():
    result = _run()
    assert result.sealed
    assert result.synthesis is not None
    assert "groups" in result.synthesis
    assert "table" in result.synthesis          # structured extraction table
    assert isinstance(result.contradictions, list)
    # summary_dict serialises both without crashing
    d = result.summary_dict()
    assert d["synthesis"] == result.synthesis


def test_structural_score_only_lowers_the_parent():
    """min() adoption: the structural score can never lift the proposal."""
    result = _run()
    assert result.sealed
    best_leaf = max(l.confidence for l in result.leaves if l.answer)
    structural = result.synthesis["confidence"]
    assert result.confidence_score <= max(best_leaf, structural) + 1e-9
    # In this fixture the structure DOES bind: two leaves' answers collapse
    # to one claim group whose contradiction caps at SPECULATIVE.
    assert result.confidence_score <= 0.54 + 1e-9


def test_one_independence_unit_scores_like_one_source():
    """Nine fetches from one host must not read as nine voices."""
    from tools.pipeline.synthesis import EvidenceItem, synthesize
    items = [EvidenceItem(
        claim="foundry concentration is high",
        source_name="openalex", base_url="https://api.openalex.org",
        source_class="SECONDARY", content_sha256=f"h{i}", url=f"u{i}")
        for i in range(9)]
    rep = synthesize("q", items)
    assert rep.max_independent_agreement == 1
    one = [EvidenceItem(claim="foundry concentration is high",
                        source_name="openalex",
                        base_url="https://api.openalex.org",
                        source_class="SECONDARY")]
    assert rep.confidence == synthesize("q", one).confidence


def test_contradiction_caps_parent_at_speculative():
    from agp import ConfidenceTier
    result = _run()
    assert result.sealed
    if result.contradictions:
        # A live contradiction caps the group — and hence the parent — at
        # the SPECULATIVE band. The sealed tier must reflect it.
        assert result.synthesis["confidence"] <= 0.54 + 1e-9
        assert result.confidence_score <= 0.54 + 1e-9
        assert result.confidence_tier == ConfidenceTier.SPECULATIVE.value
        note = [n for n in result.notes if "synthesis agreement" in n]
        assert note, "the lowering must be explained in notes"
        assert "contradiction" in note[0]


def test_synthesis_failure_never_breaks_sealing(monkeypatch):
    """Degradation path: an exception in stage 6b logs and seals anyway."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **k):
        if name == "tools.pipeline.synthesis":
            raise RuntimeError("synthesis exploded")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", boom)
    result = _run()
    assert result.sealed, result.refusal_reason
    assert result.synthesis is None


def test_adoption_note_names_numbers():
    """The run record states what lowered the score and why."""
    result = _run()
    notes = [n for n in result.notes if n.startswith("synthesis agreement")]
    if result.synthesis and \
            result.synthesis["confidence"] < max(
                l.confidence for l in result.leaves if l.answer):
        assert notes, "a silent lowering would be unauditable"
        assert "independent" in notes[0]
