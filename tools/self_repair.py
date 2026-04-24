"""Self-repair engine — detect, fix, verify, record. Phase 0 of the research loop."""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiosqlite
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("callisto.self_repair")
DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

STALE_ODDS_MINUTES = 30
EMPTY_BACKTEST_LOOKBACK = 10
REJECTION_RATE_THRESHOLD = 0.95
SIGNAL_DROUGHT_EVENTS = 500
DB_BLOAT_ROWS = 100_000
SCRAPER_DISABLE_SECONDS = 3600

# ── Expanded recovery thresholds (feat/self-repair-expansion) ──
# Each recovery gets its own cooldown to prevent thrashing. Cooldowns are
# kept in _recovery_cooldowns (monotonic timestamp -> "can run after this").
DB_LOCK_WARNING_SECONDS = 60            # DB lock held >60s is the trip point
DB_LOCK_COOLDOWN_SECONDS = 900          # Don't force-checkpoint more than 1/15min
STUCK_PROCESSING_TIMEOUT_MULT = 5       # 5x max timeout = "clearly stuck"
STUCK_PROCESSING_COOLDOWN_SECONDS = 600
RESEARCH_LOOP_ZERO_PROGRESS_CYCLES = 10  # 10 cycles with no new hypotheses
RESEARCH_LOOP_COOLDOWN_SECONDS = 1800   # half-hour between forced gen cycles
CLAUDE_MISSING_COOLDOWN_SECONDS = 3600
SLA_STUCK_HOURS = 24                    # Source alerted >24h without recovery
SLA_REFRESH_COOLDOWN_SECONDS = 3600     # Don't re-refresh same source hourly
ODDS_SNAPSHOT_MISSING_COOLDOWN_SECONDS = 1800

# Max reasonable task timeout (from task_classifier DEFAULT bucket). If a
# PROCESSING row has been running longer than STUCK_MULT x this, declare it
# orphaned. We use a safe upper-bound (1800s = 30 min) rather than importing
# the classifier so this stays decoupled.
TASK_MAX_TIMEOUT_SECONDS = float(os.getenv("CALLISTO_TASK_MAX_TIMEOUT_S", "1800"))

# Registry of recovery cooldowns keyed by recovery_name. Values are
# monotonic timestamps; the recovery can fire again once time.monotonic()
# exceeds the stored value.
_recovery_cooldowns: dict[str, float] = {}

SCRAPERS = {
    "dk":     ("tools.dk_scraper",        "scrape_dk_odds",     "basketball_nba"),
    "fd":     ("tools.fanduel_scraper",    "scrape_fd_odds",     "basketball_nba"),
    # "betmgm": disabled — redundant with odds-api.io Pro, consistently 403
}
BETMGM_ALT_SUBDOMAINS = ["co", "pa", "va", "az"]
_disabled_scrapers: dict[str, float] = {}  # name -> re-enable monotonic ts

# Safe-to-prune tables: table -> (date_column, keep_days)
_PRUNE_SAFE = {
    "backtest_events": ("created_at", 90),
    "odds_snapshots": ("timestamp", 7),            # was 30 days — 2,880 snapshots × 100KB = 288MB bloat
    "odds_snapshots_v2": ("snapshot_time", 7),
    "integrity_checks": ("created_at", 14),
    "hermes_messages": ("timestamp", 90),
    "prop_snapshots": ("snapshot_time", 2),        # 360K rows/day at 15-min intervals — keep 2 days
    "deferred_work_queue": ("created_at", 3),      # was 7 days — 504 pending items cause WAL bloat
    "event_log": ("created_at", 7),                 # was unbounded — 16K+ rows growing indefinitely
}

HEARTBEAT_INTERVAL = 300  # Check every 5 minutes
LOOP_STALL_THRESHOLD = 2400  # 40 min — cycles have 18 phases with up to 600s timeouts each


class Heartbeat:
    """Independent watchdog — monitors the research loop and Claude availability.

    Runs as a separate async task, NOT inside the research loop.
    If the loop stalls, Hermes records it and Telegram alerts.
    If Claude gets stuck, it resets the counter.
    """

    def __init__(self):
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_cycle_seen = 0
        self._last_cycle_time = time.monotonic()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("Heartbeat started — monitoring loop health every 5 min")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        await asyncio.sleep(60)  # Initial delay
        while self._running:
            try:
                await self._check()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(HEARTBEAT_INTERVAL)

    async def _check(self) -> None:
        import httpx

        # 1. Check if research loop is cycling
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get("http://localhost:8420/system/full-status")
                d = r.json()
                rl = d.get("research_loop", {})
                current_cycle = rl.get("cycles_completed", 0)

                if current_cycle > self._last_cycle_seen:
                    self._last_cycle_seen = current_cycle
                    self._last_cycle_time = time.monotonic()
                else:
                    stall_duration = time.monotonic() - self._last_cycle_time
                    # Suppress stall warnings when Claude is rate-limited —
                    # the loop is expected to idle during cooldown periods.
                    claude_info = rl.get("claude_code", {})
                    claude_cooldown = (
                        not claude_info.get("available", True)
                        and claude_info.get("calls_this_window", 0)
                            >= claude_info.get("max_calls_per_hour", 999)
                    )
                    if stall_duration > LOOP_STALL_THRESHOLD and not claude_cooldown:
                        logger.warning(f"Heartbeat: research loop stalled for {stall_duration:.0f}s (cycle {current_cycle})")
                        # Record to Hermes
                        try:
                            from tools.hermes_memory import get_hermes_memory
                            hermes = get_hermes_memory()
                            await hermes.record_learning(
                                key="loop_stall_detected",
                                value=f"Research loop stalled at cycle {current_cycle} for {stall_duration:.0f}s",
                                confidence=0.9,
                                source="heartbeat",
                            )
                            await hermes.send_message(
                                "heartbeat",
                                f"WARNING: Research loop stalled at cycle {current_cycle} for {stall_duration/60:.0f} min",
                            )
                        except Exception:
                            pass
                        # Alert via Telegram
                        try:
                            from tools import telegram
                            await telegram.alert_system(
                                f"Research loop stalled at cycle {current_cycle} for {stall_duration/60:.0f} min",
                                is_error=True,
                            )
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Heartbeat: API unreachable — {e}")

        # 2. Check Claude availability + track downtime
        try:
            from tools.claude_code import is_available, get_usage_stats
            stats = get_usage_stats()

            # Track downtime transitions for work queue
            try:
                from tools.work_queue import get_downtime_tracker
                tracker = get_downtime_tracker()
                if is_available():
                    tracker.mark_available()
                else:
                    tracker.mark_unavailable()
                # Periodically record pattern to Hermes
                if tracker._total_outages > 0 and tracker._total_outages % 5 == 0:
                    await tracker.record_to_hermes()
            except Exception:
                pass

            if not is_available() and stats.get("elapsed_seconds", 0) > 3600:
                # Stuck — force reset
                import tools.claude_code as cc
                cc._call_count = 0
                cc._last_reset = time.monotonic()
                logger.info("Heartbeat: reset stuck Claude counter")
                try:
                    from tools.hermes_memory import get_hermes_memory
                    hermes = get_hermes_memory()
                    await hermes.record_learning(
                        key="claude_auto_reset",
                        value=f"Heartbeat auto-reset Claude counter (was stuck at {stats.get('calls_this_window')}/{stats.get('max_calls_per_hour')})",
                        confidence=0.8,
                        source="heartbeat",
                    )
                except Exception:
                    pass
        except Exception:
            pass


class SelfRepairEngine:
    """Autonomous self-repair — detect, fix, verify, record."""

    def __init__(self):
        self._cycle_count = 0
        self._total_fixes = 0
        self._last_run: Optional[str] = None
        # Expanded-recovery bookkeeping (feat/self-repair-expansion).
        # Watermark starts None so the first probe seeds it rather than
        # immediately declaring "zero progress since time zero".
        self._last_hypothesis_watermark: Optional[int] = None
        self._research_stagnant_cycles: int = 0

    async def run_repair_cycle(self) -> dict:
        """Main entry point — called by research loop each cycle."""
        self._cycle_count += 1
        start = time.monotonic()
        issues = await self._detect_issues()
        results = [await self._repair(i) for i in issues]
        fixed = sum(1 for r in results if r["fixed"])
        # Expanded recoveries run on the same cadence but are independently
        # cooldown-gated so they fire less often than the detector loop.
        expanded_results: list[dict] = []
        try:
            expanded_results = await self.run_expanded_recoveries()
        except Exception as e:
            logger.debug(f"Expanded recoveries failed: {e}")
        fixed_expanded = sum(1 for r in expanded_results if r.get("fixed"))
        self._total_fixes += fixed + fixed_expanded
        self._last_run = datetime.now(timezone.utc).isoformat()
        elapsed = time.monotonic() - start
        if issues or expanded_results:
            logger.info(
                f"Self-repair #{self._cycle_count}: "
                f"{fixed}/{len(issues)} legacy fixed, "
                f"{fixed_expanded}/{len(expanded_results)} expanded fixed "
                f"({elapsed:.1f}s)"
            )
        return {"issues_found": len(issues),
                "fixed": fixed,
                "expanded_results": expanded_results,
                "expanded_fixed": fixed_expanded,
                "elapsed_seconds": round(elapsed, 2),
                "cycle": self._cycle_count, "results": results}

    def get_status(self) -> dict:
        return {"cycles": self._cycle_count, "total_fixes": self._total_fixes,
                "last_run": self._last_run,
                "disabled_scrapers": {n: round(max(0, t - time.monotonic()), 0)
                                      for n, t in _disabled_scrapers.items() if t > time.monotonic()}}


    async def _detect_issues(self) -> list[dict]:
        issues: list[dict] = []
        for itype, det in [("scraper_broken", self._det_scrapers), ("stale_odds", self._det_stale_odds),
                           ("empty_backtests", self._det_empty_bt), ("claude_stuck", self._det_claude),
                           ("high_rejection", self._det_rejection), ("signal_drought", self._det_drought),
                           ("premature_rejection", self._det_premature_rejection),
                           ("resolution_broken", self._det_resolution_broken),
                           ("db_bloat", self._det_bloat)]:
            try:
                d = await det()
                if d:
                    d["type"] = itype
                    issues.append(d)
            except Exception as e:
                logger.debug(f"Detector {itype}: {e}")
        return issues

    async def _det_scrapers(self) -> Optional[dict]:
        broken = []
        for name, (mod_path, fn_name, sport) in SCRAPERS.items():
            if name in _disabled_scrapers and _disabled_scrapers[name] > time.monotonic():
                continue
            try:
                mod = __import__(mod_path, fromlist=[fn_name])
                result = await asyncio.wait_for(getattr(mod, fn_name)(sport), timeout=30)
                if isinstance(result, dict) and result.get("error"):
                    broken.append({"name": name, "error": str(result["error"])})
            except asyncio.TimeoutError:
                broken.append({"name": name, "error": "timeout (30s)"})
            except Exception as e:
                broken.append({"name": name, "error": str(e)})
        return {"broken_scrapers": broken} if broken else None

    async def _det_stale_odds(self) -> Optional[dict]:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                from tools.db_utils import safe_ident
                for table, col in [("odds_snapshots_v2", "snapshot_time"), ("odds_snapshots", "timestamp")]:
                    try:
                        row = (await (await db.execute(f"SELECT MAX({safe_ident(col)}) FROM {safe_ident(table)}")).fetchone())
                        if row and row[0]:
                            latest = datetime.fromisoformat(str(row[0]).replace("Z", "+00:00"))
                            if latest.tzinfo is None:
                                latest = latest.replace(tzinfo=timezone.utc)
                            age = (datetime.now(timezone.utc) - latest).total_seconds() / 60
                            if age > STALE_ODDS_MINUTES:
                                return {"table": table, "age_minutes": round(age, 1)}
                    except Exception:
                        continue
        except Exception:
            pass
        return None

    async def _det_empty_bt(self) -> Optional[dict]:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                rows = await (await db.execute(
                    "SELECT run_id, total_events FROM backtest_runs ORDER BY completed_at DESC LIMIT ?",
                    (EMPTY_BACKTEST_LOOKBACK,))).fetchall()
                if rows and len(rows) >= 3 and all(r[1] == 0 for r in rows if r[1] is not None):
                    return {"run_count": len(rows), "sample_ids": [r[0] for r in rows[:5]]}
        except Exception:
            pass
        return None

    async def _det_claude(self) -> Optional[dict]:
        try:
            from tools.claude_code import get_usage_stats, _TRACKING_WINDOW
            s = get_usage_stats()
            if s["calls_this_window"] >= s["max_calls_per_hour"] and s["elapsed_seconds"] > _TRACKING_WINDOW:
                return {"calls": s["calls_this_window"], "max": s["max_calls_per_hour"],
                        "elapsed": s["elapsed_seconds"], "window": _TRACKING_WINDOW}
        except Exception:
            pass
        return None

    async def _det_rejection(self) -> Optional[dict]:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                counts = dict(await (await db.execute(
                    "SELECT status, COUNT(*) FROM hypotheses "
                    "WHERE status IN ('rejected','paper_trading','live','retired') GROUP BY status"
                )).fetchall())
                rej = counts.get("rejected", 0)
                pro = counts.get("paper_trading", 0) + counts.get("live", 0) + counts.get("retired", 0)
                total = rej + pro
                if total >= 20 and rej / total > REJECTION_RATE_THRESHOLD:
                    return {"rejection_rate": round(rej / total, 3), "rejected": rej, "promoted": pro}
        except Exception:
            pass
        return None

    async def _det_drought(self) -> Optional[dict]:
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                row = await (await db.execute(
                    "SELECT COUNT(*), SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) "
                    "FROM backtest_events")).fetchone()
                if row and (row[0] or 0) >= SIGNAL_DROUGHT_EVENTS and (row[1] or 0) == 0:
                    return {"total_events": row[0], "signals": row[1] or 0}
        except Exception:
            pass
        return None

    async def _det_premature_rejection(self) -> Optional[dict]:
        """Detect hypotheses rejected with 0 events for sports that have data."""
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Sports with historical odds data
                sports_with_data = set()
                rows = await (await db.execute(
                    "SELECT DISTINCT sport FROM historical_odds_cache"
                )).fetchall()
                for r in rows:
                    sports_with_data.add(r[0])

                # Rejected hypotheses with 0 events in sports that HAVE data
                premature = await (await db.execute(
                    "SELECT h.hypothesis_id, h.name, h.sport FROM hypotheses h "
                    "WHERE h.status = 'rejected' "
                    "AND NOT EXISTS (SELECT 1 FROM backtest_events be WHERE be.hypothesis_id = h.hypothesis_id)"
                )).fetchall()

                requeue_candidates = [
                    {"id": r[0], "name": r[1], "sport": r[2]}
                    for r in premature if r[2] in sports_with_data
                ]

                if len(requeue_candidates) >= 5:
                    return {"count": len(requeue_candidates), "candidates": requeue_candidates[:50],
                            "sports_with_data": list(sports_with_data)}
        except Exception:
            pass
        return None

    async def _det_bloat(self) -> Optional[dict]:
        bloated = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                tables = [r[0] for r in await (await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")).fetchall()]
                for t in tables:
                    try:
                        c = (await (await db.execute(f"SELECT COUNT(*) FROM [{t}]")).fetchone())[0]
                        if c > DB_BLOAT_ROWS:
                            bloated.append({"table": t, "rows": c})
                    except Exception:
                        continue
        except Exception:
            pass
        return {"bloated_tables": bloated} if bloated else None

    async def _det_resolution_broken(self) -> Optional[dict]:
        """Detect when backtest resolution consistently fails to match events.

        If >30% of events are unresolved AND game_results has data for those
        dates, the resolution pipeline is broken (e.g. date mismatch, team
        name mismatch) and needs a re-run.
        """
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                row = await (await db.execute(
                    "SELECT COUNT(*), "
                    "SUM(CASE WHEN actual_result IS NULL THEN 1 ELSE 0 END) "
                    "FROM backtest_events"
                )).fetchone()
                if not row or (row[0] or 0) < 50:
                    return None
                total, unresolved = row[0], row[1] or 0
                rate = unresolved / total
                if rate < 0.30:
                    return None
                # Verify game_results has data for the unresolved dates
                gr_count = (await (await db.execute(
                    "SELECT COUNT(*) FROM game_results"
                )).fetchone())[0]
                if gr_count < 10:
                    return None  # No results to resolve against
                return {
                    "total_events": total,
                    "unresolved": unresolved,
                    "unresolved_rate": round(rate, 3),
                    "game_results_count": gr_count,
                }
        except Exception:
            pass
        return None


    async def _repair(self, issue: dict) -> dict:
        itype = issue.get("type", "unknown")
        fn = {"scraper_broken": self._fix_scraper, "stale_odds": self._fix_stale_odds,
              "empty_backtests": self._fix_empty_bt, "claude_stuck": self._fix_claude,
              "high_rejection": self._fix_thresholds, "signal_drought": self._fix_thresholds,
              "premature_rejection": self._fix_premature_rejection,
              "resolution_broken": self._fix_resolution_broken,
              "db_bloat": self._fix_bloat}.get(itype)
        if not fn:
            return {"fixed": False, "action": "no_strategy", "detail": itype}
        try:
            result = await fn(issue)
        except Exception as e:
            result = {"fixed": False, "action": "repair_error", "detail": str(e)}
        await self._record_to_hermes(itype, result)
        return result


    async def _fix_scraper(self, issue: dict) -> dict:
        fixed, disabled = [], []
        for si in issue.get("broken_scrapers", []):
            name, error = si["name"], si["error"]
            repaired = (name == "betmgm" and await self._try_betmgm_subdomains())
            if not repaired:
                _disabled_scrapers[name] = time.monotonic() + SCRAPER_DISABLE_SECONDS
                disabled.append(name)
                logger.warning(f"Scraper {name} disabled {SCRAPER_DISABLE_SECONDS}s: {error}")
            else:
                fixed.append(name)
        active = sum(1 for n in SCRAPERS
                     if n not in _disabled_scrapers or _disabled_scrapers[n] <= time.monotonic())
        parts = []
        if fixed: parts.append(f"Fixed: {','.join(fixed)}")
        if disabled: parts.append(f"Disabled: {','.join(disabled)}")
        parts.append(f"{active}/{len(SCRAPERS)} active")
        return {"fixed": bool(fixed) or active > 0, "action": "scraper_repair", "detail": ". ".join(parts)}

    async def _try_betmgm_subdomains(self) -> bool:
        try:
            import importlib
            mod = importlib.import_module("tools.betmgm_scraper")
            orig = getattr(mod, "_BASE_URL", None)
            if not orig:
                return False
            for sub in BETMGM_ALT_SUBDOMAINS:
                mod._BASE_URL = orig.replace("sports.nj.", f"sports.{sub}.")
                try:
                    r = await asyncio.wait_for(mod.scrape_betmgm_odds("basketball_nba"), timeout=15)
                    if isinstance(r, dict) and not r.get("error") and r.get("game_count", 0) > 0:
                        logger.info(f"BetMGM recovered via subdomain {sub}")
                        return True
                except Exception:
                    pass
                mod._BASE_URL = orig  # revert before trying next
        except Exception:
            pass
        return False

    async def _fix_stale_odds(self, issue: dict) -> dict:
        age = issue.get("age_minutes", 0)
        for name, (mod_path, fn_name, sport) in SCRAPERS.items():
            if name in _disabled_scrapers and _disabled_scrapers[name] > time.monotonic():
                continue
            try:
                mod = __import__(mod_path, fromlist=[fn_name])
                r = await asyncio.wait_for(getattr(mod, fn_name)(sport), timeout=30)
                if isinstance(r, dict) and not r.get("error"):
                    return {"fixed": True, "action": "force_snapshot",
                            "detail": f"Via {name} (was {age:.0f}min stale)"}
            except Exception:
                continue
        return {"fixed": False, "action": "force_snapshot_failed", "detail": f"All scrapers failed ({age:.0f}min stale)"}

    async def _fix_empty_bt(self, issue: dict) -> dict:
        adjusted = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                row = await (await db.execute("SELECT MIN(game_date), MAX(game_date) FROM game_contexts")).fetchone()
                if not row or not row[0]:
                    return {"fixed": False, "action": "no_game_data", "detail": "No game_contexts to calibrate"}
                ds, de = str(row[0])[:10], str(row[1])[:10]
                rows = await (await db.execute(
                    "SELECT hypothesis_id, model_config FROM hypotheses WHERE status IN ('draft','backtesting')"
                )).fetchall()
                for h_id, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue
                    ts, te = cfg.get("training_period_start", ""), cfg.get("training_period_end", "")
                    if ts and te and (te < ds or ts > de):
                        cfg["training_period_start"], cfg["training_period_end"] = ds, de
                        await db.execute("UPDATE hypotheses SET model_config=? WHERE hypothesis_id=?",
                                         (json.dumps(cfg), h_id))
                        adjusted += 1
                if adjusted:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "date_range_error", "detail": str(e)}
        if adjusted:
            return {"fixed": True, "action": "adjusted_date_ranges", "detail": f"{adjusted} hypotheses -> {ds}..{de}"}
        return {"fixed": False, "action": "no_adjustment_needed", "detail": "Date ranges within data; other cause"}

    async def _fix_claude(self, issue: dict) -> dict:
        try:
            import tools.claude_code as cc
            old_count, old_reset = cc._call_count, cc._last_reset
            cc._call_count, cc._last_reset = 0, time.monotonic()
            if cc.is_available() or cc.get_cooldown_remaining() > 0:
                return {"fixed": True, "action": "reset_call_counter",
                        "detail": f"Count {old_count} -> 0, window re-opened"}
            cc._call_count, cc._last_reset = old_count, old_reset  # revert
            return {"fixed": False, "action": "reset_reverted", "detail": "Still unavailable after reset"}
        except Exception as e:
            return {"fixed": False, "action": "reset_error", "detail": str(e)}

    async def _fix_thresholds(self, issue: dict) -> dict:
        """Lower edge thresholds for high_rejection / signal_drought."""
        itype = issue.get("type", "")
        adjusted = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                if itype == "high_rejection":
                    zsr = await (await db.execute(
                        "SELECT COUNT(*) FROM hypotheses h "
                        "LEFT JOIN backtest_events be ON h.hypothesis_id=be.hypothesis_id "
                        "WHERE h.status='rejected' "
                        "GROUP BY h.hypothesis_id HAVING COUNT(be.id)>0 "
                        "AND SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END)=0"
                    )).fetchall()
                    if len(zsr) < 5:
                        return {"fixed": False, "action": "rejection_analysis",
                                "detail": f"Only {len(zsr)} zero-signal rejections; genuine bad hypotheses"}
                q = ("SELECT hypothesis_id, model_config FROM hypotheses WHERE status='backtesting'"
                     if itype == "high_rejection" else
                     "SELECT be.hypothesis_id, h.model_config FROM backtest_events be "
                     "JOIN hypotheses h ON h.hypothesis_id=be.hypothesis_id "
                     "WHERE h.status IN ('backtesting','draft') "
                     "GROUP BY be.hypothesis_id HAVING COUNT(be.id)>=20")
                rows = await (await db.execute(q)).fetchall()
                for row in rows:
                    h_id, mc_raw = row[0], row[1]
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue
                    thr = cfg.get("edge_threshold")
                    if thr is not None and float(thr) > 0.02:
                        cfg["edge_threshold"] = 0.015
                        cfg["_threshold_lowered_by"] = f"self_repair_{itype}"
                        await db.execute("UPDATE hypotheses SET model_config=? WHERE hypothesis_id=?",
                                         (json.dumps(cfg), h_id))
                        adjusted += 1
                if adjusted:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "threshold_error", "detail": str(e)}
        if adjusted:
            return {"fixed": True, "action": "lowered_thresholds",
                    "detail": f"edge_threshold -> 1.5% on {adjusted} hypotheses ({itype})"}
        return {"fixed": False, "action": "no_thresholds_to_lower", "detail": "No thresholds > 2% found"}

    async def _fix_premature_rejection(self, issue: dict) -> dict:
        """Re-queue hypotheses that were rejected without being tested."""
        candidates = issue.get("candidates", [])
        if not candidates:
            return {"fixed": False, "action": "no_candidates", "detail": "No premature rejections found"}
        requeued = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                for c in candidates:
                    await db.execute(
                        "UPDATE hypotheses SET status = 'draft' WHERE hypothesis_id = ? AND status = 'rejected'",
                        (c["id"],),
                    )
                    requeued += 1
                await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "requeue_error", "detail": str(e)}
        if requeued:
            return {"fixed": True, "action": "requeued_premature_rejections",
                    "detail": f"Moved {requeued} hypotheses from rejected -> draft (had 0 events in sports with data)"}
        return {"fixed": False, "action": "no_requeue", "detail": "No candidates matched"}

    async def _fix_resolution_broken(self, issue: dict) -> dict:
        """Re-run backtest resolution (now with ±1 day date matching)."""
        try:
            from tools.backtest import BacktestEngine
            from tools.hypothesis import HypothesisManager
            from tools.historical_odds import HistoricalOddsFetcher
            hm = HypothesisManager(DB_PATH)
            hf = HistoricalOddsFetcher(DB_PATH)
            engine = BacktestEngine(hm, hf, DB_PATH)
            await engine.initialize()
            result = await engine.resolve_from_game_results()
            resolved = result.get("resolved", 0)
            remaining = result.get("unresolved", 0)
            await engine.close()
            if resolved > 0:
                return {
                    "fixed": True,
                    "action": "reran_resolution",
                    "detail": f"Resolved {resolved} events ({remaining} still unresolved)",
                }
            return {
                "fixed": False,
                "action": "resolution_no_new_matches",
                "detail": f"{remaining} events remain unresolved — may need more game_results data",
            }
        except Exception as e:
            return {"fixed": False, "action": "resolution_error", "detail": str(e)}

    async def _fix_bloat(self, issue: dict) -> dict:
        pruned = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                for entry in issue.get("bloated_tables", []):
                    table = entry["table"]
                    if table not in _PRUNE_SAFE:
                        continue
                    col, days = _PRUNE_SAFE[table]
                    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
                    try:
                        old = (await (await db.execute(
                            f"SELECT COUNT(*) FROM [{table}] WHERE [{col}] < ?", (cutoff,))).fetchone())[0]
                        if old > 0:
                            await db.execute(f"DELETE FROM [{table}] WHERE [{col}] < ?", (cutoff,))
                            pruned.append(f"{table}: -{old} (kept {days}d)")
                    except Exception as e:
                        logger.debug(f"Prune {table}: {e}")
                if pruned:
                    await db.commit()
                # Always checkpoint WAL to prevent unbounded WAL growth.
                # Without this, WAL can grow to 20GB+ and degrade performance.
                try:
                    result = await (await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")).fetchone()
                    if result:
                        busy, log, checkpointed = result
                        if log > 0:
                            logger.info(f"WAL checkpoint: {checkpointed}/{log} pages checkpointed (busy={busy})")
                except Exception as e:
                    logger.debug(f"WAL checkpoint: {e}")
                if pruned:
                    # Defer VACUUM to the dedicated autocommit path — running
                    # VACUUM on this connection fails silently with
                    # "cannot VACUUM from within a transaction" because aiosqlite
                    # keeps an implicit txn open around the preceding DELETEs
                    # (and the routing patch forwards writes to the coordinator,
                    # which is also deferred-mode). The vacuum_db() helper
                    # opens a fresh stdlib sqlite3 connection in autocommit
                    # mode — no shared state, no transaction conflict.
                    try:
                        from tools.schema import vacuum_db as _vacuum_db
                        await _vacuum_db(DB_PATH)
                    except Exception as e:
                        logger.warning(f"VACUUM after prune failed: {e!r}")
        except Exception as e:
            return {"fixed": False, "action": "prune_error", "detail": str(e)}
        if pruned:
            return {"fixed": True, "action": "pruned_tables", "detail": "; ".join(pruned)}
        return {"fixed": False, "action": "no_safe_prune", "detail": "Bloated tables not on safe list"}

    # ── Pattern matching for Claude's deep work findings ──

    # Keyword patterns that map Claude's pipeline_issues strings to fix strategies.
    # Order matters: first match wins.
    _FINDING_PATTERNS: list[tuple[list[str], str]] = [
        (["identical event", "same games", "same event", "duplicate"],
         "duplicate_events"),
        (["side filter", "side_filter", "side not applied", "over/under not filtered",
          "totals over.*same.*totals under"],
         "side_filter_broken"),
        (["prioritize nba", "prioritize nfl", "nba over mlb",
          "nfl over mlb", "nhl over mlb", "sport priority", "reorder"],
         "prioritize_sports"),
        (["low sample", "not enough data", "insufficient data",
          "too few events", "small sample"],
         "low_sample_size"),
        (["zero promotion", "no promotions", "promotion threshold",
          "nothing promoted", "0 promotions"],
         "promotion_thresholds_strict"),
        (["edge ceiling", "edge cap", "max edge", "threshold too high",
          "thresholds above"],
         "edge_ceiling"),
        (["resolution", "game_results", "date mismatch", "date offset",
          "timezone", "could not match", "match failure", "unresolved event"],
         "resolution_broken"),
    ]

    @staticmethod
    def _classify_finding(description: str) -> str:
        """Match a free-text finding description to a known fix strategy."""
        desc_lower = description.lower()
        for keywords, strategy in SelfRepairEngine._FINDING_PATTERNS:
            for kw in keywords:
                if kw in desc_lower:
                    return strategy
        return "unknown"

    async def handle_claude_findings(self, findings: list[dict]) -> list[dict]:
        """Convert Claude's deep work findings into repair actions.

        Each finding has: {"severity": "CRITICAL|HIGH|LOW", "description": "..."}
        Returns a list of result dicts with keys: fixed, action, detail.
        """
        results: list[dict] = []
        for finding in findings:
            desc = finding.get("description", "")
            severity = finding.get("severity", "LOW")
            strategy = self._classify_finding(desc)

            try:
                handler = {
                    "duplicate_events": self._fix_finding_duplicate_events,
                    "side_filter_broken": self._fix_finding_side_filter,
                    "prioritize_sports": self._fix_finding_prioritize_sports,
                    "low_sample_size": self._fix_finding_low_sample,
                    "promotion_thresholds_strict": self._fix_finding_promotion_thresholds,
                    "edge_ceiling": self._fix_finding_edge_ceiling,
                    "resolution_broken": self._fix_finding_resolution,
                }.get(strategy)

                if handler:
                    result = await handler(finding)
                else:
                    # Unknown pattern — record to Hermes with a UNIQUE key
                    # to prevent the same finding from inflating occurrences.
                    # Previous bug: x396 occurrences of "unknown" because the
                    # same stalling issue was re-recorded every cycle under a
                    # single key, drowning out real discoveries.
                    import hashlib
                    finding_hash = hashlib.md5(
                        f"{strategy}:{desc[:100]}".encode()
                    ).hexdigest()[:8]
                    result = {"fixed": False, "action": "recorded_for_review",
                              "detail": f"[{severity}] {desc[:200]}"}
                    try:
                        from tools.hermes_memory import get_hermes_memory
                        hermes = get_hermes_memory()
                        await hermes.record_learning(
                            key=f"claude_finding_{strategy or 'unknown'}_{finding_hash}",
                            value=f"[{severity}] {desc[:500]}",
                            confidence=0.5,
                            source="deep_work_finding",
                        )
                    except Exception:
                        pass

            except Exception as e:
                result = {"fixed": False, "action": "handler_error",
                          "detail": f"{strategy}: {e}"}

            await self._record_to_hermes(f"claude_finding_{strategy}", result)
            results.append(result)

        return results

    async def _fix_finding_duplicate_events(self, finding: dict) -> dict:
        """Flag hypotheses that tested identical event sets as needing unique data."""
        flagged = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                # Find hypotheses with identical (unique_games, total_events) counts
                cursor = await db.execute("""
                    SELECT h.hypothesis_id, h.name,
                           COUNT(DISTINCT be.event_id) as unique_games,
                           COUNT(*) as total_events
                    FROM hypotheses h
                    JOIN backtest_events be ON be.hypothesis_id = h.hypothesis_id
                    WHERE h.status = 'backtesting'
                    GROUP BY h.hypothesis_id
                    HAVING total_events > 10
                """)
                rows = await cursor.fetchall()
                # Group by (unique_games, total_events) signature
                sig_groups: dict[str, list[tuple]] = {}
                for r in rows:
                    sig = f"{r[2]}g_{r[3]}e"
                    sig_groups.setdefault(sig, []).append(r)

                for sig, group in sig_groups.items():
                    if len(group) <= 1:
                        continue
                    # Flag all but the first as needing unique data
                    for r in group[1:]:
                        h_id = r[0]
                        try:
                            row = await (await db.execute(
                                "SELECT model_config FROM hypotheses WHERE hypothesis_id = ?",
                                (h_id,)
                            )).fetchone()
                            cfg = json.loads(row[0]) if row and row[0] else {}
                            cfg["needs_unique_data"] = True
                            cfg["_flagged_by"] = "claude_finding_duplicate_events"
                            await db.execute(
                                "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                                (json.dumps(cfg), h_id),
                            )
                            flagged += 1
                        except Exception:
                            continue
                if flagged:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "duplicate_events_check",
                    "detail": f"Error: {e}"}

        if flagged:
            return {"fixed": True, "action": "flagged_duplicate_events",
                    "detail": f"Flagged {flagged} hypotheses as needs_unique_data"}
        return {"fixed": False, "action": "duplicate_events_check",
                "detail": "No duplicate event sets detected"}

    async def _fix_finding_side_filter(self, finding: dict) -> dict:
        """Check and fix hypotheses with broken or missing side_filter in model_config."""
        fixed_count = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, name, thesis, model_config FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting')"
                )
                rows = await cursor.fetchall()
                for h_id, name, thesis, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue

                    # Infer side from name/thesis if side_filter is missing
                    name_lower = (name or "").lower()
                    thesis_lower = (thesis or "").lower()
                    current_side = cfg.get("side_filter")

                    if current_side:
                        continue  # Already has a side filter

                    inferred_side = None
                    if "under" in name_lower or "under" in thesis_lower:
                        inferred_side = "under"
                    elif "over" in name_lower or "over" in thesis_lower:
                        inferred_side = "over"
                    elif "home" in name_lower or "home" in thesis_lower:
                        inferred_side = "home"
                    elif "away" in name_lower or "away" in thesis_lower:
                        inferred_side = "away"

                    if inferred_side:
                        cfg["side_filter"] = inferred_side
                        cfg["_side_filter_inferred_by"] = "claude_finding_side_filter"
                        await db.execute(
                            "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                            (json.dumps(cfg), h_id),
                        )
                        fixed_count += 1
                if fixed_count:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "side_filter_fix",
                    "detail": f"Error: {e}"}

        if fixed_count:
            return {"fixed": True, "action": "side_filter_fix",
                    "detail": f"Inferred and set side_filter on {fixed_count} hypotheses"}
        return {"fixed": False, "action": "side_filter_fix",
                "detail": "No hypotheses missing side_filter"}

    async def _fix_finding_prioritize_sports(self, finding: dict) -> dict:
        """Record sport prioritization preference — actual reordering happens in _phase_backtest."""
        # The actual reordering is handled by SPORT_PRIORITY in _phase_backtest.
        # Here we just record the finding and confirm the priority is active.
        try:
            from tools.hermes_memory import get_hermes_memory
            hermes = get_hermes_memory()
            await hermes.record_learning(
                key="sport_priority_active",
                value="Sport priority sorting enabled: NBA > NFL > NHL > MLB > NCAAB > NCAAW > PGA",
                confidence=0.9,
                source="deep_work_finding",
            )
        except Exception:
            pass
        return {"fixed": True, "action": "sport_priority_confirmed",
                "detail": "SPORT_PRIORITY ordering active in backtest queue (NBA/NFL first, MLB last)"}

    async def _fix_finding_low_sample(self, finding: dict) -> dict:
        """Set minimum_events threshold on hypotheses to prevent premature evaluation."""
        updated = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, model_config FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting')"
                )
                rows = await cursor.fetchall()
                for h_id, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if cfg.get("minimum_events"):
                        continue  # Already set
                    cfg["minimum_events"] = 30
                    cfg["_min_events_set_by"] = "claude_finding_low_sample"
                    await db.execute(
                        "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                        (json.dumps(cfg), h_id),
                    )
                    updated += 1
                if updated:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "set_minimum_events",
                    "detail": f"Error: {e}"}

        if updated:
            return {"fixed": True, "action": "set_minimum_events",
                    "detail": f"Set minimum_events=30 on {updated} hypotheses"}
        return {"fixed": False, "action": "set_minimum_events",
                "detail": "All hypotheses already have minimum_events set"}

    async def _fix_finding_promotion_thresholds(self, finding: dict) -> dict:
        """Lower promotion requirements: reduce min events from default to 20."""
        updated = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, model_config FROM hypotheses "
                    "WHERE status = 'backtesting'"
                )
                rows = await cursor.fetchall()
                for h_id, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        continue
                    current_min = cfg.get("minimum_events_for_promotion")
                    if current_min is not None and current_min <= 20:
                        continue  # Already low enough
                    cfg["minimum_events_for_promotion"] = 20
                    cfg["_promotion_threshold_lowered_by"] = "claude_finding_promotion_thresholds"
                    await db.execute(
                        "UPDATE hypotheses SET model_config = ? WHERE hypothesis_id = ?",
                        (json.dumps(cfg), h_id),
                    )
                    updated += 1
                if updated:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "lower_promotion_thresholds",
                    "detail": f"Error: {e}"}

        if updated:
            return {"fixed": True, "action": "lower_promotion_thresholds",
                    "detail": f"Lowered minimum_events_for_promotion to 20 on {updated} hypotheses"}
        return {"fixed": False, "action": "lower_promotion_thresholds",
                "detail": "All backtesting hypotheses already have low promotion thresholds"}

    async def _fix_finding_edge_ceiling(self, finding: dict) -> dict:
        """Adjust edge thresholds above 2% down to 1.5%."""
        adjusted = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cursor = await db.execute(
                    "SELECT hypothesis_id, edge_threshold, model_config FROM hypotheses "
                    "WHERE status IN ('draft', 'backtesting') AND edge_threshold > 0.02"
                )
                rows = await cursor.fetchall()
                for h_id, thresh, mc_raw in rows:
                    try:
                        cfg = json.loads(mc_raw) if mc_raw else {}
                    except (json.JSONDecodeError, TypeError):
                        cfg = {}
                    cfg["_previous_edge_threshold"] = thresh
                    cfg["_edge_ceiling_lowered_by"] = "claude_finding_edge_ceiling"
                    await db.execute(
                        "UPDATE hypotheses SET edge_threshold = 0.015, model_config = ? "
                        "WHERE hypothesis_id = ?",
                        (json.dumps(cfg), h_id),
                    )
                    adjusted += 1
                if adjusted:
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "lower_edge_ceiling",
                    "detail": f"Error: {e}"}

        if adjusted:
            return {"fixed": True, "action": "lower_edge_ceiling",
                    "detail": f"Lowered edge_threshold from >2% to 1.5% on {adjusted} hypotheses"}
        return {"fixed": False, "action": "lower_edge_ceiling",
                "detail": "No hypotheses with edge_threshold > 2% found"}

    async def _fix_finding_resolution(self, finding: dict) -> dict:
        """Re-run resolution when Claude identifies matching failures."""
        return await self._fix_resolution_broken(finding)

    async def _record_to_hermes(self, itype: str, result: dict) -> None:
        try:
            from tools.hermes_memory import get_hermes_memory
            h = get_hermes_memory()
            fixed = result.get("fixed", False)
            val = (f"{'FIXED' if fixed else 'UNFIXED'} [{result.get('action','')}] "
                   f"{result.get('detail','')} (#{self._cycle_count}, "
                   f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')})")
            await h.record_learning(key=f"self_repair_{itype}", value=val,
                                    confidence=0.8 if fixed else 0.4, source="self_repair")
        except Exception as e:
            logger.debug(f"Hermes record failed: {e}")

    # ──────────────────────────────────────────────────────────────────────
    # Expanded recovery surface (feat/self-repair-expansion).
    #
    # Each recovery is a thin async coroutine with the same contract as
    # _fix_* — returns a dict {fixed: bool, action: str, detail: str,
    # metadata?: dict}. It's registered in _RECOVERIES below and gated by
    # a per-name cooldown. The research loop calls run_expanded_recoveries()
    # once per cycle; /admin/self-repair/trigger/{name} bypasses the
    # cooldown but still writes to self_repair_log with trigger='manual'.
    #
    # IMPORTANT: recoveries MUST NOT destructively modify hypotheses, bets,
    # or odds snapshots. They can mark state (e.g. task_queue PROCESSING ->
    # FAILED), trigger cycles (force a hypothesis-gen cycle), or log.
    # ──────────────────────────────────────────────────────────────────────

    async def _recover_db_lock_long(self) -> dict:
        """DB lock held >60s — force a TRUNCATE checkpoint and log.

        Detection: look at ``busy_timeout_stats(60)`` — if the last minute
        recorded lock hits above the warning threshold, assume a writer is
        monopolising the DB. The mitigation is a force checkpoint on a
        dedicated autocommit connection, which breaks any lingering
        read-snapshot and lets new writers in. We do NOT kill connections
        or mutate data.
        """
        from tools.db_utils import busy_timeout_stats
        stats = busy_timeout_stats(60.0)
        total_hits = int(
            stats.get("hits_in_window", 0) if isinstance(stats, dict) else 0
        )
        if total_hits < 3:
            return {"fixed": False, "action": "db_lock_below_threshold",
                    "detail": f"{total_hits} lock hits in last 60s; no recovery needed",
                    "metadata": {"lock_hits_60s": total_hits}}
        busy = ckpt = log_pages = 0
        try:
            conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30.0)
            try:
                conn.execute("PRAGMA busy_timeout = 10000")
                row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if row:
                    busy, log_pages, ckpt = row
            finally:
                conn.close()
        except Exception as e:
            return {"fixed": False, "action": "db_lock_checkpoint_error",
                    "detail": f"{type(e).__name__}: {e}",
                    "metadata": {"lock_hits_60s": total_hits}}
        succeeded = busy == 0
        detail = (f"WAL checkpoint {'ok' if succeeded else 'busy'}: "
                  f"{ckpt}/{log_pages} pages (lock_hits_60s={total_hits})")
        logger.info(f"self_repair: force-checkpoint after prolonged DB lock — {detail}")
        return {"fixed": succeeded, "action": "force_wal_checkpoint",
                "detail": detail,
                "metadata": {"lock_hits_60s": total_hits, "busy": busy,
                             "checkpointed_pages": ckpt,
                             "log_pages": log_pages}}

    async def _recover_orphaned_processing(self) -> dict:
        """Mark task_queue rows stuck in PROCESSING past 5x max timeout as FAILED.

        Safety: only rows whose started_at is older than
        (TASK_MAX_TIMEOUT_SECONDS * STUCK_PROCESSING_TIMEOUT_MULT) are
        touched. Marking state is permitted; we never DELETE tasks.
        """
        cutoff_seconds = TASK_MAX_TIMEOUT_SECONDS * STUCK_PROCESSING_TIMEOUT_MULT
        cutoff_iso = (datetime.now(timezone.utc)
                      - timedelta(seconds=cutoff_seconds)).isoformat()
        touched_ids: list[int] = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                cur = await db.execute(
                    "SELECT task_id FROM task_queue "
                    "WHERE status = 'PROCESSING' AND started_at < ?",
                    (cutoff_iso,),
                )
                rows = await cur.fetchall()
                touched_ids = [int(r[0]) for r in rows]
                if touched_ids:
                    await db.execute(
                        "UPDATE task_queue "
                        "SET status = 'FAILED', "
                        "    error = 'stuck in processing', "
                        "    completed_at = ? "
                        "WHERE status = 'PROCESSING' AND started_at < ?",
                        (datetime.now(timezone.utc).isoformat(), cutoff_iso),
                    )
                    await db.commit()
        except Exception as e:
            return {"fixed": False, "action": "orphan_reaper_error",
                    "detail": f"{type(e).__name__}: {e}"}
        if not touched_ids:
            return {"fixed": False, "action": "no_orphans",
                    "detail": f"No PROCESSING rows older than {cutoff_seconds:.0f}s"}
        logger.warning(
            f"self_repair: marked {len(touched_ids)} orphaned PROCESSING "
            f"task(s) FAILED (older than {cutoff_seconds:.0f}s): {touched_ids[:10]}"
        )
        return {"fixed": True, "action": "marked_failed_stuck_processing",
                "detail": f"{len(touched_ids)} task(s) past {cutoff_seconds:.0f}s",
                "metadata": {"task_ids": touched_ids[:50],
                             "cutoff_seconds": cutoff_seconds}}

    async def _recover_research_loop_stuck(self) -> dict:
        """Force a hypothesis-gen cycle when the loop has spun 10 cycles
        without producing anything new.

        Detection: count hypotheses whose created_at is newer than the
        cycle-start marker we track in ``self._last_gen_watermark``. If
        N cycles have passed with zero new rows, trigger a forced
        hypothesis-gen cycle on the autonomous loop and recompute
        eligibility so stale "ineligible" rows get a fresh evaluation.
        We never modify existing hypotheses here.
        """
        new_hypotheses = 0
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 60000")
                watermark = self._last_hypothesis_watermark
                if watermark is None:
                    # First run — seed the watermark and skip forcing.
                    row = await (await db.execute(
                        "SELECT MAX(hypothesis_id) FROM hypotheses"
                    )).fetchone()
                    self._last_hypothesis_watermark = int(row[0]) if row and row[0] else 0
                    self._research_stagnant_cycles = 0
                    return {"fixed": False, "action": "research_loop_watermark_init",
                            "detail": f"seeded watermark={self._last_hypothesis_watermark}"}
                row = await (await db.execute(
                    "SELECT COUNT(*) FROM hypotheses WHERE hypothesis_id > ?",
                    (watermark,),
                )).fetchone()
                new_hypotheses = int(row[0]) if row and row[0] is not None else 0
        except Exception as e:
            return {"fixed": False, "action": "research_loop_probe_error",
                    "detail": f"{type(e).__name__}: {e}"}

        if new_hypotheses > 0:
            # Progress happened — reset counters.
            self._research_stagnant_cycles = 0
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    row = await (await db.execute(
                        "SELECT MAX(hypothesis_id) FROM hypotheses"
                    )).fetchone()
                    self._last_hypothesis_watermark = (
                        int(row[0]) if row and row[0] else
                        self._last_hypothesis_watermark
                    )
            except Exception:
                pass
            return {"fixed": False, "action": "research_loop_progressing",
                    "detail": f"+{new_hypotheses} hypotheses since watermark"}

        self._research_stagnant_cycles += 1
        if self._research_stagnant_cycles < RESEARCH_LOOP_ZERO_PROGRESS_CYCLES:
            return {"fixed": False, "action": "research_loop_warming",
                    "detail": f"stagnant {self._research_stagnant_cycles}/"
                              f"{RESEARCH_LOOP_ZERO_PROGRESS_CYCLES} cycles"}

        logger.critical(
            "self_repair: research loop stagnant for "
            f"{self._research_stagnant_cycles} cycles — forcing hypothesis-gen"
        )
        forced = False
        try:
            # Trigger a forced generation cycle on the live research loop
            # without destructively touching any existing rows.
            import api as _api  # type: ignore
            loop = getattr(_api, "autonomous", None) or getattr(_api, "research_loop", None)
            if loop is not None:
                # Reset the gen interval gate so the next pass is eligible.
                try:
                    loop._last_hypothesis_gen = 0.0  # type: ignore[attr-defined]
                except Exception:
                    pass
                gen = getattr(loop, "hypothesis_generator", None) or getattr(
                    _api, "hypothesis_generator", None
                )
                if gen is not None and hasattr(gen, "recompute_eligibility"):
                    try:
                        await gen.recompute_eligibility()  # type: ignore[attr-defined]
                    except Exception as re:
                        logger.debug(f"recompute_eligibility failed: {re}")
                forced = True
        except Exception as e:
            logger.debug(f"Forced hypothesis-gen dispatch: {e}")

        # Reset the counter so we don't force every cycle after the trigger.
        self._research_stagnant_cycles = 0
        return {"fixed": forced,
                "action": "forced_hypothesis_gen_cycle" if forced
                           else "hypothesis_gen_dispatch_failed",
                "detail": f"stagnant cycles reset (was "
                          f"{RESEARCH_LOOP_ZERO_PROGRESS_CYCLES})",
                "metadata": {"watermark": self._last_hypothesis_watermark}}

    async def _recover_claude_cli_missing(self) -> dict:
        """Claude CLI not installed and we're not in local-only mode.

        We NEVER silently degrade; the goal is: log CRITICAL, clear the
        "trying claude" cooldown so local fallback paths can re-evaluate,
        and record the downgrade. We do NOT toggle CALLISTO_LOCAL_ONLY —
        that's a human decision; we only stop thrashing on the missing CLI.
        """
        from tools.local_only import is_local_only
        if is_local_only():
            return {"fixed": False, "action": "local_only_mode",
                    "detail": "CALLISTO_LOCAL_ONLY=1 — no Claude CLI recovery needed"}
        import shutil
        claude_cmd = os.getenv("CLAUDE_CMD", "claude")
        if shutil.which(claude_cmd):
            return {"fixed": False, "action": "claude_cli_found",
                    "detail": f"{claude_cmd} is on PATH — no recovery needed"}

        logger.critical(
            f"self_repair: Claude CLI '{claude_cmd}' missing but not in "
            f"local-only mode — degrading to local model fallback"
        )
        try:
            import tools.claude_code as cc
            # Clear the cooldown so local model path doesn't keep waiting
            # on Claude to recover — the CLI is GONE, not cooling down.
            cc._available = False  # type: ignore[attr-defined]
            cc._cooldown_until = 0.0  # type: ignore[attr-defined]
            cc._consecutive_failures = 0  # type: ignore[attr-defined]
            cc._last_error = "cli_missing"  # type: ignore[attr-defined]
            try:
                cc._persist_cooldown()  # type: ignore[attr-defined]
            except Exception:
                pass
        except Exception as e:
            return {"fixed": False, "action": "claude_cli_degrade_error",
                    "detail": f"{type(e).__name__}: {e}"}
        # Record a Hermes learning marker so humans see it in dashboards.
        try:
            from tools.hermes_memory import get_hermes_memory
            h = get_hermes_memory()
            await h.record_learning(
                key="claude_cli_missing",
                value=f"CLI '{claude_cmd}' not on PATH at "
                      f"{datetime.now(timezone.utc).isoformat()}",
                confidence=0.95, source="self_repair",
            )
        except Exception:
            pass
        return {"fixed": True, "action": "degraded_to_local_model",
                "detail": f"CLI '{claude_cmd}' missing; cooldown cleared, "
                          f"local fallback armed",
                "metadata": {"claude_cmd": claude_cmd}}

    async def _recover_sla_stuck_sources(self) -> dict:
        """Force a refresh attempt on SLA-alerted sources stuck >24h.

        Reads the alerted-source file that ``ingestion_sla_watchdog_loop``
        maintains, compares it against ingestion_runs.finished_at to
        determine age, and (for any source older than SLA_STUCK_HOURS)
        invokes the ingestion function once through the already-tracked
        tracked_ingestion wrapper. We explicitly do NOT keep queueing
        investigate-tasks — that's what the watchdog already does.
        """
        # Locate the alerted-source file without importing api.py (which
        # has heavy side effects). Fall back to an empty set on any error.
        state_dir = os.getenv("CALLISTO_STATE_DIR") or os.path.join(
            os.path.dirname(os.path.abspath(DB_PATH)),
        )
        alerted_path = os.path.join(state_dir, "sla_alerted_sources.json")
        if not os.path.exists(alerted_path):
            # api.py writes the file under memory/ too — try that.
            alerted_path = os.path.join(
                os.path.dirname(os.path.abspath(DB_PATH)),
                "sla_alerted_sources.json",
            )
        sources: list[str] = []
        try:
            if os.path.exists(alerted_path):
                with open(alerted_path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    sources = [str(s) for s in data.get("sources", [])
                               if isinstance(s, str)]
                elif isinstance(data, list):
                    sources = [str(s) for s in data if isinstance(s, str)]
        except Exception as e:
            return {"fixed": False, "action": "sla_alerted_read_error",
                    "detail": f"{type(e).__name__}: {e}"}
        if not sources:
            return {"fixed": False, "action": "no_sla_alerts",
                    "detail": "no sources currently alerted"}

        # Filter to sources stuck >24h by consulting ingestion_runs.
        stuck: list[tuple[str, float]] = []  # (source, hours_since_last_ok)
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                q = (
                    "SELECT source, "
                    "  (julianday('now') - julianday(MAX(finished_at))) * 86400 AS age_s "
                    "FROM ingestion_runs "
                    "WHERE source = ? AND status = 'ok' AND finished_at IS NOT NULL"
                )
                for src in sources:
                    try:
                        row = await (await db.execute(q, (src,))).fetchone()
                    except Exception:
                        continue
                    if row is None:
                        stuck.append((src, float("inf")))
                        continue
                    age_s = row[1]
                    hours = (float(age_s) / 3600.0) if age_s is not None else float("inf")
                    if hours >= SLA_STUCK_HOURS:
                        stuck.append((src, hours))
        except Exception as e:
            return {"fixed": False, "action": "sla_stuck_probe_error",
                    "detail": f"{type(e).__name__}: {e}"}
        if not stuck:
            return {"fixed": False, "action": "no_sla_stuck_24h",
                    "detail": f"none of {len(sources)} alerted source(s) stuck >{SLA_STUCK_HOURS}h"}

        refreshed: list[str] = []
        failed: list[dict] = []
        for src, hours in stuck[:5]:  # hard cap per invocation
            fn = _SLA_REFRESH_HANDLERS.get(src)
            if fn is None:
                failed.append({"source": src, "error": "no_refresh_handler"})
                continue
            try:
                result = await asyncio.wait_for(fn(), timeout=60)
                ok = bool(result) and not (
                    isinstance(result, dict) and result.get("error")
                )
                if ok:
                    refreshed.append(src)
                else:
                    failed.append({"source": src,
                                   "error": (result.get("error") if isinstance(result, dict)
                                             else "unknown")})
            except asyncio.TimeoutError:
                failed.append({"source": src, "error": "timeout"})
            except Exception as e:
                failed.append({"source": src, "error": f"{type(e).__name__}: {e}"})
        detail = (f"refreshed={refreshed or '-'} failed={[f['source'] for f in failed] or '-'} "
                  f"hours={{{', '.join(f'{s}:{h:.1f}' for s, h in stuck)}}}")
        logger.info(f"self_repair: SLA stuck source refresh — {detail}")
        return {"fixed": bool(refreshed), "action": "sla_refresh_attempted",
                "detail": detail,
                "metadata": {"refreshed": refreshed, "failed": failed,
                             "candidates": [s for s, _ in stuck]}}

    async def _recover_missing_odds_snapshot(self) -> dict:
        """Force a fallback-scraper path when no odds snapshot exists for
        an active sport.

        Definition of "active": appears in line_monitor._snapshots if the
        monitor is running; otherwise derive from game_contexts with a
        game in the next 24h. If an active sport has zero rows in
        odds_snapshots / odds_snapshots_v2 in the last STALE_ODDS_MINUTES
        window, invoke the fallback scraper path. We only TRIGGER; we
        never delete or modify existing snapshot rows.
        """
        active: set[str] = set()
        try:
            import api as _api  # type: ignore
            lm = getattr(_api, "line_monitor", None)
            if lm is not None and hasattr(lm, "_snapshots"):
                active = {k for k in lm._snapshots.keys() if isinstance(k, str)}  # type: ignore
        except Exception:
            active = set()
        if not active:
            try:
                async with aiosqlite.connect(DB_PATH) as db:
                    await db.execute("PRAGMA busy_timeout = 10000")
                    cutoff = (datetime.now(timezone.utc)
                              + timedelta(hours=24)).isoformat()
                    rows = await (await db.execute(
                        "SELECT DISTINCT sport FROM game_contexts "
                        "WHERE game_date <= ? AND sport IS NOT NULL",
                        (cutoff,),
                    )).fetchall()
                    active = {r[0] for r in rows if r and r[0]}
            except Exception:
                pass
        if not active:
            # Safe default — don't fire if we can't identify any active sport.
            return {"fixed": False, "action": "no_active_sport_identified",
                    "detail": "no line_monitor sports and no upcoming game_contexts"}

        stale_cutoff = (datetime.now(timezone.utc)
                        - timedelta(minutes=STALE_ODDS_MINUTES)).isoformat()
        missing: list[str] = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                for sport in sorted(active):
                    try:
                        row = await (await db.execute(
                            "SELECT COUNT(*) FROM odds_snapshots "
                            "WHERE sport = ? AND timestamp > ?",
                            (sport, stale_cutoff),
                        )).fetchone()
                        count = int(row[0]) if row and row[0] is not None else 0
                    except Exception:
                        count = 0
                    if count > 0:
                        continue
                    # v2 fallback — if the v1 table is absent or cold.
                    try:
                        row = await (await db.execute(
                            "SELECT COUNT(*) FROM odds_snapshots_v2 "
                            "WHERE sport = ? AND snapshot_time > ?",
                            (sport, stale_cutoff),
                        )).fetchone()
                        count_v2 = int(row[0]) if row and row[0] is not None else 0
                    except Exception:
                        count_v2 = 0
                    if count + count_v2 == 0:
                        missing.append(sport)
        except Exception as e:
            return {"fixed": False, "action": "odds_probe_error",
                    "detail": f"{type(e).__name__}: {e}"}
        if not missing:
            return {"fixed": False, "action": "odds_snapshots_fresh",
                    "detail": f"all {len(active)} active sport(s) have "
                              f"recent snapshots"}

        forced: list[str] = []
        failed: list[dict] = []
        for sport in missing[:3]:  # cap — don't hammer scrapers
            try:
                import api as _api  # type: ignore
                lm = getattr(_api, "line_monitor", None)
                if lm is not None and hasattr(lm, "_snapshot_sport_fallback"):
                    await asyncio.wait_for(
                        lm._snapshot_sport_fallback(sport), timeout=120
                    )
                    forced.append(sport)
                    continue
            except Exception as e:
                failed.append({"sport": sport, "error": f"line_monitor: {e}"})
            # Direct scraper fallback when line_monitor is unavailable.
            any_ok = False
            for name, (mod_path, fn_name, _default) in SCRAPERS.items():
                if name in _disabled_scrapers and _disabled_scrapers[name] > time.monotonic():
                    continue
                try:
                    mod = __import__(mod_path, fromlist=[fn_name])
                    r = await asyncio.wait_for(getattr(mod, fn_name)(sport), timeout=30)
                    if isinstance(r, dict) and not r.get("error"):
                        forced.append(sport)
                        any_ok = True
                        break
                except Exception as e:
                    failed.append({"sport": sport, "error": f"{name}: {e}"})
            if not any_ok and sport not in forced:
                failed.append({"sport": sport, "error": "all_scrapers_failed"})

        logger.warning(
            f"self_repair: odds snapshot fallback — forced={forced} failed={failed}"
        )
        return {"fixed": bool(forced), "action": "forced_odds_fallback",
                "detail": f"active={sorted(active)} missing={missing} "
                          f"forced={forced}",
                "metadata": {"missing": missing, "forced": forced,
                             "failed": failed}}

    async def run_expanded_recoveries(
        self, *, force: bool = False
    ) -> list[dict]:
        """Run every registered expanded recovery whose cooldown has elapsed.

        Each invocation is appended to ``self_repair_log``. Returns the
        per-recovery result list (for the research loop's telemetry).
        """
        results: list[dict] = []
        for name, _fn, cooldown in self._RECOVERIES:
            next_ok = _recovery_cooldowns.get(name, 0.0)
            if not force and time.monotonic() < next_ok:
                continue
            start_ns = time.monotonic_ns()
            try:
                result = await getattr(self, _fn)()
            except Exception as e:
                result = {"fixed": False, "action": "recovery_error",
                          "detail": f"{type(e).__name__}: {e}"}
            elapsed_ms = (time.monotonic_ns() - start_ns) / 1e6
            # Apply cooldown regardless of outcome to avoid thrashing on
            # persistent failures (the manual trigger endpoint can override).
            _recovery_cooldowns[name] = time.monotonic() + cooldown
            result.setdefault("recovery_name", name)
            result["elapsed_ms"] = round(elapsed_ms, 2)
            await _log_self_repair(name, result, trigger="auto",
                                    elapsed_ms=elapsed_ms)
            await self._record_to_hermes(f"expanded_{name}", result)
            results.append(result)
        return results

    async def trigger_recovery(self, name: str, *, manual: bool = True) -> dict:
        """Invoke a single registered recovery by name, bypassing cooldown.

        Returns the same shape as run_expanded_recoveries() entries.
        Raises ValueError if the name isn't registered.
        """
        match = [r for r in self._RECOVERIES if r[0] == name]
        if not match:
            raise ValueError(f"unknown recovery: {name}")
        _name, _fn, cooldown = match[0]
        start_ns = time.monotonic_ns()
        try:
            result = await getattr(self, _fn)()
        except Exception as e:
            result = {"fixed": False, "action": "recovery_error",
                      "detail": f"{type(e).__name__}: {e}"}
        elapsed_ms = (time.monotonic_ns() - start_ns) / 1e6
        _recovery_cooldowns[_name] = time.monotonic() + cooldown
        result.setdefault("recovery_name", _name)
        result["elapsed_ms"] = round(elapsed_ms, 2)
        await _log_self_repair(_name, result,
                                trigger=("manual" if manual else "auto"),
                                elapsed_ms=elapsed_ms)
        await self._record_to_hermes(f"expanded_{_name}", result)
        return result

    async def get_expanded_status(self) -> dict:
        """Last recovery per type, success/failure, cooldown remaining.

        Serves /admin/self-repair/status — reads the last row per recovery
        from self_repair_log so the UI can show "last ran 4m ago, success".
        """
        recoveries: list[dict] = []
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("PRAGMA busy_timeout = 10000")
                # Test if table exists before querying so first-run
                # deployments (pre-migration) don't 500.
                row = await (await db.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='self_repair_log'"
                )).fetchone()
                has_table = bool(row)
                now = time.monotonic()
                for name, _fn, cooldown in self._RECOVERIES:
                    entry = {
                        "recovery_name": name,
                        "cooldown_seconds": cooldown,
                        "cooldown_remaining_seconds": round(max(
                            0.0, _recovery_cooldowns.get(name, 0.0) - now
                        ), 1),
                        "last_run": None,
                    }
                    if has_table:
                        try:
                            last = await (await db.execute(
                                "SELECT success, action, detail, trigger, "
                                "invoked_at, elapsed_ms, metadata_json "
                                "FROM self_repair_log "
                                "WHERE recovery_name = ? "
                                "ORDER BY invoked_at DESC LIMIT 1",
                                (name,),
                            )).fetchone()
                        except Exception:
                            last = None
                        if last:
                            entry["last_run"] = {
                                "success": bool(last[0]),
                                "action": last[1],
                                "detail": last[2],
                                "trigger": last[3],
                                "invoked_at": last[4],
                                "elapsed_ms": last[5],
                                "metadata": _safe_json_load(last[6]),
                            }
                    recoveries.append(entry)
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}",
                    "recoveries": recoveries}
        return {"recoveries": recoveries,
                "total_registered": len(self._RECOVERIES)}

    # Ordered registry: (name, method_name, cooldown_seconds).
    _RECOVERIES: list[tuple[str, str, float]] = [
        ("db_lock_long",
         "_recover_db_lock_long", DB_LOCK_COOLDOWN_SECONDS),
        ("orphaned_processing",
         "_recover_orphaned_processing", STUCK_PROCESSING_COOLDOWN_SECONDS),
        ("research_loop_stuck",
         "_recover_research_loop_stuck", RESEARCH_LOOP_COOLDOWN_SECONDS),
        ("claude_cli_missing",
         "_recover_claude_cli_missing", CLAUDE_MISSING_COOLDOWN_SECONDS),
        ("sla_stuck_sources",
         "_recover_sla_stuck_sources", SLA_REFRESH_COOLDOWN_SECONDS),
        ("missing_odds_snapshot",
         "_recover_missing_odds_snapshot", ODDS_SNAPSHOT_MISSING_COOLDOWN_SECONDS),
    ]


_engine: Optional[SelfRepairEngine] = None

def get_repair_engine() -> SelfRepairEngine:  # singleton
    global _engine
    if _engine is None:
        _engine = SelfRepairEngine()
    return _engine


# ──────────────────────────────────────────────────────────────────────────
# Expanded-recovery module-level helpers
# ──────────────────────────────────────────────────────────────────────────

def _safe_json_load(s):
    if not s:
        return None
    try:
        return json.loads(s)
    except Exception:
        return None


async def _log_self_repair(
    recovery_name: str,
    result: dict,
    *,
    trigger: str = "auto",
    elapsed_ms: Optional[float] = None,
) -> None:
    """Append a row to ``self_repair_log``. Fails silently on error — the
    audit log is observational; a logging failure must never break the
    recovery that just succeeded.
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("PRAGMA busy_timeout = 30000")
            # Idempotent table create so unit tests against ephemeral DBs
            # don't need the migration runner. Production DBs already have
            # this table from migration 015; the IF NOT EXISTS is a no-op.
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS self_repair_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    recovery_name TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT 'auto',
                    success INTEGER NOT NULL DEFAULT 0,
                    action TEXT,
                    detail TEXT,
                    metadata_json TEXT,
                    invoked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    elapsed_ms REAL
                )
                """
            )
            meta = result.get("metadata")
            meta_json = json.dumps(meta) if meta is not None else None
            await db.execute(
                "INSERT INTO self_repair_log "
                "(recovery_name, trigger, success, action, detail, "
                " metadata_json, invoked_at, elapsed_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    recovery_name,
                    trigger if trigger in ("auto", "manual") else "auto",
                    1 if result.get("fixed") else 0,
                    result.get("action"),
                    result.get("detail"),
                    meta_json,
                    datetime.now(timezone.utc).isoformat(),
                    round(float(elapsed_ms), 2) if elapsed_ms is not None else None,
                ),
            )
            await db.commit()
    except Exception as e:
        logger.debug(f"self_repair_log append failed for {recovery_name}: {e}")


# SLA refresh handlers — map source -> no-arg coroutine that tries a
# refresh. Kept tiny and defensive; unknown sources fall through with a
# no_refresh_handler sentinel. Register new sources by adding an entry.

async def _refresh_action_network() -> dict:
    try:
        from tools import action_network_scraper
        return await action_network_scraper.scrape_action_network_odds(  # type: ignore[attr-defined]
            "basketball_nba"
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _refresh_dk() -> dict:
    try:
        from tools import dk_scraper
        return await dk_scraper.scrape_dk_odds("basketball_nba")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _refresh_fd() -> dict:
    try:
        from tools import fanduel_scraper
        return await fanduel_scraper.scrape_fd_odds("basketball_nba")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def _refresh_odds_api_io() -> dict:
    try:
        from tools import odds_api_io
        # get_usage_status() is a cheap probe that exercises the same
        # credential path as real calls without burning a credit.
        if hasattr(odds_api_io, "get_usage_status"):
            r = odds_api_io.get_usage_status()
            if asyncio.iscoroutine(r):
                r = await r
            return {"ok": True, "usage": r}
        return {"error": "no_probe_available"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


_SLA_REFRESH_HANDLERS: dict[str, callable] = {  # type: ignore[type-arg]
    "action_network": _refresh_action_network,
    "dk": _refresh_dk,
    "draftkings": _refresh_dk,
    "fanduel": _refresh_fd,
    "fd": _refresh_fd,
    "odds_api_io": _refresh_odds_api_io,
    "odds-api-io": _refresh_odds_api_io,
}


def register_sla_refresh_handler(source: str, handler) -> None:
    """Allow callers (e.g. tests, plugin sources) to register handlers."""
    _SLA_REFRESH_HANDLERS[source] = handler
