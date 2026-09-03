"""AGP synthesis and manager-review steps extracted from session_steps.

``step_synthesize`` and ``step_manager_review`` stay defined in
``tools.orch.session_steps`` as thin delegates so Orchestrator facades
and ``session_flow`` imports keep working. Bodies live here.

``execute_tool`` stays in session_steps.

Do not import the autonomous facade. Do not arm live betting.
Do not add live to paper-signal.
"""
from __future__ import annotations

import logging

from agp import (
    EMPTY_SYNTHESIS_MARKER,
    SessionSummary,
)
from agp.thresholds import (
    CONTRADICTION_PENALTY,
    DB_CONFIDENCE_FLOOR,
)
from inference import (
    _parse_json_response,
    escalate_with_ladder,
)
from tools.claude_code import (
    is_available as claude_available,
)
from tools.orch.pipeline_support import (
    _best_source_class,
    _clamp_confidence,
    _json_compact,
    _safe_parse,
)

logger = logging.getLogger("callisto.orchestrator")


async def step_synthesize(orch, session, used_tools: bool, arch_prompt: str) -> SessionSummary:
    """Architect synthesizes evidence into a conclusion.

    Claude Code is the PRIMARY reasoning engine. Local models are the fallback
    when Claude is rate-limited or unavailable.
    """
    evidence_compact = _json_compact([e.to_dict() for e in session.evidence])

    tool_warning = ""
    if not used_tools:
        tool_warning = "\nNo real-time sources. All INFERRED. Max confidence=0.55.\n"

    # ── Claude Code PRIMARY path ──
    if claude_available():
        logger.info(f"Session {session.session_id}: step 6 synthesis using Claude Code (primary)")
        claude_prompt = (
            f"Synthesize the following evidence into a conclusion.\n"
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n"
            f"Contradictions: {len(session.contradictions)}\n"
            f"{tool_warning}"
            f'Respond with JSON: {{"conclusion":"...","confidence_score":0.0-1.0}}'
        )
        claude_context = (
            f"You are the synthesis agent in an AGP (Agentic Governance Protocol) session. "
            f"Synthesize all evidence into a coherent conclusion with calibrated confidence. "
            f"Confidence ceilings: INFERRED max=0.55, SECONDARY max=0.75, PRIMARY max=1.0. "
            f"Be honest about uncertainty — never inflate confidence beyond what evidence supports."
        )
        result = await escalate_with_ladder(
            claude_prompt,
            system_context=claude_context,
            task_type="reasoning",
            timeout=120,
        )

        if not result.get("error") and not result.get("rate_limited"):
            content = result.get("content", "")
            parsed = _parse_json_response(content) if content else None

            conclusion = EMPTY_SYNTHESIS_MARKER
            confidence = DB_CONFIDENCE_FLOOR
            if parsed and isinstance(parsed, dict):
                conclusion = parsed.get("conclusion", conclusion)
                confidence = float(parsed.get("confidence_score", confidence))
            elif content:
                # Claude responded but not in JSON — use raw text
                conclusion = content[:1000]
                confidence = 0.70

            best_sc = _best_source_class(session.evidence, used_tools)
            confidence = _clamp_confidence(confidence, best_sc)

            logger.info(f"Session {session.session_id}: Claude Code synthesis confidence={confidence}")
            return SessionSummary(
                scope=session.scope,
                domain=session.domain,
                conclusion=conclusion,
                confidence_score=confidence,
                evidence_count=len(session.evidence),
                contradiction_count=len(session.contradictions),
            )
        else:
            logger.info(
                f"Session {session.session_id}: Claude Code unavailable for synthesis "
                f"(error={result.get('error')}), falling back to local model"
            )

    # ── Local model FALLBACK path ──
    logger.info(f"Session {session.session_id}: step 6 synthesis using local model (fallback)")

    messages = [
        {"role": "system", "content": arch_prompt},
        {"role": "user", "content": (
            f"Synthesize.\n"
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n"
            f"Contradictions: {len(session.contradictions)}\n"
            f"{tool_warning}"
            f'JSON: {{"conclusion":"...","confidence_score":0.0-1.0}}'
        )},
    ]
    response = await orch.architect.achat(messages)
    parsed = _safe_parse(response)

    conclusion = EMPTY_SYNTHESIS_MARKER
    confidence = DB_CONFIDENCE_FLOOR
    if parsed:
        conclusion = parsed.get("conclusion", conclusion)
        confidence = float(parsed.get("confidence_score", confidence))

    best_sc = _best_source_class(session.evidence, used_tools)
    confidence = _clamp_confidence(confidence, best_sc)

    return SessionSummary(
        scope=session.scope,
        domain=session.domain,
        conclusion=conclusion,
        confidence_score=confidence,
        evidence_count=len(session.evidence),
        contradiction_count=len(session.contradictions),
    )

async def step_manager_review(
    orch, session, summary: SessionSummary, used_tools: bool
) -> SessionSummary:
    """Manager reviews synthesis. Enforces confidence discipline."""
    source_classes = set(e.source_class.value for e in session.evidence)
    best_sc = _best_source_class(session.evidence, used_tools)

    # Compact evidence for review
    evidence_compact = _json_compact([
        {"c": e.content[:200], "sc": e.source_class.value, "conf": e.confidence_score}
        for e in session.evidence
    ])

    messages = [
        {"role": "user", "content": (
            f"Review AGP synthesis.\n"
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"Conclusion: {summary.conclusion}\n"
            f"Confidence: {summary.confidence_score} | Sources: {source_classes} | Tools: {used_tools}\n"
            f"Evidence ({summary.evidence_count}):\n{evidence_compact}\n"
            f"Contradictions: {summary.contradiction_count}\n\n"
            f"Rules: adjust confidence DOWN only. "
            f"INFERRED max=0.55, SECONDARY max=0.75, only PRIMARY supports VERIFIED(0.90+).\n"
            f'JSON: {{"approved":true/false,"adjusted_confidence":null/float,'
            f'"objections":["..."],"reasoning":"..."}}'
        )},
    ]
    response = await orch.manager.achat(messages)
    parsed = _safe_parse(response)

    if parsed and isinstance(parsed, dict):
        adjusted = parsed.get("adjusted_confidence")
        if adjusted is not None:
            adjusted = float(adjusted)
            if adjusted < summary.confidence_score:
                summary.confidence_score = adjusted
                logger.info(f"Manager adjusted confidence to {adjusted}")

        objections = parsed.get("objections", [])
        if objections:
            summary.manager_objections = objections
            for obj in objections:
                session.add_manager_objection(obj)

    # Final hard enforcement — code, not policy
    summary.confidence_score = _clamp_confidence(summary.confidence_score, best_sc)

    # ── Contradiction penalty pass ──
    # Applied AFTER _clamp_confidence, BEFORE seal(). Previously
    # contradictions were passed to the LLM prompt as a count string
    # and had zero code-path effect on confidence. That made the
    # "contradiction-checked" claim cosmetic.
    pre_penalty = summary.confidence_score
    critical = sum(
        1 for c in session.contradictions if c.severity.upper() == "CRITICAL"
    )
    major = sum(
        1 for c in session.contradictions if c.severity.upper() == "MAJOR"
    )
    penalty = (
        critical * CONTRADICTION_PENALTY["CRITICAL"]
        + major * CONTRADICTION_PENALTY["MAJOR"]
    )
    if penalty > 0:
        penalized = max(DB_CONFIDENCE_FLOOR, round(pre_penalty - penalty, 2))
        logger.info(
            f"Session {session.session_id}: contradiction penalty "
            f"(CRITICAL={critical}, MAJOR={major}) → "
            f"confidence {pre_penalty} - {round(penalty, 2)} = {penalized}"
        )
        summary.confidence_score = penalized
    else:
        logger.info(
            f"Session {session.session_id}: no contradiction penalty "
            f"(CRITICAL=0, MAJOR=0, confidence={pre_penalty})"
        )

    return summary

