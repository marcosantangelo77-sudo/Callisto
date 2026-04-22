"""
System health monitor — Layer 2 resilience for autonomous operation.

Runs inside the main api.py process. Checks every subsystem periodically
and takes corrective action without human intervention.

Subsystems monitored:
  - Ollama (models responding, not stuck)
  - SQLite (writable, not corrupt, reasonable size)
  - Disk space (enough room for DB growth)
  - Memory (process RSS, leak detection)
  - Network (ESPN, Odds API, Claude CLI reachable)
  - Research loop (phases completing, not stalled)
  - Error rates (per-subsystem, circuit breakers)

Circuit breakers:
  When a subsystem fails N times consecutively, it is DISABLED.
  Other subsystems continue running. The breaker resets after a
  cooldown period. This prevents cascade failures.

Self-repair:
  When a persistent error is detected and local models can't fix it,
  escalate to Claude Code with structured diagnostics.
"""

import asyncio
import logging
import os
import shutil
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import aiosqlite
import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("callisto.health")

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

# Health check interval
CHECK_INTERVAL = 120  # 2 minutes

# Circuit breaker thresholds
BREAKER_FAIL_THRESHOLD = 5      # Consecutive failures to trip
BREAKER_COOLDOWN = 600           # 10 min cooldown before retry
MAX_ERRORS_PER_HOUR = 50         # Error rate limit per subsystem

# Resource thresholds
MIN_DISK_GB = 2.0                # Alert below 2GB free
MAX_DB_SIZE_GB = 5.0             # Alert above 5GB
MAX_MEMORY_MB = 4096             # Alert above 4GB RSS
MEMORY_GROWTH_MB_PER_HOUR = 100  # Leak detection threshold

# Ollama inference timeout — if a model takes longer than this, it's stuck
OLLAMA_HEALTH_TIMEOUT = 15       # seconds for a simple health-check prompt

# Subsystem names
SUBSYSTEMS = [
    "ollama", "sqlite", "disk", "memory", "network",
    "research_loop", "embedding", "data_collector",
]

# ── Data collector SLA configuration ──
# Per-source maximum age (seconds) between successful ingestion runs before
# the source is considered stale. Two tiers:
#   * WARN tier  — logged; surfaced in /health but does not trip the breaker
#   * CRITICAL tier = 3x warn — trips the circuit breaker
#
# Source tags are hierarchical (`<api>.<resource>.<sport>`) — see
# tools/ingestion_tracking.py. When a source name isn't listed explicitly
# we fall back to SOURCE_SLA_DEFAULTS matched by prefix.
#
# These SLAs are tuned to the observed cadence of the callers (line_monitor
# polls scoreboards ~every 5 min; box scores hit ~15 min post-game). Tighten
# them after observing real throughput; loosening is safer than tightening.
SOURCE_SLAS: dict[str, int] = {
    # Scoreboards — frequent polling expected
    "odds_api_io.v3.odds.updated": 600,          # WebSocket-adjacent, 10 min
    "odds_api_io.v3.live_events.all": 900,
    "odds_api.v4.odds.basketball_nba": 900,
    # Game-day ESPN scrapes
    "espn.scoreboard.baseball_mlb": 900,         # 15 min — active day
    "espn.scoreboard.basketball_nba": 900,
    "espn.scoreboard.icehockey_nhl": 900,
    "espn.scoreboard.americanfootball_nfl": 900,
    # Box scores — post-game, more lenient
    "espn.boxscore.baseball_mlb": 1800,
    "espn.boxscore.basketball_nba": 1800,
    # Roster / injuries — daily cadence
    "espn.injuries.baseball_mlb": 21600,         # 6 hr
    "espn.injuries.basketball_nba": 21600,
    "mlb_stats.players": 172800,                 # 2 days
    "nhl_api.players": 172800,
    "nflverse.players": 172800,
    "nba_api.players": 172800,
    # Historical / backfill
    "nflverse.combine": 2592000,                 # 30 days
    # Calendar refresh
    "game_scheduler.refresh_calendar": 7200,     # 2 hr
}

# Prefix-based fallback for sources not explicitly listed.
SOURCE_SLA_DEFAULTS: list[tuple[str, int]] = [
    ("odds_api_io.", 900),
    ("odds_api.",    900),
    ("espn.scoreboard.", 1800),
    ("espn.boxscore.", 3600),
    ("espn.pbp.", 3600),
    ("espn.roster.", 21600),
    ("espn.injuries.", 21600),
    ("espn.odds.", 1800),
    ("espn.ncaa_hoops.", 3600),
    ("espn.golf.", 7200),
    ("nhl_api.", 3600),
    ("nba_api.", 3600),
    ("nflverse.", 3600),
    ("mlb_stats.", 86400),
    ("statcast.", 21600),
    ("openmeteo.", 3600),
    ("game_scheduler.", 7200),
]

# Critical-tier multiplier: source is CRITICAL (breaker trips) if
# last_success older than SLA * CRITICAL_MULTIPLIER.
CRITICAL_MULTIPLIER = 3


def resolve_sla_seconds(source: str) -> int:
    """Return the SLA (max age since last successful run) for a source tag."""
    if source in SOURCE_SLAS:
        return SOURCE_SLAS[source]
    for prefix, sla in SOURCE_SLA_DEFAULTS:
        if source.startswith(prefix):
            return sla
    # Unknown source — generous default so we don't false-alarm
    return 7200


class CircuitBreaker:
    """Per-subsystem circuit breaker."""

    def __init__(self, name: str, fail_threshold: int = BREAKER_FAIL_THRESHOLD):
        self.name = name
        self.fail_threshold = fail_threshold
        self.consecutive_failures = 0
        self.is_open = False  # open = disabled
        self.opened_at = 0.0
        self.last_error = ""
        self.total_trips = 0

    def record_success(self) -> None:
        self.consecutive_failures = 0
        if self.is_open:
            self.is_open = False
            logger.info(f"Circuit breaker CLOSED for {self.name} — recovered")

    def record_failure(self, error: str) -> bool:
        """Record failure. Returns True if breaker just tripped."""
        self.consecutive_failures += 1
        self.last_error = error

        if not self.is_open and self.consecutive_failures >= self.fail_threshold:
            self.is_open = True
            self.opened_at = time.monotonic()
            self.total_trips += 1
            logger.error(
                f"Circuit breaker OPEN for {self.name} — "
                f"{self.consecutive_failures} consecutive failures. "
                f"Disabling for {BREAKER_COOLDOWN}s. Error: {error}"
            )
            return True
        return False

    def should_attempt(self) -> bool:
        """Check if we should attempt this subsystem."""
        if not self.is_open:
            return True
        # Check cooldown
        elapsed = time.monotonic() - self.opened_at
        if elapsed >= BREAKER_COOLDOWN:
            logger.info(f"Circuit breaker half-open for {self.name} — attempting retry")
            return True
        return False

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "healthy": not self.is_open,
            "consecutive_failures": self.consecutive_failures,
            "is_open": self.is_open,
            "total_trips": self.total_trips,
            "last_error": self.last_error,
            "cooldown_remaining": max(
                0, BREAKER_COOLDOWN - (time.monotonic() - self.opened_at)
            ) if self.is_open else 0,
        }


class ErrorTracker:
    """Tracks error rates per subsystem per hour."""

    def __init__(self):
        self._errors: dict[str, list[float]] = defaultdict(list)
        self._total: dict[str, int] = defaultdict(int)

    def record(self, subsystem: str, error: str) -> None:
        now = time.monotonic()
        self._errors[subsystem].append(now)
        self._total[subsystem] += 1
        # Prune old entries
        cutoff = now - 3600
        self._errors[subsystem] = [
            t for t in self._errors[subsystem] if t > cutoff
        ]

    def rate_per_hour(self, subsystem: str) -> int:
        now = time.monotonic()
        cutoff = now - 3600
        return sum(1 for t in self._errors.get(subsystem, []) if t > cutoff)

    def is_rate_exceeded(self, subsystem: str) -> bool:
        return self.rate_per_hour(subsystem) >= MAX_ERRORS_PER_HOUR

    def get_summary(self) -> dict:
        return {
            sub: {
                "errors_last_hour": self.rate_per_hour(sub),
                "total_errors": self._total[sub],
            }
            for sub in self._errors
        }


class SystemHealth:
    """
    Comprehensive system health monitor.

    Call check_all() periodically. Each check updates the internal state
    and takes corrective action if possible. The full status is available
    via get_full_report() for the /health API endpoint.
    """

    def __init__(self):
        self._breakers = {name: CircuitBreaker(name) for name in SUBSYSTEMS}
        self._errors = ErrorTracker()
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._check_count = 0
        self._started_at = time.monotonic()
        self._last_check: dict[str, dict] = {}
        self._memory_samples: list[tuple[float, float]] = []  # (timestamp, rss_mb)
        self._stalled_phases: set[str] = set()

        # External references (set after initialization)
        self.research_loop = None
        self.autonomous_loop = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._started_at = time.monotonic()
        self._task = asyncio.create_task(self._loop())
        logger.info("System health monitor started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("System health monitor stopped")

    async def _loop(self) -> None:
        await asyncio.sleep(30)  # Let other systems start first
        while self._running:
            try:
                await self.check_all()
                self._check_count += 1
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
            await asyncio.sleep(CHECK_INTERVAL)

    async def check_all(self) -> dict:
        """Run all health checks. Returns summary."""
        results = {}

        checks = [
            ("ollama", self._check_ollama),
            ("sqlite", self._check_sqlite),
            ("disk", self._check_disk),
            ("memory", self._check_memory),
            ("network", self._check_network),
            ("data_collector", self._check_data_collector),
        ]

        for name, check_fn in checks:
            breaker = self._breakers[name]
            if not breaker.should_attempt():
                results[name] = {
                    "status": "circuit_open",
                    "message": f"Disabled — retry in {breaker.to_dict()['cooldown_remaining']:.0f}s",
                }
                continue

            try:
                result = await check_fn()
                results[name] = result
                self._last_check[name] = result

                status = result.get("status", "ok")
                if status in ("ok", "warning"):
                    # "warning" = informational (e.g. memory leak suspected
                    # but RSS still under limit). Don't trip the breaker.
                    breaker.record_success()
                else:
                    error_msg = result.get("error", result.get("message", "unknown"))
                    tripped = breaker.record_failure(error_msg)
                    self._errors.record(name, error_msg)

                    if tripped:
                        await self._on_breaker_trip(name, error_msg)

            except Exception as e:
                error_str = str(e)
                results[name] = {"status": "error", "error": error_str}
                breaker.record_failure(error_str)
                self._errors.record(name, error_str)

        return results

    # ── Individual health checks ──

    async def _check_ollama(self) -> dict:
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

    async def _check_sqlite(self) -> dict:
        """Check SQLite database health."""
        if not os.path.exists(DB_PATH):
            return {"status": "error", "error": f"Database not found: {DB_PATH}"}

        db_size_mb = os.path.getsize(DB_PATH) / (1024 * 1024)
        db_size_gb = db_size_mb / 1024

        try:
            async with aiosqlite.connect(DB_PATH) as db:
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

    async def _check_disk(self) -> dict:
        """Check available disk space."""
        try:
            usage = shutil.disk_usage(os.path.dirname(os.path.abspath(DB_PATH)))
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

    async def _check_memory(self) -> dict:
        """Check process memory usage and detect leaks."""
        try:
            import psutil
            process = psutil.Process()
            mem = process.memory_info()
            rss_mb = mem.rss / (1024 * 1024)

            # Track for leak detection
            now = time.monotonic()
            self._memory_samples.append((now, rss_mb))
            # Keep last 2 hours of samples
            cutoff = now - 7200
            self._memory_samples = [
                (t, m) for t, m in self._memory_samples if t > cutoff
            ]

            # Leak detection: linear regression on samples
            # Exclude first 5 minutes of samples — startup memory allocation
            # (loading models, SQLite, embeddings) skews growth rate enormously.
            # A 33-minute window with startup included can report 400+ MB/hr
            # when the true steady-state rate is <50 MB/hr.
            leak_detected = False
            growth_rate = 0.0
            STARTUP_GRACE_SECONDS = 900  # 15 minutes — loading 500MB DB + scipy takes time
            if len(self._memory_samples) >= 10:
                # Find first sample after startup grace period
                process_start = self._memory_samples[0][0]
                stable_samples = [
                    (t, m) for t, m in self._memory_samples
                    if t - process_start >= STARTUP_GRACE_SECONDS
                ]
                if len(stable_samples) >= 5:
                    first_time, first_mem = stable_samples[0]
                    last_time, last_mem = stable_samples[-1]
                    elapsed_hours = (last_time - first_time) / 3600
                    if elapsed_hours > 0.1:
                        growth_rate = (last_mem - first_mem) / elapsed_hours
                        leak_detected = growth_rate > MEMORY_GROWTH_MB_PER_HOUR

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
                "samples": len(self._memory_samples),
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

    async def _check_network(self) -> dict:
        """Check network connectivity to key services."""
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
            return {"status": "error", "error": f"Network check failed: {e}"}

        all_ok = all(v == "ok" for v in results.values())
        return {
            "status": "ok" if all_ok else "degraded",
            "services": results,
        }

    async def _check_data_collector(self) -> dict:
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
        if not os.path.exists(DB_PATH):
            return {"status": "error", "error": f"DB not found: {DB_PATH}"}

        try:
            async with aiosqlite.connect(DB_PATH) as db:
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

    # ── Corrective actions ──

    async def _on_breaker_trip(self, subsystem: str, error: str) -> None:
        """Called when a circuit breaker trips. Take corrective action."""
        logger.error(f"Breaker tripped for {subsystem}: {error}")

        # Try Telegram alert
        try:
            from tools import telegram
            await telegram.alert_system(
                f"HEALTH ALERT: {subsystem} circuit breaker OPEN\n"
                f"Error: {error}\n"
                f"Subsystem disabled for {BREAKER_COOLDOWN}s.\n"
                f"Other subsystems continue running.",
                is_error=True,
            )
        except Exception as e:
            logger.warning(f"Telegram health alert failed for {subsystem}: {e}")

        # Subsystem-specific recovery
        if subsystem == "ollama":
            # Try restarting Ollama
            try:
                proc = await asyncio.create_subprocess_exec(
                    "ollama", "serve",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                logger.info("Attempted Ollama restart")
            except Exception as e:
                logger.warning(f"Ollama restart failed: {e}")

        elif subsystem == "sqlite" and "corrupt" in error.lower():
            # SQLite corruption — try WAL checkpoint
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                logger.info("SQLite WAL checkpoint forced")
            except Exception as e:
                logger.warning(f"SQLite WAL checkpoint failed: {e}")

    # ── Public API ──

    def get_breaker(self, subsystem: str) -> Optional[CircuitBreaker]:
        return self._breakers.get(subsystem)

    def is_subsystem_healthy(self, subsystem: str) -> bool:
        """Check if a subsystem is healthy (breaker closed)."""
        breaker = self._breakers.get(subsystem)
        if not breaker:
            return True
        return not breaker.is_open

    def get_full_report(self) -> dict:
        """Full health report for /health endpoint."""
        uptime = time.monotonic() - self._started_at

        breaker_status = {
            name: b.to_dict() for name, b in self._breakers.items()
        }

        all_healthy = all(not b.is_open for b in self._breakers.values())

        return {
            "healthy": all_healthy,
            "uptime_seconds": round(uptime, 0),
            "uptime_hours": round(uptime / 3600, 1),
            "checks_completed": self._check_count,
            "check_interval_seconds": CHECK_INTERVAL,
            "subsystems": breaker_status,
            "error_rates": self._errors.get_summary(),
            "last_checks": self._last_check,
            "stalled_phases": list(self._stalled_phases),
        }

    def write_health_file(self) -> None:
        """
        Write health status to a file for Layer 3 (sentinel) to read.
        This works even if the HTTP server is down.
        """
        import json
        health_file = os.path.join(
            os.path.dirname(os.path.abspath(DB_PATH)), "health.json"
        )
        try:
            report = self.get_full_report()
            report["timestamp"] = datetime.now(timezone.utc).isoformat()
            report["pid"] = os.getpid()
            with open(health_file, "w") as f:
                json.dump(report, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to write health file: {e}")
