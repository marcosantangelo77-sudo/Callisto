"""Post-settle side effects: bankroll append, CLV log, hypothesis_stats,
Telegram alert (split from ``tools/order_reconciler``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.order_manager import SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH, OrderManager
from tools.orderrec.odds import _american_to_implied

logger = logging.getLogger("callisto.order_reconciler")


async def _apply_bankroll(db, order, pnl: float, result: str) -> None:
    """Append a bankroll row (same shape as clv_tracker.resolve_bet).

    Uses BEGIN IMMEDIATE semantics via the connection's busy_timeout + the
    commit path already enforced by OrderManager._transition.
    """
    if result == "push" or not pnl:
        return
    try:
        bal_cur = await db.execute(
            "SELECT balance FROM bankroll ORDER BY timestamp DESC LIMIT 1"
        )
        bal_row = await bal_cur.fetchone()
        current = float(bal_row[0]) if bal_row else 0.0
        new_balance = current + float(pnl)
        now = datetime.now(timezone.utc).isoformat()
        desc = (
            f"Order {order.order_id} {result}: "
            f"{order.game_description or order.event_id or ''}"
        )
        # BEGIN IMMEDIATE enforced on sqlite write path by the connection's
        # busy_timeout=60000 (set in OrderManager.initialize).
        await db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, bet_id, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (now, new_balance, float(pnl), order.bet_id, desc),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"bankroll append skipped for {order.order_id}: {e}")


async def _record_clv(db, order, result: str, pnl: float) -> None:
    """Write a clv_log row using the canonical prob-bp unit."""
    try:
        # Lift the closing line for (event_id, market, side) from
        # closing_lines. The table keys on (event_id, market, team),
        # case-sensitive — mirror the LOWER() pattern used by
        # clv_tracker.record_closing_line so casing doesn't drop matches.
        closing_cur = await db.execute(
            "SELECT closing_odds, closing_point, closing_implied, source "
            "FROM closing_lines "
            "WHERE event_id = ? AND LOWER(market) = LOWER(?) "
            "AND LOWER(team) = LOWER(?) "
            "ORDER BY captured_at DESC LIMIT 1",
            (order.event_id or "", order.market or "", order.side or ""),
        )
        clc = await closing_cur.fetchone()

        placement_implied = _american_to_implied(order.price_american or 0)
        closing_implied = None
        clv_prob_bp = None
        close_reliable = False
        closing_source = "unknown"
        if clc:
            closing_implied = (
                float(clc["closing_implied"]) if clc["closing_implied"] is not None
                else _american_to_implied(clc["closing_odds"] or 0)
            )
            closing_source = clc["source"] or "pinnacle"
            close_reliable = closing_source.lower() in {"pinnacle", "circa"}
            if placement_implied is not None and closing_implied is not None:
                # Canonical CLV unit: prob-bp. Positive = our price beat close.
                clv_prob_bp = round(
                    (closing_implied - placement_implied) * 10000.0, 1
                )
        else:
            logger.warning(
                f"closing line missing for order={order.order_id} "
                f"event={order.event_id} market={order.market} "
                f"side={order.side} — clv_log row written with NULL CLV"
            )

        our_decimal = None
        if order.price_american:
            p = int(order.price_american)
            our_decimal = (1 + p / 100.0) if p > 0 else (1 + 100.0 / abs(p))

        now = datetime.now(timezone.utc).isoformat()
        # INSERT OR REPLACE — idempotent on bet_id PK. We key on
        # order_id (unique, durable) rather than the integer bets.id
        # which may be NULL on a stripped-down test DB.
        clv_key = f"order:{order.order_id}"
        await db.execute(
            "INSERT OR REPLACE INTO clv_log "
            "(bet_id, event, outcome, point, book, our_odds_decimal, "
            "pinnacle_close_fair_prob, pinnacle_close_fair_decimal, "
            "clv_cents, clv_prob_bp, actual_result, actual_pnl, "
            "close_reliable, logged_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clv_key,
                order.event_id or "",
                order.side or "",
                None,  # point — spreads/totals stash in notes, optional
                order.book or "",
                our_decimal,
                closing_implied,
                (1 / closing_implied) if closing_implied else None,
                clv_prob_bp,  # clv_cents mirrors prob-bp on new rows
                clv_prob_bp,
                {"win": "won", "loss": "lost", "push": "push"}[result],
                float(pnl),
                close_reliable,
                now,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"clv_log write skipped for {order.order_id}: {e}")


async def _refresh_hypothesis_stats(db, hypothesis_id: Optional[str]) -> None:
    """Append a fresh rolling-20 ``stage='live'`` row to ``hypothesis_stats``.

    Source of truth: the last 20 settled orders for this hypothesis
    (joined with clv_log for avg CLV). Append-only — the new
    ``_phase_review_live`` looks at the most-recent row.
    """
    if not hypothesis_id:
        return
    try:
        cur = await db.execute(
            "SELECT state, pnl_dollars, stake_dollars, order_id "
            "FROM orders "
            "WHERE hypothesis_id = ? "
            "AND state IN (?, ?, ?) "
            "ORDER BY settled_at DESC LIMIT 20",
            (hypothesis_id, SETTLED_WIN, SETTLED_LOSS, SETTLED_PUSH),
        )
        rows = await cur.fetchall()
        if not rows:
            return
        wins = sum(1 for r in rows if r["state"] == SETTLED_WIN)
        losses = sum(1 for r in rows if r["state"] == SETTLED_LOSS)
        pushes = sum(1 for r in rows if r["state"] == SETTLED_PUSH)
        n = wins + losses + pushes
        decided = wins + losses
        hit_rate = (wins / decided) if decided else None
        total_staked = sum(float(r["stake_dollars"] or 0) for r in rows)
        total_pnl = sum(float(r["pnl_dollars"] or 0) for r in rows)
        roi_pct = (total_pnl / total_staked * 100.0) if total_staked else None

        # Average CLV across the same 20 orders.
        order_ids = tuple(r["order_id"] for r in rows)
        placeholders = ",".join(["?"] * len(order_ids))
        clv_cur = await db.execute(
            f"SELECT AVG(clv_prob_bp) FROM clv_log "
            f"WHERE bet_id IN ({placeholders}) AND clv_prob_bp IS NOT NULL",
            tuple(f"order:{oid}" for oid in order_ids),
        )
        avg_clv_row = await clv_cur.fetchone()
        avg_clv = avg_clv_row[0] if avg_clv_row else None

        now = datetime.now(timezone.utc).isoformat()
        await db.execute(
            "INSERT INTO hypothesis_stats "
            "(hypothesis_id, stage, computed_at, total_n, signals_n, "
            "win, loss, push_, hit_rate, avg_edge, avg_ev, avg_clv, "
            "positive_clv_rate, roi_pct, sharpe, max_drawdown, "
            "p_value, is_significant) "
            "VALUES (?, 'live', ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, "
            "NULL, ?, NULL, NULL, NULL, 0)",
            (
                hypothesis_id, now, n, n, wins, losses, pushes, hit_rate,
                avg_clv, roi_pct,
            ),
        )
        await db.commit()
    except Exception as e:
        logger.debug(f"hypothesis_stats refresh skipped: {e}")


async def _emit_settle_telegram(
    manager: OrderManager, order, result: str, pnl: float
) -> None:
    """Fire a one-line Telegram confirmation on settle.

    Respects the injected ``_telegram_sender`` so tests don't hit the
    network. Best-effort — Telegram failure MUST NOT unwind the settle.
    """
    short = order.order_id[-6:]
    verb = {"win": "WIN", "loss": "LOSS", "push": "PUSH"}[result]
    sign = "+" if pnl > 0 else ""
    msg = f"#{short} settled {verb} {sign}${pnl:.2f}"
    try:
        # If the manager has a test sender, use it; otherwise use the
        # normal telegram module.
        if manager._telegram_sender is not None:
            await manager._telegram_sender(msg)
            return
        from tools import telegram as _tg
        await _tg.send_alert(msg, parse_mode="")
    except Exception as e:
        logger.debug(f"settle telegram skipped for {order.order_id}: {e}")
