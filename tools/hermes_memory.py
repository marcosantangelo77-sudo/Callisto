"""
Hermes — Callisto's nervous system. Persistent memory + bidirectional bridge.

Hermes is NOT just a context builder. It is the continuous memory layer that:
1. READS state → builds context for every Claude call (identity, bets, edges, research, code)
2. WRITES back ← Claude stores discoveries, learnings, and insights after each call
3. PRIORITIZES sections based on caller intent (hypothesis gen vs edge analysis vs deep work)
4. NOTIFIES across sessions via a message queue (cross-session awareness)

Every Claude CLI subprocess gets Hermes context automatically via the bridge
in claude_code.py. No call is stateless. No session is blind.

Memory sections:
1. IDENTITY       — who Callisto is, rules, capabilities
2. BETS           — bankroll, open bets, P/L, CLV track record
3. EDGES          — recent +EV opportunities, analysis sessions
4. PATTERNS       — learned market/book patterns from operation
5. ACTIVE STATE   — open bets, current monitoring
6. RESEARCH       — hypothesis counts, top tested, recently disproven
7. CODE CHANGES   — git commits, uncommitted modifications (cross-session awareness)
8. LEARNINGS      — discoveries Claude has made (bidirectional memory)
9. MESSAGES       — cross-session notification queue

2026-08 split: the implementation moved to ``tools/hmem/`` (sanitize,
identity, sections, memory). This module is kept as the stable public
import surface — every name below re-exports from tools.hmem.
"""

from tools.hmem.memory import (  # noqa: F401
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
from tools.hmem.sanitize import (  # noqa: F401
    sanitize_learning_key,
    sanitize_learning_value,
)
from tools.hmem.sections import (  # noqa: F401
    build_active_state,
    build_bet_history,
    build_code_changes,
    build_edge_history,
    build_learned_patterns,
    build_learnings,
    build_messages,
    build_research_state,
)


# Backward compat
def get_cache_manager():
    """Get the tiered CacheManager (hot/warm/cold). Preferred over HermesMemory."""
    from tools.cache_manager import get_cache_manager as _get_cm
    return _get_cm()
