"""AGP session 7-step flow (extracted from tools.orch.session_steps).

``run_session_flow`` is the full AGP session pipeline: cache load, domain
assignment, source enumeration, evidence collection, contradiction check,
synthesis, Claude enhancement, manager review, and seal. ``session_steps``
keeps a thin ``async def run_session_flow`` delegate so Orchestrator and
existing patches stay on that module.

``step_synthesize``, ``step_manager_review``, and ``execute_tool`` stay in
session_steps.

Do not pull in the autonomous loop module. Do not arm live betting.
Do not add live to paper-signal.
"""
import asyncio
import logging
import time

from agp import (
    AGPSession,
    AGPSealRefused,
    AGPViolation,
    SessionStep,
)
from tools.orch.pipeline_support import (
    _dedup_search_results,
    _detect_freshness,
    run_searches_parallel,
)
from tools.orch.session_steps import (
    architect_system_prompt,
    domain_search_query,
    load_session_cache,
    step_assign_domain,
    step_check_contradictions,
    step_collect_evidence,
    step_enumerate_sources,
    step_escalate_to_claude,
    step_manager_review,
    step_synthesize,
)

logger = logging.getLogger("callisto.orchestrator")


async def run_session_flow(orch, query: str, skip_search: bool = False) -> dict:
    """Execute a full 7-step AGP session (was Orchestrator.run_session body).

    ``orch`` is the Orchestrator instance (duck-typed). Returns the sealed
    session dict. skip_search skips web searches for internal tasks that
    already have all the data they need.
    """
    session = AGPSession(query)
    _current_task = asyncio.current_task()
    if _current_task is not None:
        orch._active_sessions[_current_task] = session
    logger.info(f"Session {session.session_id}: starting — {query}")
    t0 = time.monotonic()

    # Load tiered cache (hot cache auto-injected, warm available via tools).
    # Local variable, not instance attribute, to prevent cross-session pollution
    # if the same Orchestrator instance handles concurrent run_session() calls.
    memory_context = await load_session_cache()
    logger.info(f"Session {session.session_id}: hot cache loaded ({len(memory_context)} chars)")

    try:
        arch_prompt = architect_system_prompt(orch.architect, memory_context)

        # Step 1: Declare Scope
        session.scope = query
        logger.info(f"Session {session.session_id}: step 1 — scope declared")

        # Step 2: Sentinel classifies WHILE Brave pre-searches run in parallel
        session.advance_to(SessionStep.ASSIGN_DOMAIN)
        domain_task = asyncio.create_task(step_assign_domain(orch.sentinel, session))

        if skip_search:
            pre_results = []
            freshness = None
            pre_queries = []
        else:
            # Extract a short search query — use first line only, max 200 chars
            search_query = query.split("\n")[0][:200].strip()
            pre_queries = [search_query, f"{search_query.rstrip('?').strip()} 2025 2026 latest"]
            # Sports/player/team queries: enforce freshness to avoid stale roster data
            freshness = _detect_freshness(query)
            search_task = asyncio.create_task(
                run_searches_parallel(pre_queries, freshness=freshness)
            )

        domain = await domain_task
        session.domain = domain
        t_domain = time.monotonic() - t0
        logger.info(f"Session {session.session_id}: step 2 — domain={domain.value} [{t_domain:.1f}s]")

        # Collect pre-search results + one domain-specific search
        if not skip_search:
            pre_results = await search_task
            domain_q = domain_search_query(query, domain)
            if domain_q:
                extra = await run_searches_parallel(
                    [domain_q], freshness=freshness
                )
                pre_results.extend(extra)
            pre_results = _dedup_search_results(pre_results)

        # Step 3: Source Enumeration (Architect)
        session.advance_to(SessionStep.SOURCE_ENUMERATION)
        sources = await step_enumerate_sources(
            orch.architect, arch_prompt, session
        )
        session.sources = sources
        t_sources = time.monotonic() - t0
        logger.info(f"Session {session.session_id}: step 3 — {len(sources)} sources [{t_sources:.1f}s]")

        # Run any additional searches from Architect's source list (parallel)
        if not skip_search:
            # Use only first line of query to avoid URL overflow on multi-line prompts
            short_query = query.split("\n")[0][:200].rstrip("?").strip()
            extra_queries = []
            for src in sources[:2]:
                q = f"{src} {short_query}"
                if q not in pre_queries:
                    extra_queries.append(q)
            if extra_queries:
                extra_results = await run_searches_parallel(
                    extra_queries, freshness=freshness
                )
                pre_results.extend(extra_results)
                pre_results = _dedup_search_results(pre_results)

        # Step 4: Primary Collection (Architect + search results)
        session.advance_to(SessionStep.PRIMARY_COLLECTION)
        evidence_list, used_tools = await step_collect_evidence(
            orch, session, pre_results, arch_prompt
        )
        for ev in evidence_list:
            session.add_evidence(ev)
        t_evidence = time.monotonic() - t0
        logger.info(
            f"Session {session.session_id}: step 4 — "
            f"{len(session.evidence)} evidence, tools={used_tools} [{t_evidence:.1f}s]"
        )

        # Step 5: Contradiction Check (Architect — already loaded)
        session.advance_to(SessionStep.CONTRADICTION_CHECK)
        contradictions = await step_check_contradictions(orch, session, arch_prompt)
        for c in contradictions:
            session.add_contradiction(c)
        t_contra = time.monotonic() - t0
        logger.info(f"Session {session.session_id}: step 5 — {len(contradictions)} contradictions [{t_contra:.1f}s]")

        # Step 6: Synthesis (Claude Code primary, local fallback) + Enhancement + Manager Review
        session.advance_to(SessionStep.SYNTHESIS)
        summary = await step_synthesize(orch, session, used_tools, arch_prompt)
        t_synth = time.monotonic() - t0
        logger.info(
            f"Session {session.session_id}: step 6a — "
            f"synthesis confidence={summary.confidence_score} [{t_synth:.1f}s]"
        )

        # Claude Code enhancement pass — always attempted when available
        summary, escalated = await step_escalate_to_claude(orch, session, summary)
        if escalated:
            used_tools = True  # Claude Code counts as a tool
            t_escalate = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 6b — "
                f"Claude Code enhancement → confidence={summary.confidence_score} [{t_escalate:.1f}s]"
            )

        summary = await step_manager_review(orch, session, summary, used_tools)
        session.summary = summary
        t_review = time.monotonic() - t0
        logger.info(
            f"Session {session.session_id}: step 6c — "
            f"final confidence={summary.confidence_score} ({summary.confidence_tier.value}) [{t_review:.1f}s]"
        )

        # Step 7: Session Close — seal and store
        session.advance_to(SessionStep.SESSION_CLOSE)
        try:
            seal_hash = session.seal()
        except AGPSealRefused as e:
            # Seal refused — garbage synthesis, empty evidence, or mostly
            # filtered. Do NOT write a SPECULATIVE row to DB; return an
            # error shape so callers know the result is unsealed/unstored.
            logger.warning(
                f"Session {session.session_id}: seal refused: {e}"
            )
            out = session.to_dict()
            out["stored"] = False
            out["sealed"] = False
            out["seal_refused_reason"] = str(e)
            out["error"] = "seal_refused"
            return out
        logger.info(f"Session {session.session_id}: step 7 — sealed {seal_hash[:16]}...")

        # Persist to memory
        for ev in session.evidence:
            await orch.memory.store_evidence(session.session_id, ev)
        await orch.memory.store_session(session)

        total = time.monotonic() - t0
        logger.info(f"Session {session.session_id}: complete in {total:.1f}s")
        out = session.to_dict()
        out["stored"] = True
        out["sealed"] = True
        return out

    except AGPViolation as e:
        logger.error(f"Session {session.session_id}: AGP violation: {e}")
        raise
    except Exception as e:
        logger.error(f"Session {session.session_id}: failed: {e}", exc_info=True)
        raise
    finally:
        # Drop the session from the active registry so task_worker's
        # adaptive-timeout watcher doesn't see a stale reference after
        # normal completion or cancellation.
        if _current_task is not None:
            orch._active_sessions.pop(_current_task, None)
