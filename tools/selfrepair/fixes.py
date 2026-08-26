"""Repair strategies (fixers) for the self-repair engine (mixin).

Gate policy note: the former threshold-lowering paths are kept as explicit
refusers so any stale caller fails safe. See gate_policy.py.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone

import aiosqlite

from .config import (
    BETMGM_ALT_SUBDOMAINS,
    DB_PATH,
    PRUNE_SAFE,
    SCRAPERS,
    SCRAPER_DISABLE_SECONDS,
    _disabled_scrapers,
)
from .gate_policy import ALLOW_REQUEUE_ENV

logger = logging.getLogger("callisto.self_repair")


class FixesMixin:
    """Repair strategies: each takes an issue dict, returns a result dict."""

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
        if fixed:
            parts.append(f"Fixed: {','.join(fixed)}")
        if disabled:
            parts.append(f"Disabled: {','.join(disabled)}")
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
                    if table not in PRUNE_SAFE:
                        continue
                    col, days = PRUNE_SAFE[table]
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
