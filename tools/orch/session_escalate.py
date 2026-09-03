"""AGP session Claude enhancement (extracted from tools.orch.session_steps).

``step_escalate_to_claude`` is the synthesis enhancement pass: always
attempted when Claude Code is available, skipped only when rate-limited
or unavailable. ``session_steps`` keeps a thin ``async def
step_escalate_to_claude`` delegate so ``run_session_flow`` and existing
patches stay on that module.

``step_synthesize``, ``step_manager_review``, ``run_session_flow``, and
``execute_tool`` stay in session_steps.

Do not pull in the autonomous loop module. Do not arm live betting.
Do not add live to paper-signal.
"""
import logging

from agp import Evidence, SessionSummary, SourceClass
from agp.thresholds import ESCALATION_THRESHOLD, MAX_CONFIDENCE_BY_SOURCE
from inference import _parse_json_response, escalate_with_ladder
from tools.claude_code import claude_code_available
from tools.orch.pipeline_support import _clamp_confidence

logger = logging.getLogger("callisto.orchestrator")


async def step_escalate_to_claude(orch, session, summary: SessionSummary):
    """Claude Code enhancement pass — ALWAYS attempted when available.

    Claude Code is the primary reasoning engine. This step enhances or
    replaces the local synthesis with Claude's analysis. Only skipped
    when Claude is rate-limited or unavailable.

    Returns updated summary and whether enhancement occurred.
    """
    if not await claude_code_available():
        logger.info("Claude enhancement skipped: Claude Code CLI not available")
        return summary, False

    is_low_confidence = summary.confidence_score < ESCALATION_THRESHOLD
    logger.info(
        f"Session {session.session_id}: Claude Code enhancement pass "
        f"(current confidence={summary.confidence_score}, "
        f"low_conf={is_low_confidence})"
    )

    # Build concise context — keep under 2K tokens for fast processing
    evidence_summary = "\n".join(
        f"- [{e.source_class.value}] {e.content[:150]}"
        for e in session.evidence[:6]
    )
    context = (
        f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')}\n"
        f"Question: {session.scope}\n"
        f"Prior synthesis (conf={summary.confidence_score}):\n"
        f"{summary.conclusion[:500]}\n\n"
        f"Evidence ({len(session.evidence)} items):\n{evidence_summary}\n"
        f"Contradictions: {len(session.contradictions)}"
    )
    if is_low_confidence:
        prompt = (
            f"The prior synthesis has low confidence ({summary.confidence_score}). "
            f"Provide a superior, well-supported analysis that addresses the gaps. "
            f"Respond with JSON: {{\"conclusion\":\"...\",\"confidence_score\":0.0-1.0,"
            f"\"key_findings\":[\"...\"],\"gaps\":[\"...\"]}}"
        )
    else:
        prompt = (
            f"Review and enhance the prior synthesis (confidence={summary.confidence_score}). "
            f"Strengthen the analysis, identify any missed nuances, and provide your own "
            f"calibrated confidence. If the prior synthesis is solid, confirm it with your reasoning. "
            f"Respond with JSON: {{\"conclusion\":\"...\",\"confidence_score\":0.0-1.0,"
            f"\"key_findings\":[\"...\"],\"gaps\":[\"...\"]}}"
        )

    result = await escalate_with_ladder(
        prompt,
        system_context=context,
        task_type="deep_work",
        timeout=180,
    )

    if result.get("error"):
        logger.warning(f"Claude Code enhancement failed: {result['error']}")
        return summary, False

    content = result.get("content", "")
    if not content:
        return summary, False

    # Parse Claude's response
    parsed = _parse_json_response(content) if content else None

    # Citation grounding: a citation counts ONLY when it names a URL the
    # session actually fetched (ledger), never a bare "http://" literal.
    cited = orch._provenance.cites_verified_url(content) or \
        orch._provenance.cites_verified_url(parsed.get("conclusion", "") if parsed else "")

    if parsed and isinstance(parsed, dict):
        # Claude Code is reasoning/synthesis, not primary documents.
        # Default tier: INFERRED (ceiling 0.55). Only upgrade to SECONDARY
        # (ceiling 0.75) when the response cites URLs that the session
        # actually fetched — a response without verified citations is
        # pure reasoning, no matter how many URLs it prints.
        conclusion_text = parsed.get("conclusion", content[:500])
        tier = SourceClass.SECONDARY if cited else SourceClass.INFERRED
        source_name = (
            f"Claude Code ({result['model']})"
            + (" [cited]" if cited else " [uncited]")
        )
        claude_evidence = Evidence(
            content=conclusion_text,
            source_class=tier,
            confidence_score=_clamp_confidence(
                float(parsed.get("confidence_score", 0.85)), tier.value
            ),
            domain=session.domain,
            origin_agent="claude_code",
            source_name=source_name,
        )
        session.add_evidence(claude_evidence)

        # Update summary with Claude's analysis — clamped to the tier
        # the response actually earned (cited → SECONDARY, else INFERRED)
        summary.conclusion = parsed.get("conclusion", summary.conclusion)
        new_confidence = _clamp_confidence(
            float(parsed.get("confidence_score", 0.85)), tier.value
        )
        summary.confidence_score = new_confidence
        summary.evidence_count = len(session.evidence)
        logger.info(
            f"Claude Code enhancement ({tier.value}, cited={cited}) "
            f"→ confidence={new_confidence}"
        )
    else:
        # Couldn't parse JSON — use raw text, tier by VERIFIED citations.
        # CRITICAL fix (instance4 C1): the old path granted the FULL
        # ceiling (0.75) outright whenever "http://" appeared anywhere in
        # an unparseable response. Now: unverified citations buy nothing —
        # INFERRED tier, confidence clamped to the INFERRED ceiling.
        cited = orch._provenance.cites_verified_url(content)
        tier = SourceClass.SECONDARY if cited else SourceClass.INFERRED
        confidence = _clamp_confidence(
            MAX_CONFIDENCE_BY_SOURCE[tier.value], tier.value
        )
        claude_evidence = Evidence(
            content=content[:500],
            source_class=tier,
            confidence_score=confidence,
            domain=session.domain,
            origin_agent="claude_code",
            source_name=(
                f"Claude Code ({result['model']})"
                + (" [cited]" if cited else " [uncited]")
            ),
        )
        session.add_evidence(claude_evidence)
        summary.conclusion = content[:1000]
        summary.confidence_score = confidence
        summary.evidence_count = len(session.evidence)
        logger.info(
            f"Claude Code enhancement used raw text ({tier.value}, "
            f"cited={cited}, conf={confidence})"
        )

    return summary, True
