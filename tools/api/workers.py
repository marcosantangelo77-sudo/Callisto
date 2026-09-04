"""Background-worker infrastructure (moved from api.py).

Holds the task-queue worker, the adaptive-timeout session runner, the WAL
checkpoint/memory-guardian loop, the restart-signal watcher, the ingestion
SLA watchdog, and the order maintenance cron. ``api.py`` keeps thin aliases
so existing callers/tests that poke ``api.task_worker`` /
``api._run_session_with_adaptive_timeout`` / ``api._AdaptiveTimeout``
keep working unchanged.

Module state is accessed through late ``from api import ...`` inside the
function bodies (never at import time) both to avoid a circular import and
so tests can ``monkeypatch.setattr(api, ...)`` singletons and tuning knobs
and have the loops observe the patched values on their next read.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime

import aiosqlite


# ---------------------------------------------------------------------------
# Internal-query classifier (pure function)
# ---------------------------------------------------------------------------

def _is_internal_query(query: str) -> bool:
    """Detect queries that reference internal state and don't need web search."""
    q = query.lower().strip()
    # Direct DB lookups
    if "backtest results for hypothesis" in q:
        return True
    # Internal pipeline operations
    internal_prefixes = (
        "synthesis override", "synthesis complete", "synthesis review",
        "deep work cycle", "cycle ", "re-run backtest", "fix ",
        "triage ", "investigate ", "run pipeline", "recycle ",
        "track hold", "process task", "reject hypothesis",
        "generate compound", "verify ", "lower threshold",
    )
    for prefix in internal_prefixes:
        if q.startswith(prefix):
            return True
    return False


def _task_blocked_by_local_only(research_loop) -> bool:
    """True when POST /task must not run the orchestrator (Claude/hosted).

    ``CALLISTO_LOCAL_ONLY`` (env) and ``research_loop._local_only`` both
    mean hosted/Claude dispatch is forbidden. Honor the env even when
    ``research_loop`` is None — otherwise a worker with no loop object
    would still call Claude under LOCAL_ONLY.

    Truthiness matches ``tools.infrouter.local_only.local_only_enabled``
    (1/true/yes). Do not import that module here: it pulls inference_kernel.
    """
    env_lo = os.getenv("CALLISTO_LOCAL_ONLY", "").strip().lower() in (
        "1", "true", "yes")
    if env_lo:
        return True
    return bool(research_loop and getattr(research_loop, "_local_only", False))


def _post_task_orchestrator_forbidden(short_circuit_result) -> bool:
    """True when POST /task must 403 instead of enqueueing Claude/hosted work.

    Wiki short-circuit hits are local (no orchestrator) and still allowed.
    """
    if short_circuit_result is not None:
        return False
    return _task_blocked_by_local_only(None)


# ---------------------------------------------------------------------------
# Auto-followup enqueueing
# ---------------------------------------------------------------------------

async def _maybe_auto_followup(parent_task_id: int, result: dict) -> None:
    """If a session concluded with INSUFFICIENT DATA and a clear next step, auto-queue follow-up.

    Hardened per feat/auto-followup-hardening:
      - depth cap (CALLISTO_MAX_FOLLOWUP_DEPTH, default 5)
      - fan-out cap (CALLISTO_MAX_FOLLOWUP_FANOUT, default 3)
      - quality gate (vague language / verbatim / entity-free rejected)
      - semantic dedup against recent queue (cosine ≥ threshold in 1h window)
      - chain budget (CALLISTO_MAX_CHAIN_BUDGET_USD, default $1.00)
      - ancestry tracking via task_queue.parent_task_id / root_task_id

    Every rejection is logged at WARNING or INFO so the gating is visible
    in api_stderr_*.log. Nothing in this path raises; auto-followup is
    always best-effort relative to the parent task's completion.
    """
    import logging

    logger = logging.getLogger("callisto.api")
    from api import memory, queue
    try:
        summary = result.get("summary", {})
        conclusion = summary.get("conclusion", "")
        confidence = summary.get("confidence_score", 1.0)

        # Only follow up on low-confidence results with explicit next steps
        if confidence > 0.50 or "INSUFFICIENT DATA" not in conclusion.upper():
            return

        # Extract next step from conclusion — look for "Next step:" or "Recommending:"
        next_step = ""
        for marker in ["Next step:", "next step:", "Recommending:", "NEXT STEP:"]:
            if marker in conclusion:
                next_step = conclusion.split(marker, 1)[1].strip()
                break

        if not next_step or len(next_step) < 20:
            return

        followup_query = f"AUTO-FOLLOWUP from task {parent_task_id}: {next_step}"

        # Evaluate guards against the live DB snapshot. Keep the scope
        # tight: one connection open just for the guard + insert path.
        from tools import followup_guard
        async with aiosqlite.connect(memory.db_path) as guard_db:
            await guard_db.execute("PRAGMA busy_timeout = 30000")
            decision = await followup_guard.evaluate_followup(
                guard_db, parent_task_id, followup_query
            )

        if not decision.allowed:
            # Audit trail: rejection reason always logged.
            logger.info(
                "auto_followup_rejected: parent=%d reason=%s",
                parent_task_id, decision.reason,
            )
            return

        # Enqueue via the normal TaskQueue path so WriteCoordinator is
        # used when active. Then stamp the followup bookkeeping columns
        # in a follow-up UPDATE (the coordinator's insert helper doesn't
        # accept extra columns today; keeping the two-step pattern avoids
        # a breaking change to task_queue.submit_task).
        task_id = await queue.submit_task(followup_query, priority=1)
        try:
            async with aiosqlite.connect(memory.db_path) as stamp_db:
                await stamp_db.execute("PRAGMA busy_timeout = 30000")
                await stamp_db.execute(
                    "UPDATE task_queue "
                    "SET followup_depth = ?, parent_task_id = ?, root_task_id = ? "
                    "WHERE task_id = ?",
                    (decision.depth, decision.parent_task_id,
                     decision.root_task_id, task_id),
                )
                await stamp_db.commit()
        except Exception as stamp_err:
            logger.warning(
                "auto_followup: stamp depth/parent failed for task %d: %r",
                task_id, stamp_err,
            )

        logger.info(
            "auto_followup_queued: task=%d parent=%d depth=%d root=%d",
            task_id, parent_task_id, decision.depth, decision.root_task_id,
        )
    except Exception as e:
        logger.warning(f"Auto-followup check failed (non-fatal): {e}")


# ---------------------------------------------------------------------------
# WAL checkpoint + memory guardian
# ---------------------------------------------------------------------------

async def wal_checkpoint_loop():
    """Periodic WAL checkpoint + memory guardian.

    Every 5 minutes:
    1. Checkpoint WAL to prevent bloat
    2. Check process memory — if RSS > 2GB, signal graceful restart
       The watchdog will pick us back up with fresh memory.
    """
    import logging

    logger = logging.getLogger("callisto.api")
    from api import DB_PATH
    MEMORY_RESTART_MB = 2048  # 2GB — restart before Windows kills us at ~3-4GB
    while True:
        try:
            await asyncio.sleep(300)  # 5 minutes

            # ── Memory Guardian ──
            try:
                import psutil
                rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                if rss_mb > MEMORY_RESTART_MB:
                    logger.warning(
                        f"MEMORY GUARDIAN: RSS={rss_mb:.0f}MB > {MEMORY_RESTART_MB}MB — "
                        f"requesting graceful restart to prevent OOM crash"
                    )
                    # Signal the watchdog to restart us (off-OneDrive state dir)
                    from tools.state_paths import restart_signal_path
                    restart_file = restart_signal_path()
                    with open(restart_file, "w", encoding="utf-8") as f:
                        f.write(f"memory_guardian: RSS={rss_mb:.0f}MB at {datetime.now()}")
                    # Give the signal file a moment to be detected, then exit cleanly
                    await asyncio.sleep(2)
                    logger.warning("MEMORY GUARDIAN: exiting for restart")
                    os._exit(0)  # Clean exit — watchdog restarts us
                elif rss_mb > MEMORY_RESTART_MB * 0.75:
                    logger.info(f"Memory check: {rss_mb:.0f}MB (warning threshold: {MEMORY_RESTART_MB}MB)")
            except ImportError:
                pass  # psutil not installed
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                row = await cursor.fetchone()
                if row:
                    busy, log_pages, checkpointed = row
                    wal_size_mb = (log_pages * 4096) / (1024 * 1024)
                    logger.info(
                        f"WAL checkpoint: busy={busy}, log={log_pages} pages "
                        f"({wal_size_mb:.1f} MB), checkpointed={checkpointed}"
                    )
                    # If PASSIVE couldn't checkpoint enough, try TRUNCATE with
                    # a dedicated connection and longer busy_timeout. PASSIVE
                    # never works when aiosqlite holds persistent readers.
                    if log_pages > 5000 and checkpointed < log_pages // 2:
                        async with aiosqlite.connect(DB_PATH) as trunc_db:
                            await trunc_db.execute("PRAGMA busy_timeout = 30000")
                            cursor2 = await trunc_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                            row2 = await cursor2.fetchone()
                            if row2:
                                t_busy, t_log, t_ckpt = row2
                                logger.info(
                                    f"WAL TRUNCATE checkpoint: busy={t_busy}, "
                                    f"log={t_log}, checkpointed={t_ckpt}"
                                )
                                if t_busy and t_log > 0:
                                    logger.warning(
                                        f"WAL TRUNCATE could not complete: {t_log} pages remain. "
                                        f"Persistent readers are preventing checkpoint."
                                    )
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"WAL checkpoint failed (non-fatal): {e}")


async def restart_signal_watcher():
    """Poll the off-OneDrive restart-signal file and exit cleanly when found.

    The watchdog also polls this file and will restart the API.  Having the
    API self-consume the signal (via os._exit(0)) decouples restart from
    watchdog liveness — if the watchdog is frozen, the API still cycles
    itself, and the *restarted* watchdog reclaims oversight on the next spawn.
    """
    import logging

    logger = logging.getLogger("callisto.api")
    from tools.state_paths import restart_signal_path
    signal_path = restart_signal_path()
    while True:
        try:
            await asyncio.sleep(10)
            if signal_path.exists():
                try:
                    reason = signal_path.read_text(encoding="utf-8").strip()
                except Exception:
                    reason = "(unreadable)"
                logger.warning(
                    f"RESTART SIGNAL detected at {signal_path} — exiting process. Reason: {reason!r}"
                )
                # We do NOT unlink the file here — the watchdog also consumes
                # it, and whichever component starts first after exit will
                # clear it.  Unlinking here would race with the watchdog's
                # own restart flow.
                await asyncio.sleep(0.5)
                os._exit(0)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"restart_signal_watcher tick failed (non-fatal): {e}")


# ──────────────────────────────────────────────────────────────
# Ingestion SLA watchdog — self-healing observability
# ──────────────────────────────────────────────────────────────
# Every 5 minutes, check tools/health.py::resolve_sla_seconds + the
# ingestion_runs ledger for sources past 3x SLA. When we find one,
# self-submit a research task asking Callisto to investigate. This
# turns a silent data drop into a first-class research query — the
# AGP pipeline will pull evidence, contradictions, and form a finding.
#
# The set `_sla_alerted_sources` dedupes — one alert per source until
# it recovers. If we didn't dedupe, a 6-hour ESPN outage would flood
# the task queue with 72 identical tasks and starve real work.
_sla_alerted_sources: set[str] = set()
INGESTION_SLA_CHECK_INTERVAL_S = 300  # 5 min


async def ingestion_sla_watchdog_loop():
    """Periodic SLA audit. Self-submits /task queries on breach."""
    import logging

    logger = logging.getLogger("callisto.api")
    from api import DB_PATH, queue
    from tools.health import resolve_sla_seconds, CRITICAL_MULTIPLIER

    while True:
        try:
            await asyncio.sleep(INGESTION_SLA_CHECK_INTERVAL_S)

            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("PRAGMA busy_timeout = 10000")
                    cursor = await db.execute(
                        "SELECT name FROM sqlite_master "
                        "WHERE type='table' AND name='ingestion_runs'"
                    )
                    if not await cursor.fetchone():
                        continue

                    cursor = await db.execute(
                        "SELECT source, status, "
                        "  (julianday('now') - julianday(finished_at)) * 86400 AS age_s "
                        "FROM ingestion_runs "
                        "WHERE finished_at IS NOT NULL "
                        "  AND id IN ("
                        "    SELECT MAX(id) FROM ingestion_runs "
                        "    WHERE finished_at IS NOT NULL GROUP BY source"
                        "  )"
                    )
                    rows = await cursor.fetchall()
            except Exception as e:
                logger.debug(f"SLA watchdog query failed: {e}")
                continue

            still_stale: set[str] = set()
            for source, last_status, age_s in rows:
                if age_s is None:
                    continue
                sla = resolve_sla_seconds(source)
                if float(age_s) > sla * CRITICAL_MULTIPLIER:
                    still_stale.add(source)
                    if source in _sla_alerted_sources:
                        continue  # already filed
                    minutes = int(float(age_s) / 60)
                    query = (
                        f"investigate: ingestion source '{source}' has not "
                        f"successfully ingested for {minutes} minutes "
                        f"(SLA: {sla}s, last_status: {last_status}). "
                        f"Check whether this is a credential / URL / upstream "
                        f"outage, an off-season lull, or a schema drift. "
                        f"Propose a remediation."
                    )
                    try:
                        await queue.submit_task(query, priority=2)
                        _sla_alerted_sources.add(source)
                        logger.warning(
                            f"SLA watchdog: filed investigation task for {source} "
                            f"({minutes} min stale)"
                        )
                    except Exception as e:
                        logger.warning(f"SLA watchdog: submit_task failed: {e}")

            # Recovery: drop sources that have recovered so a future breach
            # re-files.
            recovered = _sla_alerted_sources - still_stale
            for src in recovered:
                _sla_alerted_sources.discard(src)
                logger.info(f"SLA watchdog: {src} recovered, re-arming alert")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"SLA watchdog loop error (non-fatal): {e}")


# ---------------------------------------------------------------------------
# Adaptive-timeout session runner
# ---------------------------------------------------------------------------
# Per-task hard timeout. AGP sessions that route through Claude Code
# occasionally run 7+ minutes (observed 419s on task 484, 2026-04-18),
# and the worker processes tasks serially — one slow session stalls every
# pending task behind it. CALLISTO_TASK_TIMEOUT_S remains honored as the
# DEFAULT bucket for backward-compat; per-task-type buckets live in
# tools/task_classifier.py and override this when the query matches a
# heuristic (or the caller passed an explicit task_type).
TASK_WORKER_TIMEOUT_S = float(os.getenv("CALLISTO_TASK_TIMEOUT_S", "300"))


class _AdaptiveTimeout(asyncio.TimeoutError):
    """Internal — carries telemetry for the task_worker to report."""

    def __init__(self, telemetry: dict):
        super().__init__()
        self.telemetry = telemetry


async def _run_session_with_adaptive_timeout(
    query: str,
    skip_search: bool,
    initial_budget_s: float,
    hard_ceiling_s: float,
) -> tuple[dict, dict]:
    """Run the orchestrator session with a budget that extends on live progress.

    Returns (result, telemetry). Raises ``asyncio.TimeoutError`` if the
    hard ceiling is hit or the session stalls past the current deadline.

    Telemetry shape (always populated, even on timeout via the outer
    except): {
        'phase': current orchestrator step name or 'UNKNOWN',
        'evidence_count': int,
        'filtered_evidence_count': int,
        'progress_events': int,
        'contradictions': int,
        'elapsed_s': float,
        'extensions': int,
        'stalled': bool,     # True if we cut off due to idle, not budget
    }

    Implementation: we spawn the orchestrator as an asyncio.Task, look it
    up against orchestrator_instance._active_sessions, and poll every
    _ADAPTIVE_POLL_S seconds. On each tick:
      - if the task finished → return its result
      - if monotonic() > current_deadline:
          - session made progress recently? extend up to hard ceiling
          - else terminate
      - if monotonic() - last_progress > stall_window → terminate

    Tuning knobs and the orchestrator singleton are read from the ``api``
    module at call time so tests can monkeypatch them.
    """
    import logging
    import time

    logger = logging.getLogger("callisto.api")

    # Adaptive-extension knobs. If the orchestrator has made progress within
    # PROGRESS_WINDOW_S, the watchdog adds EXTENSION_S to the deadline (up to
    # the hard ceiling). If no progress for STALL_WINDOW_S, terminate at the
    # current deadline instead of extending.
    #
    # Sizing rationale: a single Claude-through-ladder step can take 30-120s
    # (observed 2026-04-22), so the orchestrator legitimately goes silent
    # between progress events for up to ~2min while waiting on one call.
    # We want to extend through those, but cut off at 4-5 min silence which
    # is definitely a stuck session (timeout, deadlock, or dropped socket).
    from api import (
        orchestrator_instance,
        _ADAPTIVE_PROGRESS_WINDOW_S,
        _ADAPTIVE_STALL_WINDOW_S,
        _ADAPTIVE_EXTENSION_S,
        _ADAPTIVE_POLL_S,
    )

    start_monotonic = time.monotonic()
    deadline = start_monotonic + initial_budget_s
    hard_deadline = start_monotonic + hard_ceiling_s
    extensions = 0

    run_task = asyncio.create_task(
        orchestrator_instance.run_session(query, skip_search=skip_search)
    )

    def _snapshot() -> dict:
        """Grab current session state for telemetry."""
        session = orchestrator_instance.active_session_for(run_task)
        if session is None:
            return {
                "phase": "UNKNOWN",
                "evidence_count": 0,
                "filtered_evidence_count": 0,
                "progress_events": 0,
                "contradictions": 0,
                "last_progress_at": None,
            }
        return {
            "phase": session.current_step.name,
            "evidence_count": len(session.evidence),
            "filtered_evidence_count": session.filtered_evidence_count,
            "progress_events": session.progress_events,
            "contradictions": len(session.contradictions),
            "last_progress_at": session.last_progress_at,
        }

    try:
        while True:
            # Sleep until the next poll tick OR the deadline, whichever is sooner.
            now = time.monotonic()
            time_to_deadline = max(0.0, deadline - now)
            wait = min(_ADAPTIVE_POLL_S, time_to_deadline) if time_to_deadline > 0 else 0.1
            try:
                result = await asyncio.wait_for(asyncio.shield(run_task), timeout=wait)
                # Session completed cleanly.
                snap = _snapshot()
                snap.update({
                    "elapsed_s": time.monotonic() - start_monotonic,
                    "extensions": extensions,
                    "stalled": False,
                })
                return result, snap
            except asyncio.TimeoutError:
                pass  # tick — evaluate progress

            if run_task.done():
                # Could be exception; let exception propagate on await below.
                result = await run_task
                snap = _snapshot()
                snap.update({
                    "elapsed_s": time.monotonic() - start_monotonic,
                    "extensions": extensions,
                    "stalled": False,
                })
                return result, snap

            now = time.monotonic()
            snap = _snapshot()
            last_progress = snap.get("last_progress_at") or start_monotonic
            idle_s = now - last_progress

            # Hard-ceiling guard first — no extension ever crosses this line.
            if now >= hard_deadline:
                raise asyncio.TimeoutError("hard ceiling")

            # Stall guard — if the orchestrator has been silent too long,
            # terminate regardless of remaining budget. Extension is only
            # for sessions demonstrably making progress.
            if idle_s >= _ADAPTIVE_STALL_WINDOW_S:
                raise asyncio.TimeoutError("stalled")

            # Deadline reached — extend if progress was recent.
            if now >= deadline:
                if idle_s <= _ADAPTIVE_PROGRESS_WINDOW_S:
                    # Progress within window → extend, capped at hard ceiling.
                    new_deadline = min(deadline + _ADAPTIVE_EXTENSION_S, hard_deadline)
                    if new_deadline <= deadline:
                        # Already at hard ceiling.
                        raise asyncio.TimeoutError("hard ceiling")
                    deadline = new_deadline
                    extensions += 1
                    logger.info(
                        f"Adaptive timeout: extending by {_ADAPTIVE_EXTENSION_S:.0f}s "
                        f"(phase={snap['phase']}, evidence={snap['evidence_count']}, "
                        f"progress_events={snap['progress_events']}, extension #{extensions})"
                    )
                else:
                    raise asyncio.TimeoutError("budget")
    except asyncio.TimeoutError as te:
        # Kill the underlying task and annotate telemetry.
        reason = str(te) or "budget"
        if not run_task.done():
            run_task.cancel()
            try:
                await run_task
            except (asyncio.CancelledError, Exception):
                pass
        snap = _snapshot()
        snap.update({
            "elapsed_s": time.monotonic() - start_monotonic,
            "extensions": extensions,
            "stalled": reason == "stalled",
            "timeout_reason": reason,
        })
        raise _AdaptiveTimeout(snap) from te


# ---------------------------------------------------------------------------
# Task worker
# ---------------------------------------------------------------------------

async def task_worker():
    """Background worker: polls task queue and runs AGP sessions."""
    import logging
    import time

    logger = logging.getLogger("callisto.api")
    from tools.task_classifier import classify_and_budget, get_hard_ceiling_s

    while True:
        try:
            from api import queue as _queue, memory as _memory, research_loop as _research_loop
            task = await _queue.get_next()
            if task is None:
                await asyncio.sleep(2)
                continue

            task_id = task["task_id"]
            query = task["query"]
            skip_search = _is_internal_query(query)

            # Classify and resolve per-task budget. The classifier is
            # keyword-based; miscategorizations merely spend extra wall-
            # clock — they never kill a session early below the old 300s.
            task_type, initial_budget = classify_and_budget(query)
            hard_ceiling = get_hard_ceiling_s()
            logger.info(
                f"Worker picked up task {task_id} "
                f"(type={task_type.value}, budget={initial_budget:.0f}s, "
                f"ceiling={hard_ceiling:.0f}s, skip_search={skip_search}): {query}"
            )

            # In local_only mode, skip tasks that would require Claude
            # (orchestrator calls claude_code_query without checking local_only).
            # Env CALLISTO_LOCAL_ONLY must fail-close even if research_loop is None.
            if _task_blocked_by_local_only(_research_loop):
                logger.info(f"Task {task_id} skipped — local_only mode, orchestrator would call Claude")
                await _queue.fail_task(task_id, "local_only mode — Claude unavailable")
                continue

            try:
                result, telemetry = await _run_session_with_adaptive_timeout(
                    query,
                    skip_search=skip_search,
                    initial_budget_s=initial_budget,
                    hard_ceiling_s=hard_ceiling,
                )
                session_id = result.get("session_id")
                await _queue.complete_task(task_id, result, session_id=session_id)
                logger.info(
                    f"Task {task_id} completed in {telemetry['elapsed_s']:.1f}s "
                    f"(type={task_type.value}, extensions={telemetry['extensions']}), "
                    f"session {session_id}"
                )

                # Wiki auto-file: compound task results into knowledge base
                try:
                    conclusion = result.get("conclusion") or result.get("summary", {}).get("conclusion")
                    confidence = result.get("confidence_score") or result.get("summary", {}).get("confidence_score", 0.5)
                    domain = result.get("domain", "GENERAL")
                    if conclusion:
                        from tools.knowledge_wiki import get_wiki
                        wiki = get_wiki()
                        async with aiosqlite.connect(_memory.db_path) as wdb:
                            await wdb.execute("PRAGMA busy_timeout = 60000")
                            filed_topic = await wiki.file_task_result(
                                wdb, query, conclusion, confidence, domain,
                                task_id=str(task_id), session_id=session_id,
                            )
                            if filed_topic:
                                logger.debug(f"Task {task_id} filed to wiki: {filed_topic}")
                except Exception as e:
                    logger.debug(f"Wiki auto-file failed for task {task_id} (non-fatal): {e}")

                # Auto-follow-up: if session concluded INSUFFICIENT DATA, queue the next step
                await _maybe_auto_followup(task_id, result)
            except _AdaptiveTimeout as ate:
                t = ate.telemetry
                reason = t.get("timeout_reason", "budget")
                # Build a structured error the SLA watchdog can parse.
                err_msg = (
                    f"timeout: type={task_type.value} reason={reason} "
                    f"phase={t.get('phase')} elapsed={t.get('elapsed_s', 0):.1f}s "
                    f"evidence={t.get('evidence_count', 0)} "
                    f"filtered={t.get('filtered_evidence_count', 0)} "
                    f"contradictions={t.get('contradictions', 0)} "
                    f"progress_events={t.get('progress_events', 0)} "
                    f"extensions={t.get('extensions', 0)} "
                    f"stalled={t.get('stalled', False)}"
                )
                logger.error(f"Task {task_id} TIMEOUT: {err_msg}")
                partial = {
                    "task_id": task_id,
                    "task_type": task_type.value,
                    "telemetry": t,
                    "error": err_msg,
                }
                await _queue.timeout_task(task_id, err_msg, result=partial)
            except asyncio.TimeoutError:
                # Fallback path — shouldn't happen since _run_session_with_adaptive_timeout
                # wraps into _AdaptiveTimeout, but be defensive.
                err_msg = (
                    f"timeout: orchestrator exceeded {initial_budget:.0f}s budget "
                    f"(type={task_type.value}, no telemetry)"
                )
                logger.error(f"Task {task_id} TIMEOUT (bare): {err_msg}")
                await _queue.timeout_task(task_id, err_msg)
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                await _queue.fail_task(task_id, str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Order maintenance cron
# ---------------------------------------------------------------------------

async def order_cron_loop() -> None:
    """Periodic maintenance for the orders table.

    Every 60s:   expire pending_approval rows past their TTL.
    Every 300s:  reconcile ``filled`` rows against ``game_results`` and
                 auto-settle those that have resolved.
    Every 900s:  detect postponed/cancelled games and void filled orders.
    """
    import logging

    logger = logging.getLogger("callisto.api")
    ticks = 0
    while True:
        try:
            await asyncio.sleep(60)
            ticks += 1
            from api import order_manager_instance
            if order_manager_instance is None:
                continue
            try:
                expired = await order_manager_instance.expire_stale()
                if expired:
                    logger.info(f"order cron: expired {len(expired)} stale orders")
            except Exception as e:
                logger.warning(f"order cron expire_stale failed: {e}")
            if ticks % 5 == 0:  # every 5 min
                try:
                    from tools.order_manager import (
                        reconcile_filled_orders,
                        detect_voided_orders,
                    )
                    stats = await reconcile_filled_orders(order_manager_instance)
                    if stats.get("settled") or stats.get("stuck"):
                        logger.info(f"order cron: reconcile {stats}")
                except Exception as e:
                    logger.warning(f"order cron reconcile failed: {e}")
            if ticks % 15 == 0:  # every 15 min
                try:
                    from tools.order_manager import (
                        reconcile_filled_orders,
                        detect_voided_orders,
                    )
                    void_stats = await detect_voided_orders(order_manager_instance)
                    if void_stats.get("voided"):
                        logger.info(f"order cron: void scan {void_stats}")
                except Exception as e:
                    logger.warning(f"order cron void scan failed: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"order_cron_loop iteration error: {e}", exc_info=True)
            await asyncio.sleep(10)
