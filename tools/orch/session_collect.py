"""AGP session evidence collection (extracted from tools.orch.session_steps).

``step_collect_evidence`` is the PRIMARY_COLLECTION step: wiki priors,
Claude Code primary extraction, local-model fallback with tool calls,
then provenance relabel from the ledger. ``session_steps`` keeps a thin
``async def step_collect_evidence`` delegate so ``run_session_flow`` and
existing patches stay on that module.

``execute_tool`` stays in session_steps (also used by Orchestrator).

Do not pull in the autonomous loop module. Do not arm live betting.
Do not add live to paper-signal.
"""
from __future__ import annotations

import json
import logging
import os

import aiosqlite

from agp import Evidence, SourceClass
from agp.provenance import relabel_evidence
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE
from inference import _parse_json_response, escalate_with_ladder
from tools.claude_code import is_available as claude_available
from tools.orch.pipeline_support import (
    MAX_TOOL_CALL_ROUNDS,
    _clamp_confidence,
    _default_registry,
    _json_compact,
    _safe_parse,
)
from tools.orch.session_steps import execute_tool
from tools.orch.tool_schemas import HERMES_TOOL_PROMPT

logger = logging.getLogger("callisto.orchestrator")


async def step_collect_evidence(
    orch, session, search_results: list[dict], arch_prompt: str
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
    from agp.provenance import ProvenanceLedger

    architect = orch.architect
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
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n\n"
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
    if not architect.config.supports_native_tools:
        tool_prompt = HERMES_TOOL_PROMPT

    messages = [
        {"role": "system", "content": arch_prompt},
        {"role": "user", "content": (
            f"Domain: {(session.domain.value if session.domain else 'UNASSIGNED')} | Scope: {session.scope}\n\n"
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
    response = await architect.achat(
        messages, tools=available_tools, options={"num_predict": 2048}
    )

    # Handle tool calls
    for _ in range(MAX_TOOL_CALL_ROUNDS):
        if not response.get("tool_calls"):
            break
        used_tools = True
        for tc in response["tool_calls"]:
            result = await execute_tool(tc["name"], tc["arguments"])
            # Record the real tool return so its URLs/bytes carry
            # provenance (the model cannot add to this ledger).
            try:
                payload = result if isinstance(result, str) else json.dumps(result)
                ledger.record_tool_result(tc["name"], payload)
            except (TypeError, ValueError):
                pass
            messages.append({"role": "assistant", "content": response["content"] or ""})
            messages.append({
                "role": "tool" if architect.config.supports_native_tools else "user",
                "content": f"Tool result for {tc['name']}:\n" + (
                    json.dumps(result) if not isinstance(result, str) else result
                ),
            })
        response = await architect.achat(
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
