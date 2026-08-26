"""Stuck / void detection for the order reconciler (split from
``tools/order_reconciler``)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from tools.order_manager import FILLED, CANCELLED, OrderManager
from tools.orderrec.constants import STUCK_GAME_HOURS, STUCK_PROP_HOURS
from tools.orderrec.results import _lookup_game_context

logger = logging.getLogger("callisto.order_reconciler")


async def _maybe_mark_stuck(
    manager: OrderManager, row, report, market: str
) -> None:
    """If the event is well past its expected completion time and still
    has no game_result row, tag the order as stuck_pending_result and
    alert Marco exactly once.
    """
    db = manager._db
    order_id = row["order_id"]
    notes = row["notes"] if "notes" in row.keys() else None
    if notes and "stuck_pending_result" in notes:
        return  # already flagged; don't re-alert

    ctx = await _lookup_game_context(db, row["sport"] or "", row["event_id"] or "")
    if not ctx:
        return
    try:
        game_dt = datetime.fromisoformat(
            ctx["game_date"].replace("Z", "+00:00")
        )
    except Exception:
        return
    if game_dt.tzinfo is None:
        game_dt = game_dt.replace(tzinfo=timezone.utc)
    age_h = (datetime.now(timezone.utc) - game_dt).total_seconds() / 3600.0
    threshold = STUCK_PROP_HOURS if market == "player_props" else STUCK_GAME_HOURS
    if age_h < threshold:
        return

    flag = (
        f"stuck_pending_result; "
        f"market={market} age_h={age_h:.1f} flagged_at="
        f"{datetime.now(timezone.utc).isoformat()}"
    )
    new_notes = f"{notes}; {flag}" if notes else flag
    await db.execute(
        "UPDATE orders SET notes = ? WHERE order_id = ?", (new_notes, order_id),
    )
    await db.commit()
    report.stuck += 1
    report.stuck_order_ids.append(order_id)

    try:
        msg = (
            f"Order #{order_id[-6:]} stuck — {market} on "
            f"{row['sport']}/{row['event_id']} still has no game_result "
            f"{age_h:.1f}h after start. Manual settle needed."
        )
        if manager._telegram_sender is not None:
            await manager._telegram_sender(msg)
        else:
            from tools import telegram as _tg
            await _tg.send_alert(msg, parse_mode="")
    except Exception as e:
        logger.debug(f"stuck telegram skipped: {e}")


async def detect_voided_orders(manager: OrderManager) -> dict:
    """Scan filled orders whose game was postponed/cancelled; void them.

    Void path:
      1. Scan ``filled`` orders.
      2. For each, peek at ``game_contexts.context_json`` for a
         ``status`` key in ('postponed','cancelled','suspended').
      3. Transition state -> ``cancelled``, pnl = 0, refund stake
         (bankroll append of +stake).
      4. Telegram-alert.
    """
    db = manager._db
    assert db is not None
    voided: list[str] = []
    errors = 0

    cur = await db.execute(
        "SELECT * FROM orders WHERE state = ? ORDER BY created_at ASC",
        (FILLED,),
    )
    rows = await cur.fetchall()
    for row in rows:
        order_id = row["order_id"]
        try:
            ctx = await _lookup_game_context(
                db, row["sport"] or "", row["event_id"] or ""
            )
            if not ctx:
                continue
            status = (ctx.get("context", {}).get("status") or "").lower()
            if status not in ("postponed", "cancelled", "canceled", "suspended"):
                continue

            # Void the order — filled -> cancelled with pnl=0.
            voided_order = await manager._transition(
                order_id, CANCELLED,
                reason=f"auto_void_{status}",
                pnl_dollars=0.0,
            )
            # Refund stake to bankroll.
            stake = float(row["stake_dollars"] or 0.0)
            if stake > 0:
                bal_cur = await db.execute(
                    "SELECT balance FROM bankroll "
                    "ORDER BY timestamp DESC LIMIT 1"
                )
                bal_row = await bal_cur.fetchone()
                current = float(bal_row[0]) if bal_row else 0.0
                now = datetime.now(timezone.utc).isoformat()
                try:
                    await db.execute(
                        "INSERT INTO bankroll "
                        "(timestamp, balance, change, bet_id, description) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (now, current + stake, stake, voided_order.bet_id,
                         f"Void refund {order_id} ({status})"),
                    )
                    await db.commit()
                except Exception as e:
                    logger.debug(f"bankroll refund skipped: {e}")

            # Update the bets mirror too.
            if voided_order.bet_id:
                try:
                    await db.execute(
                        "UPDATE bets SET result = ?, payout = ? WHERE id = ?",
                        ("void", stake, voided_order.bet_id),
                    )
                    await db.commit()
                except Exception:
                    pass

            voided.append(order_id)
            try:
                msg = (
                    f"Order #{order_id[-6:]} VOIDED ({status}) — "
                    f"stake ${stake:.2f} refunded."
                )
                if manager._telegram_sender is not None:
                    await manager._telegram_sender(msg)
                else:
                    from tools import telegram as _tg
                    await _tg.send_alert(msg, parse_mode="")
            except Exception as e:
                logger.debug(f"void telegram skipped: {e}")
        except Exception as e:
            logger.warning(f"void scan failed for {order_id}: {e}", exc_info=True)
            errors += 1

    if voided:
        logger.info(f"void scan: voided {len(voided)} orders: {voided}")
    return {"voided": len(voided), "errors": errors, "order_ids": voided}
