"""Ablation: rerun the same questions with ONE mechanism disabled at a time.

Disabling happens by runtime patch INSIDE this process, restored before
return; no repo value is edited and no ceiling is relaxed in any file. This
run MEASURES — the shipped ceilings, penalties and clamps are untouched.

Arms:
  baseline          everything as shipped
  no_provenance     MAX_CONFIDENCE_BY_SOURCE treated as all-1.0
  no_requirements   EvidenceRequirement.unmet_reasons -> []
  no_inheritance    clamp_parent_confidence -> identity
  no_adversary      adversary router returns zero objections
  no_floor          DB_CONFIDENCE_FLOOR -> 0.0 (never refuse)

self_review_ceiling / ensemble_spread / synthesis_agreement have no arms:
instrument.py records them as INERT in this path — there is nothing to
disable (that absence IS the finding for those three).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Iterable

import agp.research_program as agp_rp
import tools.pipeline.engine as engine_mod
from tools.pipeline.model import PipelineModel
from tools.pipeline.retro import _AdversaryRouterStub

from tools.calibration.instrument import instrumented_run, InstrumentedRun
from tools.calibration.signature import (
    signature_adversary_router,
    signature_routes,
)

DISABLABLE = ("provenance", "requirements", "inheritance", "adversary",
              "floor")


@dataclass
class ArmResult:
    arm: str
    disabled: frozenset[str]
    probabilities: dict[str, float] = field(default_factory=dict)
    sealed: dict[str, bool] = field(default_factory=dict)
    runs: list[InstrumentedRun] = field(default_factory=list)

    @property
    def mean_certainty(self) -> float:
        """Mean |p - 0.5| — how far from coin-flip the predictions sit."""
        if not self.probabilities:
            return 0.0
        return sum(abs(p - 0.5) for p in self.probabilities.values()) \
            / len(self.probabilities)

    @property
    def mean_confidence_points(self) -> float:
        return 2.0 * self.mean_certainty

    def brier(self, questions) -> float:
        y = {q.question_id: bool(q.answer_binary) for q in questions}
        ps = self.probabilities
        if not ps:
            return float("nan")
        return sum((ps[qid] - (1.0 if y[qid] else 0.0)) ** 2
                   for qid in ps) / len(ps)


@contextlib.contextmanager
def _patch(obj, attr, value):
    original = getattr(obj, attr)
    setattr(obj, attr, value)
    try:
        yield
    finally:
        setattr(obj, attr, original)


@contextlib.contextmanager
def disable(arm: str):
    """Scope ONE mechanism's disable-patch. Restores on exit, always."""
    if arm == "provenance":
        table = engine_mod.MAX_CONFIDENCE_BY_SOURCE
        saved = dict(table)
        for k in table:
            table[k] = 1.0
        try:
            yield
        finally:
            table.clear()
            table.update(saved)
    elif arm == "requirements":
        with _patch(agp_rp.EvidenceRequirement, "unmet_reasons",
                    lambda self, *a, **k: []):
            yield
    elif arm == "inheritance":
        from tools.research_program import tier_ceiling_from_score

        def _identity(raw_score, descendant_resolutions):
            raw = max(0.0, min(1.0, float(raw_score)))
            return round(raw, 2), tier_ceiling_from_score(raw)
        with _patch(engine_mod, "clamp_parent_confidence", _identity):
            yield
    elif arm == "adversary":
        yield  # handled by router injection, not patching
    elif arm == "floor":
        with _patch(engine_mod, "DB_CONFIDENCE_FLOOR", 0.0):
            yield
    else:
        raise ValueError(f"unknown arm: {arm}")


async def run_arm(questions, *, model_factory, disabled: Iterable[str] = (),
                  transport=None, store=None) -> ArmResult:
    """Run all questions with the given mechanisms disabled.

    model_factory(q) -> PipelineModel; a fresh scripted/live model per
    question keeps runs independent and deterministic."""
    disabled = frozenset(disabled)
    unknown = disabled - set(DISABLABLE)
    if unknown:
        raise ValueError(f"unknown arms: {sorted(unknown)}")
    routes = transport if transport is not None else signature_routes()
    result = ArmResult(arm="+".join(sorted(disabled)) or "baseline",
                       disabled=disabled)
    patchable = sorted(disabled & {"provenance", "requirements",
                                   "inheritance", "floor"})
    for q in questions:
        adversary = (_AdversaryRouterStub([])
                     if "adversary" in disabled
                     else signature_adversary_router())
        with contextlib.ExitStack() as stack:
            for arm in patchable:
                stack.enter_context(disable(arm))
            run = await instrumented_run(
                question_text=q.text, model=model_factory(q),
                domain=_domain_of(q), today=q.claim_date,
                adversary_router=adversary, transport=routes, store=store)
        result.runs.append(run)
        result.sealed[q.question_id] = run.result.sealed
        result.probabilities[q.question_id] = round(
            run.parent_trace.probability, 6)
    return result


def _domain_of(q):
    from agp import Domain
    try:
        return Domain[q.domain]
    except KeyError:
        return Domain.GENERAL


def _domain_of(q):
    from agp import Domain
    try:
        return Domain[q.domain]
    except KeyError:
        return Domain.GENERAL
