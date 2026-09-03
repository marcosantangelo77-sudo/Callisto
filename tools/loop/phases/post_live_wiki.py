"""Knowledge wiki compile/lint ResearchLoop phases, extracted from post_live.

Callers still import these names from tools.loop.phases.post_live / phases_impl.
This module must never import tools.autonomous (circular).
phase_live_execute stays defined in the phases_impl facade (not relocated).

These phases compile and lint the knowledge wiki. They do not arm live
betting and do not add live to paper-signal.
"""
from __future__ import annotations

from tools.loop import phases_impl as _impl

logger = _impl.logger


async def phase_knowledge_compile(loop) -> None:
    self = loop
    """Knowledge wiki compilation — LLM Wiki pattern (Karpathy).

    Reads recent sessions/evidence/learnings and compiles them into
    persistent, cross-referenced wiki articles. Knowledge compounds
    instead of being re-discovered each time.

    Runs every COMPILE_INTERVAL_CYCLES (7) — coprime with lint (11).
    Uses Gemma 4 (local, free) for compilation.
    """
    from tools.knowledge_wiki import get_wiki, COMPILE_INTERVAL_CYCLES

    if self._cycles % COMPILE_INTERVAL_CYCLES != 0:
        return

    db = self.data_collector._db
    if not db:
        return

    try:
        wiki = get_wiki()
        stats = await wiki.compile(db, self._cycles)
        created = stats.get("articles_created", 0)
        updated = stats.get("articles_updated", 0)
        if created or updated:
            logger.info(
                f"Wiki compile: {created} new articles, {updated} updated"
            )
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_compile", True)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Knowledge compile phase failed: {e}")
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_compile", False)
        except Exception:
            pass


async def phase_knowledge_lint(loop) -> None:
    self = loop
    """Knowledge wiki lint — detect contradictions, stale claims, orphans.

    Scans wiki articles for:
      - Contradictions: conflicting claims between articles
      - Stale articles: not updated in >72 hours
      - Orphans: articles with no cross-references

    Runs every LINT_INTERVAL_CYCLES (11) — coprime with compile (7).
    Uses Qwen 3.5 4B (ultra-fast classifier) for contradiction detection.
    """
    from tools.knowledge_wiki import get_wiki, LINT_INTERVAL_CYCLES

    if self._cycles % LINT_INTERVAL_CYCLES != 0:
        return

    db = self.data_collector._db
    if not db:
        return

    try:
        wiki = get_wiki()
        stats = await wiki.lint(db, self._cycles)

        # Alert on high-severity contradictions
        contradictions = stats.get("contradictions_found", 0)
        if contradictions > 0:
            try:
                from tools import telegram
                await telegram.alert_system(
                    f"Wiki lint: {contradictions} contradictions detected. "
                    f"Stale: {stats.get('stale_articles', 0)}, "
                    f"Orphans: {stats.get('orphan_articles', 0)}"
                )
            except Exception:
                pass

        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_lint", True)
        except Exception:
            pass
    except Exception as e:
        logger.warning(f"Knowledge lint phase failed: {e}")
        try:
            from tools.pipeline_integrity import get_checker
            get_checker().record_phase_result("knowledge_lint", False)
        except Exception:
            pass
