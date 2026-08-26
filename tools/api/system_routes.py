"""System/health-status route handler bodies (moved from api.py).

The FastAPI decorators and Depends(...) remain in api.py; these are the
implementation functions that the thin wrappers there call.

CRITICAL GATING CONTRACT (pinned by tests/test_api_slice3.py):
  * /health, /health/livez, /health/readyz stay PUBLIC — no admin dep.
  * /health/detailed, /health/deep stay require_admin_or_loopback gated.

Handlers access api.py's module-level singletons (``system_health``,
``app``, ``queue``, ``autonomous``, ``research_loop``, ``line_monitor``,
``hypothesis_manager``, ``vector_store``, ``data_collector``, ``DB_PATH``,
``logger``) via late ``from api import ...`` inside the function body to
avoid a circular import at module load time.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Health signal evaluation (pure function — no api.py imports needed)
# ---------------------------------------------------------------------------

def evaluate_health_signals(report: dict) -> tuple[bool, str, list[str]]:
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


async def build_health_report() -> dict:
    """Assemble the full /health payload. Shared by /health and /readyz."""
    import time as _time
    import logging
    from tools.pipeline_integrity import get_checker as get_integrity_checker

    logger = logging.getLogger("callisto.api")
    from api import app, system_health, queue

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

    # Now evaluate demotion signals and stamp reasons
    healthy, severity, reasons = evaluate_health_signals(report)
    # Only downgrade — the subsystem loop already sets healthy=False on breakers.
    if not healthy:
        report["healthy"] = False
    report["severity"] = severity if not healthy else "ok"
    report["reasons"] = reasons
    return report


# ---------------------------------------------------------------------------
# Public health trio bodies
# ---------------------------------------------------------------------------

_HEALTH_FILE_DEBOUNCE_SECONDS = 10.0
_health_file_last_write_ts = 0.0


async def health_check():
    """Build the full /health report (no side effects).

    api.py owns the public route and adds the debounced sentinel
    health-file write on top of this builder.
    """
    return await build_health_report()


async def health_livez():
    """k8s-style liveness: process is up and responsive.
    Always 200 unless the event loop is deadlocked (in which case this
    handler wouldn't respond at all)."""
    import time as _time
    return {"alive": True, "ts": _time.time()}


async def health_readyz():
    """k8s-style readiness: ready to serve traffic.
    Returns 503 if any demotion condition is met."""
    report = await build_health_report()
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


# ---------------------------------------------------------------------------
# Gated health/status bodies
# ---------------------------------------------------------------------------

async def health_detailed():
    """
    Everything /health returns, plus per-source ingestion SLAs and
    per-subsystem trip history. For external observability tools.
    """
    report = await build_health_report()

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
                r = await asyncio.to_thread(detect_regime, sp)
                multiplier = await asyncio.to_thread(current_regime_multiplier, sp)
                safe = await asyncio.to_thread(regime_safe_for_trading, sp)
                per_sport[sp] = {
                    "multiplier": multiplier,
                    "safe_for_trading": safe,
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
            r = await asyncio.to_thread(detect_regime, sp)
            raw = float(await asyncio.to_thread(current_regime_multiplier, sp))
            applied = float(await asyncio.to_thread(_clamped_regime_multiplier, sp))
            safe = await asyncio.to_thread(regime_safe_for_trading, sp)
            out[sp] = {
                "raw_multiplier": round(raw, 3),
                "applied_multiplier": round(applied, 3),
                "safe_for_trading": safe,
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


async def writer_stats():
    """Per-DB WriteCoordinator stats: queue depth, throughput, slowest op."""
    from tools.db_writer import all_stats as _writer_stats
    return {"coordinators": _writer_stats()}


async def health_deep():
    """
    Full pipeline integrity suite — runs ALL checks on demand.
    Slower than /health (queries multiple tables). Use this for
    debugging pipeline issues, not for polling.

    Returns: complete integrity check results + subsystem health.
    """
    import logging
    from tools.pipeline_integrity import get_checker as get_integrity_checker

    logger = logging.getLogger("callisto.api")
    from api import system_health

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


async def integrity_history(limit: int = 50):
    """Get recent pipeline integrity check history."""
    import logging
    from tools.pipeline_integrity import get_checker as get_integrity_checker

    logger = logging.getLogger("callisto.api")
    try:
        checker = get_integrity_checker()
        history = await checker.get_history(limit=limit)
        return {"count": len(history), "checks": history}
    except Exception as e:
        logger.error(f"Integrity history fetch failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


async def claude_status():
    """Get Claude Code availability and usage stats."""
    from tools.claude_code import get_usage_stats
    return get_usage_stats()


async def reset_claude_rate_limit():
    """Force-reset Claude Code rate limit state after hourly limit resets."""
    from tools.claude_code import reset_rate_limit
    return reset_rate_limit()


async def full_system_status():
    """
    Single endpoint for checking everything from your phone.
    Returns all subsystem statuses in one call.
    Pipeline integrity is front-and-center so DEGRADED/BROKEN status
    is immediately visible in every Claude Code session start.
    """
    import datetime as _dt
    import logging
    from tools.claude_code import get_usage_stats as claude_stats
    from tools.pipeline_integrity import get_checker as get_integrity_checker

    logger = logging.getLogger("callisto.api")
    from api import (
        DB_PATH,
        autonomous,
        data_collector,
        hypothesis_manager,
        line_monitor,
        research_loop,
        system_health,
        vector_store,
    )

    status = {
        "timestamp": _dt.datetime.now(_dt.timezone.utc).isoformat(),
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
