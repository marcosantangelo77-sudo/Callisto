"""
Centralized kill-switch for CALLISTO_LOCAL_ONLY mode.

`is_local_only()` is the single source of truth. Every call site that
might otherwise dispatch a Claude / Anthropic / cloud LLM request must
route through this helper BEFORE reading any API key or initiating any
network I/O. When it returns True, callers must fall back to a local
alternative (Ollama via inference.py / local_cc_bridge) or return a
clean structured result — never silently no-op, never raise unhandled.

The env var is read live on every call so tests and admin endpoints
can flip it without a restart. Truthy values: "1", "true", "yes", "on"
(case-insensitive, surrounding whitespace stripped). Anything else
(including unset) is treated as False.
"""

from __future__ import annotations

import os

_TRUE = frozenset({"1", "true", "yes", "on"})


def is_local_only() -> bool:
    """Return True iff CALLISTO_LOCAL_ONLY is set to a truthy value."""
    val = os.getenv("CALLISTO_LOCAL_ONLY", "")
    return val.strip().lower() in _TRUE


def local_only_result(
    reason: str = "CALLISTO_LOCAL_ONLY kill switch active",
    extra: dict | None = None,
) -> dict:
    """Uniform structured result for blocked Claude calls.

    Shape matches `inference.escalate_with_ladder` / `claude_code_query`
    so callers can pattern-match on `.get("error")` and `.get("content")`
    without special-casing local-only mode.
    """
    base = {
        "content": "",
        "model_used": "none",
        "source_class": "PRIMARY",
        "quality": "none",
        "ladder_step": -1,
        "call_number": 0,
        "error": "blocked_by_local_only",
        "rate_limited": False,
        "local_only": True,
        "reason": reason,
    }
    if extra:
        base.update(extra)
    return base


__all__ = ["is_local_only", "local_only_result"]
