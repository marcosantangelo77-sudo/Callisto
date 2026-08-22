"""Source-registry DomainPlugin — surfaces the registry to sessions.

Tools exposed to the model:
    source_registry_list()
        every registered source with tier, answers, and honest
        cannot-answer limits — this is what stops the model from guessing
        at sources or stopping early.
    source_registry_select(question_type, max_tier)
        which sources can answer a given kind of question.

Actual fetching tools per source are added incrementally; the registry +
select tool is the routing layer. Fetches run in an executor (urllib is
blocking) exactly like the finance plugin.
"""

from __future__ import annotations

import asyncio
import logging
import re

from tools.domain_registry import DomainPlugin
from tools.sources.registry import get_source_registry

logger = logging.getLogger("callisto.source_plugin")

LIST_TOOL = {
    "type": "function",
    "function": {
        "name": "source_registry_list",
        "description": (
            "List every registered data source with its provenance tier "
            "(1=primary structured, 2=primary documents, 4=secondary), "
            "what kinds of questions it CAN answer, and — critically — "
            "what it CANNOT answer. Consult before searching: a strong "
            "model with curated sources beats the same model guessing."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}

SELECT_TOOL = {
    "type": "function",
    "function": {
        "name": "source_registry_select",
        "description": (
            "Given a kind of question (e.g. 'macro time series', 'trial "
            "outcomes', 'scholarly work search'), return the registered "
            "sources that can answer it, best-provenance first, up to "
            "max_tier."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question_type": {"type": "string"},
                "max_tier": {"type": "integer",
                             "description": "provenance-tier ceiling 1-5"},
            },
            "required": ["question_type"],
        },
    },
}


def _list_payload() -> dict:
    reg = get_source_registry()
    specs = reg.specs()
    return {
        "ok": True,
        "count": len(specs),
        "sources": sorted(specs, key=lambda s: (s["tier"], s["name"])),
    }


def _select_payload(question_type: str, max_tier: int) -> dict:
    reg = get_source_registry()
    picks = reg.select(question_type, max_tier=max(1, min(int(max_tier), 5)))
    return {
        "ok": True,
        "question_type": question_type,
        "sources": [s.to_dict() for s in picks],
        "note": ("If no source matches, say so — do NOT fall back to web "
                 "search without stating that no registered source covers "
                 "this question."),
    }


async def _execute(name: str, arguments: dict) -> dict:
    loop = asyncio.get_event_loop()

    def _run() -> dict:
        if name == "source_registry_list":
            return _list_payload()
        if name == "source_registry_select":
            return _select_payload(arguments.get("question_type", ""),
                                   int(arguments.get("max_tier", 5)))
        raise ValueError(f"source plugin does not own tool {name!r}")

    try:
        return await loop.run_in_executor(None, _run)
    except Exception as exc:
        logger.warning("source tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "tool": name}


def build_source_plugin() -> DomainPlugin:
    return DomainPlugin(
        name="sources",
        domains=set(),          # domain-general: joins via keywords only
        keywords=re.compile(
            r"\b(source registry|registered sources?|which source|"
            r"data source|macro series|fred|clinical trials?|"
            r"clinicaltrials|federal register|treasury|fiscal data|"
            r"\bBLS\b|\bCPI\b|\bunemployment rate\b|wikidata|gdelt|"
            r"openalex|patents?)\b",
            re.IGNORECASE,
        ),
        tool_schemas=[LIST_TOOL, SELECT_TOOL],
        freshness=[],
        execute=_execute,
    )


def register_if_available(registry) -> bool:
    """Register iff the sources package imports cleanly."""
    try:
        import tools.sources.adapters  # noqa: F401
        import tools.sources.registry  # noqa: F401
    except ImportError:
        logger.info("source plugin unavailable (modules not merged yet)")
        return False
    if "sources" not in {p.name for p in registry.plugins()}:
        registry.register(build_source_plugin())
    return True
