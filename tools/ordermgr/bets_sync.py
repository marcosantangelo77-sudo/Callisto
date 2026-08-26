"""Bets-table sync helpers for the CLV pipeline.

Extracted from ``tools.order_manager`` — these write into the legacy
``bets`` table so CLV tracking keeps working when orders fill/settle.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tools.ordermgr.models import Order

logger = logging.getLogger("callisto.orders")


def _is_aiosqlite(db) -> bool:
    return hasattr(db, "_conn") or type(db).__module__.startswith("aiosqlite")


async def _exec(db, sql: str, params=()):
    if _is_aiosqlite(db):
        return await db.execute(sql, tuple(params))
    return db.execute(sql, tuple(params))


async def _commit(db):
    if _is_aiosqlite(db):
        await db.commit()
    else:
        db.commit()


async def sync_bets_on_fill(db, order: "Order") -> None:
    """Insert a row in ``bets`` matching this filled order so the
    existing CLV pipeline (tools/clv_tracker.record_closing_line) will
    backfill closing lines and compute CLV automatically.

    Schema gap noted in the audit: ``bets`` has no ``signal_id`` or
    ``odds_snapshot_id`` column. We write the order_id into ``notes``
    and ``tags`` so the join can be recovered.
    """
    if order.bet_id:
        return  # already synced
    try:
        now = datetime.now(timezone.utc).isoformat()
        implied = None
        if order.fair_prob is not None and order.edge is not None:
            implied = max(0.0, min(1.0, order.fair_prob - order.edge))
        cursor = await _exec(
            db,
            """
            INSERT INTO bets (
                placed_at, sport, event_id, game_description, bet_type,
                team, market, bookmaker, placement_odds, placement_point,
                placement_implied_prob, stake, result,
                edge_at_placement, kelly_at_placement, notes, tags
            ) VALUES (?, ?, ?, ?, 'single', ?, ?, ?, ?, NULL, ?, ?, 'pending', ?, NULL, ?, ?)
            """,
            (
                order.placed_at or now, order.sport or "", order.event_id or "",
                order.game_description or "",
                order.side or "", order.market or "", order.book or "",
                order.price_american or 0,
                implied, order.stake_dollars or 0.0,
                order.edge,
                f"order_id={order.order_id} hypothesis={order.hypothesis_id}",
                f"order:{order.order_id},hypothesis:{order.hypothesis_id}",
            ),
        )
        bet_id = cursor.lastrowid
        await _exec(
            db,
            "UPDATE orders SET bet_id = ? WHERE order_id = ?",
            (bet_id, order.order_id),
        )
        await _commit(db)
    except Exception as e:
        # bets table may not exist in a stripped test DB — log and move on.
        logger.debug(f"bets sync on fill skipped: {e}")


async def sync_bets_on_settle(db, order: "Order") -> None:
    from tools.ordermgr.states import SETTLED_LOSS, SETTLED_PUSH, SETTLED_WIN

    if not order.bet_id:
        return
    result_map = {
        SETTLED_WIN: "won",
        SETTLED_LOSS: "lost",
        SETTLED_PUSH: "push",
    }
    bets_result = result_map.get(order.state)
    if not bets_result:
        return
    try:
        payout = None
        if bets_result == "won" and order.price_american and order.stake_dollars:
            # American odds payout including stake.
            p = order.price_american
            if p > 0:
                payout = order.stake_dollars * (1 + p / 100.0)
            else:
                payout = order.stake_dollars * (1 + 100.0 / abs(p))
        elif bets_result == "push":
            payout = order.stake_dollars
        await _exec(
            db,
            "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
            (bets_result, payout, order.bet_id),
        )
        await _commit(db)
    except Exception as e:
        logger.debug(f"bets sync on settle skipped: {e}")
