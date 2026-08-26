"""Regression tests: stale-confidence diagnostics must be honest.

A stale descendant record is unresolved-at-deadline — it is NOT a resolved
descendant and earns no confidence credit anywhere in the inheritance rule
(bounded staleness penalty only, max -0.20). These tests pin the operator
diagnostics so neither the calibration trace nor the WHY explanation can be
read as counting stale records as genuine evidence.

Covers:
  1. 4 genuine + 1 stale trace (tools/calibration/instrument.py): the clamp
     stays at the SPECULATIVE cap and the trace labels resolved vs stale.
  2. 5 genuine + 100 stale WHY output (tools/why.py): the stale count and
     applied penalty are explicit.
  3. No-stale control: ordinary output remains sensible (no stale line).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load_instrument():
    """Load tools/calibration/instrument.py by file path.

    The tools.calibration package __init__ has a PRE-EXISTING broken import
    (references replay_chain / bridge.py that were never committed on this
    branch's history), so importing via the package fails for reasons this
    branch did not introduce. Loading the module file directly exercises the
    code under test without repairing or depending on that defect.
    """
    name = "_calibration_instrument_under_test"
    mod = sys.modules.get(name)
    if mod is not None:
        return mod
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "tools" / "calibration" / "instrument.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _recs(n_hit=0, n_miss=0, n_stale=0):
    out = []
    out += [{"question_id": f"h{i}", "resolved_at": "2026-01-01",
             "outcome": "hit"} for i in range(n_hit)]
    out += [{"question_id": f"m{i}", "resolved_at": "2026-01-01",
             "outcome": "miss"} for i in range(n_miss)]
    out += [{"question_id": f"s{i}", "resolved_at": "2026-01-01",
             "outcome": "stale"} for i in range(n_stale)]
    return out


# ── 1. calibration trace: 4 genuine + 1 stale ─────────────────────────────

def test_trace_labels_resolved_and_stale_counts():
    inst = _load_instrument()
    recs = _recs(n_hit=4, n_stale=1)
    tr, veto = inst.replay_parent_chain(
        best_leaf_confidence=0.55, descendant_resolutions=recs,
        objections=[])
    assert veto is None
    step = next(s for s in tr.steps if s.mechanism == "inheritance_clamp")
    # Precise terminology: stale is not resolved evidence.
    assert "n_resolved_genuine=4" in step.detail
    assert "n_stale_not_evidence=1" in step.detail
    assert step.detail.count("n_descendants=") == 1
    # Clamp stays at the SPECULATIVE cap: only 4 genuine resolutions ever,
    # and the single stale earned nothing.
    from tools.research_program import SPECULATIVE_CAP
    assert step.after == pytest.approx(min(0.55, SPECULATIVE_CAP))


def test_trace_all_genuine_control_has_zero_stale_label():
    inst = _load_instrument()
    recs = _recs(n_hit=5)
    tr, _ = inst.replay_parent_chain(
        best_leaf_confidence=0.55, descendant_resolutions=recs, objections=[])
    step = next(s for s in tr.steps if s.mechanism == "inheritance_clamp")
    assert "n_resolved_genuine=5" in step.detail
    assert "n_stale_not_evidence=0" in step.detail


# ── 2. WHY: 5 genuine + 100 stale ─────────────────────────────────────────

def test_why_exposes_stale_count_and_penalty(tmp_path):
    from tests.helpers.no_socket import NoSocket
    ns = NoSocket(); ns.install()
    import json
    from datetime import date
    from tools.pipeline.engine import ResearchPipeline, fixture_transport
    from tools.pipeline.model import ScriptedModel
    from tools.artifacts import ArtifactStore
    from tools.why import explain_result

    routes = {
        "/works": json.dumps({"results": [
            {"id": "W1", "title": "Scholarly study on the topic: a "
             "literature review of scholarly work", "publication_year": 2024}]}),
        "/graph/v1/paper/search": json.dumps({"data": [
            {"title": "Another scholarly review of the topic with scholarly "
             "work detail", "year": 2023}]}),
    }

    class _QuietAdversary:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    recs = _recs(n_hit=5, n_stale=100)
    model = ScriptedModel({
        "Architect": [{"content": json.dumps({"sub_questions": [
            {"text": "what does the scholarly literature say about the topic",
             "kind": "descriptive", "question_type": "scholarly work search",
             "min_source_tier": 2, "min_independent_sources": 1}]})}],
        "Manager": [{"content": json.dumps(
            {"answer": "the evidence supports the claim",
             "proposed_confidence": 0.8})}],
    })
    pipeline = ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes),
        store=ArtifactStore(root=tmp_path / "art"),
        descendant_resolutions=recs)
    result = pipeline.run("What is known about the topic?",
                          today=date(2026, 8, 22))
    if hasattr(result, "__await__"):
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(result)

    expl = explain_result(result, ledger=pipeline.ledger,
                          descendant_resolutions=recs)
    inh = next(c for c in expl.ceilings if c.kind == "inheritance")
    assert "100 stale descendant(s)" in inh.detail
    assert "NOT resolved evidence" in inh.detail
    # Machine-readable fields carry the same accounting.
    d = expl.to_dict()
    assert d["stale_descendants"] == 100
    assert 0 < d["stale_penalty_applied"] <= 0.20 + 1e-9
    # The applied penalty matches the documented bounded rate over ALL
    # counted records (bounded fraction -> never exceeds -0.20).
    from tools.research_program import summarize_track_record, stale_penalty_rate
    expected = round(stale_penalty_rate() *
                     summarize_track_record(recs).stale_fraction, 4)
    assert d["stale_penalty_applied"] == pytest.approx(expected)
    # Plain-language narrative names it too.
    text = expl.narrative()
    assert "NOT resolved evidence" in text
    assert "100 stale descendant" in text
    # And no stale record was credited as resolved evidence anywhere.
    assert "101 resolved" not in text and "105 resolved" not in text


# ── 3. no-stale control ───────────────────────────────────────────────────

def test_why_without_stales_omits_stale_lines(tmp_path):
    import json
    from datetime import date
    from tools.pipeline.engine import ResearchPipeline, fixture_transport
    from tools.pipeline.model import ScriptedModel
    from tools.artifacts import ArtifactStore
    from tools.why import explain_result

    routes = {
        "/works": json.dumps({"results": [
            {"id": "W1", "title": "Scholarly study on the topic: a "
             "literature review of scholarly work", "publication_year": 2024}]}),
        "/graph/v1/paper/search": json.dumps({"data": [
            {"title": "Another scholarly review of the topic with scholarly "
             "work detail", "year": 2023}]}),
    }

    class _QuietAdversary:
        async def complete(self, task_class, messages, schema=None):
            return {"parsed_json": {"objections": []}, "model": "stub"}

    recs = _recs(n_hit=5)
    model = ScriptedModel({
        "Architect": [{"content": json.dumps({"sub_questions": [
            {"text": "what does the scholarly literature say about the topic",
             "kind": "descriptive", "question_type": "scholarly work search",
             "min_source_tier": 2, "min_independent_sources": 1}]})}],
        "Manager": [{"content": json.dumps(
            {"answer": "the evidence supports the claim",
             "proposed_confidence": 0.8})}],
    })
    pipeline = ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes),
        store=ArtifactStore(root=tmp_path / "art"),
        descendant_resolutions=recs)
    result = pipeline.run("What is known about the topic?",
                          today=date(2026, 8, 22))
    if hasattr(result, "__await__"):
        import asyncio
        result = asyncio.get_event_loop().run_until_complete(result)

    expl = explain_result(result, ledger=pipeline.ledger,
                          descendant_resolutions=recs)
    d = expl.to_dict()
    assert d["stale_descendants"] == 0
    assert d["stale_penalty_applied"] == 0.0
    assert "stale" not in expl.narrative().lower()
    inh = next(c for c in expl.ceilings if c.kind == "inheritance")
    assert "resolved descendant(s)" in inh.detail
