"""STANCE PROPAGATION — the parent's direction may not be inherited from a
leaf that answers a different question.

Found by the one-real-question run (findings/one_real_question.md,
run record findings/one_real_question_run.json, commit ffeca37):

    Q: "Has the US unemployment rate been lower in 2026 than in
       January 2023?"
    Truth: Jan 2023 = 3.5%; every 2026 month >= 4.1%. Answer: NO.
    The system sealed stance=AFFIRMS at 0.55 PROBABLE — the WRONG
    DIRECTION.

Mechanism: engine.py took the parent's stance from `best_leaf`, the
highest-CONFIDENCE leaf. Two factual-lookup leaves ("what was the Jan
2023 rate?", "what has 2026 been?") each sealed VERIFIED 0.95 with
stance AFFIRMS — each affirming ITS OWN sub-question — while the only
leaf actually answering the parent's comparison claim sat at 0.54
UNDETERMINED (honestly: its evidence was insufficient, gap_kind
unprovable). A lookup leaf affirming itself is not evidence about the
parent's claim.

The rule pinned here:
  - only DECISIONAL leaves (comparative frame + quantities shared with a
    sibling leaf) with an adequate-confidence DECLARED direction may set
    the parent stance;
  - otherwise the parent is UNDETERMINED;
  - confidence is untouched by this rule in both directions.

The tests drive ResearchPipeline end-to-end over THE EXACT live
decomposition with fixture-routed FRED evidence, so the real seal path
runs.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    PipelineResult,
    ResearchPipeline,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402


ROOT_QUESTION = ("Has the US unemployment rate been lower in 2026 than it "
                 "was in January 2023?")

# The decomposition the live model produced: two lookups that affirm
# themselves, one comparison leaf that honestly reports it cannot decide.
DECOMPOSE = json.dumps({"sub_questions": [
    {"text": "What was the official US unemployment rate in January 2023?",
     "kind": "descriptive", "question_type": "macro time series",
     "min_source_tier": 1, "min_independent_sources": 1,
     "quant_required": False, "horizon_days": None},
    {"text": "What has been the US unemployment rate in 2026 to date?",
     "kind": "descriptive", "question_type": "macro time series",
     "min_source_tier": 1, "min_independent_sources": 1,
     "quant_required": False, "horizon_days": None},
    {"text": "How does the 2026 unemployment rate compare numerically to "
             "the January 2023 rate?",
     "kind": "descriptive", "question_type": "macro time series",
     "min_source_tier": 1, "min_independent_sources": 1,
     "quant_required": True, "horizon_days": None},
]})

FRED_BODY = json.dumps({
    "_series_title": "Unemployment Rate",
    "observations": [
        {"date": "2023-01-01", "value": "3.5"},
        {"date": "2026-07-01", "value": "4.1"},
    ],
})
ROUTES = {
    "/series/observations": FRED_BODY,
    "/series?series_id=UNRATE":
        json.dumps({"seriess": [{"title": "Unemployment Rate"}]}),
}


def _answer(conf: float, answer: str, stance: str) -> str:
    return json.dumps({"answer": answer, "proposed_confidence": conf,
                       "stance": stance, "compute": None})


class _Adversary:
    async def complete(self, task_class, messages, schema=None, **_ig):
        return {"parsed_json": {"objections": []}, "model": "stub-adversary"}


def _run(tmp_path, leaf_answers) -> PipelineResult:
    """Full pipeline run over THE EXACT live decomposition; per-leaf
    scripted Manager responses keyed by the engine's deterministic tag."""
    model = ScriptedModel({"Architect": [DECOMPOSE]})
    for tag, resp in leaf_answers.items():
        model.script_for(tag, "Manager", resp)
        model.script_for(tag, "Manager", resp)   # idempotent double-queue
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(ROUTES),
        store=ArtifactStore(root=tmp_path / "artifacts"),
        ledger=ProvenanceLedger())
    return asyncio.run(pipeline.run(ROOT_QUESTION, today=date(2026, 8, 24)))


# The live failure verbatim: lookups 0.95 AFFIRMS (their own questions),
# comparison leaf 0.54 UNDETERMINED. Old engine -> parent AFFIRMS. Wrong.
LEAF_ANSWERS_LIVE_CASE = {
    "leaf0": _answer(0.95, "the Jan 2023 rate was 3.5%", "AFFIRMS"),
    "leaf1": _answer(0.95, "2026 has run 4.3 to 4.1%", "AFFIRMS"),
    "leaf2": _answer(0.54,
                     "the provided observations are truncated; no numeric "
                     "comparison can be made from the given evidence",
                     "UNDETERMINED"),
}


def test_lookup_leaves_at_095_cannot_seal_parent_affirms(tmp_path):
    """THE regression: 3.5 vs 4.1 must NOT seal as AFFIRMS off confident
    lookup leaves. Parent is UNDETERMINED when only non-decisional leaves
    declare direction."""
    result = _run(tmp_path, LEAF_ANSWERS_LIVE_CASE)
    assert result.sealed, result.refusal_reason
    assert result.stance != "AFFIRMS", (
        "parent inherited AFFIRMS from lookup leaves answering their own "
        "sub-questions — stance propagation defect")
    assert result.stance == "UNDETERMINED"
    assert any("lookup leaves" in n for n in result.notes)


def test_decisional_leaf_with_declared_direction_sets_stance(tmp_path):
    """When the comparison leaf itself declares DENIES at PROBABLE+
    confidence, the parent inherits THAT direction — from the leaf that
    actually answers the parent's claim."""
    answers = dict(LEAF_ANSWERS_LIVE_CASE)
    answers["leaf2"] = _answer(
        0.70, "every 2026 month exceeds 3.5%, so 2026 was never lower",
        "DENIES")
    result = _run(tmp_path, answers)
    assert result.sealed, result.refusal_reason
    assert result.stance == "DENIES"


def test_decisional_leaf_below_confidence_bar_leaves_parent_undetermined(
        tmp_path):
    """A decisional leaf whose own confidence says 'unproven' (below the
    PROBABLE band) does not set direction either — matching leaf 3's honest
    unprovable verdict in the live run."""
    answers = dict(LEAF_ANSWERS_LIVE_CASE)
    answers["leaf2"] = _answer(0.54, "insufficient evidence", "DENIES")
    result = _run(tmp_path, answers)
    assert result.sealed, result.refusal_reason
    assert result.stance == "UNDETERMINED"


def test_confidence_never_raised_by_the_stance_rule(tmp_path):
    """No confidence score may be raised by stance selection: the sealed
    number still comes from best_leaf via provenance, only lowered by
    adversary/inheritance as before."""
    result = _run(tmp_path, LEAF_ANSWERS_LIVE_CASE)
    best = max(l.confidence for l in result.leaves)
    assert result.confidence_score <= best + 1e-9


def test_conflicting_decisional_leaves_take_the_higher_confidence_one(
        tmp_path):
    """Two DECISIONAL leaves disagreeing on direction resolve to the more
    confident one — direction selection mirrors magnitude selection WITHIN
    the decisional class only. The lookup leaf's 0.90 AFFIRMS (its own
    question) still cannot outvote a decisional leaf."""
    answers = dict(LEAF_ANSWERS_LIVE_CASE)
    # two decisional leaves: leaf2 DENIES at 0.70, leaf3 AFFIRMS at 0.80
    answers["leaf2"] = _answer(0.70, "2026 was never below 3.5%", "DENIES")
    decompose = json.dumps({"sub_questions": [
        DECOMPOSE and json.loads(DECOMPOSE)["sub_questions"][0],
        json.loads(DECOMPOSE)["sub_questions"][1],
        {"text": "Compared to the January 2023 rate of 2023 data, is the "
                 "2026 rate lower?",
         "kind": "descriptive", "question_type": "macro time series",
         "min_source_tier": 1, "min_independent_sources": 1,
         "quant_required": False, "horizon_days": None},
        {"text": "Is the 2026 unemployment rate greater than the January "
                 "2023 level of 2023?",
         "kind": "descriptive", "question_type": "macro time series",
         "min_source_tier": 1, "min_independent_sources": 1,
         "quant_required": False, "horizon_days": None},
    ]})
    model = ScriptedModel({"Architect": [decompress := decompose]})
    for tag, resp in (("leaf0", LEAF_ANSWERS_LIVE_CASE["leaf0"]),
                      ("leaf1", answers["leaf1"]),
                      ("leaf2", answers["leaf2"]),
                      ("leaf3", _answer(0.80,
                                        "2026 is higher than Jan 2023",
                                        "AFFIRMS"))):
        model.script_for(tag, "Manager", resp)
        model.script_for(tag, "Manager", resp)
    import asyncio as _aio
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(ROUTES),
        store=ArtifactStore(root=tmp_path / "artifacts"),
        ledger=ProvenanceLedger())
    result = _aio.run(pipeline.run(ROOT_QUESTION, today=date(2026, 8, 24)))
    assert result.sealed, result.refusal_reason
    assert result.stance == "AFFIRMS"
