"""AGP pipeline support helpers (extracted from orchestrator.py).

Pure helper functions, parallel-search staging, domain query refinement, and
the process-wide ToolRegistry seed. No Orchestrator class state involved.
"""

import asyncio
import json
import logging
from typing import Optional

from agp import Domain
from agp.thresholds import (
    MAX_CONFIDENCE_BY_SOURCE,
    MAX_CONFIDENCE_NO_TOOL,
)
from tools.search import web_search
from tools.domain_registry import get_tool_registry
from tools.domains.sports import build_sports_plugin
from tools.domains.compute import register_if_available as _register_compute

from tools.orch.tool_schemas import WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL, ODDS_TOOLS
from tools.orch.sports_dispatch import _sports_tool_dispatch

logger = logging.getLogger("callisto.orchestrator")

# Max native tool-call rounds per evidence-collection step
MAX_TOOL_CALL_ROUNDS = 3


import re as _re

from tools.domain_registry import get_tool_registry
from tools.domains.sports import build_sports_plugin
from tools.domains.compute import register_if_available as _register_compute


def _default_registry():
    """Build the process-wide ToolRegistry. Registration is the extension
    point: adding a domain = register a plugin here, never edit the loop."""
    global _registry_seeded
    if not _registry_seeded:
        reg = get_tool_registry()
        reg.core_tools[:] = [WEB_SEARCH_TOOL, CLAUDE_CODE_TOOL]
        reg.register(build_sports_plugin(ODDS_TOOLS, _execute_sports_tool))
        # B2's sandboxed compute (build/sandbox-artifacts) when merged.
        _register_compute(reg)
        _registry_seeded = True
        return reg
    return get_tool_registry()


_registry_seeded = False



async def _execute_sports_tool(name: str, arguments: dict):
    """Sports tool dispatcher (moved verbatim from Orchestrator._execute_tool;
    it uses no instance state). Lives behind the plugin boundary now."""
    return await _sports_tool_dispatch(name, arguments)


# (Freshness patterns moved to tools/domains/sports.py — plugin-supplied now.)


def _detect_freshness(query: str) -> Optional[str]:
    """Return Brave freshness filter for freshness-sensitive queries.

    Freshness rules are supplied by registered DomainPlugins (formerly a
    hardcoded team-name regex). A security query mentioning "Warriors" no
    longer gets mis-freshened unless a plugin claims it.
    """
    return _default_registry().freshness_for(query)


# Compact JSON serialization — fewer tokens in prompts
_json_compact = lambda obj: json.dumps(obj, separators=(",", ":"))


def _safe_parse(response: dict, fallback=None):
    """Extract parsed JSON from inference response, with fallback.

    Normalizes list-wrapped responses: if the model returns a JSON array
    containing a single dict, unwrap it automatically.
    """
    parsed = response.get("parsed_json")
    if parsed is None:
        return fallback
    if isinstance(parsed, list) and len(parsed) == 1 and isinstance(parsed[0], dict):
        return parsed[0]
    return parsed


def _parse_domain(text: str) -> Domain:
    """Parse a domain from text, defaulting to GENERAL."""
    text_upper = text.upper().strip()
    for domain in Domain:
        if domain.value in text_upper:
            return domain
    return Domain.GENERAL


def _clamp_confidence(score: float, best_source_class: str = "INFERRED") -> float:
    """Enforce confidence ceiling based on the best source class available.

    This is the hard enforcement layer — code, not policy.
    A model cannot self-report higher confidence than its evidence warrants.
    """
    score = max(0.0, min(1.0, score))
    ceiling = MAX_CONFIDENCE_BY_SOURCE.get(best_source_class, MAX_CONFIDENCE_NO_TOOL)
    score = min(score, ceiling)
    return round(score, 2)


def _best_source_class(evidence: list, used_tools: bool) -> str:
    """Determine the best (most authoritative) source class from evidence."""
    if not evidence:
        # Tools used but no evidence collected → SECONDARY (not INFERRED)
        return "SECONDARY" if used_tools else "INFERRED"
    rank = {"PRIMARY": 4, "SECONDARY": 3, "SIGNAL": 2, "INFERRED": 1}
    best = "INFERRED"
    for ev in evidence:
        sc = ev.source_class.value if hasattr(ev, "source_class") else ev.get("source_class", "INFERRED")
        if rank.get(sc, 0) > rank.get(best, 0):
            best = sc
    return best


def _dedup_search_results(results: list[dict]) -> list[dict]:
    """Deduplicate search results by URL, keeping the first occurrence."""
    seen_urls = set()
    deduped = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            deduped.append(r)
    return deduped



async def run_searches_parallel(
    queries: list[str], freshness: Optional[str] = None
) -> list[dict]:
    """Run multiple web search queries in parallel with optional freshness filter."""
    async def _single_search(q: str) -> list[dict]:
        try:
            result = await web_search(q, count=5, freshness=freshness)
            return [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "description": r.get("description", ""),
                    "source_class": "SECONDARY",
                }
                for r in result.get("results", [])
            ]
        except Exception as e:
            logger.warning(f"Brave search failed for '{q}': {e}")
            return []

    # SECURITY (audit H-13): return_exceptions=True so a single failed query
    # doesn't crash the whole batch. _single_search already returns [] on
    # caught exceptions, but defense-in-depth covers any unhandled raise.
    results_lists = await asyncio.gather(
        *[_single_search(q) for q in queries],
        return_exceptions=True,
    )
    out: list = []
    for entry in results_lists:
        if isinstance(entry, Exception):
            logger.warning(f"web_search subquery raised: {entry!r}")
            continue
        out.extend(entry)
    return out


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
