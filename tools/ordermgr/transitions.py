"""Order FSM transition engine.

Extracted from ``tools.order_manager`` — validates the edge against
:data:`tools.ordermgr.states.ALLOWED_TRANSITIONS`, appends to
``state_history_json``, and commits.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timezone
from typing import Any

from tools.ordermgr.models import Order
from tools.ordermgr.states import (
    OrderNotFound,
    assert_transition,
)

# Only a whitelisted set of columns is writable via a transition; anything
# else is silently dropped to avoid SQL injection via ``reason``/``extra``.
WRITABLE_COLUMNS = frozenset(
    {
        "placed_at", "settled_at", "pnl_dollars", "price_american",
        "telegram_msg_id", "bet_id", "notes",
    }
)


def _is_aiosqlite(db) -> bool:
    return hasattr(db, "_conn") or type(db).__module__.startswith("aiosqlite")


async def _execute(db, sql: str, params=()):
    """Execute against either an aiosqlite connection (awaitable) or a
    plain sqlite3 connection (sync)."""
    if _is_aiosqlite(db):
        return await db.execute(sql, tuple(params))
    return db.execute(sql, tuple(params))


class _CursorCompat:
    """Wraps a sync cursor so ``await cursor.fetchone()`` works."""

    def __init__(self, cursor):
        self._cursor = cursor

    def __getattr__(self, name):
        return getattr(self._cursor, name)

    async def fetchone(self):
        return self._cursor.fetchone()


async def _maybe_await(value):
    """Await ``value`` if it is awaitable (aiosqlite), else return it."""
    if inspect.isawaitable(value):
        return await value
    return value


async def load_order_row(db, order_id: str):
    cursor = await _execute(
        db, "SELECT * FROM orders WHERE order_id = ?", (order_id,)
    )
    row = await _maybe_await(cursor.fetchone())
    if not row:
        raise OrderNotFound(order_id)
    return row


async def apply_transition(
    db,
    order_id: str,
    new_state: str,
    *,
    reason: str,
    **extra: Any,
) -> Order:
    """Validate + execute one FSM transition and return the fresh order."""
    row = await load_order_row(db, order_id)
    current_state = row["state"]
    assert_transition(current_state, new_state, order_id)

    # Append history.
    try:
        history = json.loads(row["state_history_json"] or "[]")
    except Exception:
        history = []
    history.append({
        "state": new_state,
        "at": datetime.now(timezone.utc).isoformat(),
        "reason": reason,
        **{k: v for k, v in extra.items() if k not in ("settled_at", "placed_at")},
    })

    set_parts = ["state = ?", "state_history_json = ?"]
    params: list[Any] = [new_state, json.dumps(history)]
    for k, v in extra.items():
        if k in WRITABLE_COLUMNS and v is not None:
            set_parts.append(f"{k} = ?")
            params.append(v)
    params.append(order_id)

    await _execute(
        db,
        f"UPDATE orders SET {', '.join(set_parts)} WHERE order_id = ?",
        tuple(params),
    )
    if _is_aiosqlite(db):
        await db.commit()
    else:
        db.commit()
    row = await load_order_row(db, order_id)
    return Order.from_row(row)
