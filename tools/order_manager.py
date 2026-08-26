"""Order management subsystem — Telegram-approved manual placement.

Supersedes the Playwright ``bet_executor``. Callisto generates a signal,
submits an :class:`Order` in ``pending_approval`` state, and pings Marco
via Telegram. Marco approves, places the bet manually on DK/Fanatics, and
replies with the confirmation. Callisto tracks every transition in the
``orders`` table with an append-only ``state_history_json`` audit trail.

FSM (enforced by :data:`ALLOWED_TRANSITIONS`). States:

    pending_approval -> approved -> submitted -> filled -> settled_{win,loss,push}
    pending_approval -> rejected / cancelled / expired
    filled -> cancelled (voided by book)

Idempotency
-----------
``(hypothesis_id, signal_id)`` is the natural key. Submitting twice with
the same pair returns the existing ``order_id``. Settled/rejected/
cancelled/expired orders don't block resubmission (see migration 007).

Module integrates with:
  * ``tools.telegram`` — outbound approval requests
  * ``tools.clv_tracker`` (``bets`` table) — kept in sync for CLV/CLV backfill
  * ``tools.bet_executor`` (``executor_log``) — still written during transition
  * ``game_results`` table — settlement reconciler maps -> settled_{win|loss|push}

Implementation note
-------------------
The building blocks (states/FSM, ULID, Order model, transition engine,
bets sync) live in :mod:`tools.ordermgr`. This module keeps the manager +
public API surface for backwards compatibility.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

from tools.ordermgr.bets_sync import sync_bets_on_fill, sync_bets_on_settle
from tools.ordermgr.constants import (
    CREATE_ORDERS_TABLE_SQL,
    DB_PATH,
    INSERT_ORDER_SQL,
    OPEN_STATES_SQL,
    ORDER_EXPIRY_MIN,
    ORDERS_INDEXES_SQL,
)
from tools.ordermgr.models import Order, format_approval_message
from tools.ordermgr.states import (
    ALLOWED_TRANSITIONS,
    APPROVED,
    CANCELLED,
    EXPIRED,
    FILLED,
    InvalidTransition,
    OPEN_STATES,
    OrderNotFound,
    PENDING_APPROVAL,
    REJECTED,
    SETTLED_LOSS,
    SETTLED_PUSH,
    SETTLED_WIN,
    SUBMITTED,
    TERMINAL_STATES,
    canonical_settle_result,
)
from tools.ordermgr.transitions import apply_transition, load_order_row
from tools.ordermgr.ulid import new_ulid

# Backwards-compat toggle. 1 (default here) routes through order_manager; 0
# falls back to the legacy Playwright path in bet_executor.
USE_ORDER_MANAGER = os.getenv("CALLISTO_USE_ORDER_MANAGER", "1") == "1"

__all__ = [
    "ALLOWED_TRANSITIONS",
    "APPROVED",
    "CANCELLED",
    "DB_PATH",
    "EXPIRED",
    "FILLED",
    "InvalidTransition",
    "OPEN_STATES",
    "ORDER_EXPIRY_MIN",
    "Order",
    "OrderManager",
    "OrderNotFound",
    "PENDING_APPROVAL",
    "REJECTED",
    "SETTLED_LOSS",
    "SETTLED_PUSH",
    "SETTLED_WIN",
    "SUBMITTED",
    "TERMINAL_STATES",
    "USE_ORDER_MANAGER",
    "detect_voided_orders",
    "get_manager",
    "new_ulid",
    "reconcile_filled_orders",
    "reset_manager",
]

logger = logging.getLogger("callisto.orders")


# --- Manager -----------------------------------------------------------------


class OrderManager:
    """Owns the ``orders`` table lifecycle.

    The manager is stateless besides its aiosqlite connection and the
    ``enabled`` flag. All FSM transitions go through :meth:`_transition`
    which validates the edge, appends to ``state_history_json``, and
    commits under the WriteCoordinator.
    """

    def __init__(self, db_path: str = DB_PATH, telegram_sender=None):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None
        # Default to the real telegram module; tests inject a mock.
        self._telegram_sender = telegram_sender
        # Fail-closed: orders are refused until enable() is called explicitly.
        self._enabled = False
        self._init_lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._init_lock:
            if self._db is not None:
                return
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            with contextlib.suppress(Exception):
                from tools.db_writer import tag_connection as _tag
                _tag(self._db, self.db_path)
            await self._db.execute("PRAGMA busy_timeout = 60000")

            # Safety net for callers that construct the manager against a
            # fresh DB that hasn't run migrations yet (tests). Idempotent
            # with migration 007 — the CREATE TABLE IF NOT EXISTS is a
            # no-op when the migration framework already applied.
            await self._db.execute(CREATE_ORDERS_TABLE_SQL)
            for idx_sql in ORDERS_INDEXES_SQL:
                await self._db.execute(idx_sql)
            await self._db.commit()
            logger.info("OrderManager initialized")

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    def enable(self) -> bool:
        # Nuclear kill switch: CALLISTO_LOCAL_ONLY forbids arming order
        # submission entirely (mirrors BetExecutor.enable()).
        if os.getenv("CALLISTO_LOCAL_ONLY", "").lower() in ("1", "true", "yes"):
            logger.warning(
                "OrderManager.enable() refused: CALLISTO_LOCAL_ONLY is set — "
                "order submission stays disabled"
            )
            return False
        self._enabled = True
        return True

    def disable(self) -> None:
        self._enabled = False

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    # --- Telegram wiring ----------------------------------------------------

    async def _send_telegram(self, message: str) -> Optional[str]:
        """Dispatch an approval request. Returns the telegram message_id or
        None on failure/no config. Tests inject a sender that records calls.
        """
        if self._telegram_sender is not None:
            return await self._telegram_sender(message)
        try:
            from tools import telegram as _tg
            ok = await _tg.send_alert(message, parse_mode="HTML")
            return "sent" if ok else None
        except Exception as e:
            logger.warning(f"Telegram dispatch failed: {e}")
            return None

    # --- Public API ---------------------------------------------------------

    async def submit_order(
        self,
        *,
        hypothesis_id: str,
        signal: dict,
        stake_units: float,
        stake_dollars: float,
        book: str = "draftkings",
        odds_snapshot_id: Optional[int] = None,
        edge: Optional[float] = None,
        fair_prob: Optional[float] = None,
        clv_prior: Optional[float] = None,
    ) -> str:
        """Create a ``pending_approval`` order + emit Telegram request.

        Idempotent on (hypothesis_id, signal_id): the unique partial index
        guarantees no two open orders share that pair. If a duplicate is
        attempted, returns the existing ``order_id``.
        """
        if not self._enabled:
            raise RuntimeError("OrderManager is disabled — refusing to submit")
        assert self._db is not None, "OrderManager.initialize() not called"

        signal_id = signal.get("signal_id") or signal.get("id")

        # Idempotency check: if an open order already exists for this
        # (hypothesis, signal), return it.
        existing = await self._find_open_order(hypothesis_id, signal_id)
        if existing:
            logger.info(
                f"Idempotent submit_order: returning existing "
                f"order_id={existing} for hypothesis={hypothesis_id} "
                f"signal_id={signal_id}"
            )
            return existing

        now = datetime.now(timezone.utc)
        order_id = new_ulid()
        expires_at = (now + timedelta(minutes=ORDER_EXPIRY_MIN)).isoformat()

        initial_history = [{
            "state": PENDING_APPROVAL,
            "at": now.isoformat(),
            "reason": "submitted",
        }]

        try:
            await self._db.execute(
                INSERT_ORDER_SQL,
                (
                    order_id, hypothesis_id, signal_id, odds_snapshot_id,
                    signal.get("sport"), signal.get("event_id"),
                    signal.get("market"), signal.get("side"),
                    signal.get("price_american") or signal.get("book_odds_american"),
                    float(stake_units), float(stake_dollars),
                    PENDING_APPROVAL, json.dumps(initial_history),
                    book, expires_at, now.isoformat(),
                    edge, fair_prob,
                    signal.get("game_description"),
                ),
            )
            await self._db.commit()
        except aiosqlite.IntegrityError as e:
            # Unique index fired: another caller slipped in between our
            # SELECT and INSERT. Fall through to re-read.
            logger.info(f"IntegrityError on insert — re-reading: {e}")
            await self._db.rollback()
            if signal_id is not None:
                existing = await self._find_open_order(hypothesis_id, signal_id)
                if existing:
                    return existing
            raise

        # Fire the approval request — best effort, don't block on Telegram.
        msg = format_approval_message(
            order_id=order_id,
            signal=signal,
            book=book,
            stake_units=stake_units,
            stake_dollars=stake_dollars,
            hypothesis_id=hypothesis_id,
            edge=edge,
            clv_prior=clv_prior,
            expiry_min=ORDER_EXPIRY_MIN,
        )
        tg_msg_id = await self._send_telegram(msg)
        if tg_msg_id:
            await self._db.execute(
                "UPDATE orders SET telegram_msg_id = ? WHERE order_id = ?",
                (tg_msg_id, order_id),
            )
            await self._db.commit()

        logger.info(
            f"Order {order_id} submitted — hyp={hypothesis_id} "
            f"signal={signal_id} stake_u={stake_units:.2f} "
            f"${stake_dollars:.2f} @ {book}"
        )
        return order_id

    async def _find_open_order(
        self, hypothesis_id: str, signal_id: Optional[str]
    ) -> Optional[str]:
        """Return the open order_id for (hypothesis, signal), if any."""
        assert self._db is not None
        if signal_id is None:
            return None
        cursor = await self._db.execute(
            OPEN_STATES_SQL,
            (hypothesis_id, signal_id,
             PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED),
        )
        row = await cursor.fetchone()
        return row["order_id"] if row else None

    async def get_order(self, order_id: str) -> Order:
        assert self._db is not None
        row = await load_order_row(self._db, order_id)
        return Order.from_row(row)

    async def list_orders(
        self, *, state: Optional[str] = None, limit: int = 50
    ) -> list[Order]:
        assert self._db is not None
        if state:
            cursor = await self._db.execute(
                "SELECT * FROM orders WHERE state = ? "
                "ORDER BY created_at DESC LIMIT ?",
                (state, int(limit)),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            )
        rows = await cursor.fetchall()
        return [Order.from_row(r) for r in rows]

    # --- Transitions --------------------------------------------------------

    async def approve(self, order_id: str, *, reason: str = "user_approved") -> Order:
        return await self._transition(order_id, APPROVED, reason=reason)

    async def reject(self, order_id: str, reason: str = "user_rejected") -> Order:
        return await self._transition(order_id, REJECTED, reason=reason)

    async def cancel(self, order_id: str, reason: str = "cancelled") -> Order:
        return await self._transition(order_id, CANCELLED, reason=reason)

    async def mark_submitted(self, order_id: str, *, reason: str = "placed_at_book") -> Order:
        now = datetime.now(timezone.utc).isoformat()
        return await self._transition(
            order_id, SUBMITTED, reason=reason, placed_at=now,
        )

    async def mark_filled(
        self,
        order_id: str,
        *,
        actual_price: Optional[int] = None,
        reason: str = "filled_at_book",
    ) -> Order:
        """Marco pasted the bookmaker confirmation back. We also sync
        the ``bets`` table so CLV tracking keeps working.
        """
        extra: dict[str, Any] = {}
        if actual_price is not None:
            extra["price_american"] = int(actual_price)
        order = await self._transition(
            order_id, FILLED, reason=reason, **extra,
        )
        # Keep bets/bankroll in sync for CLV backfill + bankroll math.
        await self._sync_bets_on_fill(order)
        return order

    async def settle(
        self,
        order_id: str,
        *,
        result: str,
        pnl_dollars: Optional[float] = None,
        reason: str = "auto_from_game_results",
    ) -> Order:
        """Mark a filled order as settled. ``result`` is one of
        ``win``/``loss``/``push`` (short form) or ``settled_win`` etc.
        """
        canonical = canonical_settle_result(result)
        now = datetime.now(timezone.utc).isoformat()
        order = await self._transition(
            order_id, canonical, reason=reason,
            settled_at=now, pnl_dollars=pnl_dollars,
        )
        await self._sync_bets_on_settle(order)
        return order

    async def expire_stale(self, *, now: Optional[datetime] = None) -> list[str]:
        """Move all ``pending_approval`` orders past ``expires_at`` to
        ``expired``. Returns the list of expired order_ids. Called from
        a cron path in api.py lifespan.
        """
        assert self._db is not None
        ts = (now or datetime.now(timezone.utc)).isoformat()
        cursor = await self._db.execute(
            "SELECT order_id FROM orders "
            "WHERE state = ? AND expires_at IS NOT NULL AND expires_at < ?",
            (PENDING_APPROVAL, ts),
        )
        rows = await cursor.fetchall()
        expired: list[str] = []
        for r in rows:
            try:
                await self._transition(
                    r["order_id"], EXPIRED, reason="ttl_expired",
                )
                expired.append(r["order_id"])
            except InvalidTransition:
                continue
        if expired:
            logger.info(f"Expired {len(expired)} stale orders: {expired}")
        return expired

    # --- Internal -----------------------------------------------------------

    async def _transition(
        self,
        order_id: str,
        new_state: str,
        *,
        reason: str,
        **extra: Any,
    ) -> Order:
        assert self._db is not None
        order = await apply_transition(
            self._db, order_id, new_state, reason=reason, **extra,
        )
        logger.info(f"Order {order_id}: -> {new_state} ({reason})")
        return order

    async def _sync_bets_on_fill(self, order: Order) -> None:
        assert self._db is not None
        await sync_bets_on_fill(self._db, order)

    async def _sync_bets_on_settle(self, order: Order) -> None:
        assert self._db is not None
        await sync_bets_on_settle(self._db, order)


# --- Settlement reconciler ---------------------------------------------------

# The real reconciler lives in ``tools.order_reconciler``. Re-exported here
# for backwards compatibility — ``api.py`` and the /orders/reconcile endpoint
# both import ``reconcile_filled_orders`` from this module. Keeping the
# alias means the order-management branch's wiring still works.


async def reconcile_filled_orders(manager: "OrderManager", *, limit: int = 100) -> dict:
    """Shim delegating to :func:`tools.order_reconciler.reconcile_filled_orders`.

    The full reconciler handles moneyline/spread/total/player-prop/SGP
    resolution, CLV logging, hypothesis_stats refresh, stuck detection,
    and Telegram confirmation. Imported lazily so a degraded install
    (missing reconciler) still lets OrderManager function.
    """
    from tools.order_reconciler import reconcile_filled_orders as _real
    return await _real(manager, limit=limit)


async def detect_voided_orders(manager: "OrderManager") -> dict:
    """Shim — see :func:`tools.order_reconciler.detect_voided_orders`."""
    from tools.order_reconciler import detect_voided_orders as _real
    return await _real(manager)


# --- Module-level singleton --------------------------------------------------

_manager: Optional[OrderManager] = None


async def get_manager() -> OrderManager:
    """Return a process-wide OrderManager. Lazily initialised."""
    global _manager
    if _manager is None:
        _manager = OrderManager()
        await _manager.initialize()
    return _manager


async def reset_manager() -> None:
    """Test hook: drop the singleton so a fresh DB can be used."""
    global _manager
    if _manager is not None:
        await _manager.close()
    _manager = None
