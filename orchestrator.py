"""
AGP session orchestrator — runs the full 7-step research cycle.

Coordinates Architect, Manager, and Sentinel across all AGP session steps.
Uses Brave Search for real evidence gathering. Enforces honest confidence calibration.

Optimizations: parallel Brave searches, pipelined model loading, compressed prompts.

Pipeline helpers live in ``tools/orch/`` now:
  - tools/orch/tool_schemas.py    — all Ollama tool-calling JSON schemas
  - tools/orch/sports_dispatch.py — sports tool implementations
  - tools/orch/pipeline_support.py— registry seed, search staging, pure helpers
This module re-exports the public names so existing
``from orchestrator import X`` call sites stay stable.
"""

import asyncio
import json
import logging
import os
import time
from typing import Optional

import aiosqlite

from agp import (
    AGPSealRefused,
    AGPSession,
    AGPViolation,
    Contradiction,
    Domain,
    EMPTY_SYNTHESIS_MARKER,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)
from agp.provenance import ProvenanceLedger, relabel_evidence
from agp.thresholds import (
    CONTRADICTION_PENALTY,
    DB_CONFIDENCE_FLOOR,
    ESCALATION_THRESHOLD,
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
from inference import (
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
    domain_search_query,
    run_searches_parallel,
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

        skip_search: If True, skip web searches entirely (for internal tasks
        like edge analysis that already have all data they need).
        """
        session = AGPSession(query)
        _current_task = asyncio.current_task()
        if _current_task is not None:
            self._active_sessions[_current_task] = session
        logger.info(f"Session {session.session_id}: starting — {query}")
        t0 = time.monotonic()

        # Load tiered cache (hot cache auto-injected, warm available via tools).
        # Local variable, not instance attribute, to prevent cross-session pollution
        # if the same Orchestrator instance handles concurrent run_session() calls.
        cache = get_cache_manager()
        memory_context = await cache.get_memory_context()
        logger.info(f"Session {session.session_id}: hot cache loaded ({len(memory_context)} chars)")

        try:
            # Step 1: Declare Scope
            session.scope = query
            logger.info(f"Session {session.session_id}: step 1 — scope declared")

            # Step 2: Sentinel classifies WHILE Brave pre-searches run in parallel
            session.advance_to(SessionStep.ASSIGN_DOMAIN)
            domain_task = asyncio.create_task(self._step_assign_domain(session))

            if skip_search:
                pre_results = []
            else:
                # Extract a short search query — use first line only, max 200 chars
                search_query = query.split("\n")[0][:200].strip()
                pre_queries = [search_query, f"{search_query.rstrip('?').strip()} 2025 2026 latest"]
                # Sports/player/team queries: enforce freshness to avoid stale roster data
                freshness = _detect_freshness(query)
                search_task = asyncio.create_task(
                    self._run_searches_parallel(pre_queries, freshness=freshness)
                )

            domain = await domain_task
            session.domain = domain
            t_domain = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 2 — domain={domain.value} [{t_domain:.1f}s]")

            # Collect pre-search results + one domain-specific search
            if not skip_search:
                pre_results = await search_task
                domain_q = self._domain_search_query(query, domain)
                if domain_q:
                    extra = await self._run_searches_parallel(
                        [domain_q], freshness=freshness
                    )
                    pre_results.extend(extra)
                pre_results = _dedup_search_results(pre_results)

            # Step 3: Source Enumeration (Architect)
            session.advance_to(SessionStep.SOURCE_ENUMERATION)
            sources = await self._step_enumerate_sources(session)
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
                    extra_results = await self._run_searches_parallel(
                        extra_queries, freshness=freshness
                    )
                    pre_results.extend(extra_results)
                    pre_results = _dedup_search_results(pre_results)

            # Step 4: Primary Collection (Architect + search results)
            session.advance_to(SessionStep.PRIMARY_COLLECTION)
            evidence_list, used_tools = await self._step_collect_evidence(
                session, pre_results
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
            contradictions = await self._step_check_contradictions(session)
            for c in contradictions:
                session.add_contradiction(c)
            t_contra = time.monotonic() - t0
            logger.info(f"Session {session.session_id}: step 5 — {len(contradictions)} contradictions [{t_contra:.1f}s]")

            # Step 6: Synthesis (Claude Code primary, local fallback) + Enhancement + Manager Review
            session.advance_to(SessionStep.SYNTHESIS)
            summary = await self._step_synthesize(session, used_tools)
            t_synth = time.monotonic() - t0
            logger.info(
                f"Session {session.session_id}: step 6a — "
                f"synthesis confidence={summary.confidence_score} [{t_synth:.1f}s]"
            )

            # Claude Code enhancement pass — always attempted when available
            summary, escalated = await self._step_escalate_to_claude(session, summary)
            if escalated:
                used_tools = True  # Claude Code counts as a tool
                t_escalate = time.monotonic() - t0
                logger.info(
                    f"Session {session.session_id}: step 6b — "
                    f"Claude Code enhancement → confidence={summary.confidence_score} [{t_escalate:.1f}s]"
                )

            summary = await self._step_manager_review(session, summary, used_tools)
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
                await self.memory.store_evidence(session.session_id, ev)
            await self.memory.store_session(session)

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
                self._active_sessions.pop(_current_task, None)

    def _domain_search_query(self, query: str, domain: Domain) -> Optional[str]:
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

    async def _step_assign_domain(self, session: AGPSession) -> Domain:
        """Sentinel classifies the query into a domain.

        Uses Ollama structured output (constrained decoding) for guaranteed valid JSON.
        """
        messages = [
            {"role": "system", "content": self.sentinel.config.system_prompt},
            {"role": "user", "content": (
                f"Classify into one domain: FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL.\n"
                f"Query: {session.query}"
            )},
        ]
        response = await self.sentinel.achat(
            messages, format=self.DOMAIN_SCHEMA, options={"num_predict": 32}
        )
        parsed = _safe_parse(response)
        if parsed and "domain" in parsed:
            return _parse_domain(parsed["domain"])
        return _parse_domain(response.get("content", "GENERAL"))

    def _architect_system_prompt(self) -> str:
        """Build the Architect's system prompt with Hermes persistent memory."""
        base = self.architect.config.system_prompt
        memory = getattr(self, "_memory_context", "")
        if memory:
            return f"{base}\n\n{memory}"
        return base

    async def _step_enumerate_sources(self, session: AGPSession) -> list[str]:
        """Architect lists sources to consult."""
        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"List sources and search queries to consult.\n"
                f'JSON: {{"sources":["source1","source2"],"search_queries":["query1"]}}'
            )},
        ]
        response = await self.architect.achat(messages, options={"num_predict": 256})
        parsed = _safe_parse(response)
        if parsed and "sources" in parsed:
            return parsed["sources"]
        return [session.scope]

    async def _step_collect_evidence(
        self, session: AGPSession, search_results: list[dict]
    ) -> tuple[list[Evidence], bool]:
        """Architect analyzes search results and extracts structured evidence.

        Claude Code is the PRIMARY reasoning engine. Local models are the fallback
        when Claude is rate-limited or unavailable.

        Wiki-in-the-loop (feat/wiki-in-the-loop, 2026-04-22):
          Before LLM analysis, the knowledge wiki is queried for articles
          relevant to ``session.scope``. High-similarity hits (>0.85) are
          injected as PRIMARY evidence WITH cites, short-circuiting cheap
          look-ups and providing a citation trail. We AUGMENT external
          search rather than replacing it (per AGP rigor).
        """
        used_tools = len(search_results) > 0

        # Provenance ledger (findings/instance4.md P1): every real tool
        # return is recorded here, by the code path that executed it.
        # Source class is later assigned FROM this ledger — never from the
        # model's self-declared label. A fabricated URL cannot enter it.
        ledger = ProvenanceLedger()
        for r in search_results:
            ledger.record_tool_result(
                "web_search",
                f'{r.get("title", "")}\n{r.get("description", "")}',
                urls=[r.get("url", "")] if r.get("url") else None,
            )

        # ── Wiki retrieval (pre-LLM) ──
        wiki_evidence: list[Evidence] = []
        wiki_in_loop = os.getenv("CALLISTO_WIKI_IN_LOOP", "1") == "1"
        if wiki_in_loop:
            try:
                from tools.knowledge_wiki import get_wiki
                wiki = get_wiki()
                async with aiosqlite.connect(wiki.db_path) as wdb:
                    await wdb.execute("PRAGMA busy_timeout = 30000")
                    wiki_hits = await wiki.search(
                        wdb, session.scope, top_k=5, min_similarity=0.0,
                    )
                for hit in wiki_hits:
                    sim = hit.get("similarity")
                    # High-similarity hits: promote to SECONDARY with wiki cite.
                    if isinstance(sim, (int, float)) and sim >= 0.85:
                        content = (
                            f"[wiki prior: {hit.get('topic')}] "
                            f"{(hit.get('summary') or hit.get('content') or '')[:400]}"
                        )
                        ev = Evidence(
                            content=content,
                            source_class=SourceClass.SECONDARY,
                            confidence_score=min(0.75, float(hit.get("confidence", 0.5))),
                            domain=session.domain,
                            origin_agent="knowledge_wiki",
                            source_name=f"wiki://{hit.get('topic')}",
                        )
                        wiki_evidence.append(ev)
                if wiki_evidence:
                    logger.info(
                        f"Session {session.session_id}: wiki retrieval injected "
                        f"{len(wiki_evidence)} high-similarity evidence items"
                    )
            except Exception as e:
                logger.warning(
                    f"Session {session.session_id}: wiki retrieval failed (non-fatal): {e}"
                )

        if search_results:
            compact = [
                {"t": r["title"][:80], "u": r["url"], "d": r["description"][:150]}
                for r in search_results[:12]
            ]
            search_context = (
                f"Web results (SECONDARY sources):\n{_json_compact(compact)}\n\n"
                f"Extract up to 8 evidence items from these results."
            )
        else:
            search_context = (
                "No web results. All evidence is INFERRED. "
                "Source class=INFERRED, max confidence=0.55."
            )

        # ── Claude Code PRIMARY path ──
        if claude_available():
            logger.info(f"Session {session.session_id}: step 4 using Claude Code (primary)")
            claude_prompt = (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n\n"
                f"{search_context}\n\n"
                f"For each piece of evidence, provide: content (1 sentence), "
                f"source_class (SECONDARY if from web results, INFERRED if from your training), "
                f"confidence_score (0.0-1.0, max 0.55 for INFERRED, max 0.75 for SECONDARY), "
                f"source_name (URL if available).\n"
                f'Respond with JSON: {{"evidence":[{{"content":"...","source_class":"SECONDARY",'
                f'"confidence_score":0.7,"source_name":"url"}}]}}'
            )
            claude_context = (
                f"You are the evidence collection agent in an AGP (Agentic Governance Protocol) session. "
                f"Analyze the provided web search results and extract structured evidence items. "
                f"Be rigorous: only claim SECONDARY for web-sourced evidence, INFERRED for reasoning."
            )
            result = await escalate_with_ladder(
                claude_prompt,
                system_context=claude_context,
                task_type="reasoning",
                timeout=120,
            )

            if not result.get("error") and not result.get("rate_limited"):
                used_tools = True
                content = result.get("content", "")
                parsed = _parse_json_response(content) if content else None
                evidence_list = []
                if parsed and isinstance(parsed, dict) and "evidence" in parsed:
                    for item in parsed["evidence"]:
                        try:
                            source_class = SourceClass(item.get("source_class", "INFERRED"))
                            raw_confidence = float(item.get("confidence_score", 0.3))
                            confidence = _clamp_confidence(raw_confidence, source_class.value)
                            ev = Evidence(
                                content=item.get("content", ""),
                                source_class=source_class,
                                confidence_score=confidence,
                                domain=session.domain,
                                origin_agent="claude_code",
                                source_name=item.get("source_name", ""),
                            )
                            evidence_list.append(ev)
                        except (ValueError, KeyError) as e:
                            logger.warning(f"Skipping malformed evidence from Claude: {e}")
                if evidence_list:
                    # Prepend wiki evidence so its cites appear first in the trail.
                    combined = wiki_evidence + evidence_list
                    logger.info(
                        f"Session {session.session_id}: Claude Code extracted "
                        f"{len(evidence_list)} evidence items (+ {len(wiki_evidence)} wiki priors)"
                    )
                    return combined, used_tools or bool(wiki_evidence)
                else:
                    logger.warning(f"Session {session.session_id}: Claude Code returned no parseable evidence, falling back to local")
            else:
                logger.info(
                    f"Session {session.session_id}: Claude Code unavailable for evidence collection "
                    f"(error={result.get('error')}), falling back to local model"
                )

        # ── Local model FALLBACK path ──
        logger.info(f"Session {session.session_id}: step 4 using local model (fallback)")

        tool_prompt = ""
        if not self.architect.config.supports_native_tools:
            tool_prompt = HERMES_TOOL_PROMPT

        messages = [
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n\n"
                f"{search_context}\n\n"
                f"For each: content (1 sentence), source_class (SECONDARY/INFERRED), "
                f"confidence_score (0.0-1.0, max 0.55 for INFERRED), source_name (URL).\n"
                f'JSON: {{"evidence":[{{"content":"...","source_class":"SECONDARY",'
                f'"confidence_score":0.7,"source_name":"url"}}]}}'
                f"{tool_prompt}"
            )},
        ]

        # Domain-scoped toolkit: core tools + only the plugins this
        # session's domain/query actually calls for (BUILD_MANDATE item 3).
        available_tools = _default_registry().tools_for(session.domain, session.query)
        response = await self.architect.achat(
            messages, tools=available_tools, options={"num_predict": 2048}
        )

        # Handle tool calls
        for _ in range(MAX_TOOL_CALL_ROUNDS):
            if not response.get("tool_calls"):
                break
            used_tools = True
            for tc in response["tool_calls"]:
                result = await self._execute_tool(tc["name"], tc["arguments"])
                # Record the real tool return so its URLs/bytes carry
                # provenance (the model cannot add to this ledger).
                try:
                    payload = result if isinstance(result, str) else json.dumps(result)
                    ledger.record_tool_result(tc["name"], payload)
                except (TypeError, ValueError):
                    pass
                messages.append({"role": "assistant", "content": response["content"] or ""})
                messages.append({
                    "role": "tool" if self.architect.config.supports_native_tools else "user",
                    "content": f"Tool result for {tc['name']}:\n" + (
                        json.dumps(result) if not isinstance(result, str) else result
                    ),
                })
            response = await self.architect.achat(
                messages, tools=available_tools, options={"num_predict": 2048}
            )

        parsed = _safe_parse(response)
        evidence_list = []
        if parsed and "evidence" in parsed:
            for item in parsed["evidence"]:
                try:
                    source_class = SourceClass(item.get("source_class", "INFERRED"))
                    raw_confidence = float(item.get("confidence_score", 0.3))
                    confidence = _clamp_confidence(raw_confidence, source_class.value)

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
        # Prepend wiki priors so their cites appear first in the trail.
        combined = wiki_evidence + evidence_list
        # Provenance relabel: source class is assigned from the ledger, not
        # the model's declaration. Declared SECONDARY without real tool
        # provenance demotes to INFERRED (0.55 ceiling); real tool bytes can
        # promote. Confidence re-clamps to the assigned class's ceiling.
        demoted = relabel_evidence(combined, ledger, MAX_CONFIDENCE_BY_SOURCE)
        if demoted:
            logger.info(
                f"Session {session.session_id}: provenance relabel — "
                f"{demoted} evidence item(s) demoted"
            )
        return combined, used_tools or bool(wiki_evidence)

    async def _run_searches_parallel(
        self, queries: list[str], freshness: Optional[str] = None
    ) -> list[dict]:
        """Run multiple web search queries in parallel (delegates to pipeline_support)."""
        return await run_searches_parallel(queries, freshness=freshness)

    async def _execute_tool(self, name: str, arguments: dict):
        """Execute a tool call."""
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

    async def _step_check_contradictions(self, session: AGPSession) -> list[Contradiction]:
        """Architect actively searches for contradictions.

        Claude Code is the PRIMARY reasoning engine (mirrors _step_synthesize).
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
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
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
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence:\n{evidence_compact}\n\n"
                f"Find contradictions. Absence is a flag.\n"
                f'JSON: {{"contradictions":[{{"claim_a":"...","claim_b":"...","source_a":"...",'
                f'"source_b":"...","severity":"MINOR|MAJOR|CRITICAL","resolution":"..."}}],'
                f'"notes":"..."}}'
            )},
        ]
        response = await self.architect.achat(messages, options={"num_predict": 512})
        parsed = _safe_parse(response)
        return _parse_contradictions(parsed)

    async def _step_synthesize(self, session: AGPSession, used_tools: bool) -> SessionSummary:
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
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
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
            {"role": "system", "content": self._architect_system_prompt()},
            {"role": "user", "content": (
                f"Synthesize.\n"
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
                f"Evidence ({len(session.evidence)}):\n{evidence_compact}\n"
                f"Contradictions: {len(session.contradictions)}\n"
                f"{tool_warning}"
                f'JSON: {{"conclusion":"...","confidence_score":0.0-1.0}}'
            )},
        ]
        response = await self.architect.achat(messages)
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

    async def _step_escalate_to_claude(
        self, session: AGPSession, summary: SessionSummary
    ) -> tuple[SessionSummary, bool]:
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
            f"Domain: {session.domain.value}\n"
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
        cited = self._provenance.cites_verified_url(content) or \
            self._provenance.cites_verified_url(parsed.get("conclusion", "") if parsed else "")

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
            cited = self._provenance.cites_verified_url(content)
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

    async def _step_manager_review(
        self, session: AGPSession, summary: SessionSummary, used_tools: bool
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
                f"Domain: {session.domain.value} | Scope: {session.scope}\n"
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
        response = await self.manager.achat(messages)
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
