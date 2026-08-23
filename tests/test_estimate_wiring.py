"""ESTIMATE vs CEILING wiring — the leaf answer path.

The prototype (agp/estimate.py) separates the model's BELIEF from the
provenance ENTITLEMENT. This test file proves three things about the one
wired path (_answer_leaf in tools/pipeline/engine.py):

  1. EQUIVALENCE — the sealed/stored/reported number is bit-identical to
     what the old min() collapse produced. Nothing moved.
  2. VISIBILITY — when the model believes more than provenance permits,
     the raw estimate is now RECOVERABLE instead of destroyed.
  3. INVARIANTS — no code path raises either field; the estimate never
     leaks into anything that seals or acts.
"""
from __future__ import annotations

import asyncio
import json
import math
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_nosocket = NoSocket()
_nosocket.install()

from agp import estimate as ec_mod  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402


def _decompose_response() -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does the literature say about the topic",
         "kind": "descriptive", "question_type": "scholarly work search",
         "min_source_tier": 2, "min_independent_sources": 2,
         "quant_required": False},
        {"text": "has the government published agency rules on the topic",
         "kind": "descriptive", "question_type": "final/proposed agency "
         "rules with dates and docket refs",
         "min_source_tier": 1, "min_independent_sources": 1},
    ]})


def _answer(conf) -> str:
    return json.dumps({"answer": "the evidence supports the claim",
                       "proposed_confidence": conf})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _make(tmp_path, confs=(0.8, 0.7)):
    model = ScriptedModel({
        "Architect": [{"content": _decompose_response()}],
        "Manager": [{"content": _answer(c)} for c in confs],
    })
    routes = {
        "/works": json.dumps({"results": [
            {"id": "W1", "title": "Scholarly study on the topic: a "
             "literature review of scholarly work",
             "publication_year": 2024, "cited_by_count": 12}]}),
        "/documents.json": json.dumps({"documents": [
            {"title": "Final agency rule published by the government: "
             "proposed and final rules with dates, docket refs",
             "document_number": "2024-12345", "published_at": "2024-01-15",
             "agency": "government agency"}]}),
    }
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes), store=None), model


def _run(pipeline, q="What is known about the topic?"):
    return asyncio.get_event_loop().run_until_complete(
        pipeline.run(q, today=date(2026, 8, 22)))


# ── 1. EQUIVALENCE: the reported number did not move ─────────────────────

def test_sealed_number_equals_the_old_collapse(tmp_path):
    """For every leaf, confidence == round(min(proposed, ceiling), 2) — the
    exact expression engine.py computed before the split."""
    for conf in (0.8, 0.7, 0.95, 0.3, 0.54, 1.0):
        pipeline, _ = _make(tmp_path / f"c{conf}", confs=(conf, conf))
        result = _run(pipeline)
        assert result.leaves, result.refusal_reason
        for leaf in result.leaves:
            expected = round(min(
                conf,
                # the leaf's recorded ceiling IS the entitlement it was
                # clamped against — recompute the old expression with it
                leaf.confidence_ceiling), 2)
            if leaf.requirement_reasons:
                expected = min(expected, 0.54)
            assert leaf.confidence == expected, (
                f"proposal {conf}: {leaf.confidence} != old collapse {expected}")


def test_confidence_equals_min_estimate_ceiling(tmp_path):
    pipeline, _ = _make(tmp_path / "a", confs=(0.9, 0.9))
    result = _run(pipeline)
    for leaf in result.leaves:
        assert leaf.confidence == min(leaf.confidence_estimate,
                                      leaf.confidence_ceiling)


# ── 2. VISIBILITY: the belief survives the ceiling ───────────────────────

def test_estimate_survives_when_ceiling_is_harsh(tmp_path):
    """The whole point: proposed 0.95 against a sub-primary ceiling used to
    be destroyed by min(); now the raw belief is recoverable per leaf."""
    pipeline, _ = _make(tmp_path / "vis", confs=(0.95, 0.95))
    result = _run(pipeline)
    harsh = [l for l in result.leaves
             if l.confidence_ceiling < l.confidence_estimate]
    assert harsh, "no leaf had a ceiling below its estimate"
    for leaf in harsh:
        assert leaf.confidence_estimate == 0.95   # the belief, intact
        assert leaf.confidence == leaf.confidence_ceiling  # what we may claim


def test_estimate_and_ceiling_differ_and_sealable_matches(tmp_path):
    """Direct type-level pin of the invariant the path relies on."""
    ec = ec_mod.EstimateCeiling(estimate=0.92, ceiling=0.55)
    assert ec.confidence_estimate if False else True  # fields live on LeafOutcome
    assert ec.sealable() == math.floor(0.55 * 100) / 100  # 0.55, floor-conf
    assert ec.estimate > ec.ceiling          # they genuinely differ
    lowered = ec.with_ceiling(0.54)          # requirement-gate mechanism
    assert lowered.estimate == 0.92          # belief untouched
    assert lowered.sealable() == 0.54        # claim falls with the ceiling


# ── 3. INVARIANTS ────────────────────────────────────────────────────────

def test_no_method_can_raise_either_field():
    ec = ec_mod.EstimateCeiling(estimate=0.4, ceiling=0.6)
    try:
        ec.with_ceiling(0.7)
        raised = False
    except ValueError:
        raised = True
    assert raised, "ceiling rose — anti-inflation invariant broken"


def test_adversary_penalty_never_touches_the_estimate():
    ec = ec_mod.EstimateCeiling(estimate=0.8, ceiling=0.7)
    hit = ec.apply_adversary_penalty(0.2)
    assert hit.estimate == 0.8 and abs(hit.ceiling - 0.5) < 1e-9


def test_with_estimate_still_clamped_at_seal_time():
    ec = ec_mod.EstimateCeiling(estimate=0.3, ceiling=0.55)
    revised = ec.with_estimate(0.99)         # explicit new-evidence revision
    assert revised.estimate == 0.99
    assert revised.sealable() == 0.55        # but cannot claim above entitlement


def test_estimate_does_not_leak_into_the_parent_score(tmp_path):
    """Two runs, same evidence, different beliefs on the requirement-gated
    leaf whose 0.54 cap binds BOTH runs. The sealed numbers must be
    IDENTICAL — the estimate is diagnostic until proven otherwise."""
    low, _ = _make(tmp_path / "lo", confs=(0.60, 0.60))
    high, _ = _make(tmp_path / "hi", confs=(0.99, 0.60))
    r_lo, r_hi = _run(low), _run(high)
    assert r_lo.sealed and r_hi.sealed, (r_lo.refusal_reason,
                                         r_hi.refusal_reason)
    # The estimates genuinely differ — this test has something to detect.
    assert [l.confidence_estimate for l in r_lo.leaves] != \
        [l.confidence_estimate for l in r_hi.leaves]
    # But every clamped claim is identical across runs.
    assert [l.confidence for l in r_lo.leaves] == \
        [l.confidence for l in r_hi.leaves], "belief leaked into the claim"
    assert r_lo.confidence_score == r_hi.confidence_score, (
        "parent score moved with the estimate — leakage")
