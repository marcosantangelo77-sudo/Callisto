"""Issue detectors for the self-repair engine (mixin)."""

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from .config import (
    DB_BLOAT_ROWS,
    DB_PATH,
    EMPTY_BACKTEST_LOOKBACK,
    REJECTION_RATE_THRESHOLD,
    SCRAPERS,
    SIGNAL_DROUGHT_EVENTS,
    STALE_ODDS_MINUTES,
    _disabled_scrapers,
)

logger = logging.getLogger("callisto.self_repair")


class DetectorsMixin:
    """Detectors: each returns an issue dict or None. Never mutates state."""

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
