"""Individual subsystem health checks (ollama, sqlite, disk, memory, network,
data collector freshness)."""

import logging
import os
import shutil
import time
from typing import Optional

import aiosqlite
import httpx

from .config import (
    CRITICAL_MULTIPLIER,
    db_path,
    MAX_DB_SIZE_GB,
    MAX_MEMORY_MB,
    MEMORY_GROWTH_MB_PER_HOUR,
    MIN_DISK_GB,
    NETWORK_CACHE_TTL_S,
    NETWORK_ESCALATE_AFTER_S,
    OLLAMA_HOST,
    resolve_sla_seconds,
)

logger = logging.getLogger("callisto.health")


async def check_ollama() -> dict:
    """Check Ollama is running and models are available."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{OLLAMA_HOST}/api/tags")
            resp.raise_for_status()
            data = resp.json()

            models = [m["name"] for m in data.get("models", [])]
            has_embed = any("nomic" in m for m in models)

            # Check running models (are any stuck?)
            try:
                ps_resp = await client.get(f"{OLLAMA_HOST}/api/ps")
                if ps_resp.status_code == 200:
                    ps_data = ps_resp.json()
                    running = ps_data.get("models", [])
                else:
                    running = []
            except Exception:
                running = []

            return {
                "status": "ok",
                "models_available": len(models),
                "has_embedding_model": has_embed,
                "models_running": len(running),
                "model_list": models[:10],
            }
    except httpx.ConnectError:
        return {
            "status": "critical",
            "error": "Ollama not running — cannot connect",
            "action": "restart_ollama",
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_sqlite() -> dict:
    """Check SQLite database health."""
    if not os.path.exists(db_path()):
        return {"status": "error", "error": f"Database not found: {db_path()}"}

    db_size_mb = os.path.getsize(db_path()) / (1024 * 1024)
    db_size_gb = db_size_mb / 1024

    try:
        async with aiosqlite.connect(db_path()) as db:
            # Quick integrity check (fast mode)
            cursor = await db.execute("PRAGMA quick_check(1)")
            result = await cursor.fetchone()
            integrity_ok = result and result[0] == "ok"

            # Check WAL mode
            cursor = await db.execute("PRAGMA journal_mode")
            journal = (await cursor.fetchone())[0]

            # Count key tables
            from tools.db_utils import safe_ident
            counts = {}
            for table in ["hypotheses", "backtest_events", "embeddings",
                          "game_contexts", "game_results", "player_stats"]:
                try:
                    cursor = await db.execute(f"SELECT COUNT(*) FROM {safe_ident(table)}")
                    counts[table] = (await cursor.fetchone())[0]
                except Exception:
                    counts[table] = -1

        status = "ok" if integrity_ok else "critical"
        if db_size_gb > MAX_DB_SIZE_GB:
            status = "warning"

        return {
            "status": status,
            "size_mb": round(db_size_mb, 1),
            "integrity": "ok" if integrity_ok else "FAILED",
            "journal_mode": journal,
            "table_counts": counts,
            "warning": f"DB size {db_size_gb:.1f}GB exceeds {MAX_DB_SIZE_GB}GB threshold"
                if db_size_gb > MAX_DB_SIZE_GB else None,
        }
    except Exception as e:
        return {"status": "critical", "error": f"SQLite error: {e}"}


def check_disk() -> dict:
    """Check available disk space."""
    try:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path())))
        free_gb = usage.free / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
        used_pct = (usage.used / usage.total) * 100

        status = "ok"
        if free_gb < MIN_DISK_GB:
            status = "critical"
        elif free_gb < MIN_DISK_GB * 3:
            status = "warning"

        return {
            "status": status,
            "free_gb": round(free_gb, 1),
            "total_gb": round(total_gb, 1),
            "used_pct": round(used_pct, 1),
            "warning": f"Only {free_gb:.1f}GB free" if status != "ok" else None,
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}


class MemoryLeakDetector:
    """Tracks RSS samples over time and detects sustained growth.

    Excludes the first STARTUP_GRACE_SECONDS of samples — startup memory
    allocation (loading models, SQLite, embeddings) skews growth rate
    enormously. A 33-minute window with startup included can report 400+
    MB/hr when the true steady-state rate is <50 MB/hr.
    """

    STARTUP_GRACE_SECONDS = 900  # 15 minutes — loading 500MB DB + scipy takes time

    def __init__(self):
        self._samples: list[tuple[float, float]] = []  # (timestamp, rss_mb)

    def record(self, rss_mb: float) -> None:
        now = time.monotonic()
        self._samples.append((now, rss_mb))
        # Keep last 2 hours of samples
        cutoff = now - 7200
        self._samples = [(t, m) for t, m in self._samples if t > cutoff]

    def estimate(self) -> tuple[bool, float]:
        """Return (leak_detected, growth_rate_mb_per_hour)."""
        leak_detected = False
        growth_rate = 0.0
        if len(self._samples) >= 10:
            process_start = self._samples[0][0]
            stable_samples = [
                (t, m) for t, m in self._samples
                if t - process_start >= self.STARTUP_GRACE_SECONDS
            ]
            if len(stable_samples) >= 5:
                first_time, first_mem = stable_samples[0]
                last_time, last_mem = stable_samples[-1]
                elapsed_hours = (last_time - first_time) / 3600
                if elapsed_hours > 0.1:
                    growth_rate = (last_mem - first_mem) / elapsed_hours
                    leak_detected = growth_rate > MEMORY_GROWTH_MB_PER_HOUR
        return leak_detected, growth_rate


_memory_detector = MemoryLeakDetector()


def check_memory(detector: Optional[MemoryLeakDetector] = None) -> dict:
    """Check process memory usage and detect leaks."""
    detector = detector or _memory_detector
    try:
        import psutil
        process = psutil.Process()
        mem = process.memory_info()
        rss_mb = mem.rss / (1024 * 1024)

        # Track for leak detection
        detector.record(rss_mb)
        leak_detected, growth_rate = detector.estimate()

        status = "ok"
        if rss_mb > MAX_MEMORY_MB:
            status = "critical"
        elif leak_detected:
            status = "warning"

        return {
            "status": status,
            "rss_mb": round(rss_mb, 1),
            "growth_rate_mb_per_hour": round(growth_rate, 1),
            "leak_suspected": leak_detected,
            "samples": len(detector._samples),
            "warning": (
                f"Memory leak suspected: {growth_rate:.0f}MB/hr"
                if leak_detected else
                f"RSS {rss_mb:.0f}MB exceeds {MAX_MEMORY_MB}MB"
                if rss_mb > MAX_MEMORY_MB else None
            ),
        }
    except ImportError:
        # psutil not installed — skip memory check
        return {"status": "ok", "note": "psutil not installed, memory check skipped"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


async def check_network(
    cache_state: Optional[dict] = None,
) -> dict:
    """Check network connectivity to key services.

    Caches results for NETWORK_CACHE_TTL_S (5 min) to avoid two live HTTP
    requests every 2 minutes. Transient Wi-Fi flakiness demotes to
    "warning" rather than "error" — only escalates to real failure if the
    outage persists past NETWORK_ESCALATE_AFTER_S (10 min).

    ``cache_state`` carries the escalation state between calls:
      {"cache": dict|None, "cache_ts": float, "first_failure_ts": float|None}
    It is mutated in place so callers keep their own state.
    """
    state = cache_state if cache_state is not None else {
        "cache": None, "cache_ts": 0.0, "first_failure_ts": None,
    }
    now = time.monotonic()
    if (
        state["cache"] is not None
        and (now - state["cache_ts"]) < NETWORK_CACHE_TTL_S
    ):
        cached = dict(state["cache"])
        cached["cached"] = True
        cached["cache_age_s"] = round(now - state["cache_ts"], 1)
        return cached

    results = {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # ESPN
            try:
                r = await client.get(
                    "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
                    params={"limit": 1},
                )
                results["espn"] = "ok" if r.status_code == 200 else f"HTTP {r.status_code}"
            except Exception as e:
                results["espn"] = f"error: {e}"

            # Odds API (check odds-api.io connectivity, don't waste credits)
            try:
                r = await client.get(
                    "https://api.odds-api.io/v3/events",
                    params={"sport": "basketball", "league": "usa-nba", "apiKey": "test"},
                )
                # 401/403 = reachable but bad key, which is fine for health check
                results["odds_api"] = "ok" if r.status_code in (200, 401, 403) else f"HTTP {r.status_code}"
            except Exception as e:
                results["odds_api"] = f"error: {e}"

    except Exception as e:
        # Transient — classify as warning unless persistent.
        if state["first_failure_ts"] is None:
            state["first_failure_ts"] = now
        elapsed = now - state["first_failure_ts"]
        status = "critical" if elapsed > NETWORK_ESCALATE_AFTER_S else "warning"
        out = {
            "status": status,
            "error": f"Network check failed: {e}",
            "failing_for_seconds": round(elapsed, 1),
        }
        state["cache"] = out
        state["cache_ts"] = now
        return out

    all_ok = all(v == "ok" for v in results.values())
    if all_ok:
        state["first_failure_ts"] = None
        out = {"status": "ok", "services": results, "cached": False}
    else:
        if state["first_failure_ts"] is None:
            state["first_failure_ts"] = now
        elapsed = now - state["first_failure_ts"]
        # Demote to warning first; only escalate if the outage persists.
        status = "critical" if elapsed > NETWORK_ESCALATE_AFTER_S else "warning"
        out = {
            "status": status,
            "services": results,
            "failing_for_seconds": round(elapsed, 1),
            "cached": False,
        }
    state["cache"] = out
    state["cache_ts"] = now
    return out


async def check_data_collector() -> dict:
    """
    Check data collector freshness via the `ingestion_runs` ledger.

    WHY THIS EXISTS
    ---------------
    SUBSYSTEMS has listed "data_collector" since day one but the probe was
    never implemented — meaning the breaker could never trip even when
    ESPN / NHL / nflverse had been silently failing for hours. The audit
    classified this as a P1 silent-failure class because empty returns
    look identical to "no games today" in every downstream consumer.

    WHAT THIS DOES
    --------------
    For every source we have ever written a tracking row for, find the
    most-recent FINISHED run (not 'running' — those could be hung). Call
    a source stale if:
      • last_success_age > SLA (warning)
      • last_success_age > SLA * CRITICAL_MULTIPLIER (critical)

    Rate-limited runs count as semi-stale — they show the source CAN be
    reached but is being throttled; we surface them distinctly rather
    than treat them as successes.

    Returns a dict shaped for the standard check-result contract:
    status = 'ok' | 'warning' | 'critical' | 'error'
    plus diagnostic fields for /health.
    """
    if not os.path.exists(db_path()):
        return {"status": "error", "error": f"DB not found: {db_path()}"}

    try:
        async with aiosqlite.connect(db_path()) as db:
            await db.execute("PRAGMA busy_timeout = 10000")
            # Confirm table exists — if this is a fresh DB that hasn't
            # run ensure_schema() yet, warn instead of crashing.
            cursor = await db.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='ingestion_runs'"
            )
            if not await cursor.fetchone():
                return {
                    "status": "warning",
                    "message": "ingestion_runs table does not exist yet — migration pending",
                }

            # Get most-recent finished run per source.
            cursor = await db.execute(
                "SELECT source, status, finished_at, "
                "  (julianday('now') - julianday(finished_at)) * 86400 AS age_s "
                "FROM ingestion_runs "
                "WHERE finished_at IS NOT NULL "
                "  AND id IN ("
                "    SELECT MAX(id) FROM ingestion_runs "
                "    WHERE finished_at IS NOT NULL "
                "    GROUP BY source"
                "  )"
            )
            rows = await cursor.fetchall()
    except Exception as e:
        return {"status": "error", "error": f"ingestion_runs query failed: {e}"}

    if not rows:
        return {
            "status": "warning",
            "message": "No ingestion runs recorded yet — either Callisto just started or the decorator isn't being hit",
            "sources": 0,
        }

    stale_warn: list[dict] = []
    stale_critical: list[dict] = []
    rate_limited: list[dict] = []
    healthy = 0
    total_sources = 0

    for source, last_status, finished_at, age_s in rows:
        total_sources += 1
        sla = resolve_sla_seconds(source)
        age = float(age_s) if age_s is not None else 0.0

        entry = {
            "source": source,
            "last_status": last_status,
            "last_finished_at": finished_at,
            "age_seconds": round(age, 0),
            "sla_seconds": sla,
        }

        if last_status == "rate_limited":
            rate_limited.append(entry)
            continue

        if age > sla * CRITICAL_MULTIPLIER:
            stale_critical.append(entry)
        elif age > sla:
            stale_warn.append(entry)
        elif last_status in ("ok", "partial"):
            healthy += 1
        else:
            # Most recent terminal run was 'failed' but within SLA window
            # (retry may still land). Surface as warning.
            stale_warn.append({**entry, "note": "last run failed, within SLA window"})

    if stale_critical:
        status = "critical"
    elif stale_warn or rate_limited:
        status = "warning"
    else:
        status = "ok"

    return {
        "status": status,
        "sources_total": total_sources,
        "healthy": healthy,
        "stale_warn": stale_warn[:20],
        "stale_critical": stale_critical[:20],
        "rate_limited": rate_limited[:10],
        "error": (
            f"{len(stale_critical)} source(s) past 3x SLA: "
            + ", ".join(e["source"] for e in stale_critical[:5])
            if stale_critical else None
        ),
    }
