"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
Uses Brave Search for real evidence gathering. Enforces honest confidence calibration.
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
from tools.brave_search import brave_search

logger = logging.getLogger("callisto.orchestrator")

# Brave Search tool schema for Ollama native tool calling
BRAVE_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "brave_search",
        "description": "Search the web for current information. Use this to gather real evidence from real sources. Always search before making claims about current state of anything.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results (1-20)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
}

# Maximum confidence for evidence without real-time source verification
MAX_CONFIDENCE_NO_TOOL = 0.55  # caps at PROBABLE — cannot be CORROBORATED or VERIFIED without real sources
MAX_TOOL_CALL_ROUNDS = 3  # prevent infinite tool call loops


def _safe_parse(response: dict, fallback=None):
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


def _clamp_confidence(score: float, has_real_sources: bool) -> float:
    """Enforce confidence ceiling based on whether real sources were consulted."""
    score = max(0.0, min(1.0, score))
    if not has_real_sources:
        score = min(score, MAX_CONFIDENCE_NO_TOOL)
    return round(score, 2)


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
        logger.info(f"Session {session.session_id}: starting — {query}")

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
            logger.info(f"Session {session.session_id}: step 3 — {len(sources)} sources")

            # Step 4: Primary Collection (Architect + Brave Search)
            session.advance_to(SessionStep.PRIMARY_COLLECTION)
            evidence_list, used_tools = await self._step_collect_evidence(session)
            for ev in evidence_list:
                session.add_evidence(ev)
            logger.info(
                f"Session {session.session_id}: step 4 — "
                f"{len(session.evidence)} storable evidence, tools_used={used_tools}"
            )

            # Step 5: Contradiction Check (Architect)
            session.advance_to(SessionStep.CONTRADICTION_CHECK)
            contradictions = await self._step_check_contradictions(session)
            for c in contradictions:
                session.add_contradiction(c)
            logger.info(f"Session {session.session_id}: step 5 — {len(contradictions)} contradictions")

            # Step 6: Synthesis (Architect) + Manager Review
            session.advance_to(SessionStep.SYNTHESIS)
            summary = await self._step_synthesize(session, used_tools)
            summary = await self._step_manager_review(session, summary, used_tools)
            session.summary = summary
            logger.info(
                f"Session {session.session_id}: step 6 — "
                f"confidence={summary.confidence_score} ({summary.confidence_tier.value})"
            )

            # Step 7: Session Close — seal and store
            session.advance_to(SessionStep.SESSION_CLOSE)
            seal_hash = session.seal()
            logger.info(f"Session {session.session_id}: step 7 — sealed {seal_hash[:16]}...")

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
                f"Classify into one domain. "
                f"Domains: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL.\n\n"
                f"Query: {session.query}\n\n"
                f"JSON: {{\"domain\": \"DOMAIN_NAME\"}}"
            )},
        ]
        response = await self.sentinel.achat(messages)
        parsed = _safe_parse(response)
        if parsed and "domain" in parsed:
            return _parse_domain(parsed["domain"])
        return _parse_domain(response.get("content", "GENERAL"))

    async def _step_enumerate_sources(self, session: AGPSession) -> list[str]:
        """Architect lists sources to consult."""
        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 3 — Source Enumeration.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"You have access to brave_search for web searches. "
                f"List the sources and search queries you will use.\n"
                f"JSON: {{\"sources\": [\"source1\", ...], \"search_queries\": [\"query1\", ...]}}"
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)
        if parsed and "sources" in parsed:
            return parsed["sources"]
        return [session.scope]

    async def _step_collect_evidence(self, session: AGPSession) -> tuple[list[Evidence], bool]:
        """Architect gathers evidence using Brave Search.

        Returns (evidence_list, used_real_tools).
        """
        # First, run brave searches based on the query and sources
        search_results = await self._run_searches(session)
        used_tools = len(search_results) > 0

        # Build context from search results
        search_context = ""
        if search_results:
            search_context = (
                f"\n\nSearch results from Brave Search (REAL web data — these are SECONDARY sources):\n"
                f"{json.dumps(search_results, indent=2)}\n\n"
                f"IMPORTANT: Evidence from these search results is SECONDARY source class. "
                f"Only your own direct analysis of primary documents can be PRIMARY."
            )
        else:
            search_context = (
                "\n\nWARNING: No search results available. All evidence you provide is INFERRED "
                "from your training data. Source class must be INFERRED. "
                "Confidence scores must not exceed 0.55 (PROBABLE ceiling) without real sources."
            )

        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 4 — Primary Collection.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n"
                f"Sources: {json.dumps(session.sources)}\n"
                f"{search_context}\n\n"
                f"For each evidence item provide:\n"
                f"- content: the finding\n"
                f"- source_class: SECONDARY (from search) or INFERRED (from training data)\n"
                f"- confidence_score: 0.0-1.0 (max 0.55 for INFERRED)\n"
                f"- source_name: URL or source name\n\n"
                f"JSON: {{\"evidence\": [...]}}"
            )},
        ]

        # Allow Architect to also call tools natively
        response = await self.architect.achat(messages, tools=[BRAVE_SEARCH_TOOL])

        # Handle native tool calls (up to MAX_TOOL_CALL_ROUNDS)
        for _ in range(MAX_TOOL_CALL_ROUNDS):
            if not response.get("tool_calls"):
                break
            used_tools = True
            for tc in response["tool_calls"]:
                result = await self._execute_tool(tc["name"], tc["arguments"])
                messages.append({"role": "assistant", "content": response["content"] or ""})
                messages.append({
                    "role": "tool",
                    "content": json.dumps(result) if not isinstance(result, str) else result,
                })
            response = await self.architect.achat(messages, tools=[BRAVE_SEARCH_TOOL])

        parsed = _safe_parse(response)
        evidence_list = []
        if parsed and "evidence" in parsed:
            for item in parsed["evidence"]:
                try:
                    source_class = SourceClass(item.get("source_class", "INFERRED"))
                    raw_confidence = float(item.get("confidence_score", 0.3))
                    confidence = _clamp_confidence(raw_confidence, used_tools or source_class != SourceClass.INFERRED)

                    ev = Evidence(
                        content=item.get("content", ""),
                        source_class=source_class,
                        confidence_score=confidence,
                        domain=session.domain,
                        origin_agent="architect",
                        source_name=item.get("source_name", ""),
                    )
                    evidence_list.append(ev)
                except (ValueError, KeyError) as e:
                    logger.warning(f"Skipping malformed evidence: {e}")
        return evidence_list, used_tools

    async def _run_searches(self, session: AGPSession) -> list[dict]:
        """Run Brave Search queries derived from the session scope and sources."""
        results = []
        queries = [session.scope]
        # Add first few sources as additional queries
        for src in session.sources[:3]:
            if src != session.scope:
                queries.append(f"{src} {session.scope}")

        for q in queries[:4]:  # max 4 searches per session
            try:
                search_result = await brave_search(q, count=5)
                if search_result.get("results"):
                    for r in search_result["results"]:
                        results.append({
                            "title": r.get("title", ""),
                            "url": r.get("url", ""),
                            "description": r.get("description", ""),
                            "source_class": "SECONDARY",
                        })
            except Exception as e:
                logger.warning(f"Brave search failed for '{q}': {e}")
        return results

    async def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool call, supporting both sync and async tools."""
        if name == "brave_search":
            return await brave_search(
                query=arguments.get("query", ""),
                count=arguments.get("count", 5),
            )
        return execute_function_call(name, arguments)

    async def _step_check_contradictions(self, session: AGPSession) -> list[Contradiction]:
        """Architect actively searches for contradictions."""
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
                f"Evidence:\n{evidence_summary}\n\n"
                f"Search for contradictions. Absence is a flag, not comfort.\n"
                f"JSON: {{\"contradictions\": ["
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

    async def _step_synthesize(self, session: AGPSession, used_tools: bool) -> SessionSummary:
        """Architect synthesizes evidence into a conclusion."""
        evidence_summary = json.dumps(
            [e.to_dict() for e in session.evidence], indent=2
        )
        contradiction_summary = json.dumps(
            [c.to_dict() for c in session.contradictions], indent=2
        )

        tool_warning = ""
        if not used_tools:
            tool_warning = (
                "\nWARNING: No real-time sources were consulted. All evidence is from training data. "
                "Your confidence score MUST NOT exceed 0.55 (PROBABLE). "
                "Anything higher would violate Pillar IV (Output Honesty).\n"
            )

        messages = [
            {"role": "system", "content": self.architect.config.system_prompt},
            {"role": "user", "content": (
                f"AGP Step 6 — Synthesis.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"Evidence ({len(session.evidence)} items):\n{evidence_summary}\n\n"
                f"Contradictions ({len(session.contradictions)}):\n{contradiction_summary}\n"
                f"{tool_warning}\n"
                f"Synthesize. Confidence MUST match evidence quality.\n"
                f"JSON: {{\"conclusion\": \"...\", \"confidence_score\": 0.0-1.0}}"
            )},
        ]
        response = await self.architect.achat(messages)
        parsed = _safe_parse(response)

        conclusion = "No synthesis produced."
        confidence = 0.30
        if parsed:
            conclusion = parsed.get("conclusion", conclusion)
            confidence = float(parsed.get("confidence_score", confidence))

        # Enforce confidence ceiling
        confidence = _clamp_confidence(confidence, used_tools)

        return SessionSummary(
            scope=session.scope,
            domain=session.domain,
            conclusion=conclusion,
            confidence_score=confidence,
            evidence_count=len(session.evidence),
            contradiction_count=len(session.contradictions),
        )

    async def _step_manager_review(
        self, session: AGPSession, summary: SessionSummary, used_tools: bool
    ) -> SessionSummary:
        """Manager reviews synthesis. Enforces confidence discipline."""
        evidence_summary = json.dumps(
            [e.to_dict() for e in session.evidence], indent=2
        )

        source_classes = set(e.source_class.value for e in session.evidence)
        tool_context = ""
        if not used_tools:
            tool_context = (
                "\nCRITICAL: No real-time web sources were used. All evidence is INFERRED from "
                "training data. Maximum allowable confidence is 0.55 (PROBABLE). "
                "If the Architect's score exceeds this, you MUST adjust it downward.\n"
            )

        messages = [
            {"role": "user", "content": (
                f"Review this AGP synthesis for protocol compliance.\n"
                f"Domain: {session.domain.value}\n"
                f"Scope: {session.scope}\n\n"
                f"Architect's conclusion: {summary.conclusion}\n"
                f"Architect's confidence: {summary.confidence_score}\n"
                f"Source classes present: {source_classes}\n"
                f"Real-time tools used: {used_tools}\n\n"
                f"Evidence ({summary.evidence_count} items):\n{evidence_summary}\n\n"
                f"Contradictions found: {summary.contradiction_count}\n"
                f"{tool_context}\n"
                f"Rules:\n"
                f"- Adjust confidence DOWNWARD only\n"
                f"- INFERRED-only evidence caps at 0.55 (PROBABLE)\n"
                f"- SECONDARY evidence (web search) caps at 0.75 (CORROBORATED)\n"
                f"- Only PRIMARY evidence can support VERIFIED (0.90+)\n"
                f"- Flag domain boundary violations\n"
                f"- Objections are mandatory when evidence is weak\n\n"
                f"JSON: {{\"approved\": true/false, "
                f"\"adjusted_confidence\": null or float, "
                f"\"objections\": [\"...\"], \"reasoning\": \"...\"}}"
            )},
        ]
        response = await self.manager.achat(messages)
        parsed = _safe_parse(response)

        if parsed:
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
        summary.confidence_score = _clamp_confidence(summary.confidence_score, used_tools)

        return summary
