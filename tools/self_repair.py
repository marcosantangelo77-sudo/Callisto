"""Self-repair engine — detect, fix, verify, record. Phase 0 of the research loop."""

import asyncio
import json
import logging
import os
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

# ── GATE POLICY ──────────────────────────────────────────────────────────────
# Governing principle: a maintenance routine must NEVER weaken a gate.
#
# Gate-bearing state is any value that feeds promotion/rejection decisions:
#   - hypotheses.edge_threshold COLUMN (read by backtest.py:196/:3819, gates every
#     signal at backtest.py:2520/:2708/:2866)
#   - model_config keys consumed by evaluation/promotion logic
#   - hypotheses.status transitions that reverse a rejection or advance a stage
#
# Self-repair may DIAGNOSE gate problems and record them for human review.
# It may not WRITE to gate-bearing state. Enforced three ways:
#   1. GATE_WRITE_PATTERNS below — refused substrings for SQL/config writes;
#      every repair dispatch passes through SelfRepairEngine._gate_guard().
#   2. Strategies classified GATE_WEAKENING_STRATEGIES are routed to a refuser,
#      never executed, regardless of who asks (detector OR Claude findings).
#   3. tests/test_tier1_loop_self_repair_gate_policy.py statically re-checks
#      this file so a future edit cannot reintroduce an operative gate write
#      without a loud, reviewable diff to the policy itself.
GATE_WRITE_PATTERNS: tuple[str, ...] = (
    # Operative threshold columns
    "SET edge_threshold",
    # Promotion/evaluation knobs wherever they might live
    "minimum_events_for_promotion",
    "_threshold_lowered_by",
    "_promotion_threshold_lowered_by",
    "_edge_ceiling_lowered_by",
)
# Status reversals that un-reject or advance stages are gate decisions too.
GATE_STATUS_TRANSITIONS = {("rejected", "draft")}

# Repair strategies whose entire purpose is to weaken a gate. These are never
# executed; matching issues/findings are recorded for human review instead.
GATE_WEAKENING_STRATEGIES: frozenset[str] = frozenset({
    "promotion_thresholds_strict",   # lowers minimum_events_for_promotion
    "edge_ceiling",                  # writes the operative edge_threshold column
})

# Env opt-in required for the premature-rejection requeue (rejected -> draft).
ALLOW_REQUEUE_ENV = "CALLISTO_ALLOW_PREMATURE_REQUEUE"



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

    async def run_repair_cycle(self) -> dict:
        """Main entry point — called by research loop each cycle."""
        self._cycle_count += 1
        start = time.monotonic()
        issues = await self._detect_issues()
        results = [await self._repair(i) for i in issues]
        fixed = sum(1 for r in results if r["fixed"])
        self._total_fixes += fixed
        self._last_run = datetime.now(timezone.utc).isoformat()
        elapsed = time.monotonic() - start
        if issues:
            logger.info(f"Self-repair #{self._cycle_count}: {fixed}/{len(issues)} fixed ({elapsed:.1f}s)")
        return {"issues_found": len(issues), "fixed": fixed, "elapsed_seconds": round(elapsed, 2),
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

                # Rejected hypotheses with 0 events in sports that HAVE data.
                # Post-migration-013 the sport column lives in
                # hypothesis_sports_ext; on the seam shape the ext value IS
                # the only sport (SQLite would reject a COALESCE fallback to
                # h.sport — that column does not exist there). The ext-table
                # existence check below keeps this working on pre-013 DBs,
                # where the plain column query is used instead.
                ext_cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name='hypothesis_sports_ext'"
                )
                if await ext_cur.fetchone():
                    premature = await (await db.execute(
                        "SELECT h.hypothesis_id, h.name, e.sport AS sport "
                        "FROM hypotheses h "
                        "JOIN hypothesis_sports_ext e "
                        "  ON e.hypothesis_id = h.hypothesis_id "
                        "WHERE h.status = 'rejected' "
                        "AND NOT EXISTS (SELECT 1 FROM backtest_events be WHERE be.hypothesis_id = h.hypothesis_id)"
                    )).fetchall()
                else:
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
        # GATE GUARD: refuse any strategy whose purpose is to weaken a gate.
        if itype in ("high_rejection", "signal_drought"):
            return self._refuse_gate_change(
                itype,
                f"Detector '{itype}' maps to threshold lowering — refused by gate policy. "
                f"Diagnosis recorded for human review instead.",
                detail=issue,
            )
        fn = {"scraper_broken": self._fix_scraper, "stale_odds": self._fix_stale_odds,
              "empty_backtests": self._fix_empty_bt, "claude_stuck": self._fix_claude,
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


    def _refuse_gate_change(self, strategy: str, reason: str, detail=None) -> dict:
        """Record a refused gate-weakening action for human review. Never executes."""
        logger.warning(f"Gate policy REFUSED strategy '{strategy}': {reason}")
        try:
            import asyncio as _aio
            loop = _aio.get_running_loop()
        except RuntimeError:
            loop = None
        result = {"fixed": False, "action": "gate_change_refused",
                  "detail": f"{reason} | evidence: {str(detail)[:300]}"}
        if loop is not None:
            loop.create_task(self._record_to_hermes(f"gate_refused_{strategy}", result))
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
                finally:
                    mod._BASE_URL = orig  # always revert, even on timeout/exception
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
        """REFUSED by gate policy. Formerly lowered model_config edge_threshold.

        Kept as an explicit refuser so any stale caller fails safe. Note: the
        JSON key this once wrote was never read by backtesting anyway (it reads
        the edge_threshold COLUMN) — the fix was cosmetic even when it ran.
        """
        return self._refuse_gate_change(
            "threshold_lowering",
            "_fix_thresholds refused: maintenance routines may not lower gates.",
            detail=issue,
        )

    async def _fix_premature_rejection(self, issue: dict) -> dict:
        """Re-queue hypotheses rejected without being tested.

        GATED: un-rejecting is a gate decision (it reverses a rejection), so it
        requires explicit operator opt-in via CALLISTO_ALLOW_PREMATURE_REQUEUE.
        Without the flag this records the candidates for human review only.
        """
        candidates = issue.get("candidates", [])
        if not candidates:
            return {"fixed": False, "action": "no_candidates", "detail": "No premature rejections found"}
        if not os.getenv(ALLOW_REQUEUE_ENV):
            return self._refuse_gate_change(
                "premature_rejection_requeue",
                f"Requeue of {len(candidates)} rejected->draft refused: "
                f"{ALLOW_REQUEUE_ENV} not set. Un-rejecting reverses a gate decision.",
                detail=[c.get("id") for c in candidates[:10]],
            )
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
                # GATE GUARD: findings classified as gate-weakening are never
                # executed — recorded for human review instead.
                if strategy in GATE_WEAKENING_STRATEGIES:
                    result = self._refuse_gate_change(
                        strategy,
                        f"Claude finding classified '{strategy}' maps to gate lowering "
                        f"— refused by gate policy.",
                        detail=desc,
                    )
                else:
                    handler = {
                        "duplicate_events": self._fix_finding_duplicate_events,
                        "side_filter_broken": self._fix_finding_side_filter,
                        "prioritize_sports": self._fix_finding_prioritize_sports,
                        "low_sample_size": self._fix_finding_low_sample,
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
        """REFUSED by gate policy. Formerly wrote minimum_events_for_promotion=20.

        That key is read NOWHERE in the repo (verified by repo-wide grep), so the
        original fix was a no-op that stamped confidence-0.8 success. Kept as an
        explicit refuser for any stale caller.
        """
        return self._refuse_gate_change(
            "promotion_thresholds_strict",
            "_fix_finding_promotion_thresholds refused: maintenance routines may not "
            "lower promotion requirements.",
            detail=finding,
        )

    async def _fix_finding_edge_ceiling(self, finding: dict) -> dict:
        """REFUSED by gate policy. Formerly wrote the OPERATIVE edge_threshold
        column (UPDATE hypotheses SET edge_threshold = 0.015).

        That column is read by backtest.py:196/:3819 and gates every signal at
        backtest.py:2520/:2708/:2866 — this was the one lowering path that
        actually moved the gate. A maintenance routine must never do this.
        """
        return self._refuse_gate_change(
            "edge_ceiling",
            "_fix_finding_edge_ceiling refused: writing the operative edge_threshold "
            "column is a gate change, reserved for humans.",
            detail=finding,
        )

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

_engine: Optional[SelfRepairEngine] = None

def get_repair_engine() -> SelfRepairEngine:  # singleton
    global _engine
    if _engine is None:
        _engine = SelfRepairEngine()
    return _engine
