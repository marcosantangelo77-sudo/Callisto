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
import tracemalloc
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

import aiosqlite
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agp import Domain
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


def _client_is_loopback(request: Request) -> bool:
    host = (request.client.host if request.client else "") or ""
    return host in ("127.0.0.1", "::1", "localhost")


async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Hard-gate: require Bearer token. Fails closed if CALLISTO_ADMIN_TOKEN unset."""
    if not CALLISTO_ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="CALLISTO_ADMIN_TOKEN not configured; admin endpoint disabled",
        )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, CALLISTO_ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")


async def require_admin_or_loopback(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Soft-gate for read endpoints. Allow loopback when token unset; otherwise require token."""
    if not CALLISTO_ADMIN_TOKEN:
        if _client_is_loopback(request):
            return
        raise HTTPException(status_code=403, detail="Loopback only when admin token unset")
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(status_code=401, detail="Bearer token required")
    if not _secrets.compare_digest(credentials.credentials, CALLISTO_ADMIN_TOKEN):
        raise HTTPException(status_code=403, detail="Forbidden")

# Shared state
memory: Optional[MemoryStore] = None
queue: Optional[TaskQueue] = None
orchestrator_instance: Optional[Orchestrator] = None
monitor: Optional[HealthMonitor] = None
line_monitor: Optional[LineMonitor] = None
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
    """If a session concluded with INSUFFICIENT DATA and a clear next step, auto-queue follow-up."""
    try:
        summary = result.get("summary", {})
        conclusion = summary.get("conclusion", "")
        confidence = summary.get("confidence_score", 1.0)
        tier = summary.get("confidence_tier", "")

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
        task_id = await queue.submit_task(followup_query, priority=1)
        logger.info(f"Auto-queued follow-up task {task_id} from parent {parent_task_id}")
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
                    # Signal the watchdog to restart us
                    restart_file = os.path.join(
                        os.path.dirname(__file__), "memory", "restart_requested"
                    )
                    with open(restart_file, "w") as f:
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


# Per-task hard timeout. AGP sessions that route through Claude Code
# occasionally run 7+ minutes (observed 419s on task 484, 2026-04-18),
# and the worker processes tasks serially — one slow session stalls every
# pending task behind it (today: task 485 waited >5 min). Sessions beyond
# this budget are cancelled and marked FAILED; the worker moves on so the
# queue stays fluid. Overridable via CALLISTO_TASK_TIMEOUT_S env var.
TASK_WORKER_TIMEOUT_S = float(os.getenv("CALLISTO_TASK_TIMEOUT_S", "300"))


async def task_worker():
    """Background worker: polls task queue and runs AGP sessions."""
    while True:
        try:
            task = await queue.get_next()
            if task is None:
                await asyncio.sleep(2)
                continue

            task_id = task["task_id"]
            query = task["query"]
            skip_search = _is_internal_query(query)
            logger.info(f"Worker picked up task {task_id} (skip_search={skip_search}): {query}")

            # In local_only mode, skip tasks that would require Claude
            # (orchestrator calls claude_code_query without checking local_only)
            if research_loop and research_loop._local_only:
                logger.info(f"Task {task_id} skipped — local_only mode, orchestrator would call Claude")
                await queue.fail_task(task_id, "local_only mode — Claude unavailable")
                continue

            try:
                # Hard-cap the whole AGP session. asyncio.wait_for cancels the
                # inner coroutine on timeout so the orchestrator stops burning
                # Claude credits / VRAM for a result that will be discarded.
                result = await asyncio.wait_for(
                    orchestrator_instance.run_session(query, skip_search=skip_search),
                    timeout=TASK_WORKER_TIMEOUT_S,
                )
                session_id = result.get("session_id")
                await queue.complete_task(task_id, result, session_id=session_id)
                logger.info(f"Task {task_id} completed, session {session_id}")

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
            except asyncio.TimeoutError:
                logger.error(
                    f"Task {task_id} TIMEOUT after {TASK_WORKER_TIMEOUT_S}s — "
                    f"orchestrator session cancelled to unblock queue."
                )
                await queue.fail_task(
                    task_id,
                    f"timeout: orchestrator exceeded {TASK_WORKER_TIMEOUT_S}s budget",
                )
            except Exception as e:
                logger.error(f"Task {task_id} failed: {e}", exc_info=True)
                await queue.fail_task(task_id, str(e))

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Worker error: {e}", exc_info=True)
            await asyncio.sleep(5)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle manager."""
    global memory, queue, orchestrator_instance, monitor, line_monitor, clv_tracker, autonomous, telegram_listener, hypothesis_manager, historical_fetcher, backtest_engine, vector_store, hypothesis_generator, data_collector, research_loop, system_health, learned_correlation_store, worker_task, wal_checkpoint_task

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

    worker_task = asyncio.create_task(task_worker())
    wal_checkpoint_task = asyncio.create_task(wal_checkpoint_loop())
    logger.info(f"Callisto API started on port {CALLISTO_PORT} (WAL checkpoint every 5m)")

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
    if worker_task:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
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
    """
    try:
        task_id = await queue.submit_task(submission.query, submission.priority)
        return TaskResponse(task_id=task_id)
    except Exception as e:
        logger.error(f"POST /task failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.get("/task/{task_id}")
async def get_task(task_id: int, _auth: None = Depends(require_admin_or_loopback)):
    """Get task status and result."""
    task = await queue.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/session/{session_id}")
async def get_session(session_id: str, _auth: None = Depends(require_admin_or_loopback)):
    """Get a sealed AGP session with full provenance."""
    session = await memory.get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@app.get("/world/{domain}")
async def query_world(
    domain: str,
    keyword: Optional[str] = None,
    min_confidence: Optional[float] = None,
    limit: int = 50,
):
    """Query a domain world. When ``keyword`` is present, retrieval is
    SEMANTIC (vector similarity) with a keyword-LIKE fallback; otherwise
    the recent-first ordering is used.
    """
    try:
        domain_enum = Domain(domain.upper())
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid domain. Must be one of: {[d.value for d in Domain]}",
        )
    # Hard cap on limit so ?limit=1000000 can't blow the API's memory.
    if limit < 1:
        limit = 1
    limit = min(limit, 500)
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


@app.post("/odds/snapshot/{sport}")
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


@app.post("/odds/parlay-scan/{sport}")
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
        return {"error": odds_data["error"]}

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
        return {
            "error": f"No snapshot data for {sport}. "
            f"Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}",
        }

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
        return {"error": games_data["error"], "games": []}

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
    return (await line_monitor.get_status()) if line_monitor else {"error": "Monitor not initialized"}


@app.get("/odds/learned-correlations")
async def get_learned_correlations():
    """Get learned correlation estimates — Bayesian blend of priors + empirical data."""
    if learned_correlation_store is None:
        return {"error": "Learned correlation store not initialized"}
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


class BetResolution(BaseModel):
    result: str = Field(..., pattern="^(won|lost|push)$")
    payout: Optional[float] = Field(default=None, ge=0, le=10_000_000)


@app.post("/bets/record", dependencies=[Depends(require_admin)])
async def record_bet(bet: BetSubmission):
    """Record a bet at placement time for CLV tracking."""
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
    )
    return {"bet_id": bet_id}


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
        return {"error": odds_data["error"]}

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
        return {"error": odds_data["error"]}

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
        return {"error": f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}"}

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
        return {"error": f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}"}

    _dn_sport = sport.lower()
    if _dn_sport not in SPORT_ALIASES:
        return {"error": f"Sport '{sport}' not supported for dead number analysis. Supported: {list(set(SPORT_ALIASES.values()))}"}

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
        return {"error": f"No game results for {sport}"}
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
        return {"error": f"No snapshot data for {sport}. Wait for next snapshot cycle or force one via POST /odds/snapshot/{sport}"}

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


@app.post("/simulate/basketball")
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


@app.post("/simulate/poisson")
async def simulate_poisson_game(req: PoissonRequest):
    """Run Poisson simulation for low-scoring sports."""
    from tools.simulation import simulate_poisson
    return simulate_poisson(req.home_expected, req.away_expected)


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
        return {"error": f"No snapshot available for {sport}. Trigger a snapshot first.",
                "sport": sport}

    games = snapshot.get("games", [])
    if not games:
        return {"error": "No games in snapshot", "sport": sport}

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
        return {"error": f"Sport {sport} not supported by injury model"}

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
            return {"error": alt_data["error"]}
        gaps = scan_line_gaps(alt_data.get("bookmakers", []), market_key=market)
        return {"event_id": event_id, "market": market, "gap_count": len(gaps), "gaps": gaps}

    # No event_id — scan first 5 games
    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

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
            return {"error": prop_data["error"]}
        gaps = scan_prop_gaps(prop_data)
        return {"event_id": event_id, "gap_count": len(gaps), "gaps": gaps}

    odds_data = await _get_odds(sport=sport, regions="us", markets="h2h")
    if odds_data.get("error"):
        return {"error": odds_data["error"]}

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


@app.post("/boosts/evaluate-fixed")
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


@app.post("/boosts/evaluate-percentage")
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


@app.post("/boosts/evaluate-free-bet")
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


@app.post("/boosts/hedge")
async def hedge_calc(req: HedgeRequest):
    """Calculate optimal hedge for guaranteed profit."""
    from tools.boost_evaluator import calculate_hedge

    return calculate_hedge(
        boost_stake=req.boost_stake,
        boosted_odds=req.boosted_odds,
        hedge_odds=req.hedge_odds,
        fair_probability=req.fair_probability,
    )


@app.post("/boosts/devig")
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


@app.post("/boosts/evaluate-parlay")
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


@app.post("/hypothesis")
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


@app.post("/backtest/resolve/{run_id}")
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


@app.post("/embeddings/search")
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


@app.get("/health")
async def health_check():
    """
    Comprehensive health check — Layer 2.
    Returns all subsystem statuses, circuit breaker states, error rates,
    and pipeline integrity (is the system producing expected output).
    The sentinel (Layer 3) and watchdog poll this to detect problems.

    A system with broken pipelines should NOT report "ok" — the pipeline
    integrity checker downgrades the healthy flag if critical issues exist.
    """
    # Track watchdog/sentinel pings for self-monitoring
    import time as _time
    if not hasattr(app.state, "_last_health_ping"):
        app.state._last_health_ping = _time.time()
        app.state._health_ping_count = 0
    app.state._last_health_ping = _time.time()
    app.state._health_ping_count += 1

    if not system_health:
        return {"healthy": False, "error": "Health monitor not initialized"}
    report = system_health.get_full_report()

    # Pipeline integrity — use cached results from the last run (fast)
    try:
        checker = get_integrity_checker()
        integrity = checker.get_latest_report()
        report["pipeline_integrity"] = integrity
        # Degrade overall healthy flag if pipeline has critical issues
        if not integrity.get("healthy", True):
            report["healthy"] = False
            report["pipeline_broken"] = True
    except Exception as e:
        logger.error(f"Pipeline integrity report failed: {e}", exc_info=True)
        report["pipeline_integrity"] = {
            "status": "error",
            "error": f"integrity check failed: {e}",
        }

    # Watchdog self-monitoring: if no one has pinged us in 5 min, that's a warning
    import time as _time
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

    # WriteCoordinator stats — exposed on /health for at-a-glance lock visibility.
    try:
        from tools.db_writer import all_stats as _writer_stats
        report["write_coordinators"] = _writer_stats()
    except Exception:
        report["write_coordinators"] = []

    # Write health file for sentinel to read if HTTP is down
    system_health.write_health_file()
    return report


@app.get("/admin/writer", dependencies=[Depends(require_admin)])
async def writer_stats():
    """Per-DB WriteCoordinator stats: queue depth, throughput, slowest op."""
    from tools.db_writer import all_stats as _writer_stats
    return {"coordinators": _writer_stats()}


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
# Task listing & context sync
# ---------------------------------------------------------------------------

@app.get("/tasks")
async def list_tasks(status: Optional[str] = None, limit: int = 10):
    """List recent tasks from the queue."""
    # Refresh WAL snapshot to see externally-committed rows
    try:
        await queue._db.commit()
    except Exception:
        pass
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
async def admin_restart(confirm: str = "", _auth: None = Depends(require_admin)):
    """Graceful restart — exits process, watchdog brings it back with new code.

    Requires confirm=YES to prevent accidental restarts.
    Without watchdog.bat running, this will KILL the system with no relaunch.
    """
    # SECURITY: timing-safe equality (audit C-2). Token is "YES" — short, but pattern is
    # what matters: never use `==` or `!=` on auth-adjacent strings.
    if not _secrets.compare_digest(confirm, "YES"):
        return {"error": "Add ?confirm=YES to actually restart. WARNING: without watchdog, system will not relaunch."}
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
        return {
            "rss_mb": round(rss_mb, 1),
            "error": "tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable",
        }

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
        return {"error": "tracemalloc not active — set CALLISTO_TRACEMALLOC=1 and restart to enable"}

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


@app.post("/admin/sql")
async def admin_sql(request: Request, _auth: None = Depends(require_admin)):
    """Read-only SQL query against callisto.db for debugging.

    Only SELECT statements allowed. Useful for ad-hoc diagnostics
    without needing a separate sqlite3 client.
    """
    body = await request.json()
    sql = body.get("sql", "").strip()

    if not sql:
        return {"error": "No SQL provided"}

    # Safety: only allow SELECT statements
    # Strip SQL comments first to prevent bypass via -- or /* */
    import re as _re
    cleaned = _re.sub(r'--[^\n]*', '', sql)  # Remove single-line comments
    cleaned = _re.sub(r'/\*.*?\*/', '', cleaned, flags=_re.DOTALL)  # Remove block comments
    normalized = cleaned.upper().strip()

    if not normalized.startswith("SELECT") and not normalized.startswith("PRAGMA"):
        return {"error": "Only SELECT and PRAGMA statements allowed"}

    # Block multi-statement queries (semicolons followed by more SQL)
    if ";" in cleaned.strip().rstrip(";"):
        return {"error": "Multi-statement queries not allowed"}

    # Block dangerous patterns (word-boundary to avoid false positives like CREATED_AT).
    # Also block ATTACH (cross-DB write/read) and EXPLAIN (can hide other operations downstream).
    for forbidden in ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "ATTACH", "DETACH", "REINDEX", "VACUUM", "REPLACE"):
        if _re.search(rf'\b{forbidden}\b', normalized):
            return {"error": f"Forbidden keyword: {forbidden}"}
    # SECURITY (audit C-2): defeat unicode-escape regex bypass by re-validating raw string
    # against the same forbidden list after upper-casing the original (no comment-stripping).
    raw_upper = sql.upper()
    for forbidden in ("DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "CREATE", "ATTACH"):
        if _re.search(rf'\b{forbidden}\b', raw_upper):
            return {"error": f"Forbidden keyword: {forbidden}"}

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA query_only = ON")
            cursor = await db.execute(sql)
            rows = await cursor.fetchall()
            cols = [d[0] for d in cursor.description] if cursor.description else []
            return {
                "columns": cols,
                "rows": [list(r) for r in rows[:500]],  # Cap at 500 rows
                "row_count": len(rows),
                "truncated": len(rows) > 500,
            }
    except Exception as e:
        return {"error": str(e)}


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
    """Enable the bet executor — live bets will be placed."""
    ex = await _get_executor()
    ex.enable()
    # Wire into research loop if available
    if hasattr(app.state, "research_loop"):
        app.state.research_loop._bet_executor = ex
    return {"status": "enabled", "message": "Bet executor is now LIVE — bets will be placed automatically"}


@app.post("/executor/disable")
async def executor_disable():
    """Disable the bet executor — no bets will be placed."""
    ex = await _get_executor()
    ex.disable()
    return {"status": "disabled", "message": "Bet executor disabled — no bets will be placed"}


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
