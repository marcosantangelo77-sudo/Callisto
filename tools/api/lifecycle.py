"""Lifespan startup phases for api.py (moved from api.py, slice 6).

The ~370-line ``lifespan`` coroutine in api.py is decomposed into ordered
startup phases that live here; api.py's ``lifespan`` keeps the overall
ordering, the ``yield``, and the FULL shutdown sequence inline (pinned by
tests/test_event_bus_lifecycle.py shutdown-ordering source invariants and
tests/test_ws_single_owner.py "Sole owner" comment pin).

Phase order (must not change — see tests/test_api_slice6.py):
  1. startup_tracemalloc        (optional memory profiling)
  2. startup_write_coordinator  (single-writer DB routing)
  3. startup_migrations         (schema + followup columns + versioned migrations)
  4. startup_model_warmup       (VRAM preload)
  5. startup_correlation_store  (learned correlations)
  6. startup_core_services      (memory/queue/orchestrator/monitor)
  7. startup_line_monitor       (sole odds-WebSocket owner)
  8. startup_research_stack     (CLV, orders, hypothesis/backtest/vector/research)
  9. startup_watchdogs          (integrity + system health + heartbeat)
  10. startup_sidecars          (telegram, game scheduler, event-bus drain, live state)

Each phase returns whatever api.py needs to keep in its module globals.
All heavy imports stay lazy inside the phase bodies so importing
tools.api.lifecycle stays cheap and test-friendly.

No phase ever arms the bet executor, touches paper-trade statuses, or
starts a second odds stream (LineMonitor is the sole owner).
"""

from __future__ import annotations

import asyncio
import logging
import os
import tracemalloc

logger = logging.getLogger("callisto.api")


# ---------------------------------------------------------------------------
# Phase 1 — optional tracemalloc
# ---------------------------------------------------------------------------

def startup_tracemalloc() -> bool:
    """Start tracemalloc only when explicitly requested.

    tracemalloc tracks every allocation in C-level metadata (~50-100 bytes
    each), which adds 55-110 MB of invisible overhead from the JSON decoder
    alone (1.1M allocations) plus severe fragmentation. Returns whether it
    was started.
    """
    if os.environ.get("CALLISTO_TRACEMALLOC") == "1":
        tracemalloc.start(3)
        logger.info("tracemalloc started with 3-frame depth (CALLISTO_TRACEMALLOC=1)")
        return True
    logger.info("tracemalloc disabled (set CALLISTO_TRACEMALLOC=1 to enable)")
    return False


# ---------------------------------------------------------------------------
# Phase 2 — single-writer coordinator
# ---------------------------------------------------------------------------

async def startup_write_coordinator(db_path: str):
    """Install process-wide aiosqlite routing and start the WriteCoordinator.

    Single-writer coordinator (root-cause fix for "database is locked").
    install_aiosqlite_routing() patches aiosqlite.Connection so EVERY
    write — including from modules that use raw db.execute() instead of
    our retry helpers — routes through the coordinator transparently.
    MUST run before ensure_schema and any other connection so the patched
    aiosqlite is in effect for the rest of the process lifetime.
    """
    from tools.db_writer import (
        install_aiosqlite_routing as _install_routing,
        get_writer as _get_writer,
    )
    _install_routing()
    writer = await _get_writer(db_path)
    logger.info(f"WriteCoordinator active for {db_path} (process-wide routing installed)")
    return writer


# ---------------------------------------------------------------------------
# Phase 3 — schema + migrations
# ---------------------------------------------------------------------------

async def startup_migrations(db_path: str) -> None:
    """Ensure schema, followup-hardening columns, then versioned migrations."""
    from tools.schema import ensure_schema

    # Startup — ensure DB schema is up to date (now uses patched aiosqlite).
    await ensure_schema()

    # Followup hardening columns (feat/auto-followup-hardening).
    # Adds followup_depth / parent_task_id / root_task_id / cost_usd to
    # task_queue so _maybe_auto_followup can enforce depth + budget caps
    # and /task/{id}/chain can walk the ancestry tree.
    import aiosqlite
    try:
        from tools.followup_guard import ensure_followup_columns
        async with aiosqlite.connect(db_path) as _fg_db:
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
        mig_result = apply_pending_migrations(db_path)
        logger.info(f"Migrations: {mig_result}")
    except Exception as e:
        logger.error(f"Migration framework failed: {e!r}")
        raise


# ---------------------------------------------------------------------------
# Phase 4 — model warmup
# ---------------------------------------------------------------------------

async def startup_model_warmup() -> None:
    """Preload priority models into VRAM (devstral-small-2 takes 28s cold, <1s warm)."""
    from inference import warmup_models
    await warmup_models()


# ---------------------------------------------------------------------------
# Phase 5 — learned correlations
# ---------------------------------------------------------------------------

async def startup_correlation_store():
    """Bayesian blend of hardcoded priors + empirical data.

    Returns the initialised LearnedCorrelationStore for api.py's global.
    """
    from tools.learned_correlations import LearnedCorrelationStore
    from tools.correlation import set_learned_store, SPORT_CORRELATIONS

    store = LearnedCorrelationStore()
    await store.initialize()
    await store.seed_from_priors(SPORT_CORRELATIONS)
    set_learned_store(store)
    return store


# ---------------------------------------------------------------------------
# Phase 6 — core services
# ---------------------------------------------------------------------------

async def startup_core_services():
    """MemoryStore + TaskQueue + Orchestrator + HealthMonitor.

    Returns (memory, queue, orchestrator, monitor).
    """
    from memory import MemoryStore
    from monitor import HealthMonitor
    from orchestrator import Orchestrator
    from task_queue import TaskQueue

    memory = MemoryStore()
    await memory.initialize()

    queue = TaskQueue()
    await queue.initialize()

    orchestrator = Orchestrator(memory)
    monitor = HealthMonitor()
    await monitor.start()
    return memory, queue, orchestrator, monitor


# ---------------------------------------------------------------------------
# Phase 7 — line monitor (sole odds WebSocket owner)
# ---------------------------------------------------------------------------

async def startup_line_monitor():
    """Start the line movement monitor.

    Sole owner of the application-lifespan odds WebSocket: the provider
    allows one connection per API key, so nothing else may start
    start_odds_stream() or a competing OddsWebSocket here.
    """
    from tools.line_monitor import LineMonitor

    line_monitor = LineMonitor()
    await line_monitor.initialize()
    await line_monitor.start()
    return line_monitor


# ---------------------------------------------------------------------------
# Phase 8 — research stack
# ---------------------------------------------------------------------------

async def startup_research_stack(orchestrator, line_monitor):
    """CLV tracker, order manager, hypothesis/backtest stack, research loop.

    Returns a dict with every component api.py stores in its module
    globals:
      clv_tracker, order_manager, hypothesis_manager, historical_fetcher,
      backtest_engine, vector_store, hypothesis_generator, data_collector,
      research_loop
    """
    from agp import Domain  # noqa: F401  (kept: parity with historical imports)
    from tools.clv_tracker import CLVTracker
    from tools.order_manager import get_manager as _get_order_manager
    from tools.hypothesis import HypothesisManager
    from tools.historical_odds import HistoricalOddsFetcher
    from tools.backtest import BacktestEngine
    from tools.embeddings import VectorStore
    from tools.hypothesis_generator import HypothesisGenerator
    from tools.data_collector import DataCollector
    from tools.autonomous import ResearchLoop

    # CLV tracker — bet tracking and closing line value measurement
    clv_tracker = CLVTracker()
    await clv_tracker.initialize()

    # Order management — Telegram-approved manual placement (supersedes
    # the Playwright executor when CALLISTO_USE_ORDER_MANAGER=1, default).
    order_manager = await _get_order_manager()

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

    # Research loop — 24/7 hypothesis machine
    research_loop = ResearchLoop(
        hypothesis_manager=hypothesis_manager,
        hypothesis_generator=hypothesis_generator,
        backtest_engine=backtest_engine,
        data_collector=data_collector,
        vector_store=vector_store,
        orchestrator=orchestrator,
        line_monitor=line_monitor,
    )
    await research_loop.start()

    return {
        "clv_tracker": clv_tracker,
        "order_manager": order_manager,
        "hypothesis_manager": hypothesis_manager,
        "historical_fetcher": historical_fetcher,
        "backtest_engine": backtest_engine,
        "vector_store": vector_store,
        "hypothesis_generator": hypothesis_generator,
        "data_collector": data_collector,
        "research_loop": research_loop,
    }


# ---------------------------------------------------------------------------
# Phase 9 — watchdogs
# ---------------------------------------------------------------------------

async def startup_watchdogs(research_loop):
    """Pipeline integrity checker + SystemHealth monitor + Heartbeat.

    Returns (system_health, heartbeat).
    """
    from tools.health import SystemHealth
    from tools.pipeline_integrity import initialize as init_integrity
    from tools.self_repair import Heartbeat

    # Pipeline integrity checker — detects silent failures
    await init_integrity()

    # System health monitor — Layer 2 resilience
    system_health = SystemHealth()
    system_health.research_loop = research_loop
    await system_health.start()

    # Heartbeat — independent watchdog for loop stalls and Claude availability
    heartbeat = Heartbeat()
    await heartbeat.start()
    return system_health, heartbeat


# ---------------------------------------------------------------------------
# Phase 10 — sidecars
# ---------------------------------------------------------------------------

async def startup_telegram_listener(orchestrator, line_monitor, clv_tracker):
    """Bidirectional Telegram listener (communication from phone)."""
    from tools.telegram import TelegramListener

    listener = TelegramListener(
        orchestrator=orchestrator,
        line_monitor=line_monitor,
        clv_tracker=clv_tracker,
    )
    await listener.start()
    return listener


async def startup_game_scheduler(app):
    """Game scheduler — fires events at T-60min and T-15min before games.

    Failure is non-fatal: logs a warning and returns None.
    """
    try:
        from tools.game_scheduler import GameScheduler
        from tools.event_bus import get_event_bus
        game_scheduler = GameScheduler(event_bus=get_event_bus())
        await game_scheduler.start()
        app.state.game_scheduler = game_scheduler
        logger.info(f"Game scheduler started — {len(game_scheduler._games)} upcoming games")
        return game_scheduler
    except Exception as e:
        logger.warning(f"Game scheduler failed to start: {e}")
        return None


async def startup_event_bus_drain() -> None:
    """Persist important event-bus events to SQLite. Non-fatal on failure."""
    try:
        from tools.event_bus import get_event_bus
        bus = get_event_bus()
        await bus.start_audit_drain()
        logger.info("Event bus audit drain started")
    except Exception as e:
        logger.warning(f"Event bus audit drain failed: {e}")


async def startup_live_state_collector(db_path: str):
    """Live in-game state collector (ESPN poller).

    Env-gated (default ON) and wrapped in try/except so a failure here can
    NEVER break the rest of startup. If the DB migration hasn't been
    applied yet, the collector self-disables and logs.

    Returns (collector_or_None, task_or_None).
    """
    collector = None
    task = None
    if os.environ.get("CALLISTO_LIVE_STATE_ENABLED", "1") == "1":
        try:
            from tools.live_state import start_collector as _start_live_collector
            collector = await _start_live_collector(db_path=db_path)
            if collector is not None:
                # start_collector already called create_task inside the
                # collector; we don't need a second task. Expose the
                # module's task via the collector so shutdown can find it.
                task = collector._task
                logger.info(
                    "Live state collector started "
                    f"(sports={list(collector.sports)}, interval=30s)"
                )
            else:
                logger.warning(
                    "Live state collector not started — table missing or disabled"
                )
        except Exception as e:
            logger.warning(f"Live state collector failed to start: {e}")
            collector = None
            task = None
    else:
        logger.info("Live state collector disabled via CALLISTO_LIVE_STATE_ENABLED=0")
    return collector, task


def spawn_background_tasks():
    """Create the application-lifespan asyncio tasks after startup completes.

    Returns a dict of tasks: worker, wal_checkpoint, restart_signal,
    sla_watchdog, order_cron, prop_resolver (prop_resolver may be None if
    its module fails to import — non-fatal by design).
    """
    from tools.api.workers import (
        ingestion_sla_watchdog_loop,
        order_cron_loop,
        restart_signal_watcher,
        task_worker,
        wal_checkpoint_loop,
    )

    tasks = {
        "worker": asyncio.create_task(task_worker()),
        "wal_checkpoint": asyncio.create_task(wal_checkpoint_loop()),
        # Signal-file consumer — decouples restart from watchdog liveness.
        "restart_signal": asyncio.create_task(restart_signal_watcher()),
        "sla_watchdog": asyncio.create_task(ingestion_sla_watchdog_loop()),
        "order_cron": asyncio.create_task(order_cron_loop()),
        "prop_resolver": None,
    }
    # Prop resolution — fills backtest_events.actual_result for player_* markets.
    # Without this, every prop hypothesis stats at 0 resolved (silent freeze).
    try:
        from tools.prop_resolver import prop_resolution_loop
        tasks["prop_resolver"] = asyncio.create_task(prop_resolution_loop())
        logger.info("prop_resolution_loop started (15m interval, 500 rows/pass)")
    except Exception as e:
        logger.warning(f"prop_resolution_loop failed to start: {e}")
    return tasks


async def announce_startup(port: int, line_monitor) -> None:
    """Telegram announcement that the API is up."""
    from tools import telegram

    sports = (await line_monitor.get_status()).get("monitored_sports", [])
    await telegram.alert_system(
        f"API started on port {port}\n"
        f"Monitoring: {', '.join(sports)}\n"
        f"Odds-API.io Pro: 15 books, 30K req/hr + WebSocket\n"
        f"Autonomous reasoning: ACTIVE\n"
        f"Research loop: ACTIVE (24/7 hypothesis machine)"
    )


# ---------------------------------------------------------------------------
# Shutdown helpers (api.py calls these from its inline shutdown sequence;
# the ordering contract stays pinned in api.py itself).
# ---------------------------------------------------------------------------

async def stop_live_state_collector() -> None:
    """Cancel the live state collector so the HTTP client closes cleanly."""
    try:
        from tools.live_state import stop_collector as _stop_live_collector
        await _stop_live_collector()
    except Exception as e:
        logger.debug(f"Live state collector shutdown failed: {e}")


async def close_http_clients() -> None:
    """Close every shared outbound HTTP client (shutdown tail)."""
    from tools.search import close_all_clients
    await close_all_clients()
    from tools.odds_api import close_client as close_odds_client
    await close_odds_client()
    from tools.contextual_data import close_client as close_ctx_client
    await close_ctx_client()
    from tools.embeddings import close_client as close_embed_client
    await close_embed_client()
    from tools.data_collector import close_client as close_dc_client
    await close_dc_client()
    from tools.dk_scraper import close_client as close_dk_client
    await close_dk_client()
