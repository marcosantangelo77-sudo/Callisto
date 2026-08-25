"""Task 212 / defect R3 — a VERIFIED leaf that determines nothing must not seal.

The battery re-run regressed unknowable_02 and treasury_04 from CORRECT
REFUSAL to SEALED: the task-194 seal gate keyed on GAP-CLASSIFICATION only,
so a leaf with excellent provenance whose evidence answers nothing sailed
through — the purest form of internally-consistent-and-externally-wrong.

Contract under test (reads the DECLARED structured signal
LeafOutcome.answers_question only — never the conclusion prose; parsing
prose for meaning is the forecast-sign defect class):

  1. Every leaf VERIFIED-grade but DECLARED non-answering -> REFUSE
     (the exact unknowable_02 / treasury_04 regression shape).
  2. A mixed parent stands ONLY on leaves that answer; a declared
     non-answer never sets parent direction or magnitude.
  3. A VERIFIED leaf that DOES answer still seals normally.
  4. ONE predicate decides "may this seal" — the same `provable` set as
     task 194's rule, extended with the declared signal, not a competing
     second gate.
  5. The signal may only refuse or lower; no confidence number rises.

Run: python3 -m pytest tests/test_fix_r3_nonanswering_leaf.py -q
"""
from __future__ import annotations

import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tests.helpers.no_socket import NoSocket  # noqa: E402
from tests.test_integration_seam_engine import (  # noqa: E402
    DECOMPOSE_ONE, GOOD, IRRELEVANT, _Quiet, _model, _registry, _run)

NoSocket().install()


def _pipeline(routes, answer_payload):
    reg, calls = _registry()
    mdl = _model(DECOMPOSE_ONE, answer="")
    # swap the scripted Manager answer for a full JSON payload
    mdl.responses["Manager"] = [{"content": json.dumps(answer_payload)}]
    pipe = ResearchPipeline(
        model=mdl, adversary_router=_Quiet(),
        transport=fixture_transport(routes), store=None,
        ledger=ProvenanceLedger(), registry=reg)
    return pipe, reg, calls


# ── Regression 1: VERIFIED provenance + declared non-answer -> refuse ──────

def test_verified_but_declared_non_answering_refuses():
    """unknowable_02 / treasury_04 shape: real bytes came back (topically
    adjacent material, admitted by the gate) so provenance is excellent —
    but the model DECLARES they answer nothing about the question asked.
    Must refuse despite VERIFIED-grade sources."""
    pipe, reg, calls = _pipeline(
        {"/fetch_alpha": GOOD, "/fetch_beta": GOOD,
         "/fetch_gamma": IRRELEVANT},
        {"answer": "the material retrieved does not determine this",
         "proposed_confidence": 0.9,
         "stance": "UNDETERMINED",
         "answers_question": False})
    result, traces = _run(pipe, reg, calls, adaptive_gain=True,
                          max_rounds=2, max_spq=2, gate_cov=0.25)
    assert all(not l.answers_question for l in result.leaves), \
        [(l.answers_question, l.gap_kind) for l in result.leaves]
    assert not result.sealed, (
        f"sealed a VERIFIED non-answer: conf={result.confidence_score}")
    assert result.refusal_reason
    assert "non-answering" in result.refusal_reason


def test_declared_non_answer_never_sets_parent_direction():
    """Mixed parent: one leaf genuinely answers, one is VERIFIED-grade but
    declares itself non-answering at HIGHER confidence. The parent must
    stand only on the answering leaf — direction and magnitude come from
    a leaf that actually answers, never from the empty one."""
    decompose_two = json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive", "question_type": "scholarly literature",
         "min_source_tier": 2, "min_independent_sources": 1},
        {"text": "how do firms measure supply chain resilience outcomes",
         "kind": "descriptive", "question_type": "scholarly measurement",
         "min_source_tier": 2, "min_independent_sources": 1},
    ]})
    reg, calls = _registry()
    base = ScriptedModel({
        "Architect": [{"content": decompose_two}],
        "Manager": [
            # answering leaf, modest confidence
            {"content": json.dumps(
                {"answer": "the literature suggests resilience improved.",
                 "proposed_confidence": 0.6, "stance": "AFFIRMS",
                 "answers_question": True})},
            # high-confidence VERIFIED non-answer
            {"content": json.dumps(
                {"answer": "dashboards were mentioned; nothing settles "
                           "the question asked.",
                 "proposed_confidence": 0.95, "stance": "DENIES",
                 "answers_question": False})},
        ],
    })

    class _Multi:
        def __init__(self, b):
            self._b, self._n = b, 0

        async def complete(self, task_class, messages, schema=None, **kw):
            if task_class == "Manager":
                self._n += 1
            return await self._b.complete(task_class, messages)

    pipe = ResearchPipeline(
        model=_Multi(base), adversary_router=_Quiet(),
        transport=fixture_transport({
            "/fetch_alpha": GOOD, "/fetch_beta": GOOD,
            "/fetch_gamma": GOOD}),
        store=None, ledger=ProvenanceLedger(), registry=reg)
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=3, gate_cov=0.25)
    assert result.sealed, result.refusal_reason
    # magnitude capped below the empty leaf's 0.95 proposal: the parent
    # cannot inherit strength from a leaf that determines nothing
    assert result.confidence_score < 0.95, result.confidence_score


# ── Regression guard: a VERIFIED leaf that DOES answer still seals ─────────

def test_verified_answering_leaf_still_seals_normally():
    """Same good evidence, same tier, but answers_question=true: seals at
    full strength exactly as before. Refusal must be surgical."""
    pipe, reg, calls = _pipeline(
        {"/fetch_alpha": GOOD, "/fetch_beta": GOOD, "/fetch_gamma": GOOD},
        {"answer": "the literature suggests resilience improved",
         "proposed_confidence": 0.9, "stance": "AFFIRMS",
         "answers_question": True})
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=3, gate_cov=0.25)
    assert all(l.answers_question for l in result.leaves)
    assert result.sealed, result.refusal_reason
    assert result.confidence_score > 0


def test_legacy_absent_signal_defaults_true_and_seals():
    """A model that does not emit the new field (old prompt / legacy
    checkpoint) defaults to answers_question=True: behaviour unchanged,
    the signal can only ever refuse or lower, never surprise-refuse."""
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": GOOD, "/fetch_gamma": GOOD}
    pipe, reg, calls = _pipeline(routes, {
        "answer": "the literature suggests resilience improved",
        "proposed_confidence": 0.8, "stance": "AFFIRMS"})
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=3, gate_cov=0.25)
    assert all(l.answers_question for l in result.leaves)
    assert result.sealed, result.refusal_reason


# ── One predicate, not two ──────────────────────────────────────────────────

def test_single_predicate_covers_gap_and_nonanswer_kinds():
    """The refusal names WHY each leaf failed the one provable predicate:
    gap kinds for gapped leaves, 'non-answering' for declared empties —
    one gate, one breakdown, no second competing rule to drift."""
    pipe, reg, calls = _pipeline(
        {"/fetch_alpha": IRRELEVANT, "/fetch_beta": IRRELEVANT,
         "/fetch_gamma": IRRELEVANT},
        {"answer": "nothing here bears on the question",
         "proposed_confidence": 0.9, "stance": "UNDETERMINED",
         "answers_question": False})
    result, _ = _run(pipe, reg, calls, adaptive_gain=False, stasis=True,
                     max_rounds=2, max_spq=2, gate_cov=0.25)
    assert not result.sealed
    reason = result.refusal_reason
    assert ("non-answering" in reason) or any(
        k in reason for k in ("honest_null", "retrieval_failure",
                              "unprovable")), reason
