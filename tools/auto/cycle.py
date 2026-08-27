"""Paper research cycle extracted from tools.auto.research.CycleLoopMixin.

``CycleLoopMixin._loop`` is the 24/7 ResearchLoop cycle (sequencer PHASES
then PERIODIC_PHASES, progress check, prune, sleep). ``_quant_scan_loop``
refreshes the paper edge surface on a market cadence. The mixin is
re-exported from tools.auto.research so ResearchLoop composition and
``hasattr(research_mod, "CycleLoopMixin")`` stay intact.

Do not import tools.autonomous (cycle). Do not arm live betting.
Do not add live to paper-signal. Quant scan does not enable bet_executor.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from tools.loop.phases_impl import RESEARCH_CYCLE_INTERVAL, RESEARCH_SPORTS
from tools.loop.sequencer import PERIODIC_PHASES, PHASES

logger = logging.getLogger("callisto.auto.research")


class CycleLoopMixin:

    async def _quant_scan_loop(self) -> None:
        """Continuously refresh the live edge surface.

        Every ``QUANT_SCAN_INTERVAL_S`` seconds, pull current odds for
        every research sport, build per-market snapshots across all
        available books, run the ranker, and persist the output. The
        resulting table (``live_edge_surface``) is what the /edges/live
        API endpoint reads, what the Telegram alerting can consume, and
        what the bet_executor will read once it's enabled.

        Runs independently of the main research cycle so the two
        cadences don't fight each other. Research cycle is human-scale
        (5 min, statistical work). Quant scan is market-scale (60s,
        line movement and soft-book divergence).
        """
        import os as _os
        interval = float(_os.getenv("CALLISTO_QUANT_SCAN_INTERVAL_S", "60"))
        # Brief startup delay so the main loop wins initial DB contention
        # and telemetry collectors have a chance to populate.
        await asyncio.sleep(30)

        from tools.quant import scan_all_sports
        while self._running:
            if self._paused:
                await asyncio.sleep(min(interval, 15))
                continue
            try:
                db = self.data_collector._db if self.data_collector else None
                if db is None:
                    await asyncio.sleep(interval)
                    continue
                result = await scan_all_sports(
                    list(RESEARCH_SPORTS),
                    db,
                    placement_books={"draftkings", "fanatics"},
                    min_recommend_edge=0.02,
                    top_n_per_sport=25,
                )
                total = result.get("total_recommended", 0)
                if total:
                    logger.info(
                        f"Quant scan: {total} recommended edges across "
                        f"{sum(1 for r in result['per_sport'].values() if r.get('recommended'))} "
                        f"sports"
                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Quant scan loop iteration failed: {e}")
            await asyncio.sleep(interval)

    async def _loop(self) -> None:
        """Main research cycle."""
        # Brief delay to let other systems start
        await asyncio.sleep(15)

        while self._running:
            try:
                self._cycles += 1
                self._reactive_collected.clear()
                _cycle_start = time.monotonic()
                logger.info(f"Research cycle #{self._cycles} starting")

                # Pause check — sleep and skip cycle
                if self._paused:
                    logger.info(f"Research cycle #{self._cycles} skipped (PAUSED)")
                    await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)
                    continue

                # ── Pause line_monitor for ENTIRE cycle to prevent SQLite lock cascade.
                # All phases do DB writes; concurrent line_monitor snapshots cause
                # deadlocks even with 120s busy_timeout. Snapshots catch up between cycles.
                # wait_for_drain() sets _paused, waits for loop ack AND in-flight DB
                # ops to complete — no more fire-and-forget WAL contention.
                if self.line_monitor:
                    drained = await self.line_monitor.wait_for_drain(timeout=30)
                    if drained:
                        logger.debug("line_monitor paused and drained for research cycle")
                    else:
                        logger.warning("line_monitor drain incomplete — proceeding (may contend on WAL)")

                # ── Sequential phases — order lives in tools.loop.sequencer ──
                # Each phase runs under its own wait_for timeout; failures are
                # recorded non-fatally via the phase-failure ledger.
                for spec in PHASES:
                    if spec.every_n and self._cycles % spec.every_n != 0:
                        continue
                    try:
                        coro = getattr(self, spec.method)()
                        if spec.timeout is None:
                            await coro
                        else:
                            await asyncio.wait_for(coro, timeout=spec.timeout)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Phase {spec.name} timed out after {spec.timeout}s — skipping"
                        )
                        self._record_phase_failure(spec.name, "timeout")
                    except Exception as e:
                        logger.warning(f"Phase {spec.name} failed (non-fatal): {e}")
                        self._record_phase_failure(spec.name, "exception", e)

                    if not self._running:
                        break

                if not self._running:
                    break

                # ── Periodic phases: defer if core phases already consumed >5 min ──
                # This prevents phase collision from stacking 10+ min cycles
                # (was causing stalls at cycles 6, 10, 15, 16, 20).
                _cycle_elapsed = time.monotonic() - _cycle_start
                _CYCLE_TIME_BUDGET = 300  # 5 min — if core phases took this long, skip periodic
                if _cycle_elapsed > _CYCLE_TIME_BUDGET:
                    logger.info(
                        f"Cycle #{self._cycles} core phases took {_cycle_elapsed:.0f}s "
                        f"(>{_CYCLE_TIME_BUDGET}s) — deferring periodic phases"
                    )
                else:
                    for spec in PERIODIC_PHASES:
                        if spec.every_n and self._cycles % spec.every_n != 0:
                            continue
                        try:
                            coro = getattr(self, spec.method)()
                            await asyncio.wait_for(coro, timeout=spec.timeout)
                        except asyncio.TimeoutError:
                            logger.warning(
                                f"Phase {spec.name} timed out after {spec.timeout}s — skipping"
                            )
                            self._record_phase_failure(spec.name, "timeout")
                        except Exception as e:
                            logger.warning(f"Phase {spec.name} failed (non-fatal): {e}")
                            self._record_phase_failure(spec.name, "exception", e)

                        if not self._running:
                            break

                    if not self._running:
                        break

                # ── Progress tracking: detect spinning ──
                await self._check_progress()

                _cycle_total = time.monotonic() - _cycle_start

                # Force garbage collection after each cycle — large numpy arrays
                # and JSON dicts from backtest processing don't always get freed promptly.
                # Also clear linecache (tracemalloc causes it to grow ~1.5 MB/session).
                gc.collect()
                gc.collect()  # Second pass catches reference cycles
                import linecache
                linecache.clearcache()

                # ── Memory telemetry: track RSS per cycle to detect leaks ──
                try:
                    import psutil
                    _rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                    logger.info(
                        f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s | "
                        f"RSS={_rss_mb:.0f}MB | KL_cache={len(self.line_monitor._kl_cache) if self.line_monitor else '?'}"
                    )
                except Exception:
                    logger.info(f"Research cycle #{self._cycles} completed in {_cycle_total:.0f}s")

                # Proactive DB prune — prop_snapshots grows 15K rows/hr,
                # backtest_events from rejected hypotheses bloat DB indefinitely
                try:
                    import aiosqlite
                    _prune_db = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
                    _prune_cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
                    async with aiosqlite.connect(_prune_db) as _pdb:
                        await _pdb.execute("PRAGMA busy_timeout = 60000")
                        await _pdb.execute(
                            "DELETE FROM prop_snapshots WHERE snapshot_time < ?",
                            (_prune_cutoff,)
                        )
                        await _pdb.execute(
                            "DELETE FROM deferred_work_queue WHERE status = 'done' AND created_at < ?",
                            (_prune_cutoff,)
                        )
                        # Prune backtest_events for rejected hypotheses (>2 days old)
                        # With 3192 rejected hyps, this recovers massive DB space
                        _pruned = await _pdb.execute(
                            "DELETE FROM backtest_events WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        _pruned_count = _pruned.rowcount
                        # Also prune backtest_runs for rejected hypotheses
                        await _pdb.execute(
                            "DELETE FROM backtest_runs WHERE hypothesis_id IN ("
                            "  SELECT hypothesis_id FROM hypotheses "
                            "  WHERE status = 'rejected' AND updated_at < ?"
                            ")",
                            (_prune_cutoff,)
                        )
                        await _pdb.commit()
                        if _pruned_count > 0:
                            logger.info(
                                f"DB prune: deleted {_pruned_count} backtest_events "
                                f"from rejected hypotheses"
                            )
                        # WAL checkpoint — prevents unbounded WAL growth (was 1.4GB).
                        # Persistent connections block wal_autocheckpoint; this fresh
                        # connection after commit can checkpoint freed pages.
                        try:
                            wal_result = await (await _pdb.execute(
                                "PRAGMA wal_checkpoint(TRUNCATE)"
                            )).fetchone()
                            if wal_result:
                                busy, log, ckpt = wal_result
                                if log > 0:
                                    logger.info(
                                        f"WAL checkpoint: {ckpt}/{log} pages "
                                        f"(busy={busy})"
                                    )
                        except Exception as wal_e:
                            logger.debug(f"WAL checkpoint: {wal_e}")
                except Exception:
                    pass  # Non-critical — self_repair will catch it

                # Force GC to reclaim large transient allocations from backtest/resolve
                # phases. CPython's pymalloc holds freed blocks; gc.collect() nudges
                # the allocator to release pages back to the OS.
                gc.collect()

                # ── Unpause line_monitor BEFORE sleeping so it can take snapshots
                # during the inter-cycle window. Previously this was in the finally
                # block which ran after the sleep, giving the monitor ~0ms to run.
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock atomically
                    self.line_monitor._pause_ack.clear()
                    logger.info("line_monitor unpaused for inter-cycle snapshot window")

                logger.info(
                    f"Research cycle #{self._cycles} complete — "
                    f"sleeping {RESEARCH_CYCLE_INTERVAL}s"
                )
                await asyncio.sleep(RESEARCH_CYCLE_INTERVAL)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Research loop error: {e}", exc_info=True)
                await asyncio.sleep(120)
            finally:
                # ── Safety net: always unpause on exception/cancel too ──
                if self.line_monitor:
                    self.line_monitor.resume()  # Releases snapshot lock if held
                    self.line_monitor._pause_ack.clear()
