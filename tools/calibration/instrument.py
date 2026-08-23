"""Instrument one pipeline run end to end and attribute every point.

The engine's chain (tools/pipeline/engine.py, run()) is, in order:

  1. model proposes `proposed_confidence` per leaf        (the ESTIMATE)
  2. provenance ceiling   min(E, MAX_CONFIDENCE_BY_SOURCE[best_class])
  3. requirement gate     min(x, 0.54) when evidence requirements unmet
  4. inheritance clamp    min(best_leaf, inherited_ceiling(descendants))
  5. adversary penalties  x - sum(severity penalties) | BLOCKING vetoes
  6. floor                below DB_CONFIDENCE_FLOOR the run refuses
  7. BRIDGE (tools/pipeline/retro.py) maps the sealed score to a
     probability: p = 0.5 +/- conf/2, sign from a keyword scan of the
     conclusion text (_leans_yes).

Three further mechanisms are DECLARED in agp/ but are not wired into this
path: SELF_REVIEW_CEILING (agp/ensemble.py applies it only through
PanelVerdict, which engine.run never builds), ensemble_spread
(clamp_with_ensemble needs score_evaluations, never supplied here), and
synthesis agreement (confidence_from_agreement exists but run() takes the
best leaf instead). They are recorded as present-but-inert so the table
cannot silently credit or excuse them.

Every number here is measured against what the code actually did: the
replay must reproduce the observed final score EXACTLY or the trace says
so (`verified=False`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from tools.pipeline.engine import (
    MAX_CONFIDENCE_BY_SOURCE,
    ResearchPipeline,
)
from tools.pipeline.model import PipelineModel, parse_model_json
from tools.research_program import clamp_parent_confidence
import tools.pipeline.retro as retro_bridge
from agp.adversary import AdversaryObjection
from agp.thresholds import DB_CONFIDENCE_FLOOR

#: mechanisms in the order they act on a confidence score
MECHANISMS = [
    "model_estimate",
    "provenance_ceiling",
    "requirement_gate",
    "inheritance_clamp",
    "adversary_penalty",
    "self_review_ceiling",     # declared (agp/ensemble.py) — NOT wired into engine.run
    "ensemble_spread",         # declared (agp/adversary.py) — no evaluations supplied
    "synthesis_agreement",     # built (tools/pipeline/synthesis.py) — not consumed by run()
    "floor_refusal",
    "bridge_half_scale",       # retro.py: p = 0.5 +/- conf/2
    "bridge_sign_keyword",     # retro.py: sign from _leans_yes keyword scan
]

_ABSENT = {
    "self_review_ceiling": "agp/ensemble.py PanelVerdict.apply is the only "
                           "applier; engine.run calls Adversary.apply_verdict "
                           "directly, so the cap never binds in this path",
    "ensemble_spread": "clamp_with_ensemble requires score_evaluations; "
                       "engine.run supplies none",
    "synthesis_agreement": "synthesize()/confidence_from_agreement exist but "
                           "run() derives parent confidence from the best leaf",
}


def _r2(x: float) -> float:
    return round(max(0.0, float(x)), 2)


@dataclass
class AttributionStep:
    """One adjustment: mechanism, score before, score after, points removed."""
    mechanism: str
    before: Optional[float]
    after: Optional[float]
    detail: str

    @property
    def removed(self) -> float:
        if self.before is None or self.after is None:
            return 0.0
        return round(self.before - self.after, 6)

    def to_dict(self) -> dict:
        return {"mechanism": self.mechanism,
                "before": self.before, "after": self.after,
                "removed": self.removed, "detail": self.detail}


@dataclass
class ConfidenceTrace:
    """Ordered attribution for one claim (a leaf, or the parent conclusion)."""
    subject: str
    raw_estimate: Optional[float]
    steps: list[AttributionStep] = field(default_factory=list)
    observed_final: Optional[float] = None
    replayed_final: Optional[float] = None
    probability: Optional[float] = None          # after the retro bridge
    sign_source: str = ""                        # which side / why
    verified: bool = True

    @property
    def total_removed(self) -> float:
        return round(sum(s.removed for s in self.steps), 6)

    def to_dict(self) -> dict:
        return {"subject": self.subject,
                "raw_estimate": self.raw_estimate,
                "steps": [s.to_dict() for s in self.steps],
                "total_removed_pipeline": round(
                    sum(s.removed for s in self.steps
                        if not s.mechanism.startswith("bridge")), 6),
                "observed_final": self.observed_final,
                "replayed_final": self.replayed_final,
                "probability": self.probability,
                "sign_source": self.sign_source,
                "verified": self.verified}

    def summary_lines(self) -> list[str]:
        lines = [f"{self.subject}: estimate {self.raw_estimate}"]
        for s in self.steps:
            if s.removed > 0:
                lines.append(f"  -{s.removed:>5.2f}  {s.mechanism}"
                             f" ({s.before} -> {s.after}) {s.detail}")
            elif s.mechanism in _ABSENT:
                lines.append(f"   0.00   {s.mechanism} [INERT] {s.detail}")
            else:
                lines.append(f"   0.00   {s.mechanism} {s.detail}")
        ok = "" if self.verified else "  *** REPLAY MISMATCH ***"
        lines.append(f"  = {self.replayed_final} (observed "
                     f"{self.observed_final}){ok}")
        if self.probability is not None:
            lines.append(f"  bridge -> P(True) = {self.probability} "
                         f"[{self.sign_source}]")
        return lines


class ModelSpy(PipelineModel):
    """Wraps the real model seam and records every proposal it makes.

    Production passes RouterModel(...); tests pass ScriptedModel. Either
    way the spy sees the parsed JSON the engine acts on — including the
    RAW `proposed_confidence` before any clamp touches it."""

    def __init__(self, inner: PipelineModel):
        self.inner = inner
        self.calls: list[dict] = []

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"spy({getattr(self.inner, 'name', 'model')})"

    async def complete(self, role: str, messages: list[dict],
                       **kw) -> dict:
        resp = await self.inner.complete(role, messages, **kw)
        prompt = "\n".join(m.get("content", "") for m in messages)
        self.calls.append({
            "role": role,
            "prompt_head": prompt[:400],
            "parsed": parse_model_json(resp),
            "raw_response": resp,
        })
        return resp

    def proposals_for(self, question_text: str) -> list[dict]:
        """Manager proposals whose prompt asked about this question text."""
        hits = []
        for c in self.calls:
            if c["role"] != "Manager":
                continue
            head = c["prompt_head"].replace("\n", " ")
            if question_text[:80] in head:
                if c["parsed"] is not None:
                    hits.append(c["parsed"])
        return hits


NEGATION_WORDS = ("no evidence", "does not", "not supported", "unlikely",
                  "falsified", "refused")


def sign_of_prediction(conclusion: str) -> tuple[int, str]:
    """Mirror tools/pipeline/retro._leans_yes and NAME the token that fired.

    The side itself is read from the production function so this module
    cannot drift from what actually scored; the keyword scan only explains
    WHICH word put the prediction on that side."""
    leans = retro_bridge.PipelineResearcher._leans_yes(conclusion or "")
    t = (conclusion or "").lower()
    fired = next((n for n in NEGATION_WORDS if n in t), None)
    if not leans:
        why = (f"leans NO: keyword '{fired}' found in conclusion text"
               if fired else "leans NO (heuristic returned False)")
        return -1, why
    return +1, ("leans YES: no negation keyword in conclusion text"
                if fired is None else
                f"leans YES despite keyword '{fired}'?? heuristic drifted")


def replay_leaf_chain(*, raw_estimate: float, best_class: str,
                      requirement_reasons: Iterable[str]) -> ConfidenceTrace:
    """Replay steps 2-3 exactly as engine._answer_leaf performs them."""
    tr = ConfidenceTrace(subject="leaf", raw_estimate=float(raw_estimate))
    cur = float(raw_estimate)
    tr.steps.append(AttributionStep(
        "model_estimate", None, _r2(cur),
        "last Manager proposal for this leaf (pre-clamp)"))
    ceiling = MAX_CONFIDENCE_BY_SOURCE.get(best_class, 0.55)
    after = min(cur, ceiling)
    tr.steps.append(AttributionStep(
        "provenance_ceiling", cur, after,
        f"engine.py min(proposed, MAX_CONFIDENCE_BY_SOURCE['{best_class}']"
        f"={ceiling})"))
    cur = after
    reasons = list(requirement_reasons)
    if reasons:
        after = min(cur, 0.54)
        tr.steps.append(AttributionStep(
            "requirement_gate", cur, after,
            f"engine.py min(clamped, 0.54); unmet: {'; '.join(reasons)}"))
        cur = after
    else:
        tr.steps.append(AttributionStep(
            "requirement_gate", cur, cur, "requirements met — no cap"))
    tr.replayed_final = _r2(cur)
    return tr


def replay_parent_chain(*, best_leaf_confidence: float,
                        descendant_resolutions: Optional[list],
                        objections: Iterable[Any]) -> tuple[
                            ConfidenceTrace, Optional[str]]:
    """Replay steps 4-6 exactly as engine.run performs them.

    Returns (trace, veto_reason). veto_reason set when a BLOCKING objection
    or the floor refuses the seal."""
    tr = ConfidenceTrace(subject="parent",
                         raw_estimate=float(best_leaf_confidence))
    cur = float(best_leaf_confidence)
    tr.steps.append(AttributionStep(
        "best_leaf", None, _r2(cur),
        "run(): proposed = max(answered leaf confidence)"))
    clamped, tier = clamp_parent_confidence(cur, descendant_resolutions or [])
    ceil_ = inherited_ceiling_for_display(descendant_resolutions or [])
    tr.steps.append(AttributionStep(
        "inheritance_clamp", cur, clamped,
        f"tools/research_program.clamp_parent_confidence -> "
        f"inherited_ceiling={ceil_} (tier {tier}); "
        f"n_descendants={len(descendant_resolutions or [])}"))
    cur = clamped
    objs = list(objections)
    blocking = next((o for o in objs
                     if getattr(o, "severity", "").upper() == "BLOCKING"),
                    None)
    veto = None
    if blocking is not None:
        veto = f"adversary BLOCKING: {getattr(blocking, 'text', '')[:120]}"
        tr.steps.append(AttributionStep(
            "adversary_penalty", cur, cur,
            "BLOCKING objection — seal refused outright (score moot)"))
    else:
        penalty = sum(o.penalty for o in objs)
        after = _r2(cur - penalty) if objs else _r2(cur)
        sev = ", ".join(f"{o.severity}" for o in objs) or "none"
        tr.steps.append(AttributionStep(
            "adversary_penalty", cur, after,
            f"Adversary.apply_verdict: {len(objs)} objection(s) [{sev}] "
            f"-> -{penalty:.2f}"))
        cur = after
    for mech, why in _ABSENT.items():
        tr.steps.append(AttributionStep(mech, cur, cur, why))
    if veto is None and cur < DB_CONFIDENCE_FLOOR:
        veto = (f"confidence {cur} below DB floor {DB_CONFIDENCE_FLOOR} "
                f"— refused, scored as p=0.50")
        tr.steps.append(AttributionStep(
            "floor_refusal", cur, 0.0,
            f"< DB_CONFIDENCE_FLOOR({DB_CONFIDENCE_FLOOR}) -> unsealed -> "
            f"retro.py treats conf as 0.0"))
        cur = 0.0
    else:
        tr.steps.append(AttributionStep(
            "floor_refusal", cur, cur,
            "at/above DB floor — sealed" if veto is None else
            "(moot: blocked earlier)"))
    tr.replayed_final = _r2(cur)
    return tr, veto


def inherited_ceiling_for_display(resolutions: list) -> float:
    from tools.research_program import inherited_ceiling, normalize_records
    return inherited_ceiling(normalize_records(resolutions))


async def instrumented_run(*, question_text: str, model: PipelineModel,
                           domain, today,
                           adversary_router=None, transport=None,
                           descendant_resolutions: Optional[list] = None,
                           store=None) -> "InstrumentedRun":
    """Run the REAL pipeline once with instrumentation and full replay."""
    spy = ModelSpy(model)
    pipeline = ResearchPipeline(
        model=spy, adversary_router=adversary_router,
        transport=transport, store=store,
        descendant_resolutions=list(descendant_resolutions or []))
    result = await pipeline.run(question_text, domain=domain, today=today)

    run = InstrumentedRun(result=result, spy=spy)
    for leaf in result.leaves:
        proposals = spy.proposals_for(leaf.text)
        raw = float((proposals[-1].get("proposed_confidence") or 0.0)
                    ) if proposals else None
        classes = leaf.source_classes or ["INFERRED"]
        rank = {"INFERRED": 0, "SIGNAL": 1, "SECONDARY": 2, "PRIMARY": 3}
        best_class = max(classes, key=lambda c: rank.get(c, 0))
        lt = replay_leaf_chain(raw_estimate=raw or 0.0,
                               best_class=best_class,
                               requirement_reasons=leaf.requirement_reasons)
        lt.subject = f"leaf[{leaf.question_id[:8]}] {leaf.text[:60]}"
        lt.observed_final = leaf.confidence
        lt.verified = (lt.replayed_final == leaf.confidence)
        run.leaf_traces.append(lt)

    answered = [l for l in result.leaves if l.answer]
    best_leaf = max(answered, key=lambda l: l.confidence).confidence \
        if answered else 0.0
    pt, veto = replay_parent_chain(
        best_leaf_confidence=best_leaf,
        descendant_resolutions=descendant_resolutions,
        objections=result.objections)
    pt.observed_final = result.confidence_score if result.sealed else 0.0
    pt.verified = (pt.replayed_final == pt.observed_final)
    # Bridge: exactly what tools/pipeline/retro.PipelineResearcher does.
    conf = result.confidence_score if result.sealed else 0.0
    side, why = sign_of_prediction(result.conclusion)
    pt.probability = 0.5 + side * conf / 2.0
    pt.sign_source = why + ("" if result.sealed
                            else " | UNSEALED: conf forced to 0 -> p=0.50")
    pt.steps.append(AttributionStep(
        "bridge_half_scale", conf, abs(pt.probability - 0.5) * 2,
        "retro.py p = 0.5 +/- conf/2 — certainty HALVED at the bridge"))
    pt.steps.append(AttributionStep(
        "bridge_sign_keyword", None, None, why))
    run.parent_trace = pt
    run.veto_reason = veto
    return run


@dataclass
class InstrumentedRun:
    result: Any
    spy: ModelSpy
    leaf_traces: list[ConfidenceTrace] = field(default_factory=list)
    parent_trace: Optional[ConfidenceTrace] = None
    veto_reason: Optional[str] = None

    @property
    def verified(self) -> bool:
        return all(t.verified for t in self.leaf_traces) and \
            (self.parent_trace.verified if self.parent_trace else True)

    def attribution_table(self) -> dict:
        """Points removed per mechanism across this run's traces."""
        table: dict[str, float] = {m: 0.0 for m in MECHANISMS}
        for t in ([*self.leaf_traces, self.parent_trace]
                  if self.parent_trace else self.leaf_traces):
            for s in t.steps:
                table[s.mechanism] = round(
                    table[s.mechanism] + s.removed, 6)
        return table

    def to_dict(self) -> dict:
        return {
            "question": self.result.root_query,
            "sealed": self.result.sealed,
            "refusal_reason": self.result.refusal_reason,
            "veto_reason": self.veto_reason,
            "final_confidence": self.result.confidence_score,
            "probability": (self.parent_trace.probability
                            if self.parent_trace else None),
            "verified_replay": self.verified,
            "leaves": [t.to_dict() for t in self.leaf_traces],
            "parent": self.parent_trace.to_dict()
            if self.parent_trace else None,
            "mechanism_points_removed": self.attribution_table(),
        }
