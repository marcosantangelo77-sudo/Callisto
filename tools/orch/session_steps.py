"""AGP session step implementations (extracted from orchestrator.py).

Each function here used to be the body of an ``Orchestrator`` method. They
receive the orchestrator instance as ``orch`` (duck-typed: needs ``architect``,
``manager``, ``sentinel``, ``memory``) so no circular import exists. The
Orchestrator methods remain as thin delegating facades so
``Orchestrator._step_*`` call sites and tests stay stable.

``step_synthesize`` / ``step_manager_review`` bodies live in session_synth.

Gates preserved verbatim:
  - Provenance ledger relabeling (source class from ledger, never model claim).
  - Confidence clamps per source class.
  - Claude Code tier = INFERRED unless it cites a ledger-verified URL.
  - Manager can only adjust confidence DOWN.
  - Contradiction penalty applied after clamp, before seal.
"""

import logging
from typing import Optional

from agp import (
    EMPTY_SYNTHESIS_MARKER,
    Contradiction,
    Domain,
    Evidence,
    SessionSummary,
    SourceClass,
)
from agp.thresholds import (
    CONTRADICTION_PENALTY,
    DB_CONFIDENCE_FLOOR,
    ESCALATION_THRESHOLD,
    MAX_CONFIDENCE_BY_SOURCE,
)
from inference import (
    _parse_json_response,
    execute_function_call,
    escalate_with_ladder,
)
from tools.claude_code import (
    claude_code_available,
    claude_code_query,
    is_available as claude_available,
)
from tools.search import web_search
from tools.cache_manager import get_cache_manager

from tools.orch.pipeline_support import (
    _best_source_class,
    _clamp_confidence,
    _default_registry,
    _json_compact,
    _safe_parse,
)

logger = logging.getLogger("callisto.orchestrator")

# JSON schema for Ollama structured output — guarantees valid domain classification
DOMAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "domain": {
            "type": "string",
            "enum": ["FINANCIAL", "TECHNICAL", "SIGNAL", "SYNTHESIS", "GENERAL"],
        }
    },
    "required": ["domain"],
}


def domain_search_query(query: str, domain: Domain) -> Optional[str]:
    """Generate a domain-specific search refinement.

    Uses only the first line (max 200 chars) to avoid URL overflow
    on multi-line queries like edge analysis prompts.
    """
    core = query.split("\n")[0][:200].rstrip("?").strip()
    if domain == Domain.FINANCIAL:
        return f"{core} market analysis financial data"
    elif domain == Domain.TECHNICAL:
        return f"{core} research breakthrough"
    elif domain == Domain.SIGNAL:
        return f"{core} trend indicator"
    return None


async def load_session_cache() -> str:
    """Load the tiered cache memory context for a fresh session (hot cache
    auto-injected, warm available via tools)."""
    cache = get_cache_manager()
    return await cache.get_memory_context()


def architect_system_prompt(architect, memory_context: str = "") -> str:
    """Build the Architect's system prompt with Hermes persistent memory."""
    base = architect.config.system_prompt
    if memory_context:
        return f"{base}\n\n{memory_context}"
    return base


async def step_assign_domain(sentinel, session) -> Domain:
    """Sentinel classifies the query into a domain.

    Uses Ollama structured output (constrained decoding) for guaranteed valid JSON.
    """
    messages = [
        {"role": "system", "content": sentinel.config.system_prompt},
        {"role": "user", "content": (
            f"Classify into one domain: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL.\n"
            f"Query: {session.query}"
        )},
    ]
    response = await sentinel.achat(
        messages, format=DOMAIN_SCHEMA, options={"num_predict": 32}
    )
    parsed = _safe_parse(response)
    if parsed and "domain" in parsed:
        from tools.orch.pipeline_support import _parse_domain
        return _parse_domain(parsed["domain"])
    from tools.orch.pipeline_support import _parse_domain
    return _parse_domain(response.get("content", "GENERAL"))


async def step_enumerate_sources(architect, arch_prompt: str, session) -> list[str]:
    """Architect lists sources to consult."""
    messages = [
        {"role": "system", "content": arch_prompt},
        {"role": "user", "content": (
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n"
            f"List sources and search queries to consult.\n"
            f'JSON: {{"sources":["source1","source2"],"search_queries":["query1"]}}'
        )},
    ]
    response = await architect.achat(messages, options={"num_predict": 256})
    parsed = _safe_parse(response)
    if parsed and "sources" in parsed:
        return parsed["sources"]
    return [session.scope]


async def execute_tool(name: str, arguments: dict):
    """Execute a tool call (was Orchestrator._execute_tool body)."""
    if name == "web_search":
        # Truncate query to prevent Brave 422 on massive tool-generated queries
        raw_q = arguments.get("query", "")
        safe_q = raw_q.split("\n")[0][:300].strip()
        return await web_search(query=safe_q, count=arguments.get("count", 5))
    if name == "claude_code":
        # Route through the ladder so CALLISTO_LOCAL_ONLY + cost-aware
        # routing + time-of-day demotion all apply uniformly.
        return await escalate_with_ladder(
            prompt=arguments.get("prompt", ""),
            system_context=arguments.get("system_context", ""),
            task_type="reasoning",
        )
    # Domain-plugin tools (sports today). Registration is the extension
    # point; unknown names fall through to the legacy generic dispatcher.
    handled, result = await _default_registry().dispatch(name, arguments)
    if handled:
        return result
    return execute_function_call(name, arguments)


async def step_collect_evidence(
    orch, session, search_results: list[dict], arch_prompt: str
) -> tuple[list[Evidence], bool]:
    from tools.orch.session_collect import step_collect_evidence as _impl
    return await _impl(orch, session, search_results, arch_prompt)

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


async def step_synthesize(orch, session, used_tools: bool, arch_prompt: str) -> SessionSummary:
    """Architect synthesizes evidence into a conclusion.

    Claude Code is the PRIMARY reasoning engine. Local models are the fallback
    when Claude is rate-limited or unavailable.
    """
    from tools.orch.session_synth import step_synthesize as _impl
    return await _impl(orch, session, used_tools, arch_prompt)



async def step_escalate_to_claude(orch, session, summary: SessionSummary):
    from tools.orch.session_escalate import step_escalate_to_claude as _impl
    return await _impl(orch, session, summary)


async def step_manager_review(
    orch, session, summary: SessionSummary, used_tools: bool
) -> SessionSummary:
    """Manager reviews synthesis. Enforces confidence discipline."""
    from tools.orch.session_synth import step_manager_review as _impl
    return await _impl(orch, session, summary, used_tools)



async def run_session_flow(orch, query: str, skip_search: bool = False) -> dict:
    from tools.orch.session_flow import run_session_flow as _impl
    return await _impl(orch, query, skip_search=skip_search)
