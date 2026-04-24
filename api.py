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
from tools.state_paths import restart_signal_path, state_dir
from tools.order_manager import (
    OrderManager,
    reconcile_filled_orders,
    detect_voided_orders,
    get_manager as _get_order_manager,
)
from tools.metrics import (
    get_registry as _metrics_registry,
    observe_task_duration as _metrics_observe_task_duration,
    record_task_submission as _metrics_record_task_submission,
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


_wal_health_state: dict = {
    "last_checkpoint_ts": None,
    "last_checkpoint_duration_s": None,
    "last_wal_pages_before": None,
    "last_wal_pages_after": None,
    "last_wal_mb_before": None,
    "last_wal_mb_after": None,
    "last_checkpointed": None,
    "last_mode": None,
    "last_truncate_ts": None,
    "last_truncate_pages_before": None,
    "last_truncate_pages_after": None,
    "checkpoint_errors_total": 0,
    "checkpoints_total": 0,
    "truncates_total": 0,
    "maintenance_started_ts": time.time(),
    "db_path": DB_PATH,
}

WAL_MAINTENANCE_INTERVAL_S = int(os.getenv("CALLISTO_WAL_MAINTENANCE_INTERVAL_S", "600"))
WAL_TRUNCATE_PAGE_THRESHOLD = int(os.getenv("CALLISTO_WAL_TRUNCATE_PAGE_THRESHOLD", "5000"))


async def wal_maintenance_loop():
    """Periodic WAL checkpoint + memory guardian.

    Every ``CALLISTO_WAL_MAINTENANCE_INTERVAL_S`` seconds (default 10 min):
    1. Memory guardian: if RSS > 2GB, signal graceful restart.
    2. PASSIVE checkpoint: flush committed pages back to the main DB.
    3. If WAL > WAL_TRUNCATE_PAGE_THRESHOLD pages (default 5000), run
       TRUNCATE to reset the WAL file on disk.

    All metrics (page counts, duration, success/failure) are captured in
    ``_wal_health_state`` so /admin/db/health can expose them.
    """
    MEMORY_RESTART_MB = 2048
    while True:
        try:
            await asyncio.sleep(WAL_MAINTENANCE_INTERVAL_S)

            try:
                import psutil
                rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
                if rss_mb > MEMORY_RESTART_MB:
                    logger.warning(
                        f"MEMORY GUARDIAN: RSS={rss_mb:.0f}MB > {MEMORY_RESTART_MB}MB — "
                        f"requesting graceful restart to prevent OOM crash"
                    )
                    restart_file = restart_signal_path()
                    with open(restart_file, "w", encoding="utf-8") as f:
                        f.write(f"memory_guardian: RSS={rss_mb:.0f}MB at {datetime.now()}")
                    await asyncio.sleep(2)
                    logger.warning("MEMORY GUARDIAN: exiting for restart")
                    os._exit(0)
                elif rss_mb > MEMORY_RESTART_MB * 0.75:
                    logger.info(f"Memory check: {rss_mb:.0f}MB (warning threshold: {MEMORY_RESTART_MB}MB)")
            except ImportError:
                pass

            started = time.monotonic()
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("PRAGMA busy_timeout = 60000")
                    cursor = await db.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    row = await cursor.fetchone()
                    if row:
                        busy, log_pages, checkpointed = row
                        wal_size_mb = (log_pages * 4096) / (1024 * 1024)
                        _wal_health_state["last_mode"] = "PASSIVE"
                        _wal_health_state["last_wal_pages_before"] = log_pages
                        _wal_health_state["last_wal_pages_after"] = max(log_pages - (checkpointed or 0), 0)
                        _wal_health_state["last_wal_mb_before"] = round(wal_size_mb, 3)
                        _wal_health_state["last_checkpointed"] = checkpointed
                        logger.info(
                            f"WAL checkpoint PASSIVE: busy={busy} log={log_pages} pages "
                            f"({wal_size_mb:.1f} MB) checkpointed={checkpointed}"
                        )
                        if log_pages > WAL_TRUNCATE_PAGE_THRESHOLD:
                            async with aiosqlite.connect(DB_PATH) as trunc_db:
                                await trunc_db.execute("PRAGMA busy_timeout = 30000")
                                cursor2 = await trunc_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                                row2 = await cursor2.fetchone()
                                if row2:
                                    t_busy, t_log, t_ckpt = row2
                                    _wal_health_state["last_mode"] = "TRUNCATE"
                                    _wal_health_state["last_truncate_ts"] = time.time()
                                    _wal_health_state["last_truncate_pages_before"] = log_pages
                                    _wal_health_state["last_truncate_pages_after"] = t_log
                                    _wal_health_state["last_wal_pages_after"] = t_log
                                    _wal_health_state["last_wal_mb_after"] = round((t_log * 4096) / (1024 * 1024), 3)
                                    _wal_health_state["truncates_total"] = _wal_health_state.get("truncates_total", 0) + 1
                                    logger.info(
                                        f"WAL TRUNCATE: busy={t_busy} log={t_log} "
                                        f"(was {log_pages}) checkpointed={t_ckpt} "
                                        f"threshold={WAL_TRUNCATE_PAGE_THRESHOLD}"
                                    )
                                    if t_busy and t_log > 0:
                                        logger.warning(
                                            f"WAL TRUNCATE incomplete: {t_log} pages remain — "
                                            f"persistent readers blocking checkpoint"
                                        )
                        else:
                            _wal_health_state["last_wal_mb_after"] = _wal_health_state["last_wal_mb_before"]
                _wal_health_state["last_checkpoint_ts"] = time.time()
                _wal_health_state["last_checkpoint_duration_s"] = round(time.monotonic() - started, 3)
                _wal_health_state["checkpoints_total"] = _wal_health_state.get("checkpoints_total", 0) + 1
            except Exception as ckpt_err:
                _wal_health_state["checkpoint_errors_total"] = _wal_health_state.get("checkpoint_errors_total", 0) + 1
                logger.warning(f"WAL checkpoint failed (non-fatal): {ckpt_err}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning(f"wal_maintenance_loop iteration error: {e}")


wal_checkpoint_loop = wal_maintenance_loop


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
#
# PERSISTENCE: The alerted-source set is mirrored to a JSON file under
# ``tools.state_paths.state_dir()`` so watchdog-driven API restarts don't
# re-fire investigation tasks for every stale source on startup. Prior
# to persistence, a restart with 22 stale sources would flood the queue
# with 22 duplicates instantly — and the queue had accumulated 599 such
# entries on 2026-04-23.
#
# DEFENSE IN DEPTH: Before inserting a new task we also check whether a
# PENDING or PROCESSING ``investigate: ingestion source '<X>'`` task
# already exists for this source. If the state file is ever lost
# (disk wipe, STATE_DIR env change, manual deletion) but the queue still
# holds the tasks, this DB-level guard blocks duplicates.
SLA_ALERTED_SOURCES_PATH = state_dir() / "sla_alerted_sources.json"
INGESTION_SLA_CHECK_INTERVAL_S = 300  # 5 min


def _load_sla_alerted_sources() -> set[str]:
    """Load the persisted alerted-source set. Missing/corrupt file → empty."""
    try:
        if not SLA_ALERTED_SOURCES_PATH.exists():
            return set()
        import json as _json
        raw = SLA_ALERTED_SOURCES_PATH.read_text(encoding="utf-8")
        data = _json.loads(raw)
        if isinstance(data, list):
            return {str(x) for x in data if isinstance(x, str)}
        if isinstance(data, dict) and isinstance(data.get("sources"), list):
            return {str(x) for x in data["sources"] if isinstance(x, str)}
        return set()
    except Exception as e:
        logger.warning(
            f"SLA watchdog: could not load {SLA_ALERTED_SOURCES_PATH}: {e}"
        )
        return set()


def _save_sla_alerted_sources(sources: set[str]) -> None:
    """Atomically persist the alerted-source set (tmp + replace)."""
    try:
        import json as _json
        payload = {
            "sources": sorted(sources),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }
        target = SLA_ALERTED_SOURCES_PATH
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, target)
    except Exception as e:
        logger.warning(
            f"SLA watchdog: could not persist {SLA_ALERTED_SOURCES_PATH}: {e}"
        )


_sla_alerted_sources: set[str] = _load_sla_alerted_sources()


def _sla_investigate_query_prefix(source: str) -> str:
    """Stable prefix used by the task-queue dedup LIKE match.

    Must match the exact opening of the query string submitted in
    ``ingestion_sla_watchdog_loop`` so existing PENDING/PROCESSING rows
    are detected.
    """
    return f"investigate: ingestion source '{source}'"


async def _pending_investigate_task_exists(db, source: str) -> bool:
    """Return True iff a PENDING/PROCESSING investigate-task already
    exists in ``task_queue`` for this source. Fail-open on DB errors so
    transient issues never mask a real stale-source alert."""
    try:
        cursor = await db.execute(
            "SELECT 1 FROM task_queue "
            "WHERE status IN ('PENDING','PROCESSING') "
            "  AND query LIKE ? "
            "LIMIT 1",
            (f"{_sla_investigate_query_prefix(source)}%",),
        )
        row = await cursor.fetchone()
        return row is not None
    except Exception as e:
        logger.debug(f"SLA watchdog: dedup check failed for {source}: {e}")
        return False


async def ingestion_sla_watchdog_loop():
    """Periodic SLA audit. Self-submits /task queries on breach."""
    from tools.health import resolve_sla_seconds, CRITICAL_MULTIPLIER

    while True:
        try:
            await asyncio.sleep(INGESTION_SLA_CHECK_INTERVAL_S)

            still_stale: set[str] = set()
            scan_completed = False
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
                    scan_completed = True

                    for source, last_status, age_s in rows:
                        if age_s is None:
                            continue
                        sla = resolve_sla_seconds(source)
                        if float(age_s) > sla * CRITICAL_MULTIPLIER:
                            still_stale.add(source)
                            if source in _sla_alerted_sources:
                                continue  # already filed (process-local memory)
                            if await _pending_investigate_task_exists(db, source):
                                _sla_alerted_sources.add(source)
                                _save_sla_alerted_sources(_sla_alerted_sources)
                                logger.info(
                                    f"SLA watchdog: {source} already has a "
                                    f"pending investigate-task — skipping submit"
                                )
                                continue
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
                                _save_sla_alerted_sources(_sla_alerted_sources)
                                logger.warning(
                                    f"SLA watchdog: filed investigation task for {source} "
                                    f"({minutes} min stale)"
                                )
                            except Exception as e:
                                logger.warning(f"SLA watchdog: submit_task failed: {e}")
            except Exception as e:
                logger.debug(f"SLA watchdog query failed: {e}")
                continue

            # Recovery: drop sources that have recovered so a future breach
            # re-files. Only run when the scan actually succeeded — a DB
            # error doesn't mean every source recovered.
            if scan_completed:
                recovered = _sla_alerted_sources - still_stale
                if recovered:
                    for src in recovered:
                        _sla_alerted_sources.discard(src)
                        logger.info(f"SLA watchdog: {src} recovered, re-arming alert")
                    _save_sla_alerted_sources(_sla_alerted_sources)

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
            _worker_pickup_ts = time.time()

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
                try:
                    _metrics_observe_task_duration(
                        "failed", max(0.0, time.time() - _worker_pickup_ts)
                    )
                except Exception:
                    pass
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
                try:
                    _metrics_observe_task_duration(
                        "completed", float(telemetry.get("elapsed_s") or 0.0)
                    )
                except Exception:
                    pass
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
                try:
                    _metrics_observe_task_duration(
                        "timeout", float(t.get("elapsed_s") or (time.time() - _worker_pickup_ts))
                    )
                except Exception:
                    pass
            except asyncio.TimeoutError:
                # Fallback path — shouldn't happen since _run_session_with_adaptive_timeout
                # wraps into _AdaptiveTimeout, but be defensive.
                err_msg = (
                    f"timeout: orchestrator exceeded {initial_budget:.0f}s budget "
                    f"(type={task_type.value}, no telemetry)"
                )
                logger.error(f"Task {task_id} TIMEOUT (bare): {err_msg}")
                await queue.timeout_task(task_id, err_msg)
                try:
                    _metrics_observe_task_duration(
                        "timeout", max(0.0, time.time() - _worker_pickup_ts)
                    )
                except Exception:
                    pass
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                await queue.fail_task(task_id, str(e))
                try:
                    _metrics_observe_task_duration(
                        "failed", max(0.0, time.time() - _worker_pickup_ts)
                    )
                except Exception:
                    pass

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
    global memory, queue, orchestrator_instance, monitor, line_monitor, clv_tracker, autonomous, telegram_listener, hypothesis_manager, historical_fetcher, backtest_engine, vector_store, hypothesis_generator, data_collector, research_loop, system_health, learned_correlation_store, worker_task, wal_checkpoint_task, restart_signal_task, order_cron_task, order_manager_instance, live_state_collector, live_state_task, prop_resolver_task

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

    # Line movement monitor — autonomous odds tracking
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

    # Odds WebSocket — real-time odds streaming from Odds-API.io Pro
    try:
        from tools.odds_ws import start_odds_stream
        await start_odds_stream()
        logger.info("Odds WebSocket stream started (15 books, real-time)")
    except Exception as e:
        logger.warning(f"Odds WebSocket failed to start: {e}")

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
    wal_checkpoint_task = asyncio.create_task(wal_maintenance_loop())
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
        f"(WAL maintenance {WAL_MAINTENANCE_INTERVAL_S}s, "
        f"restart-signal watcher active, ingestion SLA watchdog 5m, "
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
    try:
        from tools.odds_ws import stop_odds_stream
        await stop_odds_stream()
    except Exception:
        pass
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

        try:
            _metrics_record_task_submission(submission.priority, source="api")
        except Exception:
            pass

        if short_circuit_result is not None:
            try:
                await queue.complete_task(task_id, short_circuit_result)
                try:
                    _metrics_observe_task_duration("short_circuit", 0.0)
                except Exception:
                    pass
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


# --- Knowledge Wiki endpoints (LLM Wiki pattern) ---

@app.get("/wiki/stats")
async def wiki_stats():
    """Get wiki compilation statistics."""
    from tools.knowledge_wiki import get_wiki
    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        return await wiki.get_stats(db)


@app.get("/wiki/articles")
async def wiki_articles(domain: Optional[str] = None, limit: int = 50):
    """List wiki articles, optionally filtered by domain."""
    from tools.knowledge_wiki import get_wiki
    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        articles = await wiki.list_articles(db, domain=domain, limit=limit)
        return {"count": len(articles), "articles": articles}


@app.get("/wiki/article/{topic}")
async def wiki_article(topic: str):
    """Get a specific wiki article by topic slug."""
    from tools.knowledge_wiki import get_wiki
    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        article = await wiki.get_article(db, topic)
        if not article:
            raise HTTPException(status_code=404, detail=f"Article '{topic}' not found")
        return article


@app.get("/wiki/search")
async def wiki_search(q: str, limit: int = 10):
    """Search wiki articles by keyword."""
    from tools.knowledge_wiki import get_wiki
    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        results = await wiki.search(db, q, limit=limit)
        return {"query": q, "count": len(results), "results": results}


@app.get("/wiki/contradictions")
async def wiki_contradictions(unresolved_only: bool = True):
    """Get wiki contradiction findings."""
    from tools.knowledge_wiki import get_wiki
    wiki = get_wiki()
    async with aiosqlite.connect(memory.db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        items = await wiki.get_contradictions(db, unresolved_only=unresolved_only)
        return {"count": len(items), "contradictions": items}


# --- Betting / Odds endpoints ---

@app.get("/odds/movements")
async def get_movements(sport: Optional[str] = None, limit: int = 20):
    """Get recent line movements detected by the monitor."""
    movements = await line_monitor.get_recent_movements(sport=sport, limit=limit)
    return {"count": len(movements), "movements": movements}


@app.get("/odds/opportunities")
async def get_opportunities(status: str = "open", limit: int = 20):
    """Get current +EV betting opportunities."""
    opps = await line_monitor.get_ev_opportunities(status=status, limit=limit)
    return {"count": len(opps), "opportunities": opps}


@app.get("/odds/snapshots/{sport}")
async def get_snapshots(sport: str, limit: int = 10):
    """Get snapshot history for a sport."""
    snaps = await line_monitor.get_snapshot_history(sport=sport, limit=limit)
    return {"sport": sport, "count": len(snaps), "snapshots": snaps}


@app.post("/odds/snapshot/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def force_snapshot(sport: str):
    """Force an immediate odds snapshot for a sport."""
    result = await line_monitor.force_snapshot(sport)
    return {
        "sport": sport,
        "game_count": result.get("game_count", 0),
        "credits": result.get("credits", {}),
    }


@app.get("/odds/edges")
async def get_edges(sport: Optional[str] = None):
    """Get latest cross-book edges, sharp money signals, and low-vig opportunities."""
    report = line_monitor.get_edge_report(sport=sport)
    return report


@app.get("/edges/live")
async def get_live_edges(
    sport: Optional[str] = None,
    decision: Optional[str] = None,
    limit: int = 50,
):
    """Ranked live edge surface from the quant microstructure engine.

    Returns the most recent snapshot from ``live_edge_surface`` (refreshed
    every ~60s by the quant scanner). Filters:
      - ``sport``: restrict to one sport key (e.g., ``baseball_mlb``).
      - ``decision``: 'recommended' | 'hold' | 'skip'. Default: all.
      - ``limit``: max rows returned (default 50).

    Each row is the ranker's full output for that (event, market, outcome,
    placement_book) — consensus fair, placement fair, raw edge, effective
    edge after penalties, and per-penalty breakdown for transparency.
    """
    import json as _json
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout = 30000")
        # Most recent snapshot across the whole table.
        cur = await db.execute(
            "SELECT MAX(computed_at) FROM live_edge_surface"
        )
        row = await cur.fetchone()
        latest = row[0] if row and row[0] else None
        if not latest:
            return {"computed_at": None, "count": 0, "edges": []}

        where_parts = ["computed_at = ?"]
        params: list = [latest]
        if sport:
            where_parts.append("sport = ?")
            params.append(sport)
        if decision:
            where_parts.append("decision = ?")
            params.append(decision)
        where_clause = " AND ".join(where_parts)
        params.append(limit)

        cur = await db.execute(
            f"SELECT sport, event_id, market, outcome, placement_book, "
            f"placement_implied, placement_fair, consensus_fair, "
            f"consensus_std_err, raw_edge, effective_edge, penalty_total, "
            f"penalty_breakdown, disagreement, n_books, outlier_books, "
            f"decision, rank "
            f"FROM live_edge_surface WHERE {where_clause} "
            f"ORDER BY decision='recommended' DESC, rank ASC, "
            f"effective_edge DESC LIMIT ?",
            params,
        )
        rows = await cur.fetchall()

    edges = []
    for r in rows:
        try:
            penalties = _json.loads(r[12] or "{}")
        except Exception:
            penalties = {}
        try:
            outliers = _json.loads(r[15] or "[]")
        except Exception:
            outliers = []
        edges.append({
            "sport": r[0],
            "event_id": r[1],
            "market": r[2],
            "outcome": r[3],
            "placement_book": r[4],
            "placement_implied": r[5],
            "placement_fair": r[6],
            "consensus_fair": r[7],
            "consensus_std_err": r[8],
            "raw_edge": r[9],
            "effective_edge": r[10],
            "penalty_total": r[11],
            "penalty_breakdown": penalties,
            "disagreement": bool(r[13]),
            "n_books": r[14],
            "outlier_books": outliers,
            "decision": r[16],
            "rank": r[17],
        })
    return {
        "computed_at": latest,
        "count": len(edges),
        "filters": {"sport": sport, "decision": decision, "limit": limit},
        "edges": edges,
    }


@app.get("/odds/narrative-edges")
async def get_narrative_edges(sport: str = "basketball_nba"):
    """Detect player-level narrative edges: usage surges, role changes,
    milestone proximity, revenge games. These exploit the lag between
    a player's real situation and their prop line (set from season averages)."""
    from tools.narrative_edge import full_narrative_scan
    return await full_narrative_scan(sport)


@app.get("/odds/kl-metrics")
async def get_kl_metrics(sport: Optional[str] = None, limit: int = 50):
    """Get KL divergence metrics — measures information flow between odds snapshots.

    High KL = significant price discovery (sharp info flowing in).
    Low KL = stale/unchanged lines (thin market, no information flow).
    """
    import aiosqlite
    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA busy_timeout = 60000")
        if sport:
            cursor = await db.execute(
                "SELECT sport, event_id, market_type, kl_divergence, js_divergence, "
                "n_books, opening_entropy, closing_entropy, computed_at "
                "FROM kl_metrics WHERE sport = ? ORDER BY computed_at DESC LIMIT ?",
                (sport, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT sport, event_id, market_type, kl_divergence, js_divergence, "
                "n_books, opening_entropy, closing_entropy, computed_at "
                "FROM kl_metrics ORDER BY computed_at DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()

    metrics = [
        {
            "sport": r[0], "event_id": r[1], "market_type": r[2],
            "kl_divergence": r[3], "js_divergence": r[4],
            "n_books": r[5], "opening_entropy": r[6], "closing_entropy": r[7],
            "computed_at": r[8],
        }
        for r in rows
    ]
    cache_size = len(line_monitor._kl_cache)
    return {
        "count": len(metrics),
        "cached_in_memory": cache_size,
        "metrics": metrics,
    }


@app.post("/odds/parlay-scan/{sport}", dependencies=[Depends(require_admin_or_loopback)])
async def parlay_scan(sport: str):
    """Scan for correlated parlay edges on a sport. Pulls odds + alternates.

    Combines the parlay_scanner (cross-book alternate line exploitation) with
    the correlation engine (build_correlated_parlay) to find SGP edges where
    books misprice correlated legs as independent.
    """
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.parlay_scanner import find_correlated_parlay_edges
    from tools.correlation import build_correlated_parlay

    # Get standard odds
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_edges = []
    correlated_suggestions = []
    # Scan first 5 games (credit budget awareness)
    for game in odds_data.get("games", [])[:5]:
        event_id = game.get("id", "")
        if not event_id:
            continue
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            continue
        edges = find_correlated_parlay_edges(game, alt_data)
        all_edges.extend(edges)

        # Also run correlation engine on standard markets
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        game_data = {"home_team": home, "away_team": away}
        available_props = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    price = outcome.get("price", 0)
                    if price == 0:
                        continue
                    point = outcome.get("point")
                    desc = f"{outcome.get('name', '')} {mkt['key']}"
                    if point is not None:
                        desc += f" {point}"
                    available_props.append({
                        "market": mkt["key"],
                        "american_odds": price,
                        "description": f"{desc} ({bm['title']})",
                        "side": outcome.get("name", ""),
                    })
        if available_props:
            suggestions = build_correlated_parlay(
                available_props=available_props[:20],
                game_data=game_data,
                sport=sport,
                min_correlation=0.25,
                max_legs=3,
            )
            for s in suggestions[:5]:
                if s.get("correlation_edge_pct", 0) > 0.5:
                    correlated_suggestions.append(s)

    return {
        "sport": sport,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "edges_found": len(all_edges),
        "edges": all_edges,
        "correlated_parlay_suggestions": correlated_suggestions,
        "credits": odds_data.get("credits", {}),
    }


@app.get("/odds/sgp-analysis/{sport}")
async def sgp_analysis(sport: str):
    """Analyze SGP mispricing and excessive vig for a sport.

    Shows:
    1. Correlated parlay suggestions (legs that books treat as independent but aren't)
    2. Anti-correlated pairs to avoid (legs that fight each other)
    3. Strongest market correlations for this sport

    Uses cached snapshot data — zero extra API credits.
    """
    from tools.correlation import (
        build_correlated_parlay,
        list_correlated_markets,
        get_all_correlations,
    )

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"No snapshot data for {sport}. "
                f"Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}"
            ),
        )

    games = snapshot["games"]
    all_suggestions = []
    all_anti = []

    for game in games[:8]:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        game_data = {"home_team": home, "away_team": away}

        available_props = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                for outcome in mkt.get("outcomes", []):
                    price = outcome.get("price", 0)
                    if price == 0:
                        continue
                    point = outcome.get("point")
                    desc = f"{outcome.get('name', '')} {mkt['key']}"
                    if point is not None:
                        desc += f" {point}"
                    available_props.append({
                        "market": mkt["key"],
                        "american_odds": price,
                        "description": f"{desc} ({bm['title']})",
                        "side": outcome.get("name", ""),
                    })

        if not available_props:
            continue

        suggestions = build_correlated_parlay(
            available_props=available_props[:20],
            game_data=game_data,
            sport=sport,
            min_correlation=0.2,
            max_legs=3,
        )
        for s in suggestions[:5]:
            if s.get("correlation_edge_pct", 0) > 0.5:
                all_suggestions.append(s)

        # Check for anti-correlated pairs among available markets
        from tools.correlation import detect_anti_correlation
        anti = detect_anti_correlation(available_props[:15], sport)
        for a in anti:
            a["game"] = f"{away} @ {home}"
        all_anti.extend(anti)

    # Get strongest correlations for this sport
    all_corrs = get_all_correlations(sport)
    top_correlations = sorted(
        [
            {"market_a": k[0], "market_b": k[1], "correlation": v}
            for k, v in all_corrs.items()
        ],
        key=lambda x: abs(x["correlation"]),
        reverse=True,
    )[:20]

    return {
        "sport": sport,
        "games_analyzed": min(8, len(games)),
        "correlated_parlay_suggestions": sorted(
            all_suggestions,
            key=lambda x: x.get("correlation_edge_pct", 0),
            reverse=True,
        )[:15],
        "anti_correlated_pairs": all_anti[:10],
        "top_sport_correlations": top_correlations,
        "cached_parlay_scan": (
            autonomous.get_parlay_scan_report().get(sport)
            if autonomous else None
        ),
    }


@app.get("/odds/props/{sport}/{event_id}")
async def scan_props(sport: str, event_id: str, target_book: str = "draftkings", threshold: float = 0.015):
    """
    Scan player props for +EV edges on target book.

    Full pipeline: pull props -> devig each book -> average fair values -> flag edges.
    This is the single-call prop scanner that makes Callisto autonomous.
    """
    from tools.prop_scanner import scan_props_ev
    return await scan_props_ev(sport, event_id, target_book=target_book, edge_threshold=threshold)


@app.get("/odds/dk-props/{sport}")
async def dk_props(sport: str):
    """
    Scrape DraftKings player props for all games in a sport — FREE, no API credits.

    Returns all available player props (points, rebounds, assists, threes, PRA)
    directly from DraftKings' undocumented API. Useful for:
    - Checking current DK prop lines from your phone
    - Feeding the prop scanner with target book data
    - Finding props to cross-reference against other books
    """
    from tools.dk_scraper import scrape_dk_odds, scrape_dk_props

    # First get game list
    games_data = await scrape_dk_odds(sport)
    if games_data.get("error"):
        raise HTTPException(status_code=503, detail=games_data["error"])

    results = []
    for game in games_data.get("games", []):
        event_id = game.get("id", "")
        if not event_id:
            continue
        props = await scrape_dk_props(sport, event_id)
        if props.get("player_count", 0) > 0:
            results.append({
                "game": f"{game.get('away_team', '')} @ {game.get('home_team', '')}",
                "event_id": event_id,
                "commence_time": game.get("commence_time", ""),
                **props,
            })

    return {
        "sport": sport,
        "games_with_props": len(results),
        "total_players": sum(r.get("player_count", 0) for r in results),
        "source": "draftkings_scraper",
        "credits_used": 0,
        "games": results,
    }


@app.get("/odds/status")
async def odds_status():
    """Get line monitor status and credit info."""
    if not line_monitor:
        raise HTTPException(status_code=503, detail="Monitor not initialized")
    return await line_monitor.get_status()


@app.get("/odds/scrapers/health")
async def scrapers_health():
    """
    Liveness report for every registered sportsbook / odds scraper.

    Each scraper tracks `last_successful_pull` and `last_error`; this
    endpoint surfaces them so the watchdog / dashboard can detect when
    a primary-fallback scraper has gone stale even though the process
    is alive.
    """
    # Touch the modules so their registry side-effects run regardless of
    # whether line_monitor has imported them yet in this process.
    for _mod in (
        "tools.dk_scraper",
        "tools.fanduel_scraper",
        "tools.fanatics_scraper",
        "tools.betmgm_scraper",
        "tools.action_network_scraper",
        "tools.tci_scraper",
        "tools.prop_scraper_free",
    ):
        try:
            __import__(_mod)
        except Exception:
            continue
    from tools.scraper_utils import all_health
    return all_health()


@app.get("/odds/learned-correlations")
async def get_learned_correlations():
    """Get learned correlation estimates — Bayesian blend of priors + empirical data."""
    if learned_correlation_store is None:
        raise HTTPException(status_code=503, detail="Learned correlation store not initialized")
    estimates = await learned_correlation_store.get_all_learned()
    stats = learned_correlation_store.get_stats()
    return {"stats": stats, "estimates": estimates}


# --- Bet Tracking & CLV ---

class BetSubmission(BaseModel):
    sport: str = Field(..., min_length=1, max_length=64)
    game_description: str = Field(..., min_length=1, max_length=512)
    team: str = Field(..., min_length=1, max_length=128)
    market: str = Field(..., min_length=1, max_length=64)
    bookmaker: str = Field(..., min_length=1, max_length=64)
    placement_odds: int = Field(..., ge=-10000, le=10000)
    placement_point: Optional[float] = Field(default=None, ge=-1000, le=1000)
    stake: float = Field(default=100, ge=0, le=1_000_000)
    event_id: str = Field(default="", max_length=128)
    edge_estimate: Optional[float] = Field(default=None, ge=-1.0, le=1.0)
    notes: str = Field(default="", max_length=2000)
    # feat/bet-execution-hardening: callers retrying /bets/record (network
    # error, client retry) should pass a stable external_id so the record
    # is created exactly once. The server returns the existing bet_id on
    # subsequent calls with the same external_id.
    external_id: Optional[str] = Field(default=None, max_length=128)


class BetResolution(BaseModel):
    result: str = Field(..., pattern="^(won|lost|push)$")
    payout: Optional[float] = Field(default=None, ge=0, le=10_000_000)


@app.post("/bets/record", dependencies=[Depends(require_admin)])
async def record_bet(bet: BetSubmission):
    """Record a bet at placement time for CLV tracking.

    Idempotent when ``external_id`` is supplied: retrying the same payload
    (e.g. after a transient 500) returns the original ``bet_id`` rather
    than duplicating the row. Without external_id, a fingerprint on
    (event_id, team, market, bookmaker, odds, stake) within the last hour
    acts as a fallback dedup.
    """
    bet_id = await clv_tracker.record_bet(
        sport=bet.sport,
        game_description=bet.game_description,
        team=bet.team,
        market=bet.market,
        bookmaker=bet.bookmaker,
        placement_odds=bet.placement_odds,
        placement_point=bet.placement_point,
        stake=bet.stake,
        event_id=bet.event_id,
        edge_estimate=bet.edge_estimate,
        notes=bet.notes,
        external_id=bet.external_id,
    )
    return {"bet_id": bet_id, "external_id": bet.external_id}


@app.get("/bets/risk-report")
async def bets_risk_report():
    """Current exposure, drawdown, and circuit-breaker utilisation.

    feat/bet-execution-hardening (2026-04-23). Safe for loopback monitoring —
    no auth required (the data is operational telemetry, not secret). Shows:
      * bankroll / rolling peak / drawdown
      * open pending exposure vs cap
      * daily-risk (total stakes today) vs cap
      * daily-P/L vs loss cap
      * per-sport and per-game exposure utilisation
      * which circuit breakers are currently tripped
      * the full RiskLimits snapshot so operators can see effective env
    """
    from tools.risk_limits import compute_risk_report, RiskLimits
    import aiosqlite as _aiosqlite

    db = await _aiosqlite.connect(DB_PATH)
    try:
        report = await compute_risk_report(db, limits=RiskLimits.from_env())
    finally:
        await db.close()
    return report


@app.post("/bets/{bet_id}/resolve", dependencies=[Depends(require_admin)])
async def resolve_bet(bet_id: int, resolution: BetResolution):
    """Resolve a bet as won/lost/push."""
    return await clv_tracker.resolve_bet(bet_id, resolution.result, resolution.payout)


@app.get("/bets/clv-report")
async def clv_report(sport: Optional[str] = None):
    """Get CLV performance report — THE metric for edge measurement."""
    return await clv_tracker.get_clv_report(sport=sport)


@app.get("/bets")
async def list_bets(result: Optional[str] = None, sport: Optional[str] = None, limit: int = 50):
    """Get bet history."""
    return await clv_tracker.get_all_bets(result=result, sport=sport, limit=limit)


@app.get("/bets/bankroll")
async def bankroll_history(limit: int = 50):
    """Get bankroll balance history."""
    return await clv_tracker.get_bankroll_history(limit=limit)


@app.post("/bets/bankroll/init", dependencies=[Depends(require_admin)])
async def init_bankroll(balance: float):
    """Set initial bankroll balance."""
    if balance < 0 or balance > 100_000_000:
        raise HTTPException(status_code=422, detail="balance out of range (0..100M)")
    await clv_tracker.set_initial_bankroll(balance)
    return {"balance": balance}


# --- Market Structure Analysis ---

@app.get("/odds/market-analysis/{sport}")
async def market_analysis(sport: str):
    """Full market structure analysis — key numbers, stale lines, Pinnacle benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import full_market_analysis

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    analysis = full_market_analysis(odds_data.get("games", []), sport)
    analysis["credits"] = odds_data.get("credits", {})
    return analysis


@app.get("/odds/stale-lines/{sport}")
async def stale_lines(sport: str):
    """Find retail book lines that are stale vs sharp benchmark."""
    from tools.odds_api import get_odds as _get_odds
    from tools.market_analysis import find_stale_lines

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h,spreads,totals")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    stale = find_stale_lines(odds_data.get("games", []))
    return {"count": len(stale), "stale_lines": stale, "credits": odds_data.get("credits", {})}


# --- Market Psychology ---

@app.get("/odds/psychology/{sport}")
async def market_psychology(sport: str):
    """Run full market psychology analysis — number shading, attention arbitrage.

    Returns signals for all current games in the sport: shaded lines,
    thin-market opportunities, and closing line predictions.
    Uses cached snapshot data (zero extra API credits).
    """
    from tools.market_psychology import full_market_psychology

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    psych = full_market_psychology(
        games=snapshot["games"],
        sport=sport,
    )
    return psych


@app.get("/odds/psychology")
async def market_psychology_all():
    """Return cached market psychology signals for all monitored sports.

    This is the lightweight version — reads from the autonomous loop's
    cache rather than recomputing.  Zero cost, instant response.
    """
    if not autonomous:
        raise HTTPException(status_code=503, detail="Autonomous loop not initialized")
    return autonomous.get_psychology_report()


# --- Dead Numbers & Line Analysis ---

@app.get("/odds/dead-numbers/{sport}")
async def dead_numbers_endpoint(sport: str):
    """Show dead number steals and key number analysis for a sport.

    Scans current odds snapshot for spreads sitting on dead numbers
    while other books are on key numbers. Also includes line shopping
    opportunities and buy-points analysis.

    Uses cached snapshot data (zero extra API credits).
    """
    from tools.dead_numbers import (
        find_dead_number_steals,
        rank_line_shopping_opportunities,
        analyze_spread as dn_analyze_spread,
        SPORT_ALIASES,
    )
    from tools.odds_api import find_best_line as _find_best_line

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    _dn_sport = sport.lower()
    if _dn_sport not in SPORT_ALIASES:
        raise HTTPException(
            status_code=400,
            detail=f"Sport '{sport}' not supported for dead number analysis. Supported: {list(set(SPORT_ALIASES.values()))}",
        )

    games = snapshot.get("games", [])
    all_steals = []
    all_shopping = []
    spread_analyses = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = _find_best_line(game, market="spreads", team=team)
            all_lines = best.get("all_lines", [])
            if not all_lines:
                continue

            # Build lines list for dead number functions
            lines_for_dn = [
                {
                    "bookmaker": l["bookmaker"],
                    "spread": l.get("point", 0),
                    "price": l.get("price", -110),
                }
                for l in all_lines
                if l.get("point") is not None
            ]

            if not lines_for_dn:
                continue

            # Analyze the primary spread
            primary_spread = lines_for_dn[0]["spread"]
            try:
                analysis = dn_analyze_spread(primary_spread, sport)
                analysis["game"] = f"{away} @ {home}"
                analysis["team"] = team
                spread_analyses.append(analysis)
            except (ValueError, KeyError):
                pass

            # Find dead number steals
            if len(lines_for_dn) >= 2:
                try:
                    steals = find_dead_number_steals(lines_for_dn, sport)
                    for s in steals:
                        s["game"] = f"{away} @ {home}"
                        s["team"] = team
                    all_steals.extend(steals)
                except (ValueError, KeyError):
                    pass

                # Rank line shopping opportunities
                try:
                    shopping = rank_line_shopping_opportunities(lines_for_dn, sport)
                    for s in shopping:
                        s["game"] = f"{away} @ {home}"
                        s["team"] = team
                    all_shopping.extend(shopping)
                except (ValueError, KeyError):
                    pass

    all_steals.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    all_shopping.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)

    return {
        "sport": sport,
        "games_scanned": len(games),
        "dead_number_steals": all_steals[:20],
        "line_shopping_opportunities": all_shopping[:20],
        "spread_analyses": spread_analyses[:30],
        "steal_count": len(all_steals),
        "shopping_count": len(all_shopping),
    }


@app.get("/analysis/futures-efficiency")
async def futures_efficiency_endpoint(
    current_odds: int = -200,
    record_wins: int = 30,
    record_losses: int = 20,
    games_played: int = 50,
    season_length: int = 82,
    sport: str = "basketball_nba",
):
    """Analyze if a futures bet is efficiently priced given current trajectory."""
    from tools.market_psychology import futures_efficiency
    return futures_efficiency(
        current_odds=current_odds,
        record_wins=record_wins,
        record_losses=record_losses,
        games_played=games_played,
        season_length=season_length,
        sport=sport,
    )


@app.get("/analysis/half-market/{sport}")
async def half_market_endpoint(
    full_game_total: float = 220.0,
    half_total: float = 110.0,
    sport: str = "basketball_nba",
    half: str = "first",
):
    """Analyze half/quarter market efficiency vs full-game projections."""
    from tools.market_psychology import half_market_adjustment
    return half_market_adjustment(
        full_game_total=full_game_total,
        half_total=half_total,
        sport=sport,
        half=half,
    )


@app.get("/analysis/cross-tabulate/{sport}")
async def cross_tabulate_endpoint(sport: str, min_sample: int = 20):
    """Multi-factor interaction analysis — discovers which factor combos produce edges."""
    from tools.temporal_analysis import load_game_results, cross_tabulate
    db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
    df = load_game_results(db_path, sport=sport)
    if df.height == 0:
        raise HTTPException(status_code=503, detail=f"No game results for {sport}")
    return cross_tabulate(df, min_sample=min_sample).to_dicts()


@app.get("/odds/line-analysis/{sport}")
async def line_analysis_endpoint(sport: str):
    """Show RLM, steam moves, public side analysis, and bet timing for a sport.

    Analyzes the current snapshot for reverse line movement (sharp money
    indicator), steam moves (coordinated sharp action), estimated public
    side distribution, and optimal bet timing windows.

    Uses cached snapshot data (zero extra API credits).
    """
    from tools.line_analysis import (
        estimate_public_side as la_estimate_public,
        contrarian_value as la_contrarian,
        optimal_bet_timing as la_timing,
        detect_steam as la_detect_steam,
    )
    from tools.odds_api import find_best_line as _find_best_line

    if not line_monitor:
        raise HTTPException(status_code=503, detail="Line monitor not initialized")

    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot or not snapshot.get("games"):
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        )

    games = snapshot.get("games", [])
    public_analyses = []
    contrarian_picks = []
    timing_info = None

    # Compute bet timing for the sport
    try:
        timing_info = la_timing(sport=sport)
    except Exception:
        pass

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Get spread lines for public side estimation
        for team_side, team_name in [("home", home), ("away", away)]:
            if not team_name:
                continue

            best = _find_best_line(game, market="spreads", team=team_name)
            all_lines = best.get("all_lines", [])
            if not all_lines:
                continue

            # Use best and worst as proxy for open/current
            prices = [l.get("price", -110) for l in all_lines]
            points = [l.get("point", 0) for l in all_lines if l.get("point") is not None]

            if not points:
                continue

            best_point = max(points)
            worst_point = min(points)

            try:
                public_est = la_estimate_public(
                    line_open=worst_point,
                    line_current=best_point,
                    sport=sport,
                    team_a=team_name,
                    team_b=away if team_side == "home" else home,
                )
                public_est["game"] = f"{away} @ {home}"
                public_est["team"] = team_name
                public_analyses.append(public_est)

                # If strong public lean, compute contrarian value
                est_public_pct = max(
                    public_est.get("estimated_public_pct_a", 50),
                    public_est.get("estimated_public_pct_b", 50),
                )
                if est_public_pct >= 60:
                    cv = la_contrarian(
                        estimated_public_pct=est_public_pct,
                        sport=sport,
                        spread=best_point,
                    )
                    cv["game"] = f"{away} @ {home}"
                    cv["team"] = team_name
                    contrarian_picks.append(cv)
            except Exception:
                pass

        # Steam detection from snapshot price data
        # (Note: steam detection works best across multiple snapshots over time;
        # single-snapshot detection is limited but still catches book-to-book divergence)

    # Sort contrarian picks by adjusted ROI
    contrarian_picks.sort(key=lambda x: x.get("adjusted_roi", 0), reverse=True)

    return {
        "sport": sport,
        "games_scanned": len(games),
        "public_side_analyses": public_analyses,
        "contrarian_picks": contrarian_picks[:10],
        "bet_timing": timing_info,
        "analysis_count": len(public_analyses),
        "contrarian_count": len(contrarian_picks),
    }


@app.get("/bets/clv-forecast")
async def clv_forecast(sport: Optional[str] = None):
    """Forecast pre-game CLV for all pending bets using closing line prediction.

    Uses market psychology's predict_closing_line to estimate where each
    bet's line will close, giving a CLV estimate before the game starts.
    Useful for paper-trading evaluation.
    """
    if not clv_tracker:
        raise HTTPException(status_code=503, detail="CLV tracker not initialized")
    return await clv_tracker.forecast_clv(sport=sport)


# --- Simulation & Contextual Data ---

class SimulationRequest(BaseModel):
    home_name: str
    away_name: str
    home_off_eff: float = 105.0
    home_def_eff: float = 100.0
    away_off_eff: float = 105.0
    away_def_eff: float = 100.0
    home_pace: float = 70.0
    away_pace: float = 70.0
    home_injuries_impact: float = 0.0
    away_injuries_impact: float = 0.0
    iterations: int = 10000
    sport: str = "basketball_ncaab"
    event_id: str = ""


@app.post("/simulate/basketball", dependencies=[Depends(require_admin_or_loopback)])
async def simulate_basketball_game(req: SimulationRequest):
    """Run Monte Carlo simulation and compare against market odds."""
    from tools.simulation import simulate_basketball, compare_to_market, TeamProfile

    home = TeamProfile(
        name=req.home_name,
        offensive_efficiency=req.home_off_eff,
        defensive_efficiency=req.home_def_eff,
        pace=req.home_pace,
        injuries_impact=req.home_injuries_impact,
    )
    away = TeamProfile(
        name=req.away_name,
        offensive_efficiency=req.away_off_eff,
        defensive_efficiency=req.away_def_eff,
        pace=req.away_pace,
        injuries_impact=req.away_injuries_impact,
    )

    sim = simulate_basketball(home, away, iterations=req.iterations)

    result = {
        "simulation": {
            "home_avg_score": round(sim.home_avg_score, 1),
            "away_avg_score": round(sim.away_avg_score, 1),
            "fair_spread": round(sim.fair_spread, 1),
            "fair_total": round(sim.fair_total, 1),
            "home_win_pct": round(sim.home_win_pct * 100, 1),
            "away_win_pct": round(sim.away_win_pct * 100, 1),
            "iterations": sim.iterations,
        },
    }

    # Compare to market if we have an event_id
    if req.event_id:
        from tools.odds_api import get_event_odds
        market = await get_event_odds(
            sport=req.sport, event_id=req.event_id,
            markets="h2h,spreads,totals",
        )
        if not market.get("error"):
            edges = compare_to_market(sim, market)
            result["market_edges"] = edges
            result["edge_count"] = len([e for e in edges if e["ev"]["is_positive_ev"]])

    return result


class PoissonRequest(BaseModel):
    home_expected: float
    away_expected: float
    sport: str = "soccer_epl"
    event_id: str = ""


@app.post("/simulate/poisson", dependencies=[Depends(require_admin_or_loopback)])
async def simulate_poisson_game(req: PoissonRequest):
    """Run Poisson simulation for low-scoring sports."""
    from tools.simulation import simulate_poisson
    return simulate_poisson(req.home_expected, req.away_expected)


# =========================================================================
# Pre-LIVE bankroll Monte Carlo simulation endpoint
# feat/bankroll-montecarlo-sim (2026-04-22)
# =========================================================================
_PORTFOLIO_SIM_CACHE: dict[tuple, tuple[float, dict]] = {}
_PORTFOLIO_SIM_CACHE_TTL = 3600  # 1 hour


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

    n_sims = max(10, min(int(n_sims), 5000))
    horizon_days = max(1, min(int(horizon_days), 365))

    if all_live:
        import sqlite3 as _sqlite3
        db = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        conn = _sqlite3.connect(db)
        try:
            rows = conn.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE status = 'live'"
            ).fetchall()
        finally:
            conn.close()
        ids = [r[0] for r in rows]
    else:
        ids = [x.strip() for x in hypothesis_ids.split(",") if x.strip()]

    if not ids:
        raise HTTPException(
            status_code=400,
            detail="No hypothesis_ids supplied (pass hypothesis_ids=a,b,c or all_live=1)",
        )

    cache_key = (tuple(sorted(ids)), n_sims, horizon_days, float(starting_bankroll), float(kelly_fraction))
    now = _time.time()
    cached = _PORTFOLIO_SIM_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _PORTFOLIO_SIM_CACHE_TTL:
        return {"cached": True, "age_seconds": round(now - cached[0], 1), **cached[1]}

    result = simulate_portfolio(
        hypothesis_ids=ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
    )
    payload = result.to_dict(include_paths=False)
    _PORTFOLIO_SIM_CACHE[cache_key] = (now, payload)
    return {"cached": False, **payload}


@app.get("/model/total/{sport}")
async def get_model_total(sport: str, venue: str = "", wind_mph: float = None,
                          wind_dir: str = "", temp_f: float = None,
                          humidity: float = None, refs: str = ""):
    """Pace model total projections + environment adjustments for a sport.

    Returns the pace model's independent fair total for each game in the latest
    odds snapshot, adjusted by environment (venue/weather/refs).  This is an
    independent total model beyond cross-book divergence.
    """
    from tools.edge_scanner import scan_pace_model_total_edges

    # Build weather dict from query params
    weather_data = None
    if any(v is not None for v in [wind_mph, temp_f, humidity]):
        weather_data = {}
        if wind_mph is not None:
            weather_data["wind_speed_mph"] = wind_mph
        if wind_dir:
            weather_data["wind_direction"] = wind_dir
        if temp_f is not None:
            weather_data["temp_f"] = temp_f
        if humidity is not None:
            weather_data["humidity_pct"] = humidity

    ref_list = [r.strip() for r in refs.split(",") if r.strip()] or None

    # Get latest snapshot for this sport
    snapshot = line_monitor._snapshots.get(sport)
    if not snapshot:
        raise HTTPException(
            status_code=503,
            detail=f"No snapshot available for {sport}. Trigger a snapshot first.",
        )

    games = snapshot.get("games", [])
    if not games:
        raise HTTPException(status_code=503, detail=f"No games in snapshot for {sport}")

    edges = scan_pace_model_total_edges(
        games=games,
        sport=sport,
        weather_data=weather_data,
        venue_team=venue or None,
        refs=ref_list,
    )

    return {
        "sport": sport,
        "game_count": len(games),
        "model_edges": edges,
        "edge_count": len(edges),
        "venue_queried": venue or None,
        "weather_data": weather_data,
        "refs": ref_list,
    }


@app.get("/model/environment")
async def get_model_environment(venue: str, sport: str = "NFL",
                                wind_mph: float = None, wind_dir: str = "",
                                temp_f: float = None, humidity: float = None,
                                precipitation: str = "", refs: str = ""):
    """Environmental factors for a specific venue/game.

    Returns venue characteristics, weather adjustments, referee tendencies,
    and the combined total adjustment with confidence level.
    """
    from tools.environment import (
        total_environment_adjustment,
        get_venue_factors,
    )

    # Build weather dict
    weather_data = None
    if any(v is not None for v in [wind_mph, temp_f, humidity]) or precipitation:
        weather_data = {}
        if wind_mph is not None:
            weather_data["wind_speed_mph"] = wind_mph
        if wind_dir:
            weather_data["wind_direction"] = wind_dir
        if temp_f is not None:
            weather_data["temp_f"] = temp_f
        if humidity is not None:
            weather_data["humidity_pct"] = humidity
        if precipitation:
            weather_data["precipitation"] = precipitation

    ref_list = [r.strip() for r in refs.split(",") if r.strip()] or None

    sport_code = sport.upper()
    venue_info = get_venue_factors(venue, sport_code)
    env_result = total_environment_adjustment(
        venue=venue,
        sport=sport_code,
        weather=weather_data,
        refs=ref_list,
    )

    return {
        "venue": venue_info,
        "environment": env_result,
        "weather_input": weather_data,
        "refs_input": ref_list,
    }


@app.get("/data/injuries/{sport}")
async def get_injuries(sport: str):
    """Get current injury report from ESPN with model analysis.

    Returns raw injury data plus, for each injured starter/key player,
    the injury model's quantified impact (spread points, usage redistribution).
    """
    from tools.contextual_data import get_injuries as _get_injuries
    from tools.injury_model import player_impact as _player_impact

    data = await _get_injuries(sport)
    if data.get("error") or not data.get("injuries"):
        return data

    # Map sport key to model sport code
    _model_sport_map = {
        "basketball_nba": "NBA", "basketball_ncaab": "NBA",
        "americanfootball_nfl": "NFL", "americanfootball_ncaaf": "NFL",
        "baseball_mlb": "MLB", "icehockey_nhl": "NHL",
    }
    model_sport = _model_sport_map.get(sport, "")

    # Enrich each injury with model analysis (lightweight — no matchup/timing)
    if model_sport:
        for inj in data["injuries"]:
            status = (inj.get("status") or "").lower()
            if status not in ("out", "doubtful"):
                continue
            try:
                result = _player_impact(
                    player_name=inj.get("player", ""),
                    team=inj.get("team", ""),
                    sport=model_sport,
                    position=inj.get("position", ""),
                )
                inj["model_analysis"] = {
                    "tier": result.tier,
                    "spread_impact": result.spread_impact,
                    "total_impact": result.total_impact,
                    "confidence": result.confidence,
                    "notes": result.notes[:3],
                }
            except Exception:
                pass  # silently skip model failures

    return data


@app.get("/model/injury-impact/{sport}")
async def injury_impact_model(sport: str):
    """Run full injury model analysis for today's games.

    Fetches current injuries and scoreboard, then for each game with
    significant injuries, runs full_injury_analysis (impact quantification,
    usage redistribution, matchup adjustment, market timing).

    Returns per-game injury impact summaries with prop opportunities.
    """
    from tools.contextual_data import get_injuries as _get_injuries, get_scoreboard as _get_sb
    from tools.injury_model import full_injury_analysis as _full_analysis
    from dataclasses import asdict

    _model_sport_map = {
        "basketball_nba": "NBA", "basketball_ncaab": "NBA",
        "americanfootball_nfl": "NFL", "americanfootball_ncaaf": "NFL",
        "baseball_mlb": "MLB", "icehockey_nhl": "NHL",
    }
    model_sport = _model_sport_map.get(sport, "")
    if not model_sport:
        raise HTTPException(status_code=400, detail=f"Sport {sport} not supported by injury model")

    injuries_data = await _get_injuries(sport)
    scoreboard = await _get_sb(sport)
    injuries = injuries_data.get("injuries", [])
    games = scoreboard.get("games", [])

    if not injuries:
        return {"sport": sport, "games": [], "message": "No injuries reported"}

    # Build team-to-game mapping
    team_game_map = {}  # team_name_lower -> game dict
    for g in games:
        for side in ["home_team", "away_team"]:
            tn = g.get(side, "").lower()
            if tn:
                team_game_map[tn] = g

    # Group injuries by team
    team_injuries = {}
    for inj in injuries:
        status = (inj.get("status") or "").lower()
        if status not in ("out", "doubtful"):
            continue
        team = inj.get("team", "")
        team_injuries.setdefault(team, []).append(inj)

    results = []
    for team, injs in team_injuries.items():
        # Find the game for this team
        game = team_game_map.get(team.lower())
        if not game:
            # Try partial match
            for tn, g in team_game_map.items():
                if any(w in tn for w in team.lower().split() if len(w) > 3):
                    game = g
                    break
        if not game:
            continue

        home = game.get("home_team", "")
        away = game.get("away_team", "")
        opponent = away if team.lower() in home.lower() else home
        game_name = game.get("name", f"{away} at {home}")

        game_result = {
            "game": game_name,
            "team": team,
            "opponent": opponent,
            "injuries": [],
        }

        for inj in injs:
            try:
                analysis = _full_analysis(
                    player_name=inj.get("player", ""),
                    team=team,
                    sport=model_sport,
                    opponent=opponent,
                    position=inj.get("position", ""),
                    minutes_since_announced=30.0,
                )
                # Convert dataclasses to dicts for JSON serialization
                summary = {
                    "player": analysis["player"],
                    "actionable": analysis.get("actionable", False),
                    "edge_points": analysis.get("edge_points", 0),
                }
                impact = analysis.get("impact")
                if impact:
                    summary["impact"] = {
                        "tier": impact.tier,
                        "spread_impact": impact.spread_impact,
                        "total_impact": impact.total_impact,
                        "confidence": impact.confidence,
                        "notes": impact.notes[:3],
                    }
                matchup = analysis.get("matchup_adjusted")
                if matchup:
                    summary["matchup"] = {
                        "base_impact": matchup.base_impact,
                        "multiplier": matchup.matchup_multiplier,
                        "adjusted_spread_impact": matchup.adjusted_spread_impact,
                        "reasoning": matchup.reasoning[:3],
                    }
                mkt = analysis.get("market_timing")
                if mkt:
                    summary["market_timing"] = {
                        "pct_adjusted": mkt.pct_adjusted,
                        "window_remaining_minutes": mkt.window_remaining_minutes,
                        "edge_remaining": mkt.edge_remaining,
                        "tier": mkt.significance_tier,
                        "notes": mkt.notes[:2],
                    }
                # Usage redistribution — top 5 beneficiaries
                redist = analysis.get("redistribution", [])
                if redist:
                    summary["prop_opportunities"] = [
                        {
                            "player": r.player,
                            "role": r.role,
                            "usage_increase": r.usage_increase,
                            "stat_change": r.projected_stat_change,
                        }
                        for r in redist[:5]
                    ]
                game_result["injuries"].append(summary)
            except Exception as e:
                game_result["injuries"].append({
                    "player": inj.get("player", ""),
                    "error": str(e),
                })

        if game_result["injuries"]:
            results.append(game_result)

    return {
        "sport": sport,
        "model_sport": model_sport,
        "game_count": len(results),
        "games": results,
    }


@app.get("/data/scoreboard/{sport}")
async def get_scoreboard(sport: str):
    """Get live scoreboard from ESPN."""
    from tools.contextual_data import get_scoreboard as _get_scoreboard
    return await _get_scoreboard(sport)


@app.get("/data/weather")
async def get_weather(latitude: float, longitude: float, venue: str = ""):
    """Get weather forecast for a venue."""
    from tools.contextual_data import get_weather as _get_weather
    return await _get_weather(latitude, longitude, venue_name=venue)


@app.get("/data/referee")
async def referee_info(refs: str, sport: str = "basketball_nba"):
    """Get referee tendency adjustments. Pass refs as comma-separated names."""
    from tools.contextual_data import get_referee_adjustment
    ref_list = [r.strip() for r in refs.split(",")]
    return get_referee_adjustment(ref_list, sport)


# --- Line Gap Analysis ---

@app.get("/odds/line-gaps/{sport}")
async def line_gaps(sport: str, event_id: str = "", market: str = "alternate_spreads"):
    """Scan alternate lines for gaps — missing points that reveal risk concentration."""
    from tools.odds_api import get_odds as _get_odds, get_alternate_lines as _get_alt
    from tools.line_gaps import scan_line_gaps

    if event_id:
        alt_data = await _get_alt(sport=sport, event_id=event_id)
        if alt_data.get("error"):
            raise HTTPException(status_code=503, detail=alt_data["error"])
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        return {"event_id": event_id, "market": market, "gap_count": len(gaps), "gaps": gaps}

    # No event_id — scan first 5 games
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_gaps = []
    for game in odds_data.get("games", [])[:5]:
        eid = game.get("id", "")
        if not eid:
            continue
        alt_data = await _get_alt(sport=sport, event_id=eid)
        if alt_data.get("error"):
            continue
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
            g["event_id"] = eid
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "market": market,
        "games_scanned": min(5, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "exploitable": len([g for g in all_gaps if g.get("exploitable")]),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }


@app.get("/odds/prop-gaps/{sport}")
async def prop_gaps(sport: str, event_id: str = ""):
    """Scan player props for line gaps across bookmakers."""
    from tools.odds_api import get_odds as _get_odds, get_player_props as _get_props
    from tools.line_gaps import scan_prop_gaps

    if event_id:
        prop_data = await _get_props(sport=sport, event_id=event_id)
        if prop_data.get("error"):
            raise HTTPException(status_code=503, detail=prop_data["error"])
        gaps = scan_prop_gaps(prop_data)
        return {"event_id": event_id, "gap_count": len(gaps), "gaps": gaps}

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        raise HTTPException(status_code=503, detail=odds_data["error"])

    all_gaps = []
    for game in odds_data.get("games", [])[:3]:
        eid = game.get("id", "")
        if not eid:
            continue
        prop_data = await _get_props(sport=sport, event_id=eid)
        if prop_data.get("error"):
            continue
        gaps = scan_prop_gaps(prop_data)
        for g in gaps:
            g["game"] = f"{game.get('away_team', '')} @ {game.get('home_team', '')}"
        all_gaps.extend(gaps)

    return {
        "sport": sport,
        "games_scanned": min(3, odds_data.get("game_count", 0)),
        "gap_count": len(all_gaps),
        "gaps": all_gaps,
        "credits": odds_data.get("credits", {}),
    }


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
    hid = await hypothesis_manager.create_hypothesis(
        name=req.name,
        thesis=req.thesis,
        sport=req.sport,
        market_type=req.market_type,
        model_config=req.hypothesis_model_config,
        edge_threshold=req.edge_threshold,
        min_sample_size=req.min_sample_size,
        significance_level=req.significance_level,
        notes=req.notes,
    )
    return {"hypothesis_id": hid}


@app.get("/hypothesis")
async def list_hypotheses(status: Optional[str] = None):
    """List all hypotheses, optionally filtered by status."""
    hypotheses = await hypothesis_manager.list_hypotheses(status=status)
    return {"count": len(hypotheses), "hypotheses": hypotheses}


@app.get("/hypothesis/{hypothesis_id}")
async def get_hypothesis(hypothesis_id: str):
    """Get hypothesis details."""
    h = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    return h


@app.get("/hypothesis/{hypothesis_id}/report")
async def hypothesis_report(hypothesis_id: str):
    """Full statistical report across all stages."""
    return await hypothesis_manager.get_hypothesis_report(hypothesis_id)


@app.get("/hypothesis/{hypothesis_id}/significance")
async def hypothesis_significance(hypothesis_id: str, stage: str = "backtest"):
    """Run significance tests on a hypothesis at a given stage."""
    return await hypothesis_manager.evaluate_significance(hypothesis_id, stage)


@app.post("/hypothesis/{hypothesis_id}/promote", dependencies=[Depends(require_admin)])
async def promote_hypothesis(hypothesis_id: str):
    """Check readiness and promote to next stage if criteria are met."""
    readiness = await hypothesis_manager.check_promotion_readiness(hypothesis_id)
    if readiness.get("ready"):
        result = await hypothesis_manager.auto_promote(hypothesis_id)
        return {"promoted": True, **result}
    return {"promoted": False, **readiness}


@app.patch("/hypothesis/{hypothesis_id}", dependencies=[Depends(require_admin)])
async def update_hypothesis(hypothesis_id: str, request: Request):
    """Update hypothesis status, threshold, model_config, or notes.

    Uses a fresh DB connection per request to avoid stale-handle failures
    on the long-lived hypothesis_manager._db connection.
    """
    import json as _json
    from tools.schema import open_db

    req = await request.json()
    if not isinstance(req, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")
    # SECURITY (audit C-4 / P2 #25): allowlist top-level fields and validate model_config
    # against a known schema. Refuses unknown keys to prevent silent passthrough that
    # downstream code may interpret unsafely.
    _ALLOWED_PATCH_KEYS = {
        "status", "promoted_by", "force", "edge_threshold", "model_config", "notes",
    }
    unknown = set(req.keys()) - _ALLOWED_PATCH_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown fields: {sorted(unknown)}")
    if "model_config" in req:
        mc = req["model_config"]
        if not isinstance(mc, dict):
            raise HTTPException(status_code=422, detail="model_config must be an object")
        from tools.hypothesis import validate_model_config
        try:
            req["model_config"] = validate_model_config(mc)
        except ValueError as ve:
            raise HTTPException(status_code=422, detail=f"model_config: {ve}")
    if "notes" in req:
        if not isinstance(req["notes"], str) or len(req["notes"]) > 5000:
            raise HTTPException(status_code=422, detail="notes must be string ≤5000 chars")
    if "edge_threshold" in req:
        try:
            et = float(req["edge_threshold"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="edge_threshold must be numeric")
        if not (0.0 <= et <= 1.0):
            raise HTTPException(status_code=422, detail="edge_threshold out of [0,1]")
        req["edge_threshold"] = et

    h = await hypothesis_manager.get_hypothesis(hypothesis_id)
    if not h:
        raise HTTPException(status_code=404, detail="Hypothesis not found")
    results = {}
    db = None
    try:
        db = await open_db()
        if "status" in req:
            new_status = req["status"]
            promoted_by = req.get("promoted_by", "api")
            force = req.get("force", False)
            old_status = h.get("status", "draft")

            # Enforce promotion gates for forward transitions unless force=True
            stage_order = ["draft", "backtesting", "paper_trading", "live", "retired"]
            old_idx = stage_order.index(old_status) if old_status in stage_order else -1
            new_idx = stage_order.index(new_status) if new_status in stage_order else -1
            is_forward = new_idx > old_idx and new_status not in ("retired", "rejected")

            if is_forward and not force and old_status in ("backtesting", "paper_trading"):
                readiness = await hypothesis_manager.check_promotion_readiness(hypothesis_id)
                if not readiness.get("ready"):
                    raise HTTPException(
                        status_code=400,
                        detail={
                            "error": f"Promotion gate failed: {old_status} → {new_status}",
                            "checks": readiness.get("checks", []),
                            "hint": "Pass force=true to override",
                        },
                    )

            now = __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat()
            await db.execute(
                "UPDATE hypotheses SET status = ?, updated_at = ?, "
                "promoted_at = ?, promoted_by = ? WHERE hypothesis_id = ?",
                (new_status, now, now, promoted_by, hypothesis_id),
            )
            results["status"] = new_status
            logger.info(f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by})")
        if "edge_threshold" in req:
            await db.execute(
                "UPDATE hypotheses SET edge_threshold = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (req["edge_threshold"], hypothesis_id),
            )
            results["edge_threshold"] = req["edge_threshold"]
        if "model_config" in req:
            raw = h.get("model_config", "{}")
            existing = _json.loads(raw) if isinstance(raw, str) else (raw or {})
            existing.update(req["model_config"])
            await db.execute(
                "UPDATE hypotheses SET model_config = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (_json.dumps(existing), hypothesis_id),
            )
            results["model_config"] = existing
        if "notes" in req:
            await db.execute(
                "UPDATE hypotheses SET notes = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE hypothesis_id = ?",
                (req["notes"], hypothesis_id),
            )
            results["notes"] = req["notes"]
        await db.commit()
    except Exception as e:
        logger.error(f"PATCH /hypothesis/{hypothesis_id} failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if db:
            await db.close()
    return {"hypothesis_id": hypothesis_id, "updated": results}


@app.post("/backtest/run", dependencies=[Depends(require_admin)])
async def run_backtest(req: BacktestRequest):
    """Start a backtest run on a hypothesis against historical data."""
    return await backtest_engine.run_backtest(
        hypothesis_id=req.hypothesis_id,
        start_date=req.start_date,
        end_date=req.end_date,
        credit_budget=req.credit_budget,
    )


@app.get("/backtest/run/{run_id}")
async def get_backtest_results(run_id: str):
    """Get backtest results for a run."""
    return await backtest_engine.get_run_results(run_id)


@app.post("/backtest/resolve/{run_id}", dependencies=[Depends(require_admin_or_loopback)])
async def resolve_backtest(run_id: str, sport: str = "basketball_nba"):
    """Resolve backtest events against actual game results."""
    return await backtest_engine.resolve_with_scores(run_id, sport)


@app.get("/historical/cache")
async def historical_cache_stats():
    """Get historical odds cache statistics."""
    return await historical_fetcher.get_cache_stats()


@app.post("/historical/fetch", dependencies=[Depends(require_admin)])
async def fetch_historical(
    sport: str,
    start_date: str,
    end_date: str,
    credit_budget: int = 50,
):
    """Fetch historical odds for a date range (cached after first fetch)."""
    return await historical_fetcher.bulk_fetch_date_range(
        sport=sport,
        start_date=start_date,
        end_date=end_date,
        credit_budget=credit_budget,
    )


# ── Research Loop Endpoints ──

@app.get("/research/status")
async def research_status():
    """Get research loop status."""
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return research_loop.get_status()


@app.post("/research/pause", dependencies=[Depends(require_admin)])
async def research_pause():
    """Pause the research loop."""
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return await research_loop.pause()


@app.post("/research/resume", dependencies=[Depends(require_admin)])
async def research_resume():
    """Resume the research loop."""
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return await research_loop.resume()


@app.post("/research/local-only", dependencies=[Depends(require_admin)])
async def research_local_only(enabled: bool = True):
    """Toggle local-only mode (no Claude Code calls)."""
    if not research_loop:
        raise HTTPException(status_code=503, detail="Research loop not initialized")
    return research_loop.set_local_only(enabled)


@app.post("/research/collect", dependencies=[Depends(require_admin)])
async def research_collect(sport: str = "basketball_nba", date: Optional[str] = None):
    """Manually trigger data collection for a sport."""
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    scores = await data_collector.collect_scores(sport, date)
    box = await data_collector.collect_box_scores(sport, date)
    return {"scores": scores, "box_scores": box}


@app.post("/research/generate", dependencies=[Depends(require_admin)])
async def research_generate(sport: str = "basketball_nba", max_hypotheses: int = 20):
    """Manually trigger hypothesis generation."""
    if not hypothesis_generator:
        raise HTTPException(status_code=503, detail="Hypothesis generator not initialized")
    created = await hypothesis_generator.generate_from_templates(
        sport=sport, max_hypotheses=max_hypotheses,
    )
    return {"generated": len(created), "hypotheses": created}


@app.post("/research/batch-reject", dependencies=[Depends(require_admin)])
async def batch_reject_hypotheses(request: Request):
    """Batch-reject draft hypotheses matching regex patterns.

    Body: {"patterns": ["rest|b2b", "weather"], "dry_run": true}
    Only operates on status='draft'. Returns count and sample of affected.
    """
    import re
    from tools.schema import open_db

    body = await request.json()
    patterns = body.get("patterns", [])
    dry_run = body.get("dry_run", True)

    if not patterns:
        raise HTTPException(status_code=400, detail="patterns list required")

    compiled = [re.compile(p, re.IGNORECASE) for p in patterns]

    db = await open_db()
    try:
        cursor = await db.execute(
            "SELECT hypothesis_id, name, thesis, sport FROM hypotheses WHERE status = 'draft'"
        )
        rows = await cursor.fetchall()

        matched = []
        for row in rows:
            hid, name, thesis, sport = row
            text = f"{name or ''} {thesis or ''}"
            if any(p.search(text) for p in compiled):
                matched.append({"id": hid, "name": name, "sport": sport})

        if not dry_run and matched:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            ids = [m["id"] for m in matched]
            for i in range(0, len(ids), 500):
                chunk = ids[i:i+500]
                placeholders = ",".join("?" * len(chunk))
                params = tuple([now] + chunk)
                await db.execute(
                    f"UPDATE hypotheses SET status = 'rejected', updated_at = ?, "
                    f"promoted_by = 'batch_purge:generic_edge' "
                    f"WHERE hypothesis_id IN ({placeholders})",
                    params,
                )
            await db.commit()
            logger.info(f"Batch rejected {len(matched)} generic draft hypotheses")

        by_sport = {}
        for m in matched:
            by_sport[m["sport"]] = by_sport.get(m["sport"], 0) + 1

        return {
            "matched": len(matched),
            "dry_run": dry_run,
            "by_sport": by_sport,
            "sample": [m["name"] for m in matched[:20]],
        }
    finally:
        await db.close()


@app.get("/research/sports")
async def get_research_sports():
    """Get all researched sports — all compete equally."""
    from tools.autonomous import RESEARCH_SPORTS
    return {"sports": RESEARCH_SPORTS}


@app.get("/embeddings/stats")
async def embedding_stats(collection: Optional[str] = None):
    """Get embedding store statistics."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.get_collection_stats(collection)


@app.post("/embeddings/search", dependencies=[Depends(require_admin_or_loopback)])
async def embedding_search(
    collection: str,
    query: str,
    top_k: int = 10,
):
    """Search embeddings by text similarity."""
    if not vector_store:
        raise HTTPException(status_code=503, detail="Vector store not initialized")
    return await vector_store.search_text(collection, query, top_k)


@app.get("/data/stats")
async def data_collection_stats():
    """Get data collection statistics."""
    if not data_collector:
        raise HTTPException(status_code=503, detail="Data collector not initialized")
    return await data_collector.get_collection_stats()


def _evaluate_health_signals(report: dict) -> tuple[bool, str, list[str]]:
    """
    Audit the assembled health report for real degradation signals.

    Returns (healthy, severity, reasons). `healthy=False` if any signal trips;
    severity escalates "warning" -> "critical". `reasons` enumerates every
    concrete reason for downstream debugging.

    Demotion matrix:
      write_coordinators[*].writes_failed / writes_total > 1%  -> warning
      write_coordinators[*].queue_depth > 100                  -> warning
      watchdog_monitoring.last_ping_ago_seconds > 60           -> critical
      task_queue.depth > 50 OR oldest_pending_seconds > 600    -> warning
      stalled_phases nonempty                                   -> warning
      pipeline_integrity.healthy == False                       -> critical
      subsystems[*].is_open == True                             -> critical
    """
    reasons: list[str] = []
    severity = "ok"

    def _bump(new: str) -> None:
        nonlocal severity
        order = {"ok": 0, "warning": 1, "critical": 2}
        if order[new] > order[severity]:
            severity = new

    # --- WriteCoordinator signals ---
    for wc in report.get("write_coordinators") or []:
        if not isinstance(wc, dict):
            continue
        name = wc.get("db_path") or wc.get("name") or "writer"
        total = wc.get("writes_total") or 0
        failed = wc.get("writes_failed") or 0
        if total > 0 and (failed / max(total, 1)) > 0.01:
            pct = (failed / total) * 100
            reasons.append(
                f"writes_failed_rate[{name}]: {failed}/{total} ({pct:.2f}%)"
            )
            _bump("warning")
        qd = wc.get("queue_depth") or 0
        if qd > 100:
            reasons.append(f"writer_queue_depth[{name}]: {qd}")
            _bump("warning")

    # --- Watchdog liveness ---
    wm = report.get("watchdog_monitoring") or {}
    last_ping = wm.get("last_ping_ago_seconds")
    total_pings = wm.get("total_pings") or 0
    # Don't flag during the first few checks after boot (no external pinger yet)
    if isinstance(last_ping, (int, float)) and last_ping > 60 and total_pings > 5:
        reasons.append(f"watchdog_last_ping_ago: {last_ping:.0f}s")
        _bump("critical")

    # --- Task queue backlog ---
    tq = report.get("task_queue") or {}
    depth = tq.get("depth") or 0
    oldest = tq.get("oldest_pending_seconds")
    if depth > 50:
        reasons.append(f"task_queue_depth: {depth}")
        _bump("warning")
    if isinstance(oldest, (int, float)) and oldest > 600:
        reasons.append(
            f"task_queue_oldest_pending: {oldest/60:.1f}min"
        )
        _bump("warning")

    # --- Stalled research phases ---
    stalled = report.get("stalled_phases") or []
    if stalled:
        reasons.append(f"stalled_phases: {','.join(sorted(stalled))}")
        _bump("warning")

    # --- Pipeline integrity (already degrades healthy) ---
    pi = report.get("pipeline_integrity") or {}
    if isinstance(pi, dict) and pi.get("healthy") is False:
        issues = pi.get("issues") or pi.get("critical_issues") or []
        if issues:
            reasons.append(f"pipeline_broken: {len(issues)} critical issue(s)")
        else:
            reasons.append("pipeline_broken: integrity check failed")
        _bump("critical")

    # --- Tripped subsystem breakers ---
    for name, sub in (report.get("subsystems") or {}).items():
        if isinstance(sub, dict) and sub.get("is_open"):
            err = (sub.get("last_error") or "")[:100]
            reasons.append(f"breaker_open[{name}]: {err}")
            _bump("critical")

    return (severity == "ok", severity, reasons)


async def _build_health_report() -> dict:
    """Assemble the full /health payload. Shared by /health and /readyz."""
    import time as _time
    if not hasattr(app.state, "_last_health_ping"):
        app.state._last_health_ping = _time.time()
        app.state._health_ping_count = 0
    app.state._last_health_ping = _time.time()
    app.state._health_ping_count += 1

    if not system_health:
        return {
            "healthy": False,
            "severity": "critical",
            "reasons": ["system_health monitor not initialized"],
            "error": "Health monitor not initialized",
        }
    report = system_health.get_full_report()

    # Pipeline integrity — use cached results from the last run (fast)
    try:
        checker = get_integrity_checker()
        integrity = checker.get_latest_report()
        report["pipeline_integrity"] = integrity
        if not integrity.get("healthy", True):
            report["pipeline_broken"] = True
    except Exception as e:
        logger.error(f"Pipeline integrity report failed: {e}", exc_info=True)
        report["pipeline_integrity"] = {
            "status": "error",
            "error": f"integrity check failed: {e}",
        }

    # Watchdog self-monitoring
    _health_gap = _time.time() - getattr(app.state, "_last_health_ping", _time.time())
    if _health_gap > 300 and getattr(app.state, "_health_ping_count", 0) > 5:
        logger.warning(
            f"No watchdog health ping for {_health_gap:.0f}s — "
            "watchdog may be dead"
        )
    report["watchdog_monitoring"] = {
        "last_ping_ago_seconds": round(_health_gap, 1),
        "total_pings": getattr(app.state, "_health_ping_count", 0),
    }

    # WriteCoordinator stats
    try:
        from tools.db_writer import all_stats as _writer_stats
        report["write_coordinators"] = _writer_stats()
    except Exception:
        report["write_coordinators"] = []

    # Task queue depth + oldest pending (cheap: indexed scan)
    try:
        if queue is not None and getattr(queue, "_db", None) is not None:
            try:
                await queue._db.commit()
            except Exception:
                pass
            row = await queue._db.execute_fetchall(
                """SELECT COUNT(*),
                          COALESCE(MIN(created_at), 0)
                     FROM task_queue
                    WHERE status = 'PENDING'"""
            )
            depth = 0
            oldest_s: Optional[float] = None
            if row:
                depth = int(row[0][0] or 0)
                oldest_epoch = row[0][1]
                if oldest_epoch:
                    try:
                        oldest_s = max(0.0, _time.time() - float(oldest_epoch))
                    except (TypeError, ValueError):
                        oldest_s = None
            report["task_queue"] = {
                "depth": depth,
                "oldest_pending_seconds": round(oldest_s, 1) if oldest_s is not None else None,
            }
    except Exception as e:
        report["task_queue"] = {"error": str(e)}

    # Stale ML models — surfaces drift-flagged joblibs so operators see
    # which classifiers are being skipped by the promotion gate. Cheap
    # filesystem walk; never fails the endpoint.
    try:
        from tools.ml_promotion_gate import list_stale_models as _stale_ml
        report["stale_ml_models"] = _stale_ml()
    except Exception as e:
        report["stale_ml_models"] = []
        logger.debug(f"stale_ml_models lookup failed: {e}")

    # Now evaluate demotion signals and stamp reasons
    healthy, severity, reasons = _evaluate_health_signals(report)
    # Only downgrade — the subsystem loop already sets healthy=False on breakers.
    if not healthy:
        report["healthy"] = False
    report["severity"] = severity if not healthy else "ok"
    report["reasons"] = reasons
    return report


@app.get("/health")
async def health_check():
    """
    Comprehensive health check — Layer 2.
    Returns all subsystem statuses, circuit breaker states, error rates,
    and pipeline integrity (is the system producing expected output).
    The sentinel (Layer 3) and watchdog poll this to detect problems.

    `healthy` is demoted based on concrete signals:
      - WriteCoordinator failure rate / queue depth
      - Watchdog ping staleness
      - Task queue backlog / oldest pending
      - Stalled research phases
      - Pipeline integrity failures
      - Tripped subsystem circuit breakers
    See `reasons[]` in the response for every specific cause.
    """
    report = await _build_health_report()
    # Write health file for sentinel to read if HTTP is down
    if system_health:
        system_health.write_health_file()
    return report


@app.get("/health/livez")
async def health_livez():
    """k8s-style liveness: process is up and responsive.
    Always 200 unless the event loop is deadlocked (in which case this
    handler wouldn't respond at all)."""
    import time as _time
    return {"alive": True, "ts": _time.time()}


@app.get("/health/readyz")
async def health_readyz():
    """k8s-style readiness: ready to serve traffic.
    Returns 503 if any demotion condition is met."""
    report = await _build_health_report()
    if not report.get("healthy", False):
        raise HTTPException(
            status_code=503,
            detail={
                "ready": False,
                "severity": report.get("severity", "critical"),
                "reasons": report.get("reasons", []),
            },
        )
    return {
        "ready": True,
        "severity": "ok",
        "uptime_seconds": report.get("uptime_seconds"),
    }


@app.get("/health/detailed")
async def health_detailed():
    """
    Everything /health returns, plus per-source ingestion SLAs and
    per-subsystem trip history. For external observability tools.
    """
    report = await _build_health_report()

    # Per-subsystem trip history (added by SystemHealth.get_full_report)
    report["trip_history"] = report.get("trip_history", [])

    # Per-source ingestion SLAs — best-effort; don't fail the endpoint if
    # the observability module isn't installed yet.
    sla_report: dict = {}
    try:
        from tools import ingestion_observability  # type: ignore
        fn = getattr(ingestion_observability, "get_sla_report", None)
        if callable(fn):
            maybe = fn()
            if asyncio.iscoroutine(maybe):
                sla_report = await maybe
            else:
                sla_report = maybe or {}
    except Exception as e:
        sla_report = {"unavailable": str(e)}
    report["ingestion_sla"] = sla_report

    # feat/regime-aware-sizing (2026-04-22): surface the sizer multipliers
    # currently in effect so operators can see why LIVE stakes may be
    # reduced. Best-effort — never fail the endpoint on regime lookup.
    regimes_block: dict = {}
    try:
        from tools.market_regime import (
            current_regime_multiplier,
            regime_safe_for_trading,
            detect_regime,
        )
        from tools.bet_executor import REGIME_SIZING_ENABLED, REGIME_SAFETY_ENABLED
        sports = [
            "baseball_mlb",
            "basketball_nba",
            "icehockey_nhl",
            "americanfootball_nfl",
            "basketball_ncaab",
            "basketball_ncaaw",
        ]
        per_sport = {}
        for sp in sports:
            try:
                r = detect_regime(sp)
                per_sport[sp] = {
                    "multiplier": current_regime_multiplier(sp),
                    "safe_for_trading": regime_safe_for_trading(sp),
                    "season_phase": r.season_phase,
                    "confidence": round(r.confidence, 3),
                    "noisy_window": r.noisy_window,
                }
            except Exception as e:
                per_sport[sp] = {"error": str(e)}
        regimes_block = {
            "sizing_enabled": REGIME_SIZING_ENABLED,
            "safety_enabled": REGIME_SAFETY_ENABLED,
            "per_sport": per_sport,
        }
    except Exception as e:
        regimes_block = {"unavailable": str(e)}
    report["regimes"] = regimes_block

    return report


@app.get("/regime/sizer-multipliers", dependencies=[Depends(require_admin_or_loopback)])
async def regime_sizer_multipliers():
    """Current regime multiplier per sport, as the portfolio sizer would apply them.

    feat/regime-aware-sizing (2026-04-22). Admin-or-loopback gated — reveals
    both the raw ``current_regime_multiplier`` from the market_regime module
    and the clamped value actually used by
    ``BetExecutor.compute_portfolio_stakes`` after env-toggle + bounds.
    """
    from tools.market_regime import (
        current_regime_multiplier,
        regime_safe_for_trading,
        detect_regime,
    )
    from tools.bet_executor import (
        REGIME_SIZING_ENABLED,
        REGIME_SAFETY_ENABLED,
        _REGIME_MIN_MULT,
        _REGIME_MAX_MULT,
        _clamped_regime_multiplier,
    )
    sports = [
        "baseball_mlb",
        "basketball_nba",
        "icehockey_nhl",
        "americanfootball_nfl",
        "basketball_ncaab",
        "basketball_ncaaw",
    ]
    out: dict = {}
    for sp in sports:
        try:
            r = detect_regime(sp)
            raw = float(current_regime_multiplier(sp))
            applied = _clamped_regime_multiplier(sp)
            out[sp] = {
                "raw_multiplier": round(raw, 3),
                "applied_multiplier": round(applied, 3),
                "safe_for_trading": regime_safe_for_trading(sp),
                "season_phase": r.season_phase,
                "days_into_phase": r.days_into_phase,
                "phase_length_days": r.phase_length_days,
                "confidence": round(r.confidence, 3),
                "noisy_window": r.noisy_window,
                "historical_roi_prior": r.historical_roi_prior,
                "historical_clv_prior": r.historical_clv_prior,
            }
        except Exception as e:
            out[sp] = {"error": str(e)}
    return {
        "sizing_enabled": REGIME_SIZING_ENABLED,
        "safety_enabled": REGIME_SAFETY_ENABLED,
        "bounds": {"min": _REGIME_MIN_MULT, "max": _REGIME_MAX_MULT},
        "sports": out,
    }


@app.get("/admin/writer", dependencies=[Depends(require_admin)])
async def writer_stats():
    """Per-DB WriteCoordinator stats: queue depth, throughput, slowest op."""
    from tools.db_writer import all_stats as _writer_stats
    return {"coordinators": _writer_stats()}


@app.get("/admin/db/migrations", dependencies=[Depends(require_admin_or_loopback)])
async def db_migrations_status():
    """Migration state for the live DB.

    Returns applied list, pending list, schema version, and any
    checksum-drift entries (source file edited after apply). Read-only —
    does not acquire the migration lock.
    """
    from tools.migrations import get_migration_status
    try:
        return get_migration_status(DB_PATH)
    except Exception as e:
        logger.error(f"/admin/db/migrations failed: {e!r}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"migration status error: {e!s}")


@app.get("/admin/db/health", dependencies=[Depends(require_admin_or_loopback)])
async def admin_db_health():
    """DB health snapshot: WAL size, page count, checkpoint stats, busy-timeout hits.

    Read-only — safe to poll. Returns:
      - wal_size_mb / wal_page_count: current on-disk WAL size
      - db_size_mb / db_page_count / page_size: main DB file
      - last_checkpoint_*: metrics from the most recent wal_maintenance pass
      - busy_timeout_hits: count of "database is locked" retries in the last hour
      - coordinator: WriteCoordinator per-DB stats (queue depth, writes, slow ops)
      - file_sizes: direct stat() of .db, .db-wal, .db-shm
    """
    from tools.db_utils import busy_timeout_stats
    from tools.db_writer import all_stats as _writer_stats

    out: dict = {
        "db_path": DB_PATH,
        "busy_timeout_hits": busy_timeout_stats(3600.0),
        "maintenance": dict(_wal_health_state),
        "coordinator": _writer_stats(),
        "config": {
            "interval_s": WAL_MAINTENANCE_INTERVAL_S,
            "truncate_threshold_pages": WAL_TRUNCATE_PAGE_THRESHOLD,
        },
    }
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout = 5000")
            page_size_row = await (await db.execute("PRAGMA page_size")).fetchone()
            page_count_row = await (await db.execute("PRAGMA page_count")).fetchone()
            wal_row = await (await db.execute("PRAGMA wal_checkpoint(PASSIVE)")).fetchone()
            journal_row = await (await db.execute("PRAGMA journal_mode")).fetchone()
            freelist_row = await (await db.execute("PRAGMA freelist_count")).fetchone()
            page_size = page_size_row[0] if page_size_row else 4096
            page_count = page_count_row[0] if page_count_row else 0
            freelist = freelist_row[0] if freelist_row else 0
            wal_busy, wal_log_pages, wal_ckpt = (wal_row or (0, 0, 0))
            out["page_size"] = page_size
            out["db_page_count"] = page_count
            out["db_size_mb"] = round((page_count * page_size) / (1024 * 1024), 3)
            out["wal_page_count"] = wal_log_pages
            out["wal_size_mb"] = round((wal_log_pages * page_size) / (1024 * 1024), 3)
            out["wal_checkpoint_busy"] = wal_busy
            out["wal_checkpointed_now"] = wal_ckpt
            out["journal_mode"] = journal_row[0] if journal_row else None
            out["freelist_pages"] = freelist
            if page_count:
                out["fragmentation_ratio"] = round(freelist / max(page_count, 1), 4)
            else:
                out["fragmentation_ratio"] = 0.0
    except Exception as e:
        out["pragma_error"] = f"{type(e).__name__}: {e}"

    try:
        import os.path as _p
        files = {}
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if _p.exists(p):
                files[suffix or "db"] = {
                    "path": p,
                    "size_bytes": os.path.getsize(p),
                    "size_mb": round(os.path.getsize(p) / (1024 * 1024), 3),
                }
        out["file_sizes"] = files
    except Exception as e:
        out["file_sizes_error"] = f"{type(e).__name__}: {e}"

    return out


@app.get("/health/deep")
async def health_deep():
    """
    Full pipeline integrity suite — runs ALL checks on demand.
    Slower than /health (queries multiple tables). Use this for
    debugging pipeline issues, not for polling.

    Returns: complete integrity check results + subsystem health.
    """
    try:
        checker = get_integrity_checker()
        result = await checker.run_all_checks()
    except Exception as e:
        logger.error(f"Deep health check failed: {e}", exc_info=True)
        result = {"error": f"deep check failed: {e}"}

    # Include Layer 2 subsystem status for complete picture
    if system_health:
        result["subsystems"] = system_health.get_full_report()

    return result


@app.get("/health/integrity/history")
async def integrity_history(limit: int = 50):
    """Get recent pipeline integrity check history."""
    try:
        checker = get_integrity_checker()
        history = await checker.get_history(limit=limit)
        return {"count": len(history), "checks": history}
    except Exception as e:
        logger.error(f"Integrity history fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/claude/status")
async def claude_status():
    """Get Claude Code availability and usage stats."""
    from tools.claude_code import get_usage_stats
    return get_usage_stats()


@app.post("/admin/claude/reset", dependencies=[Depends(require_admin)])
async def reset_claude_rate_limit():
    """Force-reset Claude Code rate limit state after hourly limit resets."""
    from tools.claude_code import reset_rate_limit
    return reset_rate_limit()


@app.get("/system/full-status")
async def full_system_status():
    """
    Single endpoint for checking everything from your phone.
    Returns all subsystem statuses in one call.
    Pipeline integrity is front-and-center so DEGRADED/BROKEN status
    is immediately visible in every Claude Code session start.
    """
    from tools.claude_code import get_usage_stats as claude_stats

    status = {
        "timestamp": __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
    }

    # Pipeline integrity first — this is the most important signal
    try:
        checker = get_integrity_checker()
        integrity = checker.get_latest_report()
        status["pipeline_integrity"] = integrity
    except Exception as e:
        logger.error(f"Pipeline integrity report failed in full-status: {e}", exc_info=True)
        status["pipeline_integrity"] = {
            "status": "error",
            "error": f"integrity check failed: {e}",
        }

    status["autonomous_loop"] = autonomous.get_status() if autonomous else None
    status["research_loop"] = research_loop.get_status() if research_loop else None
    status["claude_code"] = claude_stats()
    status["line_monitor"] = (await line_monitor.get_status()) if line_monitor else None

    # Live in-game state collector — exposes running bool, active games,
    # and 24h counters so we can verify from /system/full-status that
    # the detector path is actually firing.
    try:
        from tools.live_state import (
            get_collector_status as _live_status,
            get_collector_counters_24h as _live_counters,
        )
        live_status = _live_status()
        try:
            live_status.update(await _live_counters(db_path=DB_PATH))
        except Exception as e:
            logger.debug(f"live_state 24h counters failed: {e}")
        status["live_state_collector"] = live_status
    except Exception as e:
        status["live_state_collector"] = {"error": f"{e!r}"}

    # Add hypothesis summary — ground-truth from DB, not in-memory counters
    if hypothesis_manager:
        try:
            db = hypothesis_manager._db
            # Status counts direct from DB
            cursor = await db.execute(
                "SELECT status, COUNT(*) FROM hypotheses GROUP BY status"
            )
            status_counts = {row[0]: row[1] for row in await cursor.fetchall()}
            total = sum(status_counts.values())

            # Ground-truth backtest event/signal counts — deduplicated by event_id
            # (each game generates multiple rows across books; dedup to match
            # evaluate_significance which keeps best-edge row per event)
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT event_id), "
                "COUNT(DISTINCT CASE WHEN signal_generated = 1 THEN event_id END) "
                "FROM backtest_events"
            )
            row = await cursor.fetchone()
            total_events = row[0] or 0
            total_signals = row[1] or 0

            # Per-status event counts — deduplicated by event_id
            cursor = await db.execute(
                "SELECT h.status, COUNT(DISTINCT be.event_id), "
                "COUNT(DISTINCT CASE WHEN be.signal_generated = 1 THEN be.event_id END) "
                "FROM backtest_events be "
                "JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id "
                "GROUP BY h.status"
            )
            events_by_status = {
                row[0]: {"events": row[1] or 0, "signals": row[2] or 0}
                for row in await cursor.fetchall()
            }

            # Active backtesting: only hypotheses with actual events
            cursor = await db.execute(
                "SELECT COUNT(DISTINCT be.hypothesis_id) "
                "FROM backtest_events be "
                "JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id "
                "WHERE h.status = 'backtesting'"
            )
            active_backtesting = (await cursor.fetchone())[0] or 0

            status["hypotheses"] = {
                "total": total,
                "draft": status_counts.get("draft", 0),
                "backtesting": status_counts.get("backtesting", 0),
                "backtesting_with_data": active_backtesting,
                "paper_trading": status_counts.get("paper_trading", 0),
                "live": status_counts.get("live", 0),
                "rejected": status_counts.get("rejected", 0),
                "retired": status_counts.get("retired", 0),
                "backtest_events_total": total_events,
                "backtest_signals_total": total_signals,
                "events_by_status": events_by_status,
            }
        except Exception as e:
            logger.warning(f"Failed to get hypothesis summary for full-status: {e}")

    # Add embedding stats
    if vector_store:
        try:
            status["embeddings"] = await vector_store.get_collection_stats()
        except Exception as e:
            logger.warning(f"Failed to get embedding stats for full-status: {e}")

    # Add data collection stats
    if data_collector:
        try:
            status["data"] = await data_collector.get_collection_stats()
        except Exception as e:
            logger.warning(f"Failed to get data collection stats for full-status: {e}")

    # Layer 2 health subsystems
    if system_health:
        try:
            health_report = system_health.get_full_report()
            status["system_health"] = {
                "healthy": health_report.get("healthy"),
                "uptime_hours": health_report.get("uptime_hours"),
                "stalled_phases": health_report.get("stalled_phases", []),
            }
        except Exception as e:
            logger.warning(f"Failed to get system health for full-status: {e}")

    return status


# ---------------------------------------------------------------------------
# Observability — Prometheus-text + JSON metrics exposition
# ---------------------------------------------------------------------------

@app.get("/metrics")
async def metrics_prometheus():
    """Prometheus text exposition format, version 0.0.4.

    Intentionally unauthenticated on loopback (same treatment as /health):
    metrics are for local scrapers and the ops dashboard. If you expose
    the API over a network, gate this at the reverse proxy.
    """
    from fastapi.responses import Response
    # Best-effort live gauges — refresh right before serving.
    try:
        from tools.metrics import set_db_connection_count
        # Count tracked writers / common open handles. aiosqlite doesn't
        # expose a global registry, so this is a cheap heuristic from the
        # WriteCoordinator stats where present.
        open_conns = 0
        try:
            from tools.db_writer import all_stats as _wstats
            for coord in _wstats() or []:
                if coord.get("running"):
                    open_conns += 1
        except Exception:
            pass
        set_db_connection_count(open_conns)
    except Exception:
        pass
    try:
        if queue is not None and getattr(queue, "_db", None) is not None:
            try:
                row = await queue._db.execute_fetchall(
                    """SELECT status, COUNT(*)
                         FROM task_queue
                        WHERE status IN ('PENDING','PROCESSING')
                        GROUP BY status"""
                )
                # No dedicated gauge for queue depth yet — kept here as a
                # future-extension hook; omitted from the core metrics list
                # to avoid double-counting the `status`-only histograms.
                _ = row
            except Exception:
                pass
    except Exception:
        pass
    body = _metrics_registry().render_prometheus()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@app.get("/metrics/json")
async def metrics_json():
    """Structured JSON snapshot of every metric — consumed by the ops dashboard."""
    return _metrics_registry().render_json()


# ---------------------------------------------------------------------------
# Task listing & context sync
# ---------------------------------------------------------------------------

@app.get("/tasks")
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


_tracemalloc_snapshot: Optional[tracemalloc.Snapshot] = None


@app.get("/debug/memory")
async def debug_memory(_auth: None = Depends(require_admin)):
    """tracemalloc snapshot comparison — identifies the top growing allocations.

    First call takes a baseline snapshot. Subsequent calls compare against
    the previous snapshot and return the top 30 growing allocations by size.
    Also forces gc.collect() and reports process RSS.
    """
    global _tracemalloc_snapshot
    import psutil

    gc.collect()
    process = psutil.Process()
    rss_mb = process.memory_info().rss / (1024 * 1024)

    if not tracemalloc.is_tracing():
        raise HTTPException(
            status_code=409,
            detail=f"tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable (rss_mb={round(rss_mb, 1)})",
        )

    current = tracemalloc.take_snapshot()
    current = current.filter_traces((
        tracemalloc.Filter(False, "<frozen *>"),
        tracemalloc.Filter(False, "<unknown>"),
        tracemalloc.Filter(False, tracemalloc.__file__),
    ))

    result = {
        "rss_mb": round(rss_mb, 1),
        "tracemalloc_traced_mb": round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1),
        "tracemalloc_peak_mb": round(tracemalloc.get_traced_memory()[1] / (1024 * 1024), 1),
    }

    if _tracemalloc_snapshot is not None:
        # Compare against previous snapshot — shows what GREW
        stats = current.compare_to(_tracemalloc_snapshot, "lineno")
        result["comparison"] = "vs_previous_snapshot"
        result["top_growth"] = [
            {
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 1),
                "size_diff_kb": round(stat.size_diff / 1024, 1),
                "count": stat.count,
                "count_diff": stat.count_diff,
            }
            for stat in stats[:30]
        ]
    else:
        # First call — just show current top allocations
        stats = current.statistics("lineno")
        result["comparison"] = "baseline (first call)"
        result["top_allocations"] = [
            {
                "file": str(stat.traceback),
                "size_kb": round(stat.size / 1024, 1),
                "count": stat.count,
            }
            for stat in stats[:30]
        ]

    _tracemalloc_snapshot = current
    return result


@app.get("/debug/memory/top-traces")
async def debug_memory_traces(limit: int = 10, _auth: None = Depends(require_admin)):
    """Show full stack traces for the top memory consumers."""
    if not tracemalloc.is_tracing():
        raise HTTPException(
            status_code=409,
            detail="tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable",
        )

    snapshot = tracemalloc.take_snapshot()
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen *>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))
    stats = snapshot.statistics("traceback")

    traces = []
    for stat in stats[:limit]:
        traces.append({
            "size_kb": round(stat.size / 1024, 1),
            "count": stat.count,
            "traceback": [str(line) for line in stat.traceback.format()],
        })
    return {"top_traces": traces}


@app.post("/debug/memory/gc")
async def debug_gc(_auth: None = Depends(require_admin)):
    """Force garbage collection and report stats."""
    gc.collect()
    gc.collect()  # Second pass catches ref cycles
    import psutil
    rss_mb = psutil.Process().memory_info().rss / (1024 * 1024)
    result = {
        "rss_mb": round(rss_mb, 1),
        "gc_counts": gc.get_count(),
        "gc_stats": gc.get_stats(),
    }
    if tracemalloc.is_tracing():
        result["tracemalloc_traced_mb"] = round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1)
    else:
        result["tracemalloc"] = "disabled (set CALLISTO_TRACEMALLOC=1 to enable)"
    return result


# PRAGMA allowlist for /admin/sql — read-only diagnostic pragmas only.
# ANY other PRAGMA (writable_schema=1, journal_mode=OFF, foreign_keys=OFF, etc.)
# is rejected. Value assignment to even allowed PRAGMAs is rejected.
_ALLOWED_PRAGMAS = frozenset({
    "integrity_check",
    "quick_check",
    "page_count",
    "page_size",
    "wal_autocheckpoint",
    "wal_checkpoint",
    "schema_version",
    "user_version",
    "cache_size",
    "freelist_count",
    "journal_mode",      # read-only query form
    "database_list",
    "table_info",
    "index_list",
    "index_info",
    "foreign_key_list",
    "compile_options",
})


def _validate_admin_sql(sql: str) -> Optional[str]:
    """AST-validate a /admin/sql query. Return None if OK, else error string.

    Rules:
      * exactly one statement (sqlparse must parse to exactly one non-empty stmt)
      * must be SELECT or a whitelisted read-only PRAGMA
      * PRAGMA forbidden if it assigns a value or is not in _ALLOWED_PRAGMAS
      * rejects CTEs whose body contains INSERT/UPDATE/DELETE (write-CTEs)
    """
    try:
        import sqlparse
        from sqlparse.sql import Statement
    except ImportError:
        # Degraded-mode fallback: sqlparse isn't installed. Be extra strict —
        # accept only simple SELECTs with no semicolons and no PRAGMA at all.
        normalized = sql.strip().rstrip(";")
        if ";" in normalized:
            return "Multi-statement queries not allowed"
        if not normalized.upper().startswith("SELECT"):
            return "sqlparse unavailable; only single SELECT allowed in degraded mode"
        forbidden = ("PRAGMA", "DROP", "DELETE", "INSERT", "UPDATE", "ALTER",
                     "CREATE", "ATTACH", "DETACH", "REINDEX", "VACUUM", "REPLACE")
        upper = normalized.upper()
        import re as _re
        for kw in forbidden:
            if _re.search(rf"\b{kw}\b", upper):
                return f"Forbidden keyword: {kw}"
        return None

    parsed = sqlparse.parse(sql)
    # sqlparse may return empty statements for trailing semicolons — filter them.
    real_stmts = [
        s for s in parsed
        if isinstance(s, Statement) and s.tokens and str(s).strip().rstrip(";").strip()
    ]
    if len(real_stmts) == 0:
        return "Empty statement"
    if len(real_stmts) > 1:
        return "Multi-statement queries not allowed"
    stmt = real_stmts[0]
    stmt_type = stmt.get_type()  # 'SELECT', 'PRAGMA', 'UPDATE', 'UNKNOWN', etc.

    # Check for write-verbs anywhere (e.g., hidden inside a WITH ... DELETE CTE).
    # sqlparse doesn't flag these via get_type() when wrapped in a CTE.
    upper_sql = str(stmt).upper()
    import re as _re
    for kw in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE",
               "ATTACH", "DETACH", "REINDEX", "VACUUM", "REPLACE"):
        if _re.search(rf"\b{kw}\b", upper_sql):
            return f"Forbidden keyword: {kw}"

    if stmt_type == "SELECT":
        return None

    # PRAGMA handling — sqlparse classifies the whole "PRAGMA name[=value]"
    # as a single Identifier token under an UNKNOWN statement type, so we
    # prefix-sniff the raw upper-cased text instead.
    stripped_upper = upper_sql.strip().rstrip(";").strip()
    if stripped_upper.startswith("PRAGMA"):
        # Extract PRAGMA body + check for assignment.
        #   Allowed:  PRAGMA integrity_check;   PRAGMA page_count;
        #   Rejected: PRAGMA writable_schema=1; PRAGMA journal_mode=OFF;
        #             PRAGMA foreign_keys=OFF;
        body = stripped_upper[len("PRAGMA"):].strip()
        if not body:
            return "Empty PRAGMA"
        # Reject any assignment syntax
        if "=" in body:
            return "PRAGMA value assignment not allowed"
        # Reject function-call style with args beyond the trivial form,
        # e.g. PRAGMA wal_checkpoint(TRUNCATE) — keep it very conservative.
        if "(" in body:
            name = body.split("(", 1)[0].strip().lower()
        else:
            name = body.strip().lower()
        if name not in _ALLOWED_PRAGMAS:
            return f"PRAGMA '{name}' not in allowlist"
        return None

    if stmt_type == "UNKNOWN":
        return "Unrecognized statement type; only SELECT and whitelisted PRAGMA allowed"
    return f"Statement type '{stmt_type}' not allowed"


@app.post("/admin/sql")
async def admin_sql(request: Request, _auth: None = Depends(require_admin)):
    """Read-only SQL query against callisto.db for debugging.

    AST-validated: parses via sqlparse, rejects multi-statement queries,
    write-verbs (even inside CTEs), and any PRAGMA outside a small read-only
    allowlist. Also runs under `PRAGMA query_only = ON` and a 10s timeout.
    """
    body = await request.json()
    sql = (body.get("sql") or "").strip()
    if not sql:
        raise HTTPException(status_code=400, detail="No SQL provided")

    err = _validate_admin_sql(sql)
    if err:
        _auth_logger.warning(
            "AUTH_ADMIN_SQL_REJECTED host=%s reason=%s sql=%r",
            (request.client.host if request.client else "?"),
            err,
            sql[:300],
        )
        raise HTTPException(status_code=400, detail=err)

    # 10-second execution budget. sqlite3's progress handler fires every N
    # opcodes; returning non-zero aborts the query cleanly.
    import time as _time
    import sqlite3 as _sqlite3
    start = _time.monotonic()

    def _timeout_handler():
        if _time.monotonic() - start > 10.0:
            return 1  # abort
        return 0

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA query_only = ON")
            # Attach progress handler on the underlying sqlite3 connection.
            # aiosqlite exposes it via `db._conn`; fall back to leaving it off.
            try:
                raw_conn = getattr(db, "_conn", None)
                if raw_conn is not None:
                    raw_conn.set_progress_handler(_timeout_handler, 10_000)
            except Exception:
                pass
            try:
                cursor = await db.execute(sql)
                rows = await cursor.fetchall()
            except _sqlite3.OperationalError as oe:
                if "interrupted" in str(oe).lower() or "abort" in str(oe).lower():
                    raise HTTPException(status_code=504, detail="Query exceeded 10s timeout")
                raise
            finally:
                try:
                    if raw_conn is not None:
                        raw_conn.set_progress_handler(None, 0)
                except Exception:
                    pass
            cols = [d[0] for d in cursor.description] if cursor.description else []
            return {
                "columns": cols,
                "rows": [list(r) for r in rows[:500]],  # Cap at 500 rows
                "row_count": len(rows),
                "truncated": len(rows) > 500,
            }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("admin_sql execution failed sql=%r", sql[:300])
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Bet executor endpoints
# ---------------------------------------------------------------------------
_executor = None


async def _get_executor():
    global _executor
    if _executor is None:
        from tools.bet_executor import BetExecutor
        _executor = BetExecutor()
        await _executor.initialize()
    return _executor


@app.get("/executor/status")
async def executor_status():
    """Get bet executor status."""
    ex = await _get_executor()
    return await ex.status()


@app.post("/executor/enable", dependencies=[Depends(require_admin)])
async def executor_enable():
    """Enable both the order manager and the legacy bet executor.

    The order_manager is the default active subsystem
    (CALLISTO_USE_ORDER_MANAGER=1); bet_executor is kept enabled as
    fallback. Flipping either flag off is an explicit /pause via the
    subsystem-specific endpoint below.
    """
    ex = await _get_executor()
    ex.enable()
    # Wire into research loop if available
    if hasattr(app.state, "research_loop"):
        app.state.research_loop._bet_executor = ex
    om = order_manager_instance
    if om is not None:
        om.enable()
    return {
        "status": "enabled",
        "order_manager": om.is_enabled if om else None,
        "bet_executor": ex.is_enabled,
        "message": "Order manager + bet executor are LIVE",
    }


@app.post("/executor/disable", dependencies=[Depends(require_admin_or_loopback)])
async def executor_disable():
    """Disable both subsystems — no orders will be submitted or placed."""
    ex = await _get_executor()
    ex.disable()
    om = order_manager_instance
    if om is not None:
        om.disable()
    return {
        "status": "disabled",
        "message": "Order manager + bet executor disabled",
    }


@app.get("/orders", dependencies=[Depends(require_admin_or_loopback)])
async def orders_list(state: Optional[str] = None, limit: int = 50):
    """List orders, optionally filtered by state."""
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    rows = await order_manager_instance.list_orders(state=state, limit=limit)
    return {
        "count": len(rows),
        "orders": [
            {
                "order_id": o.order_id,
                "hypothesis_id": o.hypothesis_id,
                "signal_id": o.signal_id,
                "sport": o.sport,
                "event_id": o.event_id,
                "market": o.market,
                "side": o.side,
                "price_american": o.price_american,
                "stake_units": o.stake_units,
                "stake_dollars": o.stake_dollars,
                "state": o.state,
                "book": o.book,
                "placed_at": o.placed_at,
                "settled_at": o.settled_at,
                "pnl_dollars": o.pnl_dollars,
                "expires_at": o.expires_at,
                "created_at": o.created_at,
                "bet_id": o.bet_id,
                "edge": o.edge,
            }
            for o in rows
        ],
    }


@app.get("/orders/{order_id}", dependencies=[Depends(require_admin_or_loopback)])
async def orders_get(order_id: str):
    """Fetch one order including full state history."""
    from tools.order_manager import OrderNotFound
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    try:
        o = await order_manager_instance.get_order(order_id)
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    return {
        "order_id": o.order_id,
        "hypothesis_id": o.hypothesis_id,
        "signal_id": o.signal_id,
        "odds_snapshot_id": o.odds_snapshot_id,
        "sport": o.sport,
        "event_id": o.event_id,
        "market": o.market,
        "side": o.side,
        "price_american": o.price_american,
        "stake_units": o.stake_units,
        "stake_dollars": o.stake_dollars,
        "state": o.state,
        "state_history": o.state_history,
        "book": o.book,
        "placed_at": o.placed_at,
        "settled_at": o.settled_at,
        "pnl_dollars": o.pnl_dollars,
        "expires_at": o.expires_at,
        "created_at": o.created_at,
        "bet_id": o.bet_id,
        "edge": o.edge,
        "fair_prob": o.fair_prob,
    }


@app.post("/orders/{order_id}/approve", dependencies=[Depends(require_admin)])
async def orders_approve(order_id: str):
    from tools.order_manager import OrderNotFound, InvalidTransition
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    try:
        o = await order_manager_instance.approve(order_id, reason="http_approve")
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "approved", "order_id": o.order_id, "state": o.state}


@app.post("/orders/{order_id}/reject", dependencies=[Depends(require_admin)])
async def orders_reject(order_id: str, reason: str = "http_reject"):
    from tools.order_manager import OrderNotFound, InvalidTransition
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    try:
        o = await order_manager_instance.reject(order_id, reason=reason)
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "rejected", "order_id": o.order_id, "state": o.state}


@app.post("/orders/{order_id}/fill", dependencies=[Depends(require_admin)])
async def orders_fill(order_id: str, actual_price: Optional[int] = None):
    from tools.order_manager import OrderNotFound, InvalidTransition
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    try:
        o = await order_manager_instance.mark_filled(
            order_id, actual_price=actual_price, reason="http_fill"
        )
    except OrderNotFound:
        raise HTTPException(404, f"order {order_id} not found")
    except InvalidTransition as e:
        raise HTTPException(409, str(e))
    return {"status": "filled", "order_id": o.order_id, "state": o.state,
            "price_american": o.price_american}


@app.post("/orders/reconcile", dependencies=[Depends(require_admin_or_loopback)])
async def orders_reconcile():
    """Trigger the settlement reconciler immediately (cron path)."""
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    stats = await reconcile_filled_orders(order_manager_instance)
    return {"status": "ok", **stats}


@app.post("/orders/voids", dependencies=[Depends(require_admin_or_loopback)])
async def orders_voids():
    """Trigger the postponed/cancelled game void-detector immediately."""
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    stats = await detect_voided_orders(order_manager_instance)
    return {"status": "ok", **stats}


@app.post("/orders/expire", dependencies=[Depends(require_admin_or_loopback)])
async def orders_expire():
    """Trigger the expiry sweep immediately."""
    if order_manager_instance is None:
        raise HTTPException(503, "order_manager not initialised")
    expired = await order_manager_instance.expire_stale()
    return {"status": "ok", "expired": expired, "count": len(expired)}


@app.post("/executor/login", dependencies=[Depends(require_admin)])
async def executor_login():
    """Launch browser for DraftKings login. Browser opens visible for manual login."""
    ex = await _get_executor()
    logged_in = await ex.ensure_logged_in()
    if logged_in:
        return {"status": "logged_in", "message": "DraftKings session active"}
    else:
        return {
            "status": "login_required",
            "message": "Browser opened — please log into DraftKings manually. Session will persist.",
        }


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
