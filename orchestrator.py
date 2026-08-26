"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
Uses Brave Search for real evidence gathering. Enforces honest confidence calibration.

Optimizations: parallel Brave searches, pipelined model loading, compressed prompts.

Pipeline helpers live in ``tools/orch/`` now:
  - tools/orch/tool_schemas.py    — all Ollama tool-calling JSON schemas
  - tools/orch/sports_dispatch.py — sports tool implementations
  - tools/orch/pipeline_support.py— registry seed, search staging, pure helpers
  - tools/orch/session_steps.py   — session step bodies (domain, sources,
                                    evidence, contradictions, synthesis,
                                    Claude escalation, manager review)
This module re-exports the public names so existing
``from orchestrator import X`` call sites stay stable.
"""

import asyncio
import json
import logging
import time
from typing import Optional

from agp import (
    AGPSealRefused,
    AGPSession,
    AGPViolation,
    Contradiction,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)
from agp.provenance import ProvenanceLedger
from agp.thresholds import (  # noqa: F401
    CONTRADICTION_PENALTY,
    DB_CONFIDENCE_FLOOR,
    ESCALATION_THRESHOLD,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
from inference import (  # noqa: F401
    get_architect,
    get_manager,
    get_sentinel,
    execute_function_call,
    _parse_json_response,
    escalate_with_ladder,
)
from memory import MemoryStore

# ── Extracted pipeline modules (facade re-exports below) ──
from tools.orch.tool_schemas import (  # noqa: F401
    BEST_PRICE_TOOL,
    BET_SIZE_TOOL,
    BOOST_EVAL_TOOL,
    CLAUDE_CODE_TOOL,
    DEVIG_TOOL,
    EDGE_SCAN_TOOL,
    EVALUATE_EDGE_TOOL,
    HERMES_TOOL_PROMPT,
    INJURIES_TOOL,
    LINE_GAPS_TOOL,
    ODDS_ALT_LINES_TOOL,
    ODDS_CALCULATE_EV_TOOL,
    ODDS_GET_EVENT_TOOL,
    ODDS_GET_ODDS_TOOL,
    ODDS_GET_SCORES_TOOL,
    ODDS_PLAYER_PROPS_TOOL,
    ODDS_TOOLS,
    PROP_SCANNER_TOOL,
    RECORD_BET_TOOL,
    ROSTER_TOOL,
    SGP_EVAL_TOOL,
    SIM_GAME_TOOL,
    SIM_PROP_TOOL,
    SCOREBOARD_TOOL,
    WARM_CACHE_TOOL,
    WEB_SEARCH_TOOL,
)
from tools.orch.sports_dispatch import _sports_tool_dispatch  # noqa: F401
from tools.orch.pipeline_support import (  # noqa: F401
    MAX_TOOL_CALL_ROUNDS,
    _best_source_class,
    _clamp_confidence,
    _dedup_search_results,
    _default_registry,
    _detect_freshness,
    _execute_sports_tool,
    _json_compact,
    _parse_domain,
    _registry_seeded,
    _safe_parse,
    domain_search_query as _pipeline_domain_search_query,
    run_searches_parallel,
)
from tools.claude_code import (  # noqa: F401
    claude_code_available,
    claude_code_query,
    is_available as claude_available,
)
from tools.search import web_search  # noqa: F401
from tools.cache_manager import get_cache_manager  # noqa: F401
from tools.orch.session_steps import (  # noqa: F401
    DOMAIN_SCHEMA,
    architect_system_prompt,
    execute_tool,
    load_session_cache,
    run_session_flow,
    step_assign_domain,
    step_check_contradictions,
    step_collect_evidence,
    step_enumerate_sources,
    step_escalate_to_claude,
    step_manager_review,
    step_synthesize,
)

logger = logging.getLogger("callisto.orchestrator")

class Orchestrator:
    """Coordinates the 3-agent AGP session flow."""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.architect = get_architect()
        self.manager = get_manager()
        self.sentinel = get_sentinel()
        # Registry: asyncio.Task → live AGPSession. Lets an external watcher
        # (the task_worker adaptive-timeout loop) inspect liveness without
        # changing run_session's return contract. Scoped to the asyncio Task
        # that invoked run_session so concurrent callers can't clobber each
        # other. Cleaned up in a finally: block inside run_session.
        self._active_sessions: dict = {}
        # Citation-grounding ledger for the Claude enhancement pass
        # (tools/orch/session_steps.py::step_escalate_to_claude). An empty
        # ledger is the FAIL-CLOSED default: no verified URL ⇒ citations buy
        # nothing ⇒ Claude responses stay INFERRED-tier.
        self._provenance = ProvenanceLedger()

    def active_session_for(self, task) -> Optional[AGPSession]:
        """Return the live AGPSession for a given asyncio.Task, or None.

        Used by api.py::task_worker to poll `last_progress_at`, `current_step`,
        and evidence counts for the adaptive-timeout extension logic. The
        Orchestrator instance is shared process-wide; this indirection is the
        least-invasive way to expose in-flight state.
        """
        return self._active_sessions.get(task)

    async def run_session(self, query: str, skip_search: bool = False) -> dict:
        """Execute a full 7-step AGP session. Returns the sealed session dict.

        Returns the sealed session dict (see tools.orch.session_steps).
        """
        return await run_session_flow(self, query, skip_search=skip_search)

    def _domain_search_query(self, query: str, domain: Domain) -> Optional[str]:
        """Generate a domain-specific search refinement (delegates to tools.orch)."""
        return _pipeline_domain_search_query(query, domain)

    def _architect_system_prompt(self) -> str:
        """Build the Architect's system prompt with Hermes persistent memory."""
        return architect_system_prompt(
            self.architect, getattr(self, "_memory_context", "")
        )

    async def _run_searches_parallel(
        self, queries: list[str], freshness: Optional[str] = None
    ) -> list[dict]:
        """Run multiple web search queries in parallel (delegates to pipeline_support)."""
        return await run_searches_parallel(queries, freshness=freshness)

    async def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool call (delegates to tools.orch.session_steps)."""
        return await execute_tool(name, arguments)
