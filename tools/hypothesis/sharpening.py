"""
tools.hypothesis.sharpening — fire-and-forget wiki hook on terminal status.

Split out of tools/hypothesis.py (facade re-exports everything).
"""
from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from tools.hypothesis.manager import HypothesisManager

logger = logging.getLogger("callisto.hypothesis")


_SHARPENING_TERMINAL = {"rejected", "retired", "live"}


def _fire_sharpening_hook(
    mgr: "HypothesisManager",
    hypothesis_id: str,
    prev_status: Optional[str],
    new_status: str,
) -> None:
    """Fire a best-effort sharpening wiki write when a hypothesis reaches
    a terminal-ish status. Safe no-op on any error.

    Enable with:  CALLISTO_SHARPENING_HOOK=1  (default off).
    """
    import os as _os
    if _os.getenv("CALLISTO_SHARPENING_HOOK", "0") != "1":
        return
    if new_status not in _SHARPENING_TERMINAL:
        return
    try:
        import asyncio as _asyncio
        from tools.hypothesis_generator import HypothesisGenerator
        from tools.embeddings import VectorStore

        async def _run() -> None:
            try:
                vs = VectorStore(mgr.db_path)
                await vs.initialize()
                gen = HypothesisGenerator(mgr, vs, db_path=mgr.db_path)
                await gen.initialize()
                try:
                    outcome = (
                        "success" if new_status in {"live", "retired"}
                        else "failure"
                    )
                    await gen.record_backtest_outcome_to_wiki(
                        hypothesis_id, outcome,
                        stats={"prev_status": prev_status,
                               "new_status": new_status},
                    )
                finally:
                    await gen.close()
                    await vs.close()
            except Exception as e:
                logger.debug(f"sharpening hook inner error: {e}")

        try:
            loop = _asyncio.get_running_loop()
            loop.create_task(_run())
        except RuntimeError:
            # No running loop — run a fresh one synchronously
            _asyncio.run(_run())
    except Exception as e:
        logger.debug(f"sharpening hook dispatch error: {e}")
