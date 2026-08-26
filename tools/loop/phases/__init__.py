"""Extracted ResearchLoop phase implementations.

``tools.loop.phases_impl`` re-exports these names so
``from tools.loop.phases_impl import phase_*`` stays valid.
This package must never import ``tools.autonomous``.
"""
from tools.loop.phases.post_live import (  # noqa: F401
    phase_claude_deep_work,
    phase_granger_analysis,
    phase_integrity_check,
    phase_knowledge_compile,
    phase_knowledge_lint,
    phase_narrative_edges,
    phase_regime_analysis,
    phase_review_live,
    phase_system_improvement,
    phase_system_watchdog,
)
from tools.loop.phases.pre_live import (  # noqa: F401
    phase_interpret_backtests,
    phase_paper_trade,
)
