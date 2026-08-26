"""
tools.hypothesis.store — HypothesisManager CRUD/storage methods (mixin).

Split out of tools/hypothesis.py (facade re-exports everything).
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import aiosqlite

from tools.hypothesis.config import (
    DB_PATH,
    STAGE_ORDER,
    validate_model_config,
)
from tools.hypothesis.sharpening import _fire_sharpening_hook

logger = logging.getLogger("callisto.hypothesis")


class HypothesisStoreMixin:

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        self._db = await aiosqlite.connect(self.db_path)
        # Tag for WriteCoordinator routing (single-writer pattern).
        from tools.db_writer import tag_connection as _tag
        _tag(self._db, self.db_path)
        await self._db.execute("PRAGMA journal_mode = WAL")
        await self._db.execute("PRAGMA wal_autocheckpoint = 1000")
        await self._db.execute("PRAGMA journal_size_limit = 67108864")
        await self._db.execute("PRAGMA busy_timeout = 120000")
        logger.info("Hypothesis manager initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── CRUD ──

    async def create_hypothesis(
        self,
        name: str,
        thesis: str,
        sport: str,
        market_type: str,
        model_config: dict,
        edge_threshold: float = 0.005,
        min_sample_size: int = 50,
        significance_level: float = 0.05,
        notes: str = "",
    ) -> str:
        """Create a new hypothesis. Returns hypothesis_id.

        If a hypothesis with the same name already exists, returns the existing
        hypothesis_id instead of creating a duplicate.

        Temporal metadata in model_config (set by temporal_analysis.py):
            - training_period_start: First date used for pattern discovery
            - training_period_end: Last date used for pattern discovery
            - temporal_split_gap_days: Buffer days between train/test (default 7)

        These fields are used by backtest.py to enforce temporal isolation.
        """
        # ── Deduplication guard: skip if name already exists ──
        cursor = await self._db.execute(
            "SELECT hypothesis_id FROM hypotheses WHERE name = ? LIMIT 1",
            (name,),
        )
        existing = await cursor.fetchone()
        if existing:
            logger.debug(
                f"Hypothesis '{name}' already exists as {existing[0]} — skipping duplicate"
            )
            return existing[0]

        # ── Duplicate game_filters guard: reject if same sport+market+filters already active ──
        new_gf = model_config.get("game_filters") if model_config else None
        new_gf_normalized = json.dumps(new_gf, sort_keys=True) if new_gf else None

        dup_cursor = await self._db.execute(
            "SELECT hypothesis_id, name, model_config FROM hypotheses "
            "WHERE sport = ? AND market_type = ? AND status IN ('draft', 'backtesting', 'paper_trading')",
            (sport, market_type),
        )
        dup_rows = await dup_cursor.fetchall()
        for row in dup_rows:
            existing_mc = json.loads(row[2]) if row[2] else {}
            existing_gf = existing_mc.get("game_filters")
            existing_gf_normalized = json.dumps(existing_gf, sort_keys=True) if existing_gf else None

            if new_gf_normalized == existing_gf_normalized:
                logger.warning(
                    f"DUPLICATE game_filters blocked: '{name}' has identical "
                    f"sport={sport}, market_type={market_type}, "
                    f"game_filters={new_gf_normalized or 'null'} "
                    f"as existing hypothesis '{row[1]}' ({row[0]}). Skipping creation."
                )
                return row[0]

        hid = str(uuid.uuid4())[:12]
        now = datetime.now(timezone.utc).isoformat()

        # Log whether temporal metadata is present
        has_temporal = bool(model_config.get("training_period_end"))
        if not has_temporal:
            logger.warning(
                f"Hypothesis '{name}' created WITHOUT temporal metadata in model_config. "
                "Backtest engine will not enforce temporal isolation. "
                "Use temporal_analysis.generate_hypotheses_from_analysis() to auto-populate."
            )
        else:
            logger.info(
                f"Hypothesis '{name}' has temporal metadata: "
                f"training {model_config.get('training_period_start')} "
                f"to {model_config.get('training_period_end')}, "
                f"gap {model_config.get('temporal_split_gap_days', 7)}d"
            )

        for attempt in range(8):
            try:
                await self._db.execute(
                    "INSERT INTO hypotheses "
                    "(hypothesis_id, name, thesis, sport, market_type, model_config, "
                    "edge_threshold, status, min_sample_size, significance_level, "
                    "created_at, updated_at, notes) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?)",
                    (hid, name, thesis, sport, market_type, json.dumps(model_config),
                     edge_threshold, min_sample_size, significance_level, now, now, notes),
                )
                await self._db.commit()
                logger.info(f"Hypothesis created: {hid} — {name}")
                return hid
            except Exception as e:
                if "locked" in str(e).lower() and attempt < 7:
                    import asyncio
                    # Jittered backoff: 0.5, 1, 2, 4, 8, 16, 32s (total ~63s)
                    import random
                    wait = min(0.5 * (2 ** attempt), 32) + random.uniform(0, 0.5)
                    logger.warning(f"DB locked on hypothesis create (attempt {attempt+1}/8), retrying in {wait:.1f}s")
                    await asyncio.sleep(wait)
                else:
                    raise

    async def get_hypothesis(self, hypothesis_id: str) -> Optional[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM hypotheses WHERE hypothesis_id = ?",
            (hypothesis_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        cols = [d[0] for d in cursor.description]
        h = dict(zip(cols, row))
        h["model_config"] = json.loads(h["model_config"]) if h["model_config"] else {}
        return h

    async def list_hypotheses(self, status: Optional[str] = None, limit: int = None) -> list[dict]:
        if status:
            query = "SELECT * FROM hypotheses WHERE status = ? ORDER BY updated_at DESC"
            params: tuple = (status,)
        else:
            query = "SELECT * FROM hypotheses ORDER BY updated_at DESC"
            params = ()
        if limit:
            query += f" LIMIT {int(limit)}"
        cursor = await self._db.execute(query, params)
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        result = []
        for row in rows:
            h = dict(zip(cols, row))
            h["model_config"] = json.loads(h["model_config"]) if h["model_config"] else {}
            result.append(h)
        return result

    async def count_by_status(self, *statuses: str) -> int:
        """Count hypotheses by status without loading full rows."""
        placeholders = ",".join("?" for _ in statuses)
        cursor = await self._db.execute(
            f"SELECT COUNT(*) FROM hypotheses WHERE status IN ({placeholders})",
            statuses,
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_names(self, status: Optional[str] = None) -> list[str]:
        """Get just hypothesis names (not full rows)."""
        if status:
            cursor = await self._db.execute(
                "SELECT name FROM hypotheses WHERE status = ?", (status,)
            )
        else:
            cursor = await self._db.execute("SELECT name FROM hypotheses")
        return [row[0] for row in await cursor.fetchall()]

    async def get_all_names(self) -> set[str]:
        """Get all hypothesis names as a set for dedup checks."""
        cursor = await self._db.execute("SELECT name FROM hypotheses")
        return {row[0] for row in await cursor.fetchall()}

    async def update_status(
        self,
        hypothesis_id: str,
        new_status: str,
        promoted_by: str = "manual",
        *,
        expected_status: Optional[str] = None,
    ) -> dict:
        """Move a hypothesis to a new status.

        SECURITY (audit C-7 + 2026-04-21): ``expected_status`` enables CAS —
        the UPDATE is scoped with ``WHERE status = ?`` so two concurrent
        promoters can't both succeed. Returns ``{"changed": False, ...}`` if the
        row was already moved by another worker.

        As of 2026-04-21 the ``expected_status=None`` legacy path still works
        but logs a WARNING on every call — all autonomous-loop callers have
        been migrated to CAS. Manual admin patches may still use None
        intentionally; audit the log to find any stragglers.
        """
        now = datetime.now(timezone.utc).isoformat()
        from tools.db_utils import execute_with_retry, commit_with_retry
        prev_status = expected_status
        if expected_status is None:
            logger.warning(
                f"update_status called WITHOUT expected_status for "
                f"{hypothesis_id} → {new_status} (by {promoted_by}). "
                f"Concurrent promoters may overwrite each other. "
                f"Migrate caller to CAS by passing expected_status=<current>."
            )
        if expected_status is not None:
            cursor = await execute_with_retry(
                self._db,
                "UPDATE hypotheses SET status = ?, updated_at = ?, "
                "promoted_at = ?, promoted_by = ? "
                "WHERE hypothesis_id = ? AND status = ?",
                (new_status, now, now, promoted_by, hypothesis_id, expected_status),
                operation="hypothesis update_status (cas)",
            )
            await commit_with_retry(self._db, operation="hypothesis update_status (cas)")
            changed = (cursor.rowcount or 0) > 0
            if not changed:
                logger.info(
                    f"Hypothesis {hypothesis_id}: status CAS no-op — expected "
                    f"{expected_status!r}, row already moved (concurrent promote race)"
                )
            else:
                logger.info(
                    f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by}, expected={expected_status!r})"
                )
                _fire_sharpening_hook(self, hypothesis_id, expected_status, new_status)
            return {
                "hypothesis_id": hypothesis_id,
                "new_status": new_status,
                "changed": changed,
                "expected_status": expected_status,
            }
        await execute_with_retry(
            self._db,
            "UPDATE hypotheses SET status = ?, updated_at = ?, "
            "promoted_at = ?, promoted_by = ? WHERE hypothesis_id = ?",
            (new_status, now, now, promoted_by, hypothesis_id),
            operation="hypothesis update_status",
        )
        await commit_with_retry(self._db, operation="hypothesis update_status")
        logger.info(f"Hypothesis {hypothesis_id} → {new_status} (by {promoted_by})")
        _fire_sharpening_hook(self, hypothesis_id, prev_status, new_status)
        return {"hypothesis_id": hypothesis_id, "new_status": new_status, "changed": True}

    # ── STATISTICAL EVALUATION ──

