"""SelfRepairEngine — detect, fix, verify, record. Composed from mixins."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .config import SCRAPERS, _disabled_scrapers
from .detectors import DetectorsMixin
from .findings import FindingsMixin
from .fixes import FixesMixin

logger = logging.getLogger("callisto.self_repair")


class SelfRepairEngine(DetectorsMixin, FixesMixin, FindingsMixin):
    """Autonomous self-repair — detect, fix, verify, record."""

    # Kept as a backwards-compatible alias of FindingsMixin.FINDING_PATTERNS.
    _FINDING_PATTERNS = FindingsMixin.FINDING_PATTERNS

    @staticmethod
    def _classify_finding(description: str) -> str:
        return FindingsMixin.classify_finding(description)

    def __init__(self):
        self._cycle_count = 0
        self._total_fixes = 0
        self._last_run: Optional[str] = None

    async def run_repair_cycle(self) -> dict:
        """Main entry point — called by research loop each cycle."""
        self._cycle_count += 1
        start = time.monotonic()
        issues = await self._detect_issues()
        results = [await self._repair(i) for i in issues]
        fixed = sum(1 for r in results if r["fixed"])
        self._total_fixes += fixed
        self._last_run = datetime.now(timezone.utc).isoformat()
        elapsed = time.monotonic() - start
        if issues:
            logger.info(f"Self-repair #{self._cycle_count}: {fixed}/{len(issues)} fixed ({elapsed:.1f}s)")
        return {"issues_found": len(issues), "fixed": fixed, "elapsed_seconds": round(elapsed, 2),
                "cycle": self._cycle_count, "results": results}

    def get_status(self) -> dict:
        return {"cycles": self._cycle_count, "total_fixes": self._total_fixes,
                "last_run": self._last_run,
                "disabled_scrapers": {n: round(max(0, t - time.monotonic()), 0)
                                      for n, t in _disabled_scrapers.items() if t > time.monotonic()}}

    async def _repair(self, issue: dict) -> dict:
        itype = issue.get("type", "unknown")
        # GATE GUARD: refuse any strategy whose purpose is to weaken a gate.
        if itype in ("high_rejection", "signal_drought"):
            return self._refuse_gate_change(
                itype,
                f"Detector '{itype}' maps to threshold lowering — refused by gate policy. "
                f"Diagnosis recorded for human review instead.",
                detail=issue,
            )
        fn = {"scraper_broken": self._fix_scraper, "stale_odds": self._fix_stale_odds,
              "empty_backtests": self._fix_empty_bt, "claude_stuck": self._fix_claude,
              "premature_rejection": self._fix_premature_rejection,
              "resolution_broken": self._fix_resolution_broken,
              "db_bloat": self._fix_bloat}.get(itype)
        if not fn:
            return {"fixed": False, "action": "no_strategy", "detail": itype}
        try:
            result = await fn(issue)
        except Exception as e:
            result = {"fixed": False, "action": "repair_error", "detail": str(e)}
        await self._record_to_hermes(itype, result)
        return result

    def _refuse_gate_change(self, strategy: str, reason: str, detail=None) -> dict:
        """Record a refused gate-weakening action for human review. Never executes."""
        logger.warning(f"Gate policy REFUSED strategy '{strategy}': {reason}")
        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        result = {"fixed": False, "action": "gate_change_refused",
                  "detail": f"{reason} | evidence: {str(detail)[:300]}"}
        if loop is not None:
            loop.create_task(self._record_to_hermes(f"gate_refused_{strategy}", result))
        return result

    async def _record_to_hermes(self, itype: str, result: dict) -> None:
        try:
            from tools.hermes_memory import get_hermes_memory
            h = get_hermes_memory()
            fixed = result.get("fixed", False)
            val = (f"{'FIXED' if fixed else 'UNFIXED'} [{result.get('action','')}] "
                   f"{result.get('detail','')} (#{self._cycle_count}, "
                   f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})")
            await h.record_learning(key=f"self_repair_{itype}", value=val,
                                    confidence=0.8 if fixed else 0.4, source="self_repair")
        except Exception as e:
            logger.debug(f"Hermes record failed: {e}")


_engine: Optional[SelfRepairEngine] = None


def get_repair_engine() -> SelfRepairEngine:  # singleton
    global _engine
    if _engine is None:
        _engine = SelfRepairEngine()
    return _engine
