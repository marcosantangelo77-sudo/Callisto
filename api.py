"""
FastAPI REST layer for Callisto.

Endpoints for task submission, session retrieval, world queries, and health checks.
Runs on port 8420.

Slice 6 split: auth/security primitives live in tools/api/security.py,
global exception handlers in tools/api/errors.py, lifespan startup phases
in tools/api/lifecycle.py, and the __main__ serve loop in
tools/api/serve.py. api.py keeps the FastAPI app object, the route
decorators with their original Depends(...) gating, the full shutdown
sequence (ordering pinned by tests), and backward-compat aliases.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime  # noqa: F401  (kept: parity with historical imports)
from typing import Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from agp import AGPSealTampered, Domain  # noqa: F401  (kept: AGP seal surface)
from logging_config import setup_logging

load_dotenv()

from tools.api import boost_routes as _boost_routes  # noqa: E402
from tools.api import task_routes as _task_routes  # noqa: E402
from tools.api import security as _security  # noqa: E402
from tools.api import lifecycle as _lifecycle  # noqa: E402
from tools.api import errors as _errors  # noqa: E402
from tools.api import serve as _serve  # noqa: E402

from tools.api.workers import (  # noqa: E402
    _is_internal_query,
    _maybe_auto_followup,
    wal_checkpoint_loop,
    restart_signal_watcher,
    ingestion_sla_watchdog_loop,
    _sla_alerted_sources,
    INGESTION_SLA_CHECK_INTERVAL_S,
    TASK_WORKER_TIMEOUT_S,
    _run_session_with_adaptive_timeout,
    _AdaptiveTimeout,
    task_worker,
    order_cron_loop,
)

# Backward-compat aliases for the auth helpers moved to tools/api/security.py
# (slice 6). Tests and the mini-app replay in tests/test_api_auth.py poke
# these on the api module directly.
_client_is_loopback = _security.client_is_loopback
_log_auth_denied = _security.log_auth_denied

setup_logging()
logger = logging.getLogger("callisto.api")

# Adaptive-extension knobs for the adaptive-timeout session runner (now in
# tools/api/workers.py). Kept as module globals here so tests can monkeypatch.
_ADAPTIVE_PROGRESS_WINDOW_S = float(os.getenv("CALLISTO_PROGRESS_WINDOW_S", "120"))
_ADAPTIVE_STALL_WINDOW_S = float(os.getenv("CALLISTO_STALL_WINDOW_S", "240"))
_ADAPTIVE_EXTENSION_S = float(os.getenv("CALLISTO_EXTENSION_S", "120"))
_ADAPTIVE_POLL_S = float(os.getenv("CALLISTO_ADAPTIVE_POLL_S", "5"))

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


# ---------------------------------------------------------------------------
# Auth gates — bodies live in tools/api/security.py; these thin wrappers
# remain the FastAPI Depends(...) targets so dependency signatures (and the
# ``api_mod.require_admin`` attribute tests monkeypatch around) are stable.
# ---------------------------------------------------------------------------

async def require_admin(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Hard-gate: require Bearer token. Fails closed if CALLISTO_ADMIN_TOKEN unset."""
    await _security.require_admin(request, credentials)


async def require_admin_or_loopback(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> None:
    """Soft-gate for read endpoints. Allow loopback when token unset; otherwise require token."""
    await _security.require_admin_or_loopback(request, credentials)


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
memory: Optional[object] = None
queue: Optional[object] = None
orchestrator_instance: Optional[object] = None
monitor: Optional[object] = None
line_monitor: Optional[object] = None
live_state_collector = None  # tools.live_state.LiveStateCollector | None
clv_tracker: Optional[object] = None
autonomous: Optional[object] = None
telegram_listener: Optional[object] = None
hypothesis_manager: Optional[object] = None
historical_fetcher: Optional[object] = None
backtest_engine: Optional[object] = None
vector_store: Optional[object] = None
hypothesis_generator: Optional[object] = None
data_collector: Optional[object] = None
research_loop: Optional[object] = None
system_health: Optional[object] = None
learned_correlation_store: Optional[object] = None
worker_task: Optional[asyncio.Task] = None
wal_checkpoint_task: Optional[asyncio.Task] = None
restart_signal_task: Optional[asyncio.Task] = None
order_cron_task: Optional[asyncio.Task] = None
prop_resolver_task: Optional[asyncio.Task] = None
sla_watchdog_task: Optional[asyncio.Task] = None
heartbeat: Optional[object] = None
game_scheduler: Optional[object] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown lifecycle manager.

    Startup is decomposed into ordered phases in tools/api/lifecycle.py;
    this coroutine owns the phase ORDER, the module-global wiring, the
    yield, and the complete inline shutdown sequence whose ordering
    contract is pinned by tests/test_event_bus_lifecycle.py (producers and
    drains stop BEFORE the WriteCoordinator).
    """
    global memory, queue, orchestrator_instance, monitor, line_monitor, clv_tracker, telegram_listener, hypothesis_manager, historical_fetcher, backtest_engine, vector_store, hypothesis_generator, data_collector, research_loop, system_health, learned_correlation_store, worker_task, wal_checkpoint_task, restart_signal_task, order_cron_task, order_manager_instance, live_state_collector, live_state_task, prop_resolver_task, heartbeat, game_scheduler, autonomous, sla_watchdog_task

    # Phase 1 — memory profiling only when explicitly requested.
    _lifecycle.startup_tracemalloc()

    # Phase 2 — single-writer coordinator (root-cause fix for "database is locked").
    await _lifecycle.startup_write_coordinator(DB_PATH)

    # Phase 3 — schema + migrations.
    await _lifecycle.startup_migrations(DB_PATH)

    # Phase 4 — preload priority models into VRAM.
    await _lifecycle.startup_model_warmup()

    # Phase 5 — learned correlations (Bayesian blend of priors + empirical data).
    learned_correlation_store = await _lifecycle.startup_correlation_store()

    # Phase 6 — core services.
    memory, queue, orchestrator_instance, monitor = (
        await _lifecycle.startup_core_services()
    )

    # Line movement monitor — autonomous odds tracking.
    # Sole owner of the application-lifespan odds WebSocket: the provider
    # allows one connection per API key, so nothing else may start
    # start_odds_stream() or a competing OddsWebSocket here.
    line_monitor = await _lifecycle.startup_line_monitor()

    # Phase 8 — CLV tracker + order management + hypothesis/backtest stack +
    # research loop.
    stack = await _lifecycle.startup_research_stack(orchestrator_instance, line_monitor)
    clv_tracker = stack["clv_tracker"]
    order_manager_instance = stack["order_manager"]
    app.state.order_manager = order_manager_instance
    hypothesis_manager = stack["hypothesis_manager"]
    historical_fetcher = stack["historical_fetcher"]
    backtest_engine = stack["backtest_engine"]
    vector_store = stack["vector_store"]
    hypothesis_generator = stack["hypothesis_generator"]
    data_collector = stack["data_collector"]
    research_loop = stack["research_loop"]

    # Phase 9 — pipeline integrity checker + system health monitor + heartbeat.
    system_health, heartbeat = await _lifecycle.startup_watchdogs(research_loop)
    app.state.heartbeat = heartbeat

    # Phase 10a — Telegram listener (bidirectional communication from phone).
    telegram_listener = await _lifecycle.startup_telegram_listener(
        orchestrator_instance, line_monitor, clv_tracker
    )

    # Phase 10b — game scheduler + event bus audit drain (non-fatal).
    game_scheduler = await _lifecycle.startup_game_scheduler(app)
    await _lifecycle.startup_event_bus_drain()

    # Phase 10c — live in-game state collector (env-gated, non-fatal).
    live_state_collector, live_state_task = await _lifecycle.startup_live_state_collector(
        DB_PATH
    )

    # Background workers (moved to tools/api/workers.py in slice 5).
    tasks = _lifecycle.spawn_background_tasks()
    worker_task = tasks["worker"]
    wal_checkpoint_task = tasks["wal_checkpoint"]
    restart_signal_task = tasks["restart_signal"]
    sla_watchdog_task = tasks["sla_watchdog"]
    order_cron_task = tasks["order_cron"]
    prop_resolver_task = tasks["prop_resolver"]
    logger.info(
        f"Callisto API started on port {CALLISTO_PORT} "
        f"(WAL ckpt 5m, restart-signal watcher active, ingestion SLA watchdog 5m, "
        f"prop resolver 15m)"
    )

    # Notify on Telegram
    await _lifecycle.announce_startup(CALLISTO_PORT, line_monitor)

    yield

    # Shutdown
    from tools import telegram

    await telegram.alert_system("Callisto shutting down.", is_error=True)
    await telegram_listener.stop()
    if system_health:
        system_health.write_health_file()
        await system_health.stop()
    if research_loop:
        await research_loop.stop()
    from tools.autonomous import AutonomousLoop  # noqa: F401 — type reference

    if autonomous:
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
    await _lifecycle.stop_live_state_collector()
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
        from tools.event_bus import get_event_bus
        await get_event_bus().stop()
        logger.info("Event bus audit drain stopped")
    except Exception:
        logger.exception("Event bus shutdown error (non-fatal)")
    try:
        if heartbeat:
            await heartbeat.stop()
            hb_task = getattr(heartbeat, "_task", None)
            if hb_task is not None and not hb_task.done():
                try:
                    await hb_task
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
    # Close every shared outbound HTTP client (moved to tools/api/lifecycle.py).
    await _lifecycle.close_http_clients()
    logger.info("Callisto API shut down")


app = FastAPI(
    title="Callisto",
    description="Autonomous multi-agent reasoning system governed by the Aluft Gianne Protocol",
    version="0.1.0",
    lifespan=lifespan,
)


# Global exception handlers — convert unhandled errors into structured JSON
# instead of crashing the request handler. Bodies live in tools/api/errors.py.
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    """Catch any unhandled exception and return a structured JSON error."""
    return await _errors.global_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return clean 422 instead of FastAPI's default verbose error."""
    return await _errors.validation_exception_handler(request, exc)


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
# the floor, never relaxes it. The gate core lives in
# tools/api/security.enforce_default_secure.
# ---------------------------------------------------------------------------

@app.middleware("http")
async def _default_secure_middleware(request: Request, call_next):
    response = await _security.enforce_default_secure(request)
    if response is not None:
        return response
    return await call_next(request)


# Explicit public write allowlist — EVERY entry is deliberate.
# - POST /task: AGP research submission. MCP server + CC sessions use this.
# - POST /context/sync: already hard-gated via require_admin; listed here so
#   the middleware doesn't double-check (the endpoint's own require_admin
#   remains the real gate, stricter than the loopback default).
# Keep this list minimal; prefer moving endpoints off it over adding to it.
public_endpoint("POST", "/task")
public_endpoint("POST", "/context/sync")


class TaskSubmission(_task_routes.TaskSubmission):
    pass


class TaskResponse(_task_routes.TaskResponse):
    pass


@app.post("/task", response_model=TaskResponse)
async def submit_task(
    submission: TaskSubmission,
    _auth: None = Depends(require_admin_or_loopback),
):
    """Submit a query for AGP session processing."""
    return await _task_routes.submit_task(submission)


_wiki_task_short_circuit = _task_routes.wiki_task_short_circuit


@app.get("/task/{task_id}")
async def get_task(task_id: int, _auth: None = Depends(require_admin_or_loopback)):
    """Get task status and result."""
    return await _task_routes.get_task(task_id)


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
    return await _task_routes.get_task_chain(task_id)


@app.get("/session/{session_id}")
async def get_session(session_id: str, _auth: None = Depends(require_admin_or_loopback)):
    """Get a sealed AGP session with full provenance."""
    return await _task_routes.get_session(session_id)


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
    return await _task_routes.query_world(
        domain, keyword=keyword, min_confidence=min_confidence, limit=limit
    )


from tools.api import analysis as _analysis  # noqa: E402
from tools.api import bets as _bets  # noqa: E402
from tools.api import odds_extra as _odds_extra  # noqa: E402
from tools.api import odds_routes as _odds_routes  # noqa: E402
from tools.api import simulate as _simulate  # noqa: E402
from tools.api import wiki as _wiki  # noqa: E402
from tools.api import model_routes as _model_routes  # noqa: E402
from tools.api import data_routes as _data_routes  # noqa: E402
from tools.api import hypothesis_routes as _hypothesis_routes  # noqa: E402
from tools.api import backtest_routes as _backtest_routes  # noqa: E402
from tools.api import research_routes as _research_routes  # noqa: E402
from tools.api import system_routes as _system_routes  # noqa: E402
from tools.api import debug_routes as _debug_routes  # noqa: E402
from tools.api import order_routes as _order_routes  # noqa: E402

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
    return await _simulate.simulate_portfolio_endpoint(
        hypothesis_ids=hypothesis_ids,
        n_sims=n_sims,
        horizon_days=horizon_days,
        starting_bankroll=starting_bankroll,
        kelly_fraction=kelly_fraction,
        all_live=all_live,
    )


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

class FixedBoostRequest(_boost_routes.FixedBoostRequest):
    pass


class PctBoostRequest(_boost_routes.PctBoostRequest):
    pass


class FreeBetRequest(_boost_routes.FreeBetRequest):
    pass


class HedgeRequest(_boost_routes.HedgeRequest):
    pass


class BoostedParlayLeg(_boost_routes.BoostedParlayLeg):
    pass


class BoostedParlayRequest(_boost_routes.BoostedParlayRequest):
    pass


class DevigRequest(_boost_routes.DevigRequest):
    pass


@app.post("/boosts/evaluate-fixed", dependencies=[Depends(require_admin_or_loopback)])
async def eval_fixed_boost(req: FixedBoostRequest):
    """Evaluate a fixed profit boost — devig, compare to fair, calculate edge."""
    return await _boost_routes.eval_fixed_boost(req)


@app.post("/boosts/evaluate-percentage", dependencies=[Depends(require_admin_or_loopback)])
async def eval_pct_boost(req: PctBoostRequest):
    """Evaluate a percentage profit boost token."""
    return await _boost_routes.eval_pct_boost(req)


@app.post("/boosts/evaluate-free-bet", dependencies=[Depends(require_admin_or_loopback)])
async def eval_free_bet(req: FreeBetRequest):
    """Evaluate a free bet or no-sweat bet."""
    return await _boost_routes.eval_free_bet(req)


@app.post("/boosts/hedge", dependencies=[Depends(require_admin_or_loopback)])
async def hedge_calc(req: HedgeRequest):
    """Calculate optimal hedge for guaranteed profit."""
    return await _boost_routes.hedge_calc(req)


@app.post("/boosts/devig", dependencies=[Depends(require_admin_or_loopback)])
async def devig(req: DevigRequest):
    """Devig a two-way market using multiplicative method."""
    return await _boost_routes.devig(req)


@app.post("/boosts/evaluate-parlay", dependencies=[Depends(require_admin_or_loopback)])
async def eval_boosted_parlay(req: BoostedParlayRequest):
    """Evaluate a boosted parlay using correlation-adjusted fair odds."""
    return await _boost_routes.eval_boosted_parlay(req)


# --- Hypothesis Testing & Backtesting ---

# Request schemas for /hypothesis + /backtest moved to tools.api
# (hypothesis_routes.HypothesisCreate, backtest_routes.BacktestRequest);
# re-subclassed below so OpenAPI names do not shift.


class HypothesisCreate(_hypothesis_routes.HypothesisCreate):
    pass


class BacktestRequest(_backtest_routes.BacktestRequest):
    pass


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
    return await _task_routes.list_tasks(status=status, limit=limit)


class ContextSync(_task_routes.ContextSync):
    pass

@app.post("/context/sync")
async def sync_context(ctx: ContextSync, _auth: None = Depends(require_admin)):
    """Receive context from a Claude Code session. Queues actionable items."""
    return await _task_routes.sync_context(ctx)


_restart_task: Optional[asyncio.Task] = None


def _set_restart_task(task: asyncio.Task) -> None:
    """Sink used by _task_routes.admin_restart to register its delayed-exit
    task so the shutdown handler can cancel it cleanly (audit H-14)."""
    global _restart_task
    _restart_task = task


@app.post("/admin/restart")
async def admin_restart(confirm: str = "", _auth: None = Depends(require_admin_or_loopback)):
    """Graceful restart — exits process, watchdog brings it back with new code.

    Requires confirm=YES. Auth: admin-token OR loopback.
    """
    return await _task_routes.admin_restart(
        confirm, set_restart_task=_set_restart_task
    )


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
    _serve.serve(CALLISTO_BIND_HOST, CALLISTO_PORT)
