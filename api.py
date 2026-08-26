"""
FastAPI REST layer for Callisto.

Endpoints for task submission, session retrieval, world queries, and health checks.
Runs on port 8420.
"""

import asyncio
import gc
import logging
import os
import secrets as _secrets
import time
import tracemalloc
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agp import AGPSealTampered, Domain
from logging_config import setup_logging
from memory import MemoryStore
from monitor import HealthMonitor
from orchestrator import Orchestrator
from task_queue import TaskQueue
from tools.line_monitor import LineMonitor
from tools.clv_tracker import CLVTracker
from tools.autonomous import AutonomousLoop, ResearchLoop
from tools import telegram
from tools.telegram import TelegramListener
from tools.schema import ensure_schema
from tools.hypothesis import HypothesisManager
from tools.historical_odds import HistoricalOddsFetcher
from tools.backtest import BacktestEngine
from tools.embeddings import VectorStore
from tools.hypothesis_generator import HypothesisGenerator
from tools.data_collector import DataCollector
from tools.health import SystemHealth
from tools.pipeline_integrity import get_checker as get_integrity_checker, initialize as init_integrity
from tools.learned_correlations import LearnedCorrelationStore
from tools.correlation import set_learned_store, SPORT_CORRELATIONS
from tools.state_paths import restart_signal_path
from tools.order_manager import (
    OrderManager,
    reconcile_filled_orders,
    detect_voided_orders,
    get_manager as _get_order_manager,
)

load_dotenv()

setup_logging()
logger = logging.getLogger("callisto.api")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

CALLISTO_PORT = int(os.getenv("CALLISTO_PORT", "8420"))
# SECURITY (audit C-2 2026-04-18): default-bind to loopback. Override only with intent.
CALLISTO_BIND_HOST = os.getenv("CALLISTO_BIND_HOST", "127.0.0.1")
# Optional Bearer token for /admin/*, /debug/*, /context/sync, /research/*, /executor/*,
# /admin/sql, and other state-changing or sensitive endpoints. When unset, those endpoints
# return 503. Read-only IDOR-prone endpoints (/task/{id}, /session/{id}, /hypothesis/{id})
# require the token only if it is configured (degrades to allow on loopback for dev).
CALLISTO_ADMIN_TOKEN = os.getenv("CALLISTO_ADMIN_TOKEN", "").strip()

_bearer_scheme = HTTPBearer(auto_error=False)

# Dedicated logger for auth events so probing is visible in a separate stream.
_auth_logger = logging.getLogger("callisto.api.auth")


def _client_is_loopback(request: Request) -> bool:
    """Return True iff the request originated from the local loopback interface.

    Only trusts `request.client.host` — never X-Forwarded-For — because Callisto
    binds to 127.0.0.1 by default and does not sit behind a trusted proxy. If
    someone puts it behind one, loopback-trust must be revisited.
    """
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _log_auth_denied(request: Request, reason: str, status: int) -> None:
    """Emit a WARNING for every 401/403 so probing is visible in logs."""
    host = (request.client.host if request.client else "?") or "?"
    _auth_logger.warning(
        "AUTH_DENIED host=%s method=%s path=%s status=%d reason=%s",
        host, request.method, request.url.path, status, reason,
    )


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Hard-gate: require Bearer token. Fails closed if CALLISTO_ADMIN_TOKEN unset."""
    if not CALLISTO_ADMIN_TOKEN:
        _log_auth_denied(request, "admin_token_unset", 503)
        raise HTTPException(
            status_code=503,
            detail="CALLISTO_ADMIN_TOKEN not configured; admin endpoint disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        _log_auth_denied(request, "missing_bearer", 401)
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, CALLISTO_ADMIN_TOKEN):
        _log_auth_denied(request, "bad_token", 403)
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_admin_or_loopback(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Soft-gate for read endpoints. Allow loopback when token unset; otherwise require token."""
    if not CALLISTO_ADMIN_TOKEN:
        if _client_is_loopback(request):
            return
        _log_auth_denied(request, "non_loopback_no_token", 403)
        raise HTTPException(status_code=403, detail="Loopback only when admin token unset")
    if credentials is None or credentials.scheme.lower() != "bearer":
        # Loopback path short-circuit even when a token is set: MCP server and
        # local research loop don't send Authorization headers. They still need
        # to self-consume the API. Non-loopback callers must authenticate.
        if _client_is_loopback(request):
            return
        _log_auth_denied(request, "missing_bearer", 401)
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, CALLISTO_ADMIN_TOKEN):
        _log_auth_denied(request, "bad_token", 403)
        raise HTTPException(status_code=403, detail="Forbidden")


# ---------------------------------------------------------------------------
# Default-secure write-gate
# ---------------------------------------------------------------------------
# Everything that mutates state (POST/PATCH/PUT/DELETE) gets auth by default.
# To expose a write endpoint publicly, register it via `public_endpoint(...)`.
# The middleware `_default_secure_middleware` enforces this below.
#
# Keep this list SHORT — public writes should be rare and deliberate.
# ---------------------------------------------------------------------------
_WRITE_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
_PUBLIC_WRITE_ENDPOINTS: set[tuple[str, str]] = set()


def public_endpoint(method: str, path: str) -> None:
    """Opt a write endpoint OUT of the default-secure middleware.

    Adds (METHOD, path) to the public registry. `path` must match
    `request.url.path` exactly (no pattern matching).
    """
    _PUBLIC_WRITE_ENDPOINTS.add((method.upper(), path))

# Shared state
memory: Optional[MemoryStore] = None
queue: Optional[TaskQueue] = None
orchestrator_instance: Optional[Orchestrator] = None
monitor: Optional[HealthMonitor] = None
line_monitor: Optional[LineMonitor] = None
live_state_collector = None  # tools.live_state.LiveStateCollector | None
clv_tracker: Optional[CLVTracker] = None
autonomous: Optional[AutonomousLoop] = None
telegram_listener: Optional[TelegramListener] = None
hypothesis_manager: Optional[HypothesisManager] = None
historical_fetcher: Optional[HistoricalOddsFetcher] = None
backtest_engine: Optional[BacktestEngine] = None
vector_store: Optional[VectorStore] = None
hypothesis_generator: Optional[HypothesisGenerator] = None
data_collector: Optional[DataCollector] = None
research_loop: Optional[ResearchLoop] = None
system_health: Optional[SystemHealth] = None
learned_correlation_store: Optional[LearnedCorrelationStore] = None
worker_task: Optional[asyncio.Task] = None
wal_checkpoint_task: Optional[asyncio.Task] = None
restart_signal_task: Optional[asyncio.Task] = None
order_cron_task: Optional[asyncio.Task] = None
prop_resolver_task: Optional[asyncio.Task] = None
order_manager_instance: Optional[OrderManager] = None
live_state_task: Optional[asyncio.Task] = None


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


async def wal_checkpoint_loop():
    """Periodic WAL checkpoint + memory guardian.

    Every 5 minutes:
    1. Checkpoint WAL to prevent bloat
    2. Check process memory — if RSS > 2GB, signal graceful restart
       The watchdog will pick us back up with fresh memory.
    """
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


# Per-task hard timeout. AGP sessions that route through Claude Code
# occasionally run 7+ minutes (observed 419s on task 484, 2026-04-18),
# and the worker processes tasks serially — one slow session stalls every
# pending task behind it. CALLISTO_TASK_TIMEOUT_S remains honored as the
# DEFAULT bucket for backward-compat; per-task-type buckets live in
# tools/task_classifier.py and override this when the query matches a
# heuristic (or the caller passed an explicit task_type).
TASK_WORKER_TIMEOUT_S = float(os.getenv("CALLISTO_TASK_TIMEOUT_S", "300"))

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
_ADAPTIVE_PROGRESS_WINDOW_S = float(os.getenv("CALLISTO_PROGRESS_WINDOW_S", "120"))
_ADAPTIVE_STALL_WINDOW_S = float(os.getenv("CALLISTO_STALL_WINDOW_S", "240"))
_ADAPTIVE_EXTENSION_S = float(os.getenv("CALLISTO_EXTENSION_S", "120"))
_ADAPTIVE_POLL_S = float(os.getenv("CALLISTO_ADAPTIVE_POLL_S", "5"))


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
    """
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


class _AdaptiveTimeout(asyncio.TimeoutError):
    """Internal — carries telemetry for the task_worker to report."""

    def __init__(self, telemetry: dict):
        super().__init__()
        self.telemetry = telemetry


async def task_worker():
    """Background worker: polls task queue and runs AGP sessions."""
    from tools.task_classifier import classify_and_budget, get_hard_ceiling_s

    while True:
        try:
            task = await queue.get_next()
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
            # (orchestrator calls claude_code_query without checking local_only)
            if research_loop and research_loop._local_only:
                logger.info(f"Task {task_id} skipped — local_only mode, orchestrator would call Claude")
                await queue.fail_task(task_id, "local_only mode — Claude unavailable")
                continue

            try:
                result, telemetry = await _run_session_with_adaptive_timeout(
                    query,
                    skip_search=skip_search,
                    initial_budget_s=initial_budget,
                    hard_ceiling_s=hard_ceiling,
                )
                session_id = result.get("session_id")
                await queue.complete_task(task_id, result, session_id=session_id)
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
                        async with aiosqlite.connect(memory.db_path) as wdb:
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
                await queue.timeout_task(task_id, err_msg, result=partial)
            except asyncio.TimeoutError:
                # Fallback path — shouldn't happen since _run_session_with_adaptive_timeout
                # wraps into _AdaptiveTimeout, but be defensive.
                err_msg = (
                    f"timeout: orchestrator exceeded {initial_budget:.0f}s budget "
                    f"(type={task_type.value}, no telemetry)"
                )
                logger.error(f"Task {task_id} TIMEOUT (bare): {err_msg}")
                await queue.timeout_task(task_id, err_msg)
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                await queue.fail_task(task_id, str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)


async def order_cron_loop() -> None:
    """Periodic maintenance for the orders table.

    Every 60s:   expire pending_approval rows past their TTL.
    Every 300s:  reconcile ``filled`` rows against ``game_results`` and
                 auto-settle those that have resolved.
    Every 900s:  detect postponed/cancelled games and void filled orders.
    """
    global order_manager_instance
    ticks = 0
    while True:
        try:
            await asyncio.sleep(60)
            ticks += 1
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
                    stats = await reconcile_filled_orders(order_manager_instance)
                    if stats.get("settled") or stats.get("stuck"):
                        logger.info(f"order cron: reconcile {stats}")
                except Exception as e:
                    logger.warning(f"order cron reconcile failed: {e}")
            if ticks % 15 == 0:  # every 15 min
                try:
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle manager."""
    global memory, queue, orchestrator_instance, monitor, line_monitor, clv_tracker, autonomous, telegram_listener, hypothesis_manager, historical_fetcher, backtest_engine, vector_store, hypothesis_generator, data_collector, research_loop, system_health, learned_correlation_store, worker_task, wal_checkpoint_task, restart_signal_task, order_cron_task, order_manager_instance, live_state_collector, live_state_task, prop_resolver_task, heartbeat, game_scheduler

    # Start memory profiling only when explicitly requested — tracemalloc tracks every
    # allocation in C-level metadata (~50-100 bytes each), which adds 55-110 MB of invisible
    # overhead from the JSON decoder alone (1.1M allocations) plus severe fragmentation.
    if os.environ.get("CALLISTO_TRACEMALLOC") == "1":
        tracemalloc.start(3)
        logger.info("tracemalloc started with 3-frame depth (CALLISTO_TRACEMALLOC=1)")
    else:
        logger.info("tracemalloc disabled (set CALLISTO_TRACEMALLOC=1 to enable)")

    # Single-writer coordinator (root-cause fix for "database is locked").
    # install_aiosqlite_routing() patches aiosqlite.Connection so EVERY
    # write — including from modules that use raw db.execute() instead of
    # our retry helpers — routes through the coordinator transparently.
    # MUST run before ensure_schema and any other connection so the patched
    # aiosqlite is in effect for the rest of the process lifetime.
    from tools.db_writer import (
        install_aiosqlite_routing as _install_routing,
        get_writer as _get_writer,
    )
    _install_routing()
    await _get_writer(DB_PATH)
    logger.info(f"WriteCoordinator active for {DB_PATH} (process-wide routing installed)")

    # Startup — ensure DB schema is up to date (now uses patched aiosqlite).
    await ensure_schema()

    # Followup hardening columns (feat/auto-followup-hardening).
    # Adds followup_depth / parent_task_id / root_task_id / cost_usd to
    # task_queue so _maybe_auto_followup can enforce depth + budget caps
    # and /task/{id}/chain can walk the ancestry tree.
    try:
        from tools.followup_guard import ensure_followup_columns
        async with aiosqlite.connect(DB_PATH) as _fg_db:
            await _fg_db.execute("PRAGMA busy_timeout = 30000")
            await ensure_followup_columns(_fg_db)
    except Exception as e:
        logger.warning(f"followup_guard column migration failed (non-fatal): {e!r}")

    # Versioned migrations (tools/migrations/NNN_*.py). Runs AFTER
    # ensure_schema so fresh DBs have the v1 baseline tables; for existing
    # DBs the bootstrap step marks every migration as already-applied so
    # nothing re-runs. Uses a dedicated autocommit stdlib connection so
    # DDL bypasses the WriteCoordinator entirely.
    from tools.migrations import apply_pending_migrations
    try:
        mig_result = apply_pending_migrations(DB_PATH)
        logger.info(f"Migrations: {mig_result}")
    except Exception as e:
        logger.error(f"Migration framework failed: {e!r}")
        raise

    # Preload priority models into VRAM (devstral-small-2 takes 28s cold, <1s warm)
    from inference import warmup_models
    await warmup_models()

    # Learned correlations — Bayesian blend of hardcoded priors + empirical data
    learned_correlation_store = LearnedCorrelationStore()
    await learned_correlation_store.initialize()
    await learned_correlation_store.seed_from_priors(SPORT_CORRELATIONS)
    set_learned_store(learned_correlation_store)

    memory = MemoryStore()
    await memory.initialize()

    queue = TaskQueue()
    await queue.initialize()

    orchestrator_instance = Orchestrator(memory)
    monitor = HealthMonitor()
    await monitor.start()

    # Line movement monitor — autonomous odds tracking.
    # Sole owner of the application-lifespan odds WebSocket: the provider
    # allows one connection per API key, so nothing else may start
    # start_odds_stream() or a competing OddsWebSocket here.
    line_monitor = LineMonitor()
    await line_monitor.initialize()
    await line_monitor.start()

    # CLV tracker — bet tracking and closing line value measurement
    clv_tracker = CLVTracker()
    await clv_tracker.initialize()

    # Order management — Telegram-approved manual placement (supersedes
    # the Playwright executor when CALLISTO_USE_ORDER_MANAGER=1, default).
    order_manager_instance = await _get_order_manager()
    app.state.order_manager = order_manager_instance

    # Hypothesis testing framework
    hypothesis_manager = HypothesisManager()
    await hypothesis_manager.initialize()
    historical_fetcher = HistoricalOddsFetcher()
    await historical_fetcher.initialize()
    backtest_engine = BacktestEngine(
        hypothesis_manager=hypothesis_manager,
        historical_fetcher=historical_fetcher,
    )
    await backtest_engine.initialize()

    # Vector store + hypothesis generator + data collector
    vector_store = VectorStore()
    await vector_store.initialize()
    hypothesis_generator = HypothesisGenerator(
        hypothesis_manager=hypothesis_manager,
        vector_store=vector_store,
    )
    await hypothesis_generator.initialize()
    data_collector = DataCollector()
    await data_collector.initialize()

    # Autonomous reasoning loop — proactive edge analysis
    autonomous = AutonomousLoop(orchestrator_instance, line_monitor)
    await autonomous.start()

    # Research loop — 24/7 hypothesis machine
    research_loop = ResearchLoop(
        hypothesis_manager=hypothesis_manager,
        hypothesis_generator=hypothesis_generator,
        backtest_engine=backtest_engine,
        data_collector=data_collector,
        vector_store=vector_store,
        orchestrator=orchestrator_instance,
        line_monitor=line_monitor,
    )
    await research_loop.start()

    # Pipeline integrity checker — detects silent failures
    await init_integrity()

    # System health monitor — Layer 2 resilience
    system_health = SystemHealth()
    system_health.research_loop = research_loop
    system_health.autonomous_loop = autonomous
    await system_health.start()

    # Heartbeat — independent watchdog for loop stalls and Claude availability
    from tools.self_repair import Heartbeat
    heartbeat = Heartbeat()
    await heartbeat.start()
    app.state.heartbeat = heartbeat

    # Telegram listener — bidirectional communication from phone
    telegram_listener = TelegramListener(
        orchestrator=orchestrator_instance,
        line_monitor=line_monitor,
        clv_tracker=clv_tracker,
    )
    await telegram_listener.start()

    # Game scheduler — fires events at T-60min and T-15min before games
    try:
        from tools.game_scheduler import GameScheduler
        from tools.event_bus import get_event_bus
        game_scheduler = GameScheduler(event_bus=get_event_bus())
        await game_scheduler.start()
        app.state.game_scheduler = game_scheduler
        logger.info(f"Game scheduler started — {len(game_scheduler._games)} upcoming games")
    except Exception as e:
        logger.warning(f"Game scheduler failed to start: {e}")

    # Event bus audit drain — persist important events to SQLite
    try:
        bus = get_event_bus()
        await bus.start_audit_drain()
        logger.info("Event bus audit drain started")
    except Exception as e:
        logger.warning(f"Event bus audit drain failed: {e}")

    # Live in-game state collector — polls ESPN every 30s for games
    # in progress, stores snapshots, and fires live-edge detectors.
    # Env-gated (default ON) and wrapped in try/except so a failure
    # here can NEVER break the rest of startup. If the DB migration
    # hasn't been applied yet, the collector self-disables and logs.
    live_state_collector = None
    live_state_task = None
    if os.environ.get("CALLISTO_LIVE_STATE_ENABLED", "1") == "1":
        try:
            from tools.live_state import start_collector as _start_live_collector
            live_state_collector = await _start_live_collector(db_path=DB_PATH)
            if live_state_collector is not None:
                # start_collector already called create_task inside the
                # collector; we don't need a second task. Expose the
                # module's task via the collector so shutdown can find it.
                live_state_task = live_state_collector._task
                logger.info(
                    "Live state collector started "
                    f"(sports={list(live_state_collector.sports)}, interval=30s)"
                )
            else:
                logger.warning(
                    "Live state collector not started — table missing or disabled"
                )
        except Exception as e:
            logger.warning(f"Live state collector failed to start: {e}")
            live_state_collector = None
            live_state_task = None
    else:
        logger.info("Live state collector disabled via CALLISTO_LIVE_STATE_ENABLED=0")

    worker_task = asyncio.create_task(task_worker())
    wal_checkpoint_task = asyncio.create_task(wal_checkpoint_loop())
    # Signal-file consumer — decouples restart from watchdog liveness.
    restart_signal_task = asyncio.create_task(restart_signal_watcher())
    sla_watchdog_task = asyncio.create_task(ingestion_sla_watchdog_loop())
    order_cron_task = asyncio.create_task(order_cron_loop())
    # Prop resolution — fills backtest_events.actual_result for player_* markets.
    # Without this, every prop hypothesis stats at 0 resolved (silent freeze).
    try:
        from tools.prop_resolver import prop_resolution_loop
        prop_resolver_task = asyncio.create_task(prop_resolution_loop())
        logger.info(
            "prop_resolution_loop started (15m interval, 500 rows/pass)"
        )
    except Exception as e:
        logger.warning(f"prop_resolution_loop failed to start: {e}")
    logger.info(
        f"Callisto API started on port {CALLISTO_PORT} "
        f"(WAL ckpt 5m, restart-signal watcher active, ingestion SLA watchdog 5m, "
        f"prop resolver 15m)"
    )

    # Notify on Telegram
    sports = (await line_monitor.get_status()).get("monitored_sports", [])
    await telegram.alert_system(
        f"API started on port {CALLISTO_PORT}\n"
        f"Monitoring: {', '.join(sports)}\n"
        f"Odds-API.io Pro: 15 books, 30K req/hr + WebSocket\n"
        f"Autonomous reasoning: ACTIVE\n"
        f"Research loop: ACTIVE (24/7 hypothesis machine)"
    )

    yield

    # Shutdown
    await telegram.alert_system("Callisto shutting down.", is_error=True)
    await telegram_listener.stop()
    if system_health:
        system_health.write_health_file()
        await system_health.stop()
    if research_loop:
        await research_loop.stop()
    await autonomous.stop()
    if wal_checkpoint_task:
        wal_checkpoint_task.cancel()
        try:
            await wal_checkpoint_task
        except asyncio.CancelledError:
            pass
    if sla_watchdog_task:
        sla_watchdog_task.cancel()
        try:
            await sla_watchdog_task
        except asyncio.CancelledError:
            pass
    if order_cron_task:
        order_cron_task.cancel()
        try:
            await order_cron_task
        except asyncio.CancelledError:
            pass
    if prop_resolver_task:
        prop_resolver_task.cancel()
        try:
            await prop_resolver_task
        except asyncio.CancelledError:
            pass
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    if restart_signal_task:
        restart_signal_task.cancel()
        try:
            await restart_signal_task
        except asyncio.CancelledError:
            pass
    # Live state collector — cancelled via stop_collector so the HTTP
    # client is closed cleanly. Failure here must NOT stop shutdown.
    try:
        from tools.live_state import stop_collector as _stop_live_collector
        await _stop_live_collector()
    except Exception as e:
        logger.debug(f"Live state collector shutdown failed: {e}")
    # Cancel orphaned restart task if shutdown beat it (audit H-14).
    if _restart_task and not _restart_task.done():
        _restart_task.cancel()
        try:
            await _restart_task
        except (asyncio.CancelledError, Exception):
            pass
    # Stop periodic producers and the event-bus audit drain BEFORE the write
    # coordinator: each owns a background task that may still write, and the
    # coordinator must outlive them all so their final writes can drain.
    try:
        if game_scheduler:
            await game_scheduler.stop()
            app.state.game_scheduler = None
    except Exception:
        logger.exception("Game scheduler shutdown error (non-fatal)")
    try:
        await get_event_bus().stop()
        logger.info("Event bus audit drain stopped")
    except Exception:
        logger.exception("Event bus shutdown error (non-fatal)")
    try:
        if heartbeat:
            await heartbeat.stop()
            task = getattr(heartbeat, "_task", None)
            if task is not None and not task.done():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            app.state.heartbeat = None
    except Exception:
        logger.exception("Heartbeat shutdown error (non-fatal)")

    # Stop every WriteCoordinator last so any final writes from the shutdown
    # path above were able to drain through it.
    try:
        from tools.db_writer import stop_all as _stop_writers
        await _stop_writers()
    except Exception:
        logger.exception("WriteCoordinator shutdown error (non-fatal)")
    if data_collector:
        await data_collector.close()
    if hypothesis_generator:
        await hypothesis_generator.close()
    if vector_store:
        await vector_store.close()
    await backtest_engine.close()
    await historical_fetcher.close()
    await hypothesis_manager.close()
    await clv_tracker.close()
    await line_monitor.stop()
    await monitor.stop()
    await queue.close()
    await memory.close()
    if learned_correlation_store:
        await learned_correlation_store.close()
    # Close search backend clients
    from tools.search import close_all_clients
    await close_all_clients()
    # Close odds API client
    from tools.odds_api import close_client as close_odds_client
    await close_odds_client()
    # Close contextual data client
    from tools.contextual_data import close_client as close_ctx_client
    await close_ctx_client()
    # Close embedding client
    from tools.embeddings import close_client as close_embed_client
    await close_embed_client()
    # Close data collector client
    from tools.data_collector import close_client as close_dc_client
    await close_dc_client()
    # Close DK scraper client
    from tools.dk_scraper import close_client as close_dk_client
    await close_dk_client()
    logger.info("Callisto API shut down")


app = FastAPI(
    title="Callisto",
    description="Autonomous multi-agent reasoning system governed by the Aluft Gianne Protocol",
    version="0.1.0",
    lifespan=lifespan,
)


# Global exception handler — convert any unhandled error into a structured 500
# instead of crashing the request handler. Logs full traceback for debugging.
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError
import traceback as _traceback


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch any unhandled exception and return a structured JSON error."""
    # Don't intercept FastAPI's own HTTPException — let it pass through
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": exc.detail, "status": exc.status_code},
        )
    tb = _traceback.format_exc()
    logger.error(
        f"Unhandled exception in {request.method} {request.url.path}: {exc}\n{tb}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": str(exc),
            "type": type(exc).__name__,
            "path": request.url.path,
        },
    )


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return clean 422 instead of FastAPI's default verbose error."""
    return JSONResponse(
        status_code=422,
        content={"error": "Validation failed", "details": exc.errors()},
    )


# ---------------------------------------------------------------------------
# Default-secure middleware
# ---------------------------------------------------------------------------
# Runs BEFORE any endpoint dispatch. If the method is a write and the path
# isn't on the public allowlist, the request must satisfy
# `require_admin_or_loopback`. This is the primary gate — per-endpoint
# `dependencies=[Depends(require_admin_or_loopback)]` are defense in depth.
#
# Endpoints may still be explicitly gated with `require_admin` (hard token
# requirement) via per-endpoint dependencies; the middleware only enforces
# the floor, never relaxes it.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _default_secure_middleware(request: Request, call_next):
    method = request.method.upper()
    if method in _WRITE_METHODS:
        path = request.url.path
        if (method, path) not in _PUBLIC_WRITE_ENDPOINTS:
            # Inline the token/loopback check so we can return JSON rather
            # than let HTTPException bubble up before routing.
            if _client_is_loopback(request):
                # Loopback always allowed — MCP server & research loop path.
                pass
            else:
                auth_header = request.headers.get("authorization", "")
                if not CALLISTO_ADMIN_TOKEN:
                    _log_auth_denied(request, "non_loopback_no_token", 403)
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Loopback only when admin token unset", "status": 403},
                    )
                if not auth_header.lower().startswith("bearer "):
                    _log_auth_denied(request, "missing_bearer", 401)
                    return JSONResponse(
                        status_code=401,
                        content={"error": "Bearer token required", "status": 401},
                    )
                provided = auth_header.split(" ", 1)[1].strip()
                if not _secrets.compare_digest(provided, CALLISTO_ADMIN_TOKEN):
                    _log_auth_denied(request, "bad_token", 403)
                    return JSONResponse(
                        status_code=403,
                        content={"error": "Forbidden", "status": 403},
                    )
    return await call_next(request)


# Explicit public write allowlist — EVERY entry is deliberate.
# - POST /task: AGP research submission. MCP server + CC sessions use this.
# - POST /context/sync: already hard-gated via require_admin; listed here so
#   the middleware doesn't double-check (the endpoint's own require_admin
#   remains the real gate, stricter than the loopback default).
# Keep this list minimal; prefer moving endpoints off it over adding to it.
public_endpoint("POST", "/task")
public_endpoint("POST", "/context/sync")


class TaskSubmission(BaseModel):
    query: str = Field(..., min_length=1, max_length=20000)
    priority: int = Field(default=0, ge=-10, le=10)


class TaskResponse(BaseModel):
    task_id: int


@app.post("/task", response_model=TaskResponse)
async def submit_task(
    submission: TaskSubmission,
    _auth: None = Depends(require_admin_or_loopback),
):
    """Submit a query for AGP session processing.

    Writes are auth-gated: without this, a caller could queue arbitrary LLM
    work against the billing account. GET /task/{id} is already gated, so
    writes must match.

    Wiki task short-circuit (feat/wiki-in-the-loop, 2026-04-22):
      Before enqueueing, embed the query and semantic-search the wiki. If
      a high-similarity (>0.88) article exists, create the task in COMPLETED
      state immediately with the wiki article as its result — saving ~5min
      orchestrator cycles on duplicate queries. Toggle:
      ``CALLISTO_TASK_SHORT_CIRCUIT=1`` (default on).
    """
    try:
        # Short-circuit pass — safe on any failure (returns None).
        short_circuit_result = None
        if os.getenv("CALLISTO_TASK_SHORT_CIRCUIT", "1") == "1":
            short_circuit_result = await _wiki_task_short_circuit(submission.query)

        task_id = await queue.submit_task(submission.query, submission.priority)

        if short_circuit_result is not None:
            try:
                await queue.complete_task(task_id, short_circuit_result)
                logger.info(
                    f"Task {task_id} SHORT-CIRCUITED via wiki "
                    f"(topic={short_circuit_result.get('wiki_topic')}, "
                    f"sim={short_circuit_result.get('wiki_similarity')})"
                )
            except Exception as e:
                # If we can't mark it complete, leave it PENDING — the worker
                # will pick it up normally. Not fatal.
                logger.warning(f"Short-circuit complete_task failed for {task_id}: {e}")

        return TaskResponse(task_id=task_id)
    except Exception as e:
        logger.error(f"POST /task failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


async def _wiki_task_short_circuit(query: str) -> Optional[dict]:
    """Look up a pre-existing wiki answer for ``query``.

    Returns a result-dict suitable for ``queue.complete_task(...)`` when a
    high-similarity match is found, else None. All failures return None —
    the task proceeds normally through the orchestrator.
    """
    try:
        threshold = float(os.getenv("CALLISTO_TASK_SHORT_CIRCUIT_THRESHOLD", "0.88"))
        from tools.knowledge_wiki import get_wiki
        wiki = get_wiki()
        async with aiosqlite.connect(memory.db_path) as wdb:
            await wdb.execute("PRAGMA busy_timeout = 20000")
            hits = await wiki.search(wdb, query, top_k=1, min_similarity=0.0)
        if not hits:
            return None
        top = hits[0]
        sim = top.get("similarity")
        if not isinstance(sim, (int, float)) or sim < threshold:
            return None
        return {
            "short_circuited": True,
            "wiki_topic": top.get("topic"),
            "wiki_title": top.get("title"),
            "wiki_similarity": round(sim, 4),
            "wiki_domain": top.get("domain"),
            "wiki_confidence": top.get("confidence"),
            "conclusion": top.get("summary") or top.get("content"),
            "confidence_score": top.get("confidence") or 0.5,
            "domain": top.get("domain") or "GENERAL",
            "source": "wiki_short_circuit",
        }
    except Exception as e:
        logger.debug(f"Wiki task short-circuit skipped: {e}")
        return None


@app.get("/task/{task_id}")
async def get_task(task_id: int, _auth: None = Depends(require_admin_or_loopback)):
    """Get task status and result."""
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/task/{task_id}/chain")
async def get_task_chain(
    task_id: int, _auth: None = Depends(require_admin_or_loopback)
):
    """Return the full followup tree rooted at ``task_id``'s 0-depth ancestor.

    Enables "where did this task come from / what else did it spawn?"
    debugging. Includes total cost and max-depth so a runaway chain is
    visible at a glance.

    Loopback-or-admin gated: same auth posture as GET /task/{id} since
    the chain leaks the same query text.
    """
    from tools.followup_guard import get_chain_tree
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        tree = await get_chain_tree(db, task_id)
    if tree.get("error") == "task_not_found":
        raise HTTPException(status_code=404, detail="Task not found")
    return tree


@app.get("/session/{session_id}")
async def get_session(session_id: str, _auth: None = Depends(require_admin_or_loopback)):
    """Get a sealed AGP session with full provenance.

    Returns 409 CONFLICT if the stored seal_hash fails verification — the
    session exists but its content has been tampered with or corrupted.
    """
    try:
        session = await memory.get_session(session_id)
    except AGPSealTampered as e:
        logger.error("Seal tamper detected on GET /session/%s: %s", session_id, e)
        raise HTTPException(
            status_code=409,
            detail=f"Session {session_id} seal failed verification (tampered or corrupted)",
        )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/world/{domain}")
async def query_world(
    domain: str,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    limit: int = 50,
    _auth: None = Depends(require_admin_or_loopback),
):
    """Query a domain world. When ``keyword`` is present, retrieval is
    SEMANTIC (vector similarity) with a keyword-LIKE fallback; otherwise
    the recent-first ordering is used.

    Loopback-or-admin gated: world memory can contain tagged research
    (financial, signal, synthesis) we don't want to leak to unauth'd callers
    if CALLISTO_BIND_HOST is ever set non-loopback.

    SECURITY (audit 2026-04-21): `limit` is hard-capped at 500 to prevent
    memory-exhaustion via `?limit=1000000`.
    """
    try:
        domain_enum = Domain(domain.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {[d.value for d in Domain]}",
        )
    # Cap limit defensively — anything beyond 500 materialises gigabytes on
    # well-populated domains and is almost never a legitimate query.
    # Also coerces limit to int to reject `?limit=foo`.
    limit = max(1, min(int(limit), 500))
    results = await memory.query_world(
        domain_enum, keyword=keyword, min_confidence=min_confidence, limit=limit
    )
    return {"domain": domain_enum.value, "count": len(results), "entries": results}


from tools.api import analysis as _analysis
from tools.api import bets as _bets
from tools.api import odds_extra as _odds_extra
from tools.api import odds_routes as _odds_routes
from tools.api import simulate as _simulate
from tools.api import wiki as _wiki
from tools.api import model_routes as _model_routes
from tools.api import data_routes as _data_routes
from tools.api import hypothesis_routes as _hypothesis_routes
from tools.api import backtest_routes as _backtest_routes
from tools.api import research_routes as _research_routes
from tools.api import system_routes as _system_routes
from tools.api import debug_routes as _debug_routes
from tools.api import order_routes as _order_routes

# Debounce window for /health health-file disk writes (seconds).
_HEALTH_FILE_DEBOUNCE_SECONDS = 10.0
_HEALTH_FILE_LAST_WRITE_TS = 0.0

# Re-export portfolio-sim cache helpers for backward compatibility: tests
# and operators poke these on the api module directly.
_fetch_live_hypothesis_ids = _simulate._fetch_live_hypothesis_ids
_get_portfolio_sim_cache = _simulate._get_portfolio_sim_cache
_store_portfolio_sim_cache = _simulate._store_portfolio_sim_cache
_PORTFOLIO_SIM_CACHE = _simulate._PORTFOLIO_SIM_CACHE
_PORTFOLIO_SIM_CACHE_MAX_ENTRIES = _simulate._PORTFOLIO_SIM_CACHE_MAX_ENTRIES
_PORTFOLIO_SIM_CACHE_TTL = _simulate._PORTFOLIO_SIM_CACHE_TTL

# --- Knowledge Wiki endpoints (LLM Wiki pattern) ---

@app.get("/wiki/stats", dependencies=[Depends(require_admin_or_loopback)])
async def wiki_stats():
    """Get wiki compilation statistics."""
    return await _wiki.wiki_stats()


@app.get("/wiki/articles", dependencies=[Depends(require_admin_or_loopback)])
async def wiki_articles(domain: Optional[str] = None, limit: int = 50):
    """List wiki articles, optionally filtered by domain."""
    return await _wiki.wiki_articles(domain=domain, limit=limit)


@app.get("/wiki/article/{topic}", dependencies=[Depends(require_admin_or_loopback)])
async def wiki_article(topic: str):
    """Get a specific wiki article by topic slug."""
    return await _wiki.wiki_article(topic)


@app.get("/wiki/search", dependencies=[Depends(require_admin_or_loopback)])
async def wiki_search(q: str, limit: int = 10):
    """Search wiki articles by keyword."""
    return await _wiki.wiki_search(q=q, limit=limit)


@app.get("/wiki/contradictions", dependencies=[Depends(require_admin_or_loopback)])
async def wiki_contradictions(unresolved_only: bool = True):
    """Get wiki contradiction findings."""
    return await _wiki.wiki_contradictions(unresolved_only=unresolved_only)


# --- Betting / Odds endpoints ---

@app.get("/odds/movements", dependencies=[Depends(require_admin_or_loopback)])
async def get_movements(sport: Optional[str] = None, limit: int = 20):
    """Get recent line movements detected by the monitor."""
    return await _odds_routes.get_movements(sport=sport, limit=limit)


@app.get("/odds/opportunities", dependencies=[Depends(require_admin_or_loopback)])
async def get_opportunities(status: str = "open", limit: int = 20):
    """Get current +EV betting opportunities."""
    return await _odds_routes.get_opportunities(status=status, limit=limit)


@app.get("/odds/snapshots/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def get_snapshots(sport: str, limit: int = 10):
    """Get snapshot history for a sport."""
    return await _odds_routes.get_snapshots(sport=sport, limit=limit)


@app.post("/odds/snapshot/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def force_snapshot(sport: str):
    """Force an immediate odds snapshot for a sport."""
    return await _odds_routes.force_snapshot(sport)


@app.get("/odds/edges", dependencies=[Depends(require_admin_or_loopback)])
async def get_edges(sport: Optional[str] = None):
    """Get latest cross-book edges, sharp money signals, and low-vig opportunities."""
    return _odds_routes.get_edges(sport=sport)


@app.get("/edges/live")
async def get_live_edges(
    sport: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = 50,
    _auth: None = Depends(require_admin_or_loopback),
):
    """Ranked live edge surface from the quant microstructure engine."""
    return await _odds_routes.get_live_edges(sport=sport, decision=decision, limit=limit)


@app.get("/odds/narrative-edges", dependencies=[Depends(require_admin_or_loopback)])
async def get_narrative_edges(sport: str = "basketball_nba"):
    """Detect player-level narrative edges for a sport."""
    return await _odds_routes.get_narrative_edges(sport)


@app.get("/odds/kl-metrics", dependencies=[Depends(require_admin_or_loopback)])
async def get_kl_metrics(sport: Optional[str] = None, limit: int = 50):
    """Get KL divergence metrics between odds snapshots."""
    return await _odds_routes.get_kl_metrics(sport=sport, limit=limit)


@app.post("/odds/parlay-scan/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def parlay_scan(sport: str):
    """Scan for correlated parlay edges on a sport. Pulls odds + alternates."""
    return await _odds_routes.parlay_scan(sport)


@app.get("/odds/sgp-analysis/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def sgp_analysis(sport: str):
    """Analyze SGP mispricing and excessive vig for a sport (cached snapshot data)."""
    return await _odds_routes.sgp_analysis(sport)


@app.get("/odds/props/{sport}/{event_id}", dependencies=[Depends(require_admin_or_loopback)])
async def scan_props(sport: str, event_id: str, target_book: str = "draftkings", threshold: float = 0.015):
    """Scan player props for +EV edges on target book."""
    return await _odds_routes.scan_props(
        sport, event_id, target_book=target_book, threshold=threshold
    )


@app.get("/odds/dk-props/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def dk_props(sport: str):
    """Scrape DraftKings player props for all games in a sport — FREE, no API credits."""
    return await _odds_routes.dk_props(sport)


@app.get("/odds/status", dependencies=[Depends(require_admin_or_loopback)])
async def odds_status():
    """Get line monitor status and credit info."""
    return await _odds_routes.odds_status()


@app.get("/odds/learned-correlations", dependencies=[Depends(require_admin_or_loopback)])
async def get_learned_correlations():
    """Get learned correlation estimates — Bayesian blend of priors + empirical data."""
    return await _odds_routes.get_learned_correlations()


# --- Bet Tracking & CLV ---

class BetSubmission(_bets.BetSubmission):
    pass


class BetResolution(_bets.BetResolution):
    pass


@app.post("/bets/record", dependencies=[Depends(require_admin)])
async def record_bet(bet: BetSubmission):
    """Record a bet at placement time for CLV tracking."""
    return await _bets.record_bet(bet)


@app.post("/bets/{bet_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_bet(bet_id: int, resolution: BetResolution):
    """Resolve a bet as won/lost/push."""
    return await _bets.resolve_bet(bet_id, resolution)


@app.get("/bets/clv-report", dependencies=[Depends(require_admin_or_loopback)])
async def clv_report(sport: Optional[str] = None):
    """Get CLV performance report — THE metric for edge measurement."""
    return await _bets.clv_report(sport=sport)


@app.get("/bets", dependencies=[Depends(require_admin_or_loopback)])
async def list_bets(result: Optional[str] = None, sport: Optional[str] = None, limit: int = 50):
    """Get bet history."""
    return await _bets.list_bets(result=result, sport=sport, limit=limit)


@app.get("/bets/bankroll", dependencies=[Depends(require_admin_or_loopback)])
async def bankroll_history(limit: int = 50):
    """Get bankroll balance history."""
    return await _bets.bankroll_history(limit=limit)


@app.post("/bets/bankroll/init", dependencies=[Depends(require_admin)])
async def init_bankroll(balance: float):
    """Set initial bankroll balance."""
    return await _bets.init_bankroll(balance)


# --- Market Structure Analysis ---

@app.get("/odds/market-analysis/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def market_analysis(sport: str):
    """Full market structure analysis — key numbers, stale lines, Pinnacle benchmark."""
    return await _odds_routes.market_analysis(sport)


@app.get("/odds/stale-lines/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def stale_lines(sport: str):
    """Find retail book lines that are stale vs sharp benchmark."""
    return await _odds_routes.stale_lines(sport)


@app.get("/odds/psychology/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def market_psychology(sport: str):
    """Run full market psychology analysis — number shading, attention arbitrage."""
    return await _odds_extra.market_psychology(sport)


@app.get("/odds/psychology", dependencies=[Depends(require_admin_or_loopback)])
async def market_psychology_all():
    """Return cached market psychology signals for all monitored sports."""
    return await _odds_extra.market_psychology_all()


@app.get("/odds/dead-numbers/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def dead_numbers_endpoint(sport: str):
    """Show dead number steals and key number analysis for a sport."""
    return await _odds_extra.dead_numbers_endpoint(sport)


@app.get("/analysis/futures-efficiency", dependencies=[Depends(require_admin_or_loopback)])
async def futures_efficiency_endpoint(
    current_odds: int = -200,
    record_wins: int = 30,
    record_losses: int = 20,
    games_played: int = 50,
    season_length: int = 82,
    sport: str = "basketball_nba",
):
    """Analyze if a futures bet is efficiently priced given current trajectory."""
    return _analysis.futures_efficiency_endpoint(
        current_odds=current_odds,
        record_wins=record_wins,
        record_losses=record_losses,
        games_played=games_played,
        season_length=season_length,
        sport=sport,
    )

@app.get("/analysis/half-market/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def half_market_endpoint(
    full_game_total: float = 220.0,
    half_total: float = 110.0,
    sport: str = "basketball_nba",
    half: str = "first",
):
    """Analyze half/quarter market efficiency vs full-game projections."""
    return _analysis.half_market_endpoint(
        full_game_total=full_game_total,
        half_total=half_total,
        sport=sport,
        half=half,
    )

@app.get("/analysis/cross-tabulate/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def cross_tabulate_endpoint(sport: str, min_sample: int = 20):
    """Multi-factor interaction analysis — discovers which factor combos produce edges."""
    return await _analysis.cross_tabulate_endpoint(sport, min_sample=min_sample)

@app.get("/odds/line-analysis/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def line_analysis_endpoint(sport: str):
    """Show RLM, steam moves, public side analysis, and bet timing for a sport.

    Analyzes the current snapshot for reverse line movement (sharp money
    indicator), steam moves (coordinated sharp action), estimated public
    side distribution, and optimal bet timing windows.

    Uses cached snapshot data (zero extra API credits).
    """
    return await _odds_extra.line_analysis_endpoint(sport)


@app.get("/bets/clv-forecast", dependencies=[Depends(require_admin_or_loopback)])
async def clv_forecast(sport: Optional[str] = None):
    """Forecast pre-game CLV for all pending bets using closing line prediction."""
    return await _bets.clv_forecast(sport=sport)


# --- Simulation & Contextual Data ---

class SimulationRequest(_simulate.SimulationRequest):
    pass


class PoissonRequest(_simulate.PoissonRequest):
    pass


@app.post("/simulate/basketball", dependencies=[Depends(require_admin_or_loopback)])
async def simulate_basketball_game(req: SimulationRequest):
    """Run Monte Carlo simulation and compare against market odds."""
    return await _simulate.simulate_basketball_game(req)


@app.post("/simulate/poisson", dependencies=[Depends(require_admin_or_loopback)])
async def simulate_poisson_game(req: PoissonRequest):
    """Run Poisson simulation for low-scoring sports."""
    return await _simulate.simulate_poisson_game(req)


@app.get("/simulate/portfolio", dependencies=[Depends(require_admin_or_loopback)])
async def simulate_portfolio_endpoint(
    hypothesis_ids: str = "",
    n_sims: int = 500,
    horizon_days: int = 90,
    starting_bankroll: float = 10000.0,
    kelly_fraction: float = 0.25,
    all_live: bool = False,
):
    """Run a bankroll Monte Carlo simulation for a portfolio of hypotheses.

    Query params:
      hypothesis_ids: CSV of hypothesis IDs (ignored if all_live=1)
      all_live: if true, simulate the full current LIVE roster
      n_sims: number of paths (capped at 5000)
      horizon_days: per-path horizon (capped at 365)
      starting_bankroll: dollar amount each path starts with
      kelly_fraction: Kelly multiplier (0.25 default = quarter-Kelly)

    Results cached 1hr per unique input signature.
    """
    import time as _time
    from tools.bankroll_sim import simulate_portfolio

    ids = await _simulate.resolve_portfolio_ids(hypothesis_ids=hypothesis_ids, all_live=all_live)
    n_sims, horizon_days = _simulate.normalize_portfolio_params(n_sims, horizon_days)

    cache_key = _simulate.build_portfolio_cache_key(
        ids, n_sims, horizon_days, starting_bankroll, kelly_fraction
    )
    now = _time.time()
    cached = _simulate._get_portfolio_sim_cache(cache_key)
    if cached:
        return {"cached": True, "age_seconds": round(now - cached[0], 1), **cached[1]}

    result = await asyncio.to_thread(
        simulate_portfolio,
        hypothesis_ids=ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
    )
    payload = result.to_dict(include_paths=False)
    _simulate._store_portfolio_sim_cache(cache_key, (now, payload))
    return {"cached": False, **payload}


@app.get("/model/total/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def get_model_total(sport: str, venue: str = "", wind_mph: float = None,
                          wind_dir: str = "", temp_f: float = None,
                          humidity: float = None, refs: str = ""):
    """Pace model total projections + environment adjustments for a sport."""
    return await _model_routes.get_model_total(
        sport, venue=venue, wind_mph=wind_mph, wind_dir=wind_dir,
        temp_f=temp_f, humidity=humidity, refs=refs,
    )


@app.get("/model/environment", dependencies=[Depends(require_admin_or_loopback)])
async def get_model_environment(venue: str, sport: str = "NFL",
                                wind_mph: float = None, wind_dir: str = "",
                                temp_f: float = None, humidity: float = None,
                                precipitation: str = "", refs: str = ""):
    """Environmental factors for a specific venue/game."""
    return await _model_routes.get_model_environment(
        venue, sport=sport, wind_mph=wind_mph, wind_dir=wind_dir,
        temp_f=temp_f, humidity=humidity, precipitation=precipitation, refs=refs,
    )


@app.get("/data/injuries/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def get_injuries(sport: str):
    """Get current injury report from ESPN with model analysis."""
    return await _model_routes.get_injuries(sport)


@app.get("/model/injury-impact/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def injury_impact_model(sport: str):
    """Run full injury model analysis for today's games."""
    return await _model_routes.injury_impact_model(sport)


@app.get("/data/scoreboard/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def get_scoreboard(sport: str):
    """Get live scoreboard from ESPN."""
    return await _data_routes.get_scoreboard(sport)


@app.get("/data/weather", dependencies=[Depends(require_admin_or_loopback)])
async def get_weather(latitude: float, longitude: float, venue: str = ""):
    """Get weather forecast for a venue."""
    return await _data_routes.get_weather(latitude, longitude, venue=venue)


@app.get("/data/referee", dependencies=[Depends(require_admin_or_loopback)])
async def referee_info(refs: str, sport: str = "basketball_nba"):
    """Get referee tendency adjustments. Pass refs as comma-separated names."""
    return _data_routes.referee_info(refs, sport)


# --- Line Gap Analysis ---

@app.get("/odds/line-gaps/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def line_gaps(sport: str, event_id: str = "", market: str = "alternate_spreads"):
    """Scan alternate lines for gaps — missing points that reveal risk concentration."""
    return await _odds_routes.line_gaps(sport, event_id=event_id, market=market)


@app.get("/odds/prop-gaps/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def prop_gaps(sport: str, event_id: str = ""):
    """Scan player props for line gaps across bookmakers."""
    return await _odds_routes.prop_gaps(sport, event_id=event_id)


# --- Profit Boost Evaluator ---

class FixedBoostRequest(BaseModel):
    boosted_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class PctBoostRequest(BaseModel):
    boost_pct: float
    base_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    max_stake: float = 100
    description: str = ""
    book: str = ""


class FreeBetRequest(BaseModel):
    free_bet_amount: float
    bet_odds: int
    fair_probability: Optional[float] = None
    odds_for: int = -110
    odds_against: int = -110
    stake_returned: bool = False
    description: str = ""
    book: str = ""


class HedgeRequest(BaseModel):
    boost_stake: float
    boosted_odds: int
    hedge_odds: int
    fair_probability: float


class BoostedParlayLeg(BaseModel):
    american_odds: int
    market: str
    description: str = ""


class BoostedParlayRequest(BaseModel):
    legs: list[BoostedParlayLeg]
    boosted_parlay_odds: int
    sport: str
    max_stake: float = 100
    description: str = ""
    book: str = ""


class DevigRequest(BaseModel):
    odds_a: int
    odds_b: int


@app.post("/boosts/evaluate-fixed", dependencies=[Depends(require_admin_or_loopback)])
async def eval_fixed_boost(req: FixedBoostRequest):
    """Evaluate a fixed profit boost — devig, compare to fair, calculate edge."""
    from tools.boost_evaluator import evaluate_fixed_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_fixed_boost(
        boosted_odds=req.boosted_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/evaluate-percentage", dependencies=[Depends(require_admin_or_loopback)])
async def eval_pct_boost(req: PctBoostRequest):
    """Evaluate a percentage profit boost token."""
    from tools.boost_evaluator import evaluate_percentage_boost, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_percentage_boost(
        boost_pct=req.boost_pct,
        base_odds=req.base_odds,
        fair_probability=fair_prob,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/evaluate-free-bet", dependencies=[Depends(require_admin_or_loopback)])
async def eval_free_bet(req: FreeBetRequest):
    """Evaluate a free bet or no-sweat bet."""
    from tools.boost_evaluator import evaluate_free_bet, devig_multiplicative

    fair_prob = req.fair_probability
    if fair_prob is None:
        fair_prob, _ = devig_multiplicative(req.odds_for, req.odds_against)

    return evaluate_free_bet(
        free_bet_amount=req.free_bet_amount,
        bet_odds=req.bet_odds,
        fair_probability=fair_prob,
        stake_returned=req.stake_returned,
        description=req.description,
        book=req.book,
    )


@app.post("/boosts/hedge", dependencies=[Depends(require_admin_or_loopback)])
async def hedge_calc(req: HedgeRequest):
    """Calculate optimal hedge for guaranteed profit."""
    from tools.boost_evaluator import calculate_hedge

    return calculate_hedge(
        boost_stake=req.boost_stake,
        boosted_odds=req.boosted_odds,
        hedge_odds=req.hedge_odds,
        fair_probability=req.fair_probability,
    )


@app.post("/boosts/devig", dependencies=[Depends(require_admin_or_loopback)])
async def devig(req: DevigRequest):
    """Devig a two-way market using multiplicative method."""
    from tools.boost_evaluator import devig_multiplicative, devig_additive

    mult_a, mult_b = devig_multiplicative(req.odds_a, req.odds_b)
    add_a, add_b = devig_additive(req.odds_a, req.odds_b)

    return {
        "multiplicative": {"side_a": mult_a, "side_b": mult_b},
        "additive": {"side_a": add_a, "side_b": add_b},
        "recommended": "multiplicative",
    }


@app.post("/boosts/evaluate-parlay", dependencies=[Depends(require_admin_or_loopback)])
async def eval_boosted_parlay(req: BoostedParlayRequest):
    """Evaluate a boosted parlay using correlation-adjusted fair odds.

    Books often boost parlays with correlated legs, making the boost look
    more generous than it is. This computes the TRUE fair probability using
    the correlation engine, then compares to the boosted odds.
    """
    from tools.boost_evaluator import evaluate_boosted_parlay

    legs = [leg.dict() for leg in req.legs]
    return evaluate_boosted_parlay(
        legs=legs,
        boosted_parlay_odds=req.boosted_parlay_odds,
        sport=req.sport,
        max_stake=req.max_stake,
        description=req.description,
        book=req.book,
    )


# --- Hypothesis Testing & Backtesting ---

class HypothesisCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    thesis: str = Field(..., min_length=1, max_length=10000)
    sport: str = Field(..., min_length=1, max_length=50)
    market_type: str = Field(..., min_length=1, max_length=100)
    hypothesis_model_config: dict = Field(default_factory=dict)
    edge_threshold: float = Field(default=0.02, ge=0.0, le=1.0)
    min_sample_size: int = Field(default=1000, ge=1, le=10_000_000)
    significance_level: float = Field(default=0.05, gt=0.0, lt=1.0)
    notes: str = Field(default="", max_length=5000)


class BacktestRequest(BaseModel):
    hypothesis_id: str
    start_date: str
    end_date: str
    credit_budget: int = 50


@app.post("/hypothesis", dependencies=[Depends(require_admin_or_loopback)])
async def create_hypothesis(req: HypothesisCreate):
    """Create a new testable betting hypothesis."""
    return await _hypothesis_routes.create_hypothesis(req)


@app.get("/hypothesis", dependencies=[Depends(require_admin_or_loopback)])
async def list_hypotheses(status: Optional[str] = None):
    """List all hypotheses, optionally filtered by status."""
    return await _hypothesis_routes.list_hypotheses(status=status)


@app.get(
    "/hypothesis/{hypothesis_id}",
    dependencies=[Depends(require_admin_or_loopback)],
)
async def get_hypothesis(hypothesis_id: str):
    """Get hypothesis details."""
    return await _hypothesis_routes.get_hypothesis(hypothesis_id)


@app.get(
    "/hypothesis/{hypothesis_id}/report",
    dependencies=[Depends(require_admin_or_loopback)],
)
async def hypothesis_report(hypothesis_id: str):
    """Full statistical report across all stages."""
    return await _hypothesis_routes.hypothesis_report(hypothesis_id)


@app.get(
    "/hypothesis/{hypothesis_id}/significance",
    dependencies=[Depends(require_admin_or_loopback)],
)
async def hypothesis_significance(hypothesis_id: str, stage: str = "backtest"):
    """Run significance tests on a hypothesis at a given stage."""
    return await _hypothesis_routes.hypothesis_significance(hypothesis_id, stage)


@app.post("/hypothesis/{hypothesis_id}/promote", dependencies=[Depends(require_admin)])
async def promote_hypothesis(hypothesis_id: str):
    """Check readiness and promote to next stage if criteria are met."""
    return await _hypothesis_routes.promote_hypothesis(hypothesis_id)


@app.patch("/hypothesis/{hypothesis_id}", dependencies=[Depends(require_admin)])
async def update_hypothesis(hypothesis_id: str, request: Request):
    """Update hypothesis status, threshold, model_config, or notes."""
    return await _hypothesis_routes.update_hypothesis(hypothesis_id, request)


@app.post("/backtest/run", dependencies=[Depends(require_admin)])
async def run_backtest(req: BacktestRequest):
    """Start a backtest run on a hypothesis against historical data."""
    return await _backtest_routes.run_backtest(req)


@app.get("/backtest/run/{run_id}", dependencies=[Depends(require_admin_or_loopback)])
async def get_backtest_results(run_id: str):
    """Get backtest results for a run."""
    return await _backtest_routes.get_backtest_results(run_id)


@app.post("/backtest/resolve/{run_id}", dependencies=[Depends(require_admin_or_loopback)])
async def resolve_backtest(run_id: str, sport: str = "basketball_nba"):
    """Resolve backtest events against actual game results."""
    return await _backtest_routes.resolve_backtest(run_id, sport)


@app.get("/historical/cache", dependencies=[Depends(require_admin_or_loopback)])
async def historical_cache_stats():
    """Get historical odds cache statistics."""
    return await _backtest_routes.historical_cache_stats()


@app.post("/historical/fetch", dependencies=[Depends(require_admin)])
async def fetch_historical(
    sport: str,
    start_date: str,
    end_date: str,
    credit_budget: int = 50,
):
    """Fetch historical odds for a date range (cached after first fetch)."""
    return await _backtest_routes.fetch_historical(
        sport=sport, start_date=start_date, end_date=end_date,
        credit_budget=credit_budget,
    )


# ── Research Loop Endpoints ──

@app.get("/research/status", dependencies=[Depends(require_admin_or_loopback)])
async def research_status():
    """Get research loop status."""
    return await _research_routes.research_status()


@app.post("/research/pause", dependencies=[Depends(require_admin)])
async def research_pause():
    """Pause the research loop."""
    return await _research_routes.research_pause()


@app.post("/research/resume", dependencies=[Depends(require_admin)])
async def research_resume():
    """Resume the research loop."""
    return await _research_routes.research_resume()


@app.post("/research/local-only", dependencies=[Depends(require_admin)])
async def research_local_only(enabled: bool = True):
    """Toggle local-only mode (no Claude Code calls)."""
    return _research_routes.research_local_only(enabled)


@app.post("/research/collect", dependencies=[Depends(require_admin)])
async def research_collect(sport: str = "basketball_nba", date: Optional[str] = None):
    """Manually trigger data collection for a sport."""
    return await _research_routes.research_collect(sport, date)


@app.post("/research/generate", dependencies=[Depends(require_admin)])
async def research_generate(sport: str = "basketball_nba", max_hypotheses: int = 20):
    """Manually trigger hypothesis generation."""
    return await _research_routes.research_generate(sport, max_hypotheses)


@app.post("/research/batch-reject", dependencies=[Depends(require_admin)])
async def batch_reject_hypotheses(request: Request):
    """Batch-reject draft hypotheses matching regex patterns."""
    body = await request.json()
    return await _research_routes.batch_reject_hypotheses(body)


@app.get("/research/sports", dependencies=[Depends(require_admin_or_loopback)])
async def get_research_sports():
    """Get all researched sports — all compete equally."""
    return await _research_routes.get_research_sports()


@app.get("/embeddings/stats", dependencies=[Depends(require_admin_or_loopback)])
async def embedding_stats(collection: Optional[str] = None):
    """Get embedding store statistics."""
    return await _data_routes.embedding_stats(collection)


@app.post("/embeddings/search", dependencies=[Depends(require_admin_or_loopback)])
async def embedding_search(
    collection: str,
    query: str,
    top_k: int = 10,
):
    """Search embeddings by text similarity."""
    return await _data_routes.embedding_search(collection, query, top_k)


@app.get("/data/stats", dependencies=[Depends(require_admin_or_loopback)])
async def data_collection_stats():
    """Get data collection statistics."""
    return await _data_routes.data_collection_stats()


# Health evaluation logic moved to tools/api/system_routes.py.
_evaluate_health_signals = _system_routes.evaluate_health_signals
_build_health_report = _system_routes.build_health_report
# Regime lookups (detect_regime et al.) stay off the event loop via
# `await asyncio.to_thread(detect_regime, sp)` inside tools/api/system_routes.py.


@app.get("/health")
async def health_check():
    """
    Comprehensive health check — Layer 2 (subsystems, breakers, integrity).
    PUBLIC: polled by the sentinel and watchdog; must never gain an admin dep.
    """
    # Write health file for sentinel to read if HTTP is down.
    # Debounced: watchdog polls this endpoint frequently, so skip the disk
    # write if the last successful write was < 10s ago. Offload to a thread
    # so sync JSON IO never blocks the event loop. Never fail /health here.
    global _HEALTH_FILE_LAST_WRITE_TS
    report = await _build_health_report()
    if system_health:
        import time as _time
        now_ts = _time.time()
        if (now_ts - _HEALTH_FILE_LAST_WRITE_TS) >= _HEALTH_FILE_DEBOUNCE_SECONDS:
            try:
                await asyncio.to_thread(system_health.write_health_file)
                _HEALTH_FILE_LAST_WRITE_TS = now_ts
            except Exception:
                pass
    return report


@app.get("/health/livez")
async def health_livez():
    """k8s-style liveness: process is up and responsive. PUBLIC."""
    return await _system_routes.health_livez()


@app.get("/health/readyz")
async def health_readyz():
    """k8s-style readiness: ready to serve traffic. PUBLIC; 503 when degraded."""
    return await _system_routes.health_readyz()


@app.get("/health/detailed", dependencies=[Depends(require_admin_or_loopback)])
async def health_detailed():
    """
    Everything /health returns, plus per-source ingestion SLAs and
    per-subsystem trip history. For external observability tools.
    """
    return await _system_routes.health_detailed()


@app.get("/regime/sizer-multipliers", dependencies=[Depends(require_admin_or_loopback)])
async def regime_sizer_multipliers():
    """Current regime multiplier per sport, as the portfolio sizer would apply them."""
    return await _system_routes.regime_sizer_multipliers()


@app.get("/admin/writer", dependencies=[Depends(require_admin)])
async def writer_stats():
    """Per-DB WriteCoordinator stats: queue depth, throughput, slowest op."""
    return _system_routes.writer_stats()


@app.get("/health/deep", dependencies=[Depends(require_admin_or_loopback)])
async def health_deep():
    """
    Full pipeline integrity suite — runs ALL checks on demand. GATED.
    """
    return await _system_routes.health_deep()


@app.get("/health/integrity/history", dependencies=[Depends(require_admin_or_loopback)])
async def integrity_history(limit: int = 50):
    """Get recent pipeline integrity check history."""
    return await _system_routes.integrity_history(limit=limit)


@app.get("/claude/status", dependencies=[Depends(require_admin_or_loopback)])
async def claude_status():
    """Get Claude Code availability and usage stats."""
    return await _system_routes.claude_status()


@app.post("/admin/claude/reset", dependencies=[Depends(require_admin)])
async def reset_claude_rate_limit():
    """Force-reset Claude Code rate limit state after hourly limit resets."""
    return _system_routes.reset_claude_rate_limit()


@app.get("/system/full-status", dependencies=[Depends(require_admin_or_loopback)])
async def full_system_status():
    """
    Single endpoint for checking everything from your phone.
    Returns all subsystem statuses in one call.
    """
    return await _system_routes.full_system_status()


# ---------------------------------------------------------------------------
# Task listing & context sync
# ---------------------------------------------------------------------------

@app.get("/tasks", dependencies=[Depends(require_admin_or_loopback)])
async def list_tasks(
    status: Optional[str] = None,
    limit: int = 10,
    _auth: None = Depends(require_admin_or_loopback),
):
    """List recent tasks from the queue.

    Loopback-or-admin gated: task rows embed the original user query text and
    session_ids, which leak conversation content if reachable non-loopback.
    `/task/{id}` was already gated; this brings the bulk listing in line.
    """
    # Refresh WAL snapshot to see externally-committed rows
    try:
        await queue._db.commit()
    except Exception:
        pass
    limit = max(1, min(int(limit), 500))
    rows = await queue._db.execute_fetchall(
        """SELECT task_id, query, status, priority, session_id,
                  created_at, started_at, completed_at
           FROM task_queue
           ORDER BY created_at DESC LIMIT ?""",
        (limit,)
    )
    columns = ["task_id", "query", "status", "priority", "session_id",
               "created_at", "started_at", "completed_at"]
    tasks = [dict(zip(columns, row)) for row in rows]
    if status:
        tasks = [t for t in tasks if t["status"] == status.upper()]
    return {"count": len(tasks), "tasks": tasks}


class ContextSync(BaseModel):
    session_summary: str = Field(..., min_length=1, max_length=20000)
    actionable_queries: list[str] = Field(default_factory=list, max_length=50)

@app.post("/context/sync")
async def sync_context(ctx: ContextSync, _auth: None = Depends(require_admin)):
    """Receive context from a Claude Code session. Queues actionable items."""
    submitted = []
    for q in ctx.actionable_queries:
        if not q or len(q) > 20000:
            raise HTTPException(status_code=422, detail="actionable_queries entries must be 1-20000 chars")
        task_id = await queue.submit_task(q, priority=1)
        submitted.append(task_id)
    return {
        "received": True,
        "tasks_submitted": len(submitted),
        "task_ids": submitted,
    }


_restart_task: Optional[asyncio.Task] = None


@app.post("/admin/restart")
async def admin_restart(confirm: str = "", _auth: None = Depends(require_admin_or_loopback)):
    """Graceful restart — exits process, watchdog brings it back with new code.

    Requires confirm=YES to prevent accidental restarts.
    Without watchdog.bat running, this will KILL the system with no relaunch.

    Auth: admin-token OR loopback.  Previously required CALLISTO_ADMIN_TOKEN
    unconditionally, which meant localhost scripts (and the human using curl)
    had no restart path when the token was unset — forcing reliance on the
    signal file and the watchdog picking it up.  Loopback-allowed restores
    an in-process restart path even with the token unset.
    """
    # SECURITY: timing-safe equality (audit C-2). Token is "YES" — short, but pattern is
    # what matters: never use `==` or `!=` on auth-adjacent strings.
    if not _secrets.compare_digest(confirm, "YES"):
        raise HTTPException(
            status_code=400,
            detail="Add ?confirm=YES to actually restart. WARNING: without watchdog, system will not relaunch.",
        )
    logger.info("RESTART REQUESTED via /admin/restart — shutting down gracefully")
    send_msg = "Callisto restarting (code reload requested)"
    try:
        await telegram.alert_system(send_msg)
    except Exception as e:
        logger.info(f"Telegram restart notification failed (non-critical): {e}")

    # Give time for this response to be sent, then exit
    async def _delayed_exit():
        await asyncio.sleep(1)
        logger.info("Exiting for restart...")
        os._exit(0)

    # Track task so shutdown handler can cancel it cleanly (audit H-14).
    global _restart_task
    _restart_task = asyncio.create_task(_delayed_exit())
    return {"status": "restarting", "message": "Watchdog will restart with new code in ~15 seconds"}


_tracemalloc_snapshot = _debug_routes._tracemalloc_snapshot


@app.get("/debug/memory", dependencies=[Depends(require_admin_or_loopback)])
async def debug_memory(_auth: None = Depends(require_admin)):
    """tracemalloc snapshot comparison — identifies the top growing allocations."""
    return await _debug_routes.debug_memory(_auth)


@app.get("/debug/memory/top-traces", dependencies=[Depends(require_admin_or_loopback)])
async def debug_memory_traces(limit: int = 10, _auth: None = Depends(require_admin)):
    """Show full stack traces for the top memory consumers."""
    return await _debug_routes.debug_memory_traces(limit=limit)


@app.post("/debug/memory/gc")
async def debug_gc(_auth: None = Depends(require_admin)):
    """Force garbage collection and report stats."""
    return await _debug_routes.debug_gc()


# /admin/sql validator + handler moved to tools/api/debug_routes.py.
_validate_admin_sql = _debug_routes.validate_admin_sql
_ALLOWED_PRAGMAS = _debug_routes._ALLOWED_PRAGMAS


@app.post("/admin/sql")
async def admin_sql(request: Request, _auth: None = Depends(require_admin)):
    """Read-only SQL query against callisto.db for debugging (AST-validated)."""
    body = await request.json()
    client_host = request.client.host if request.client else "?"
    return await _debug_routes.admin_sql(body, client_host=client_host)


# ---------------------------------------------------------------------------
# Bet executor endpoints
# ---------------------------------------------------------------------------
_executor = None


_get_executor = _order_routes.get_executor


@app.get("/executor/status", dependencies=[Depends(require_admin_or_loopback)])
async def executor_status():
    """Get bet executor status."""
    return await _order_routes.executor_status()


@app.post("/executor/enable", dependencies=[Depends(require_admin)])
async def executor_enable():
    """Enable both the order manager and the legacy bet executor."""
    return await _order_routes.executor_enable()


@app.post("/executor/disable", dependencies=[Depends(require_admin_or_loopback)])
async def executor_disable():
    """Disable both subsystems — no orders will be submitted or placed."""
    return await _order_routes.executor_disable()


@app.get("/orders", dependencies=[Depends(require_admin_or_loopback)])
async def orders_list(state: Optional[str] = None, limit: int = 50):
    """List orders, optionally filtered by state."""
    return await _order_routes.orders_list(state=state, limit=limit)


@app.get("/orders/{order_id}", dependencies=[Depends(require_admin_or_loopback)])
async def orders_get(order_id: str):
    """Fetch one order including full state history."""
    return await _order_routes.orders_get(order_id)


@app.post("/orders/{order_id}/approve", dependencies=[Depends(require_admin)])
async def orders_approve(order_id: str):
    return await _order_routes.orders_approve(order_id)


@app.post("/orders/{order_id}/reject", dependencies=[Depends(require_admin)])
async def orders_reject(order_id: str, reason: str = "http_reject"):
    return await _order_routes.orders_reject(order_id, reason=reason)


@app.post("/orders/{order_id}/fill", dependencies=[Depends(require_admin)])
async def orders_fill(order_id: str, actual_price: Optional[int] = None):
    return await _order_routes.orders_fill(order_id, actual_price=actual_price)


@app.post("/orders/reconcile", dependencies=[Depends(require_admin_or_loopback)])
async def orders_reconcile():
    """Trigger the settlement reconciler immediately (cron path)."""
    return await _order_routes.orders_reconcile()


@app.post("/orders/voids", dependencies=[Depends(require_admin_or_loopback)])
async def orders_voids():
    """Trigger the postponed/cancelled game void-detector immediately."""
    return await _order_routes.orders_voids()


@app.post("/orders/expire", dependencies=[Depends(require_admin_or_loopback)])
async def orders_expire():
    """Trigger the expiry sweep immediately."""
    return await _order_routes.orders_expire()


@app.post("/executor/login", dependencies=[Depends(require_admin)])
async def executor_login():
    """Launch browser for DraftKings login. Browser opens visible for manual login."""
    return await _order_routes.executor_login()


if __name__ == "__main__":
    import socket
    import uvicorn

    # Wait for port to be free — the #1 cause of crash-loops.
    # Windows holds TCP sockets in TIME_WAIT for up to 4 minutes after
    # the process dies. Without this check, uvicorn bind fails silently
    # with exit code 0xC0000142 and the watchdog loops forever.
    for attempt in range(30):  # 30 × 2s = 60s max wait
        try:
            test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_sock.bind((CALLISTO_BIND_HOST, CALLISTO_PORT))
            test_sock.close()
            break  # Port is free
        except OSError:
            if attempt < 29:
                import time as _time
                logger.warning(f"Port {CALLISTO_PORT} in use, waiting... (attempt {attempt+1}/30)")
                _time.sleep(2)
            else:
                logger.error(f"Port {CALLISTO_PORT} still in use after 60s — exiting")
                import sys
                sys.exit(1)

    uvicorn.run("api:app", host=CALLISTO_BIND_HOST, port=CALLISTO_PORT, reload=False)
