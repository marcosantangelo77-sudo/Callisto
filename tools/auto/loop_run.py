"""AutonomousLoop main-cycle body extracted from tools.auto.loop.

``AutonomousLoop._loop`` stays defined on the class as a thin async
delegate so slice3/slice4 method-name pins keep passing. The body lives
here so tools/auto/loop.py can keep shrinking without changing behaviour.

Do not import the autonomous facade (no cycles).
Do not arm live betting. Do not add live to paper-signal.
"""
from __future__ import annotations

import asyncio
import logging

from tools.auto.loop import ANALYSIS_COOLDOWN

logger = logging.getLogger("callisto.autonomous")


async def run_loop(loop) -> None:
    """Main loop — find edges, reason about them, alert if worthy."""
    self = loop
    # Wait for first snapshot cycle to populate data
    await asyncio.sleep(30)

    while self._running:
        try:
            self._loop_cycle += 1

            # Run market psychology analysis on latest snapshots
            self._run_market_psychology()

            # Refresh injury caches for active sports
            all_reports = self.line_monitor.get_edge_report()
            if isinstance(all_reports, dict):
                for _sport_key in all_reports:
                    await self._refresh_injury_cache(_sport_key)

            # Run parlay/SGP correlation scan every 4 cycles
            if self._loop_cycle % 4 == 0:
                await self._phase_parlay_correlation_scan()

            candidates = self._find_analysis_candidates()

            if candidates:
                logger.info(
                    f"Autonomous: {len(candidates)} edge candidates found, "
                    f"analyzing top {min(len(candidates), 3)}"
                )

                # Analyze top candidates sequentially (GPU bound)
                for candidate in candidates[:3]:
                    if not self._running:
                        break
                    await self._analyze_edge(candidate)

            # Clean up old dedup entries
            self._cleanup_dedup()

            await asyncio.sleep(ANALYSIS_COOLDOWN)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Autonomous loop error: {e}", exc_info=True)
            await asyncio.sleep(30)
