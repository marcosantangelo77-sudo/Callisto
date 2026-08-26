"""Order management subsystem — Telegram-approved manual placement.

Supersedes the Playwright ``bet_executor``. Callisto generates a signal,
submits an :class:`Order` in ``pending_approval`` state, and pings Marco
via Telegram. Marco approves, places the bet manually on DK/Fanatics, and
replies with the confirmation. Callisto tracks every transition in the
``orders`` table with an append-only ``state_history_json`` audit trail.

FSM (enforced by :data:`ALLOWED_TRANSITIONS`). See also
:data:`ALLOWED_TRANSITIONS` for the authoritative edge list. States:

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
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import aiosqlite

logger = logging.getLogger("callisto.orders")

DB_PATH = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")

ORDER_EXPIRY_MIN = int(os.getenv("CALLISTO_ORDER_EXPIRY_MIN", "10"))
# Backwards-compat toggle. 1 (default here) routes through order_manager; 0
# falls back to the legacy Playwright path in bet_executor.
USE_ORDER_MANAGER = os.getenv("CALLISTO_USE_ORDER_MANAGER", "1") == "1"


# --- States ----------------------------------------------------------------

PENDING_APPROVAL = "pending_approval"
APPROVED = "approved"
SUBMITTED = "submitted"
FILLED = "filled"
REJECTED = "rejected"
CANCELLED = "cancelled"
SETTLED_WIN = "settled_win"
SETTLED_LOSS = "settled_loss"
SETTLED_PUSH = "settled_push"
EXPIRED = "expired"

OPEN_STATES = frozenset({PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED})
TERMINAL_STATES = frozenset(
    {REJECTED, CANCELLED, EXPIRED, SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH}
)

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    PENDING_APPROVAL: frozenset({APPROVED, REJECTED, EXPIRED, CANCELLED}),
    APPROVED: frozenset({SUBMITTED, CANCELLED, REJECTED}),
    SUBMITTED: frozenset({FILLED, CANCELLED, REJECTED}),
    FILLED: frozenset({SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, CANCELLED}),
    REJECTED: frozenset(),
    CANCELLED: frozenset(),
    EXPIRED: frozenset(),
    SETTLED_WIN: frozenset(),
    SETTLED_LOSS: frozenset(),
    SETTLED_PUSH: frozenset(),
}


class InvalidTransition(ValueError):
    """Attempted FSM transition is not in :data:`ALLOWED_TRANSITIONS`."""


class OrderNotFound(LookupError):
    """No row in ``orders`` for the given order_id."""


# --- ULID --------------------------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid() -> str:
    """Monotonic-ish ULID implementation (no external dep).

    48 bits ms timestamp + 80 bits randomness, Crockford base32. Good enough
    for order_id — collisions are astronomically unlikely within one ms and
    the timestamp prefix means default index order = creation order.
    """
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
    rnd = secrets.randbits(80)
    v = (ts_ms << 80) | rnd
    out = [""] * 26
    for i in range(25, -1, -1):
        out[i] = _CROCKFORD[v & 0x1F]
        v >>= 5
    return "".join(out)


# --- Dataclass ---------------------------------------------------------------


@dataclass
class Order:
    order_id: str
    hypothesis_id: str
    signal_id: Optional[str]
    odds_snapshot_id: Optional[int]
    sport: Optional[str]
    event_id: Optional[str]
    market: Optional[str]
    side: Optional[str]
    price_american: Optional[int]
    stake_units: Optional[float]
    stake_dollars: Optional[float]
    state: str
    state_history: list[dict]
    book: Optional[str]
    placed_at: Optional[str]
    settled_at: Optional[str]
    pnl_dollars: Optional[float]
    telegram_msg_id: Optional[str]
    expires_at: Optional[str]
    created_at: Optional[str]
    bet_id: Optional[int] = None
    edge: Optional[float] = None
    fair_prob: Optional[float] = None
    game_description: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> "Order":
        d = dict(row)
        hist_raw = d.get("state_history_json") or "[]"
        try:
            hist = json.loads(hist_raw)
        except Exception:
            hist = []
        return cls(
            order_id=d["order_id"],
            hypothesis_id=d["hypothesis_id"],
            signal_id=d.get("signal_id"),
            odds_snapshot_id=d.get("odds_snapshot_id"),
            sport=d.get("sport"),
            event_id=d.get("event_id"),
            market=d.get("market"),
            side=d.get("side"),
            price_american=d.get("price_american"),
            stake_units=d.get("stake_units"),
            stake_dollars=d.get("stake_dollars"),
            state=d["state"],
            state_history=hist,
            book=d.get("book"),
            placed_at=d.get("placed_at"),
            settled_at=d.get("settled_at"),
            pnl_dollars=d.get("pnl_dollars"),
            telegram_msg_id=d.get("telegram_msg_id"),
            expires_at=d.get("expires_at"),
            created_at=d.get("created_at"),
            bet_id=d.get("bet_id"),
            edge=d.get("edge"),
            fair_prob=d.get("fair_prob"),
            game_description=d.get("game_description"),
            notes=d.get("notes"),
        )


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
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    hypothesis_id TEXT NOT NULL,
                    signal_id TEXT,
                    odds_snapshot_id INTEGER,
                    sport TEXT,
                    event_id TEXT,
                    market TEXT,
                    side TEXT,
                    price_american INTEGER,
                    stake_units REAL,
                    stake_dollars REAL,
                    state TEXT NOT NULL,
                    state_history_json TEXT NOT NULL DEFAULT '[]',
                    book TEXT,
                    placed_at TIMESTAMP,
                    settled_at TIMESTAMP,
                    pnl_dollars REAL,
                    telegram_msg_id TEXT,
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    bet_id INTEGER,
                    edge REAL,
                    fair_prob REAL,
                    game_description TEXT,
                    notes TEXT
                )
                """
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_state "
                "ON orders(state, created_at DESC)"
            )
            await self._db.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_hypothesis "
                "ON orders(hypothesis_id, created_at DESC)"
            )
            await self._db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_orders_signal_open "
                "ON orders(hypothesis_id, signal_id) "
                "WHERE signal_id IS NOT NULL "
                "AND state IN ('pending_approval','approved','submitted','filled')"
            )
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
        if signal_id is not None:
            cursor = await self._db.execute(
                "SELECT order_id FROM orders "
                "WHERE hypothesis_id = ? AND signal_id = ? "
                "AND state IN (?, ?, ?, ?)",
                (hypothesis_id, signal_id,
                 PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED),
            )
            existing = await cursor.fetchone()
            if existing:
                logger.info(
                    f"Idempotent submit_order: returning existing "
                    f"order_id={existing['order_id']} for hypothesis={hypothesis_id} "
                    f"signal_id={signal_id}"
                )
                return existing["order_id"]

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
                """
                INSERT INTO orders (
                    order_id, hypothesis_id, signal_id, odds_snapshot_id,
                    sport, event_id, market, side, price_american,
                    stake_units, stake_dollars, state, state_history_json,
                    book, expires_at, created_at, edge, fair_prob,
                    game_description
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                cursor = await self._db.execute(
                    "SELECT order_id FROM orders "
                    "WHERE hypothesis_id = ? AND signal_id = ? "
                    "AND state IN (?, ?, ?, ?)",
                    (hypothesis_id, signal_id,
                     PENDING_APPROVAL, APPROVED, SUBMITTED, FILLED),
                )
                existing = await cursor.fetchone()
                if existing:
                    return existing["order_id"]
            raise

        # Fire the approval request — best effort, don't block on Telegram.
        price = signal.get("price_american") or signal.get("book_odds_american") or 0
        price_str = f"+{price}" if price > 0 else str(price)
        edge_pct = (edge or 0) * 100
        clv_str = f", CLV_prior={clv_prior * 100:+.1f}%" if clv_prior is not None else ""
        msg = (
            f"<b>Order #{order_id[-6:]}</b>\n"
            f"{signal.get('sport', '?').upper()} "
            f"{signal.get('game_description') or signal.get('event_id', '?')}\n"
            f"{signal.get('side', '?')} {price_str} @ {book}\n"
            f"Stake: {stake_units:.2f}u (${stake_dollars:.0f})\n"
            f"hyp={hypothesis_id}{clv_str}, edge={edge_pct:.1f}%\n"
            f"\n"
            f"/approve {order_id}\n"
            f"/reject {order_id}\n"
            f"<i>Expires in {ORDER_EXPIRY_MIN} min.</i>"
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

    async def get_order(self, order_id: str) -> Order:
        assert self._db is not None
        cursor = await self._db.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise OrderNotFound(order_id)
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
        canonical = {
            "win": SETTLED_WIN, "won": SETTLED_WIN, "settled_win": SETTLED_WIN,
            "loss": SETTLED_LOSS, "lost": SETTLED_LOSS, "settled_loss": SETTLED_LOSS,
            "push": SETTLED_PUSH, "settled_push": SETTLED_PUSH,
        }.get(result.lower())
        if not canonical:
            raise ValueError(f"Unknown settle result: {result!r}")
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
        cursor = await self._db.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        if not row:
            raise OrderNotFound(order_id)
        current_state = row["state"]
        allowed = ALLOWED_TRANSITIONS.get(current_state, frozenset())
        if new_state not in allowed:
            raise InvalidTransition(
                f"Cannot transition {order_id} from {current_state} to {new_state}; "
                f"allowed: {sorted(allowed)}"
            )

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

        # Build update clause. Only a whitelisted set of columns is writable
        # via a transition; silently drop anything else to avoid SQL
        # injection via ``reason``/``extra``.
        WRITABLE = {
            "placed_at", "settled_at", "pnl_dollars", "price_american",
            "telegram_msg_id", "bet_id", "notes",
        }
        set_parts = ["state = ?", "state_history_json = ?"]
        params: list[Any] = [new_state, json.dumps(history)]
        for k, v in extra.items():
            if k in WRITABLE and v is not None:
                set_parts.append(f"{k} = ?")
                params.append(v)
        params.append(order_id)

        await self._db.execute(
            f"UPDATE orders SET {', '.join(set_parts)} WHERE order_id = ?",
            tuple(params),
        )
        await self._db.commit()
        cursor = await self._db.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        logger.info(
            f"Order {order_id}: {current_state} -> {new_state} ({reason})"
        )
        return Order.from_row(row)

    async def _sync_bets_on_fill(self, order: Order) -> None:
        """Insert a row in ``bets`` matching this filled order so the
        existing CLV pipeline (tools/clv_tracker.record_closing_line) will
        backfill closing lines and compute CLV automatically.

        Schema gap noted in the audit: ``bets`` has no ``signal_id`` or
        ``odds_snapshot_id`` column. We write the order_id into ``notes``
        and ``tags`` so the join can be recovered.
        """
        assert self._db is not None
        if order.bet_id:
            return  # already synced
        try:
            now = datetime.now(timezone.utc).isoformat()
            implied = None
            if order.fair_prob is not None and order.edge is not None:
                implied = max(0.0, min(1.0, order.fair_prob - order.edge))
            cursor = await self._db.execute(
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
            await self._db.execute(
                "UPDATE orders SET bet_id = ? WHERE order_id = ?",
                (bet_id, order.order_id),
            )
            await self._db.commit()
        except Exception as e:
            # bets table may not exist in a stripped test DB — log and move on.
            logger.debug(f"bets sync on fill skipped: {e}")

    async def _sync_bets_on_settle(self, order: Order) -> None:
        assert self._db is not None
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
            await self._db.execute(
                "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
                (bets_result, payout, order.bet_id),
            )
            await self._db.commit()
        except Exception as e:
            logger.debug(f"bets sync on settle skipped: {e}")


# --- Settlement reconciler --------------------------------------------------

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


# --- Module-level singleton -------------------------------------------------

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
