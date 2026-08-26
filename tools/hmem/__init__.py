"""tools.hmem — Hermes memory internals.

Split out of tools/hermes_memory.py (which remains the public facade):
- sanitize:  prompt-injection sanitizers for learning keys/values (audit C-4)
- identity:  the identity context section
- sections:  per-section context builders (bets, edges, patterns, active,
             research, learnings, messages, code changes)
- memory:    the HermesMemory class + singleton accessors
"""

from tools.hmem.identity import build_identity
from tools.hmem.memory import (
    CALLER_DEFAULT,
    CALLER_DEEP_WORK,
    CALLER_EDGE_ANALYSIS,
    CALLER_HYPOTHESIS_GEN,
    CALLER_TELEGRAM,
    DB_PATH,
    MESSAGES_FILE,
    HermesMemory,
    get_hermes_memory,
)
from tools.hmem.sanitize import sanitize_learning_key, sanitize_learning_value
from tools.hmem.sections import (
    build_active_state,
    build_bet_history,
    build_code_changes,
    build_edge_history,
    build_learned_patterns,
    build_learnings,
    build_messages,
    build_research_state,
)

__all__ = [
    "CALLER_DEFAULT",
    "CALLER_DEEP_WORK",
    "CALLER_EDGE_ANALYSIS",
    "CALLER_HYPOTHESIS_GEN",
    "CALLER_TELEGRAM",
    "DB_PATH",
    "MESSAGES_FILE",
    "HermesMemory",
    "get_hermes_memory",
    "sanitize_learning_key",
    "sanitize_learning_value",
    "build_identity",
    "build_bet_history",
    "build_edge_history",
    "build_learned_patterns",
    "build_active_state",
    "build_research_state",
    "build_learnings",
    "build_messages",
    "build_code_changes",
]
