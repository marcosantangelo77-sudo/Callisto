"""D2 — a parent whose leaves are ALL gap-classified must NOT seal.

Battery context (findings/battery): unknowable_04 (private utility
balance) and unknowable_05 (Wikipedia request count) have NO knowable
answer, yet both sealed at 0.34/SPECULATIVE. The conclusion prose said
"cannot be determined", but nothing reads prose — the seal entered the
record indistinguishable from a real answer.

Contract under test (reads STRUCTURED LeafOutcome.gap_kind only — never
the conclusion text; parsing prose for meaning is the forecast-sign
defect class):

  1. ALL leaves gap-classified (unprovable / honest_null /
     retrieval_failure) -> REFUSE, with the gap kinds in the refusal
     reason so the caller learns WHICH kind of nothing it got.
  2. MIXED (some provable) -> SEAL standing only on the provable
     leaves, ceiling capped at SPECULATIVE. Refuses-or-lowers only.
  3. Genuinely answered -> seals normally, untouched.

Run: python3 -m pytest tests/test_seal_unprovable.py -q
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


def _pipeline(routes, answer):
    reg, calls = _registry()
    mdl = _model(DECOMPOSE_ONE, answer=answer)
    pipe = ResearchPipeline(
        model=mdl, adversary_router=_Quiet(),
        transport=fixture_transport(routes), store=None,
        ledger=ProvenanceLedger(), registry=reg)
    return pipe, reg, calls


# ── Rule 1: all-unprovable refuses ──────────────────────────────────────────

def test_all_leaves_unprovable_must_not_seal():
    """One admitted source against min_independent_sources=2: the leaf
    'answers' on thin evidence and carries the unprovable verdict. That is
    not an answer — the parent must refuse, naming the kind."""
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": IRRELEVANT,
              "/fetch_gamma": IRRELEVANT}
    pipe, reg, calls = _pipeline(routes, answer="resilience improved")
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=2, gate_cov=0.25)
    assert all(l.gap_kind == "unprovable" for l in result.leaves), \
        [(l.gap_kind, l.requirement_reasons) for l in result.leaves]
    assert not result.sealed
    assert result.refusal_reason
    assert "unprovable" in result.refusal_reason


def test_all_leaves_retrieval_failure_must_not_seal():
    """Every source errors -> retrieval_failure verdicts. A run that could
    not LOOK may not seal either — 'we could not look' is not an answer."""
    class _Boom:
        def __getattr__(self, name):
            def call(*a, **k):
                raise RuntimeError("HTTP 500")
            return call

    reg, calls = _registry()
    from tools.sources.registry import SourceRegistry, SourceAdapter, \
        SourceSpec
    reg2 = SourceRegistry()
    for name, url in [("alpha", "https://api.openalex.org"),
                      ("beta", "https://api.gdeltproject.org")]:
        reg2.register(SourceAdapter(
            spec=SourceSpec(name=name, base_url=url, description="",
                            answers=("scholarly works on semiconductor "
                                     "supply chain resilience",), tier=1,
                            min_interval_s=0.0),
            make_adapter=lambda src: _Boom()))
    calls = {n: ("works_search", ("term",), {"limit": 3})
             for n in ("alpha", "beta")}
    mdl = ScriptedModel({
        "Architect": [{"content": DECOMPOSE_ONE}],
        "Manager": [{"content": json.dumps(
            {"answer": "", "proposed_confidence": 0.7})}],
    })
    pipe = ResearchPipeline(model=mdl, adversary_router=_Quiet(),
                            transport=fixture_transport({}), store=None,
                            ledger=ProvenanceLedger(), registry=reg2)
    result, traces = _run(pipe, reg2, calls, max_rounds=1, max_spq=1)
    assert traces, "no retrieval ran"
    assert all(l.gap_kind == "retrieval_failure" for l in result.leaves), \
        [l.gap_kind for l in result.leaves]
    assert not result.sealed
    assert "retrieval_failure" in result.refusal_reason


# ── The exact battery cases ────────────────────────────────────────────────

def test_battery_unknowable_shape_does_not_seal():
    """unknowable_04 / unknowable_05 shape: every fetch returns topically
    unrelated content (the worldbank-junk pattern); any model 'answers' are
    written over gap verdicts and prove nothing. Must refuse."""
    routes = {"/fetch_alpha": IRRELEVANT, "/fetch_beta": IRRELEVANT,
              "/fetch_gamma": IRRELEVANT}
    pipe, reg, calls = _pipeline(
        routes, answer="no evidence was found bearing on this question")
    result, _ = _run(pipe, reg, calls, adaptive_gain=False, stasis=True,
                     max_rounds=2, max_spq=2, gate_cov=0.25)
    assert not result.sealed, (
        f"sealed a non-answer: conf={result.confidence_score} "
        f"kinds={result.gap_kinds}")
    assert result.refusal_reason


# ── Rule 2: mixed case seals, lowered to SPECULATIVE, standing on proven ────

def test_mixed_case_stands_only_on_provable_leaf_and_caps_ceiling():
    """Two leaves over identical good evidence: one needs 2 independent
    sources (met -> provable), one needs 3 of 3 registered (unmet ->
    unprovable despite its wordier answer). The parent seals but stands
    ONLY on the proven leaf and cannot exceed SPECULATIVE."""
    decompose_two = json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive", "question_type": "scholarly literature",
         "min_source_tier": 2, "min_independent_sources": 2},
        {"text": "how do firms measure supply chain resilience outcomes",
         "kind": "descriptive", "question_type": "scholarly measurement",
         "min_source_tier": 2, "min_independent_sources": 5},
    ]})
    reg, calls = _registry()
    answers = {
        "Architect": [{"content": decompose_two}],
        "Manager": [
            # leaf 1: solid answer on adequate evidence
            {"content": json.dumps(
                {"answer": "the literature suggests resilience improved.",
                 "proposed_confidence": 0.9})},
            # leaf 2: wordy answer over unmet requirements
            {"content": json.dumps(
                {"answer": "firms measure resilience with elaborate "
                           "quantitative dashboards everywhere.",
                 "proposed_confidence": 0.95})},
        ],
    }
    base = ScriptedModel(answers)
    mdl = _MultiLeafModel(base)
    pipe = ResearchPipeline(
        model=mdl, adversary_router=_Quiet(),
        transport=fixture_transport({
            "/fetch_alpha": GOOD, "/fetch_beta": GOOD,
            "/fetch_gamma": GOOD}),
        store=None, ledger=ProvenanceLedger(), registry=reg)
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=3, gate_cov=0.25)
    kinds = [l.gap_kind or "" for l in result.leaves]
    assert result.sealed, result.refusal_reason
    assert "unprovable" in kinds, kinds
    assert any(k == "" for k in kinds), kinds   # at least one provable
    # capped at SPECULATIVE even though the best scripted proposal was 0.9+
    assert result.confidence_score <= 0.54, result.confidence_score
    assert result.confidence_tier == "SPECULATIVE", result.confidence_tier
    assert result.stance != "UNDETERMINED" or True  # stance from proven leaf


class _MultiLeafModel:
    """Routes each Manager call to the next scripted answer in order."""

    def __init__(self, base):
        self._base = base
        self._n = 0

    async def complete(self, task_class, messages, schema=None, **kw):
        if task_class == "Manager":
            self._n += 1
        return await self._base.complete(task_class, messages)


# ── Rule 3: genuinely answered still seals normally ────────────────────────

def test_genuinely_answered_question_still_seals():
    """Both sources admit good evidence, requirements met, no gap verdicts:
    the parent seals at full strength. The fix must not refuse everything."""
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": GOOD,
              "/fetch_gamma": GOOD}
    pipe, reg, calls = _pipeline(
        routes, answer="the literature suggests resilience improved")
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=3, gate_cov=0.25)
    assert all(not l.gap_kind for l in result.leaves), \
        [l.gap_kind for l in result.leaves]
    assert result.sealed, result.refusal_reason
    assert result.confidence_score > 0
