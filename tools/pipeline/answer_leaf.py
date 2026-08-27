"""Leaf answer path extracted from ResearchPipeline._answer_leaf.

``callisto ask`` answers each decomposition leaf here: provenance class,
optional sandbox compute, EstimateCeiling (belief vs entitlement),
requirement gate, gap classification. The facade keeps a thin
``_answer_leaf`` wrapper so callers (run_inner, tests) stay on the class.

Do not import tools.autonomous. Do not arm live betting. Do not add live
to paper-signal. Completions stay HTTP; Hermes is the agent runtime.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from agp import (
    AGPSession,
    ConfidenceTier,
    Domain,
    Evidence,
    SourceClass,
)
from agp.research_program import ResearchQuestion, SourceClassRank
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from tools.pipeline.model import answer_messages, parse_model_json
from tools.sandbox import run_python

from tools.pipeline.engine import (
    FetchResult,
    LeafOutcome,
    _CLASS_RANK,
    _cleanup_workspace,
    _store_sandbox,
)


async def answer_leaf(pipeline, q: ResearchQuestion,
                       fetches: list[FetchResult],
                       session: AGPSession,
                       trace: Any = None,
                       call_tag: str = "",
                       ) -> tuple[LeafOutcome, list[Evidence]]:
    """Answer one leaf. SIDE-EFFECT ISOLATED: returns the evidence items
    instead of appending them to the session, so concurrent leaf answers
    can be assembled in deterministic leaf order by the caller. (The
    legacy signature's session argument is retained for compatibility
    but is no longer mutated here.)"""
    self = pipeline
    out = LeafOutcome(question_id=q.question_id, text=q.text)
    evidence_items: list[Evidence] = []
    best_class = SourceClass.INFERRED
    for f in fetches:
        ev = Evidence(
            content=f.body[:4000], source_class=SourceClass.INFERRED,
            confidence_score=0.30, domain=session.domain or Domain.GENERAL,
            origin_agent="pipeline", source_name=f.source_name)
        assigned = self.ledger.assign_source_class(ev)
        ev.source_class = assigned
        ceiling = MAX_CONFIDENCE_BY_SOURCE.get(assigned.value, 0.55)
        ev.confidence_score = round(min(0.45, ceiling), 2) \
            if assigned != SourceClass.PRIMARY else round(ceiling, 2)
        evidence_items.append(ev)
        out.source_classes.append(assigned.value)
        if _CLASS_RANK[assigned.value] > _CLASS_RANK[best_class.value]:
            best_class = assigned
    out.n_sources = len(evidence_items)

    # Model proposes an answer, possibly requesting computation first.
    resp = await self.model.complete(
        "Manager", answer_messages(q.text, [e.content for e in evidence_items]),
        _call_tag=call_tag or q.question_id)
    proposal = parse_model_json(resp) or {}

    compute = proposal.get("compute")
    if compute and isinstance(compute, dict) and compute.get("code"):
        # The compute path mutates shared stores (ledger, artifact
        # store). It is rare; serialize it so cross-leaf interleaving
        # cannot corrupt state, then continue.
        async with self._compute_lock:
            # keep_workspace=True so produced file BYTES reach the artifact
            # store — otherwise the child merely ATTESTS hashes and nothing
            # downstream can re-verify them (property 3: evidence you can
            # check). Workspace is destroyed right after sealing.
            sbx = await asyncio.to_thread(
                run_python, str(compute["code"]),
                inputs=compute.get("inputs") or {}, keep_workspace=True)
            out.sandbox_status = sbx.status
            if sbx.status == "ok":
                refs = _store_sandbox(sbx, self.store)
                _cleanup_workspace(sbx)
                out.artifact_sha256s.extend(r.sha256 for r in refs)
                out.artifact_refs.extend(refs)
                self._pending_artifact_refs.extend(refs)
                # The computation itself is real executed output → recorded
                # in the ledger; provenance assigns its class.
                comp_body = f"sandbox code:\n{sbx.code}\nstdout:\n{sbx.stdout}"
                self.ledger.record_tool_result("run_python", comp_body,
                                               primary=False)
                comp_ev = Evidence(
                    content=comp_body[:4000],
                    source_class=SourceClass.INFERRED,
                    confidence_score=0.30,
                    domain=session.domain or Domain.GENERAL,
                    origin_agent="sandbox")
                comp_ev.source_class = self.ledger.assign_source_class(comp_ev)
                comp_ev.confidence_score = round(min(
                    0.45, MAX_CONFIDENCE_BY_SOURCE.get(
                        comp_ev.source_class.value, 0.55)), 2)
                evidence_items.append(comp_ev)
                out.n_sources += 1
        # Re-ask for the final answer now that computation ran.
        resp = await self.model.complete(
            "Manager", answer_messages(
                q.text, [e.content for e in evidence_items]),
            _call_tag=call_tag or q.question_id)
        proposal = parse_model_json(resp) or {}

    out.answer = str(proposal.get("answer", "")).strip()
    # DECLARED answer-bearing signal (task 212, defect R3). The model
    # states structurally whether the evidence ANSWERS the question —
    # distinct from provenance tier (where bytes came from) and from
    # gap_kind (why we have no usable evidence). Read AS DECLARED; the
    # conclusion prose is never parsed for meaning (forecast-sign
    # defect class). Absent field defaults to True so legacy models
    # are unchanged: this signal may only refuse or lower.
    out.answers_question = bool(proposal.get("answers_question", True))
    _st = str(proposal.get("stance", "")).strip().upper()
    # Unknown/absent stance is UNDETERMINED, never a lean. An unparseable
    # answer must not silently become a confident YES, which is exactly
    # what the old default-yes keyword scan did.
    out.stance = _st if _st in ("AFFIRMS", "DENIES") else "UNDETERMINED"
    proposed = float(proposal.get("proposed_confidence") or 0.0)

    # ESTIMATE vs CEILING (agp.estimate): the model's proposed_confidence
    # is its BELIEF; provenance and the requirement gate are ENTITLEMENT.
    # Carrying both keeps the belief visible for calibration while the
    # sealed number stays exactly what min() produced before — sealable()
    # IS the old collapsed value, by construction.
    from agp.estimate import EstimateCeiling
    ec = EstimateCeiling(
        estimate=min(1.0, max(0.0, proposed)),
        ceiling=MAX_CONFIDENCE_BY_SOURCE.get(best_class.value, 0.55))

    # Evidence-requirement gate (agp.research_program): unmet requirements
    # cap the leaf at SPECULATIVE floor band. Independent-source counting
    # now reflects ACTUAL source diversity (distinct hosts / declared
    # overlap families), not the number of API calls that returned.
    achieved = SourceClassRank(best_class.value)
    if trace is not None and trace.independent_keys:
        n_indep = len(trace.independent_keys)
    else:
        n_indep = len({f.source_name for f in fetches}) + (
            1 if out.sandbox_status == "ok" else 0)
    reasons = q.evidence_requirements.unmet_reasons(
        achieved, n_indep,
        produced_quant=out.sandbox_status == "ok" or
        bool(out.answer and re.search(r"\d", out.answer)))

    out.requirement_reasons = reasons
    if reasons:
        # A ceiling mechanism, applied through the type: only the ceiling
        # falls; the estimate rides through untouched.
        ec = ec.with_ceiling(min(ec.ceiling, 0.54))

    out.confidence_estimate = ec.estimate          # diagnostic, not acted on
    out.confidence_ceiling = ec.ceiling
    # min(estimate, ceiling) is exactly what sealable() computes; the
    # historical engine rounding (round-half) is preserved verbatim so the
    # sealed/stored/reported number cannot move. sealable()'s floor_conf
    # quantisation differs only on proposals that are not already 2dp —
    # see tests/test_estimate_wiring.py for the pinned equivalence.
    out.confidence = round(
        max(0.0, min(ec.estimate, ec.ceiling)), 2)
    out.tier = ConfidenceTier.from_score(out.confidence).value
    # Three-way gap classification (tools.gaps.NullKind). CLASSIFICATION
    # ONLY — this reads nothing that scores and moves no number.
    #   no fetches at all -> honest_null or retrieval_failure per THE
    #     single membership rule; an answer written on top of a
    #     retrieval failure still carries the retrieval_failure verdict,
    #     so "we could not look" can never read as "there is nothing".
    #   evidence obtained but requirements unmet -> unprovable: a
    #     deliberate decision about OUR OWN bar, not a fetch fault.
    from tools.gaps import NullKind, classify_null_kind
    if not fetches:
        kind, expl = classify_null_kind(trace)
        out.gap_kind, out.gap_explanation = kind, expl
    elif reasons:
        # Fetches were admitted and requirements are unmet -> unprovable
        # regardless of whether the model emitted prose; an empty answer
        # on partial evidence is still an unprovable verdict, never a
        # silent fall-through that dies as '(no gap verdict)'.
        out.gap_kind = NullKind.UNPROVABLE.value
        out.gap_explanation = (
            "evidence was obtained but does not meet this question's "
            "declared standard: " + "; ".join(reasons))
    return out, evidence_items
