"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
"""

import json
import logging
from typing import Optional

from agp import (
    AGPSession,
    AGPViolation,
    Contradiction,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
    ConfidenceTier,
)
from inference import get_architect, get_manager, get_sentinel, execute_function_call
from memory import MemoryStore

logger = logging.getLogger("callisto.orchestrator")


def _safe_parse(response: dict, fallback: any = None):
    """Extract parsed JSON from inference response, with fallback."""
    parsed = response.get("parsed_json")
    if parsed is not None:
        return parsed
    return fallback


def _parse_domain(text: str) -> Domain:
    """Parse a domain from text, defaulting to GENERAL."""
    text_upper = text.upper().strip()
    for domain in Domain:
        if domain.value in text_upper:
            return domain
    return Domain.GENERAL


class Orchestrator:
    """Coordinates the 3-agent AGP session flow."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.architect = get_architect()
        self.manager = get_manager()
        self.sentinel = get_sentinel()

    async def run_session(self, query: str) -> dict:
        """Execute a full 7-step AGP session. Returns the sealed session dict."""
        session = AGPSession(query)
        logger.info(f"Session {session.session_id}: starting for query: {query}")

        try:
            # Step 1: Declare Scope
            session.scope = query
            logger.info(f"Session {session.session_id}: step 1 — scope declared")

            # Step 2: Assign Domain Tag (Sentinel)
            session.advance_to(SessionStep.ASSIGN_DOMAIN)
            domain = await self._step_assign_domain(session)
            session.domain = domain
            logger.info(f"Session {session.session_id}: step 2 — domain={domain.value}")

            # Step 3: Source Enumeration (Architect)
            session.advance_to(SessionStep.SOURCE_ENUMERATION)
            sources = await self._step_enumerate_sources(session)
            session.sources = sources
            logger.info(f"Session {session.session_id}: step 3 — {len(sources)} sources enumerated")

            # Step 4: Primary Collection (Architect + tools)
            session.advance_to(SessionStep.PRIMARY_COLLECTION)
            evidence_list = await self._step_collect_evidence(session)
            for ev in evidence_list:
                session.add_evidence(ev)
            logger.info(f"Session {session.session_id}: step 4 — {len(session.evidence)} storable evidence collected")

            # Step 5: Contradiction Check (Architect)
            session.advance_to(SessionStep.CONTRADICTION_CHECK)
            contradictions = await self._step_check_contradictions(session)
            for c in contradictions:
                session.add_contradiction(c)
            logger.info(f"Session {session.session_id}: step 5 — {len(contradictions)} contradictions found")

            # Step 6: Synthesis (Architect) + Manager Review
            session.advance_to(SessionStep.SYNTHESIS)
            summary = await self._step_synthesize(session)
            summary = await self._step_manager_review(session, summary)
            session.summary = summary
            logger.info(f"Session {session.session_id}: step 6 — synthesis complete, confidence={summary.confidence_score}")

            # Step 7: Session Close — seal and store
            session.advance_to(SessionStep.SESSION_CLOSE)
            seal_hash = session.seal()
            logger.info(f"Session {session.session_id}: step 7 — sealed with hash {seal_hash[:16]}...")

            # Persist to memory
            for ev in session.evidence:
                await self.memory.store_evidence(session.session_id, ev)
            await self.memory.store_session(session)

            return session.to_dict()

        except AGPViolation as e:
            logger.error(f"Session {session.session_id}: AGP violation: {e}")
            raise
        except Exception as e:
            logger.error(f"Session {session.session_id}: failed: {e}")
            raise

    async def _step_assign_domain(self, session: AGPSession) -> Domain:
        """Sentinel classifies the query into a domain."""
        messages = [
            {"role": "system", "content": self.sentinel.config.system_prompt},
            {"role": "user", "content": (
                f"Classify this query into exactly one domain. "
                f"Domains: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL.\n\n"
                f"Query: {session.query}\n\n"
                f"Respond with JSON: {{\"domain\": \"DOMAIN_NAME\", \"reasoning\": \"...\"}}"
            )},
        ]
        response = await self.sentinel.achat(messages)
        parsed = _safe_parse(response)
        if parsed and "domain" in parsed:
            return _parse_domain(parsed["domain"])
        return _parse_domain(response.get("content", "GENERAL"))

    async def _step_enumerate_sources(self, session: AGPSession) -> list[str]:
        """Architect lists sources to consult before gathering evidence."""
        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 3 — Source Enumeration.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"List the sources you will consult to answer this query. "
                f"Respond with JSON: {{\"sources\": [\"source1\", \"source2\", ...]}}"
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)
        if parsed and "sources" in parsed:
            return parsed["sources"]
        return [session.scope]

    async def _step_collect_evidence(self, session: AGPSession) -> list[Evidence]:
        """Architect gathers evidence, optionally using tools."""
        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 4 — Primary Collection.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n"
                f"Sources to consult: {json.dumps(session.sources)}\n\n"
                f"Gather evidence for this query. For each piece of evidence, provide:\n"
                f"- content: the finding\n"
                f"- source_class: PRIMARY, SECONDARY, SIGNAL, or INFERRED\n"
                f"- confidence_score: 0.0-1.0\n"
                f"- source_name: where this came from\n\n"
                f"Respond with JSON: {{\"evidence\": [...]}}"
            )},
        ]

        response = await self.architect.achat(messages)

        # Handle any tool calls
        for tc in response.get("tool_calls", []):
            result = execute_function_call(tc["name"], tc["arguments"])
            messages.append({"role": "assistant", "content": response["content"]})
            messages.append({
                "role": "tool",
                "content": json.dumps(result) if not isinstance(result, str) else result,
            })
            response = await self.architect.achat(messages)

        parsed = _safe_parse(response)
        evidence_list = []
        if parsed and "evidence" in parsed:
            for item in parsed["evidence"]:
                try:
                    ev = Evidence(
                        content=item.get("content", ""),
                        source_class=SourceClass(item.get("source_class", "INFERRED")),
                        confidence_score=float(item.get("confidence_score", 0.3)),
                        domain=session.domain,
                        origin_agent="architect",
                        source_name=item.get("source_name", ""),
                    )
                    evidence_list.append(ev)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed evidence: {e}")
        return evidence_list

    async def _step_check_contradictions(self, session: AGPSession) -> list[Contradiction]:
        """Architect actively searches for contradictions in collected evidence."""
        if not session.evidence:
            return []

        evidence_summary = json.dumps(
            [e.to_dict() for e in session.evidence], indent=2
        )
        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 5 — Contradiction Check.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"Evidence collected:\n{evidence_summary}\n\n"
                f"Actively search for contradictions between these evidence items. "
                f"Absence of contradictions is a flag, not a comfort.\n\n"
                f"Respond with JSON: {{\"contradictions\": ["
                f"{{\"claim_a\": \"...\", \"claim_b\": \"...\", \"source_a\": \"...\", "
                f"\"source_b\": \"...\", \"severity\": \"MINOR|MAJOR|CRITICAL\", "
                f"\"resolution\": \"...\"}}], \"notes\": \"...\"}}"
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)
        contradictions = []
        if parsed and "contradictions" in parsed:
            for item in parsed["contradictions"]:
                try:
                    contradictions.append(Contradiction(
                        claim_a=item.get("claim_a", ""),
                        claim_b=item.get("claim_b", ""),
                        source_a=item.get("source_a", ""),
                        source_b=item.get("source_b", ""),
                        severity=item.get("severity", "MINOR"),
                        resolution=item.get("resolution", ""),
                    ))
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed contradiction: {e}")
        return contradictions

    async def _step_synthesize(self, session: AGPSession) -> SessionSummary:
        """Architect synthesizes evidence into a conclusion."""
        evidence_summary = json.dumps(
            [e.to_dict() for e in session.evidence], indent=2
        )
        contradiction_summary = json.dumps(
            [c.to_dict() for c in session.contradictions], indent=2
        )
        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 6 — Synthesis.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"Evidence:\n{evidence_summary}\n\n"
                f"Contradictions:\n{contradiction_summary}\n\n"
                f"Synthesize a conclusion. The confidence score MUST reflect the quality "
                f"of the evidence — never overstate certainty.\n\n"
                f"Respond with JSON: {{\"conclusion\": \"...\", \"confidence_score\": 0.0-1.0}}"
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)

        conclusion = "No synthesis produced."
        confidence = 0.30
        if parsed:
            conclusion = parsed.get("conclusion", conclusion)
            confidence = float(parsed.get("confidence_score", confidence))

        return SessionSummary(
            scope=session.scope,
            domain=session.domain,
            conclusion=conclusion,
            confidence_score=confidence,
            evidence_count=len(session.evidence),
            contradiction_count=len(session.contradictions),
        )

    async def _step_manager_review(
        self, session: AGPSession, summary: SessionSummary
    ) -> SessionSummary:
        """Manager reviews synthesis, may adjust confidence downward."""
        evidence_summary = json.dumps(
            [e.to_dict() for e in session.evidence], indent=2
        )
        messages = [
            {"role": "user", "content": (
                f"Review this AGP synthesis for protocol compliance.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"Architect's conclusion: {summary.conclusion}\n"
                f"Architect's confidence: {summary.confidence_score}\n\n"
                f"Evidence:\n{evidence_summary}\n\n"
                f"Contradictions found: {summary.contradiction_count}\n\n"
                f"Rules:\n"
                f"- You may adjust confidence DOWNWARD only\n"
                f"- Flag any domain boundary violations\n"
                f"- Append objections if evidence does not support the conclusion\n\n"
                f"Respond with JSON: {{\"approved\": true/false, "
                f"\"adjusted_confidence\": null or float, "
                f"\"objections\": [\"...\"], \"reasoning\": \"...\"}}"
            )},
        ]
        response = await self.manager.achat(messages)
        parsed = _safe_parse(response)

        if parsed:
            # Manager can only adjust downward
            adjusted = parsed.get("adjusted_confidence")
            if adjusted is not None:
                adjusted = float(adjusted)
                if adjusted < summary.confidence_score:
                    summary.confidence_score = adjusted
                    logger.info(f"Manager adjusted confidence down to {adjusted}")

            objections = parsed.get("objections", [])
            if objections:
                summary.manager_objections = objections
                for obj in objections:
                    session.add_manager_objection(obj)

        return summary
