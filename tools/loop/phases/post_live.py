"""Post-live_execute ResearchLoop phases, extracted from phases_impl.

Kept out of phases_impl so the live-execute env gate stays in the facade
module. Callers still import these names from tools.loop.phases_impl.

This module must never import tools.autonomous (circular).
"""
from __future__ import annotations

from tools.loop import phases_impl as _impl

# Shared cadence / wiki / regime state — defined on phases_impl before this
# module is imported (late import at the bottom of phases_impl).
_regime_cache = _impl._regime_cache
REGIME_ANALYSIS_INTERVAL = _impl.REGIME_ANALYSIS_INTERVAL
RESEARCH_SPORTS = _impl.RESEARCH_SPORTS
SYSTEM_IMPROVEMENT_INTERVAL = _impl.SYSTEM_IMPROVEMENT_INTERVAL
CLAUDE_ESCALATION_COOLDOWN = _impl.CLAUDE_ESCALATION_COOLDOWN
BACKTEST_GAP_DAYS = _impl.BACKTEST_GAP_DAYS
DEFAULT_TRAINING_WINDOW_DAYS = _impl.DEFAULT_TRAINING_WINDOW_DAYS

async def phase_review_live(loop) -> None:
    from tools.loop.phases.post_live_review import phase_review_live as _impl_fn
    return await _impl_fn(loop)


async def phase_narrative_edges(loop) -> None:
    from tools.loop.phases.post_live_review import phase_narrative_edges as _impl_fn
    return await _impl_fn(loop)


async def phase_claude_deep_work(loop) -> None:
    from tools.loop.phases.claude_deep import phase_claude_deep_work as _impl_fn
    return await _impl_fn(loop)


async def phase_granger_analysis(loop) -> None:
    from tools.loop.phases.regime_granger import phase_granger_analysis as _impl_fn
    return await _impl_fn(loop)


async def phase_regime_analysis(loop) -> None:
    from tools.loop.phases.regime_granger import phase_regime_analysis as _impl_fn
    return await _impl_fn(loop)


async def phase_knowledge_compile(loop) -> None:
    from tools.loop.phases.post_live_wiki import phase_knowledge_compile as _impl_fn
    return await _impl_fn(loop)


async def phase_knowledge_lint(loop) -> None:
    from tools.loop.phases.post_live_wiki import phase_knowledge_lint as _impl_fn
    return await _impl_fn(loop)


async def phase_system_improvement(loop) -> None:
    from tools.loop.phases.system_improve import phase_system_improvement as _impl_fn
    return await _impl_fn(loop)


async def phase_system_watchdog(loop) -> None:
    from tools.loop.phases.post_live_watch import phase_system_watchdog as _impl_fn
    return await _impl_fn(loop)


async def phase_integrity_check(loop) -> None:
    from tools.loop.phases.post_live_watch import phase_integrity_check as _impl_fn
    return await _impl_fn(loop)
