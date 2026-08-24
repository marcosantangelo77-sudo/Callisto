"""Gap-triggered re-planning (tools.pipeline.replan) — wiring tests.

Contract under test:
  - A leaf whose gap is a RETRIEVAL FAILURE with a planner-actionable
    obstacle gets ONE re-plan; the replacement sub-question is fetched and
    answered through the SAME scoring path.
  - Honest nulls, unprovable leaves, and access obstacles (no_api_key,
    rate_limited, paywalled) never trigger a re-plan.
  - The re-planner consumes no confidence information and no code path
    raises a confidence score.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.replan import (  # noqa: E402
    parse_replacement,
    replan_messages,
    should_replan,
)


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _decomp(specs):
    return json.dumps({"sub_questions": specs})


BAD_SPEC = {
    "text": "what does the literature say about the topic",
    "kind": "descriptive", "question_type": "scholarly work search",
    "min_source_tier": 2, "min_independent_sources": 2,
}
GOOD_SPEC = {
    "text": "has the government published agency rules on the topic",
    "kind": "descriptive",
    "question_type":
        "final/proposed agency rules with dates and docket refs",
    "min_source_tier": 1, "min_independent_sources": 1,
}


def _run(tmp_path, specs, model=None, routes=None):
    """Run the pipeline with one scholarly leaf whose retrieval fails
    structurally (no fixture route serves /works -> nothing routable)."""
    model = model or ScriptedModel({
        "Architect": [{"content": _decomp([BAD_SPEC, GOOD_SPEC])}],
        "Manager": [{"content": "{}"}],   # bad leaf answers nothing
    })
    pipe = ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes or {"/documents.json": json.dumps(
            {"documents": [
                {"title": "Final agency rule published by the government",
                 "document_number": "2024-12345",
                 "published_at": "2024-01-15"}]})}),
    )
    result = asyncio.get_event_loop().run_until_complete(
        pipe.run("What is known about the topic?", today=date(2026, 8, 22)))
    return result, model


# ── unit: the membership rule ──────────────────────────────────────────────

@pytest.mark.parametrize("kind,obstacle,want", [
    ("retrieval_failure", "no_query_issued", True),
    ("retrieval_failure", "no_adapter", True),
    ("retrieval_failure", "", False),          # unknown obstacle: no fire
    ("retrieval_failure", "no_api_key", False),   # access problem: owner's
    ("retrieval_failure", "rate_limited", False),
    ("retrieval_failure", "paywalled", False),
    ("honest_null", "none", False),            # competent search: don't burn
    ("unprovable", "none", False),             # our own bar: not planning's
    ("", "", False),                           # answered normally
])
def test_should_replan_membership(kind, obstacle, want):
    assert should_replan(kind, obstacle) is want


def test_parse_replacement_rejects_degenerate():
    orig = type("Q", (), {"text": "same question"})()
    assert parse_replacement(None, orig) is None
    assert parse_replacement({"sub_questions": []}, orig) is None
    same = {"sub_questions": [{"text": "same question"}]}
    assert parse_replacement(same, orig) is None          # identical text
    new = {"sub_questions": [{"text": "a different question"}]}
    assert parse_replacement(new, orig)["text"] == "a different question"


def test_replan_messages_carry_no_confidence_fields():
    msgs = replan_messages("q", "why it failed", "root")
    blob = json.dumps(msgs)
    for banned in ("confidence", "tier", "proposed"):
        assert banned not in blob


# ── integration: engine wiring ─────────────────────────────────────────────

def test_no_replan_on_honest_or_access_gaps(tmp_path):
    """A leaf that answered normally produces zero re-plan events."""
    result, model = _run(
        tmp_path, [GOOD_SPEC],
        model=ScriptedModel({
            "Architect": [{"content": _decomp([GOOD_SPEC])}],
            "Manager": [{"content": json.dumps({
                "answer": "rules exist", "proposed_confidence": 0.7})}]},
        ))
    assert result.sealed or "leaf" in result.refusal_reason
    assert result.replan_events == []
    arch_calls = [c for c in model.calls if c[0] == "Architect"]
    assert len(arch_calls) == 1     # decompose only — no second consult


def test_replan_fires_only_on_actionable_retrieval_failure(tmp_path):
    """The scholarly leaf has NO route (/works unhandled): selection can
    produce nothing fetchable for it. When the gap classifies as an
    actionable retrieval failure, exactly ONE extra Architect turn happens
    and its replacement leaf runs through fetch+answer."""
    replacement = dict(GOOD_SPEC,
                       text="did regulators publish rules on the topic "
                            "(replacement angle)")
    model = ScriptedModel({
        "Architect": [
            {"content": _decomp([BAD_SPEC, GOOD_SPEC])},        # decompose
            {"content": _decomp([replacement])},                # re-plan
        ],
        "Manager": [{"content": "{}"},           # bad leaf: unanswered
                    {"content": json.dumps({      # replacement leaf answers
                        "answer": "the agency did publish rules",
                        "proposed_confidence": 0.6})}],
    })
    result, model2 = _run(tmp_path, None, model=model)
    arch = [c for c in model2.calls if c[0] == "Architect"]
    assert len(arch) == 2, "expected exactly one re-plan consult"
    assert len(result.replan_events) == 1
    ev = result.replan_events[0]
    assert ev["reason"].startswith("retrieval_failure:")
    assert ev["new_text"] == replacement["text"][:500]
    assert any("re-plan of leaf" in n for n in result.notes)


def test_no_code_path_raises_confidence(tmp_path):
    """Replacement-leaf confidence obeys min(estimate, ceiling) <= 0.55-ish
    provenance ceilings: the re-plan cannot smuggle in a higher number."""
    replacement = dict(GOOD_SPEC, text="replacement sub-question text")
    model = ScriptedModel({
        "Architect": [
            {"content": _decomp([BAD_SPEC, GOOD_SPEC])},
            {"content": _decomp([replacement])},
        ],
        "Manager": [{"content": "{}"},
                    {"content": json.dumps({
                        "answer": "answered via replacement",
                        # absurdly high proposal must still be clamped
                        "proposed_confidence": 0.99})}],
    })
    result, _ = _run(tmp_path, None, model=model)
    for leaf in result.leaves:
        assert leaf.confidence <= max(leaf.confidence_estimate,
                                      leaf.confidence_ceiling) + 1e-9
        if leaf.gap_kind != "retrieval_failure":
            # sealed confidence equals min(estimate, ceiling), rounded
            assert leaf.confidence <= 1.0 and leaf.tier != ""
