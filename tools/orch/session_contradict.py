"""AGP contradiction-check step extracted from session_steps.

``step_check_contradictions`` stays defined in ``tools.orch.session_steps``
as a thin delegate so Orchestrator facades and ``session_flow`` imports
keep working. The body lives here.

``execute_tool`` stays in session_steps.

Do not import the autonomous facade. Do not arm live betting.
Do not add live to paper-signal.
"""
from __future__ import annotations

import logging
from typing import Optional

from agp import Contradiction
from inference import _parse_json_response
from tools.claude_code import (
    claude_code_query,
    is_available as claude_available,
)
from tools.orch.pipeline_support import (
    _json_compact,
    _safe_parse,
)

logger = logging.getLogger("callisto.orchestrator")


async def step_check_contradictions(orch, session, arch_prompt: str) -> list[Contradiction]:
    """Architect actively searches for contradictions.

    Claude Code is the PRIMARY reasoning engine (mirrors synthesis).
    This step's entire purpose is rigor — using the weakest model for it
    was the original silent-failure pattern.
    """
    if not session.evidence:
        return []

    evidence_compact = _json_compact([e.to_dict() for e in session.evidence])

    def _parse_contradictions(parsed: Optional[dict]) -> list[Contradiction]:
        found: list[Contradiction] = []
        if parsed and "contradictions" in parsed:
            for item in parsed["contradictions"]:
                try:
                    found.append(Contradiction(
                        claim_a=item.get("claim_a", ""),
                        claim_b=item.get("claim_b", ""),
                        source_a=item.get("source_a", ""),
                        source_b=item.get("source_b", ""),
                        severity=item.get("severity", "MINOR"),
                        resolution=item.get("resolution", ""),
                    ))
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed contradiction: {e}")
        return found

    # ── Claude Code PRIMARY path ──
    if claude_available():
        logger.info(
            f"Session {session.session_id}: step 5 contradictions using Claude Code (primary)"
        )
        claude_prompt = (
            f"Audit the following evidence for contradictions.\n"
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n\n"
            f"Find pairwise contradictions. Each contradiction must name the exact "
            f"claims and sources. Rate severity honestly:\n"
            f"  CRITICAL = outcome-changing, conclusion cannot hold\n"
            f"  MAJOR    = meaningfully weakens conclusion\n"
            f"  MINOR    = definitional / scope / phrasing disagreement\n"
            f"Absence of contradictions in conflicting-looking evidence is itself a flag.\n"
            f'Respond with JSON: {{"contradictions":[{{"claim_a":"...","claim_b":"...",'
            f'"source_a":"...","source_b":"...","severity":"CRITICAL|MAJOR|MINOR",'
            f'"resolution":"..."}}],"notes":"..."}}'
        )
        claude_context = (
            "You are the contradiction-check agent in an AGP (Agentic Governance "
            "Protocol) session. Your output directly penalizes session confidence — "
            "CRITICAL = -0.15, MAJOR = -0.05. Be accurate: overcalling severity "
            "wastes confidence, undercalling hides real conflicts."
        )
        try:
            result = await claude_code_query(
                claude_prompt, system_context=claude_context, timeout=120
            )
            if not result.get("error") and not result.get("rate_limited"):
                content = result.get("content", "")
                parsed = _parse_json_response(content) if content else None
                contradictions = _parse_contradictions(parsed)
                logger.info(
                    f"Session {session.session_id}: Claude Code found "
                    f"{len(contradictions)} contradictions"
                )
                return contradictions
            logger.info(
                f"Session {session.session_id}: Claude Code unavailable for "
                f"contradictions (error={result.get('error')}), falling back to local"
            )
        except Exception as e:
            logger.warning(
                f"Session {session.session_id}: Claude Code contradiction check "
                f"raised {type(e).__name__}: {e}; falling back to local"
            )

    # ── Local model FALLBACK path ──
    logger.info(
        f"Session {session.session_id}: step 5 contradictions using local model (fallback)"
    )
    messages = [
        {"role": "system", "content": arch_prompt},
        {"role": "user", "content": (
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"Evidence:\n{evidence_compact}\n\n"
            f"Find contradictions. Absence is a flag.\n"
            f'JSON: {{"contradictions":[{{"claim_a":"...","claim_b":"...","source_a":"...",'
            f'"source_b":"...","severity":"MINOR|MAJOR|CRITICAL","resolution":"..."}}],'
            f'"notes":"..."}}'
        )},
    ]
    response = await orch.architect.achat(messages, options={"num_predict": 512})
    parsed = _safe_parse(response)
    return _parse_contradictions(parsed)
