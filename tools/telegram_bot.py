"""Telegram bot command handlers for the order-management subsystem.

The existing ``tools/telegram.TelegramListener`` handles /status, /edges,
/bets, /bankroll and free-text queries. This module adds the order-approval
commands that the order_manager emits prompts for:

  /approve <order_id>            — pending_approval -> approved
  /reject  <order_id> [reason]   — pending_approval -> rejected
  /fill    <order_id> <price>    — submitted        -> filled  (actual_price recorded)
  /submitted <order_id>          — approved         -> submitted  (Marco clicked place)
  /status                        — pending orders summary
  /pause_all                     — disable both executor + order_manager
  /resume_all                    — re-enable

The split file keeps the existing listener lean and makes these handlers
easy to test with a mock client. Call :func:`register_order_commands` from
the listener's ``_handle_message`` router.
"""

from __future__ import annotations

import logging
import shlex
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("callisto.telegram_bot")


OrderCommandHandler = Callable[[str, list[str]], Awaitable[str]]


async def _cmd_approve(
    manager, args: list[str], send
) -> str:
    if not args:
        return "Usage: /approve <order_id>"
    order_id = args[0]
    try:
        order = await manager.approve(order_id, reason="telegram_approve")
        await send(
            f"Order {order.order_id[-6:]} APPROVED. "
            f"Place it on {order.book}, then /submitted {order.order_id} "
            f"then /fill {order.order_id} <actual_price>."
        )
        return f"approved {order_id}"
    except Exception as e:
        await send(f"Approve failed: {e}")
        return f"error: {e}"


async def _cmd_reject(
    manager, args: list[str], send
) -> str:
    if not args:
        return "Usage: /reject <order_id> [reason]"
    order_id = args[0]
    reason = " ".join(args[1:]) or "telegram_reject"
    try:
        await manager.reject(order_id, reason=reason)
        await send(f"Order {order_id[-6:]} REJECTED ({reason}).")
        return f"rejected {order_id}"
    except Exception as e:
        await send(f"Reject failed: {e}")
        return f"error: {e}"


async def _cmd_submitted(
    manager, args: list[str], send
) -> str:
    if not args:
        return "Usage: /submitted <order_id>"
    order_id = args[0]
    try:
        await manager.mark_submitted(order_id, reason="telegram_submitted")
        await send(
            f"Order {order_id[-6:]} marked SUBMITTED. "
            f"Reply /fill {order_id} <actual_price> once bookmaker confirms."
        )
        return f"submitted {order_id}"
    except Exception as e:
        await send(f"Submitted failed: {e}")
        return f"error: {e}"


async def _cmd_fill(
    manager, args: list[str], send
) -> str:
    if len(args) < 2:
        return "Usage: /fill <order_id> <actual_american_price>"
    order_id = args[0]
    try:
        price = int(args[1])
    except ValueError:
        await send("Actual price must be an integer American-odds value (e.g. -110, +145).")
        return "error: bad price"
    try:
        order = await manager.mark_filled(
            order_id, actual_price=price, reason="telegram_fill"
        )
        await send(
            f"Order {order.order_id[-6:]} FILLED at {price:+d}. "
            f"Will auto-settle from game_results."
        )
        return f"filled {order_id} @ {price}"
    except Exception as e:
        await send(f"Fill failed: {e}")
        return f"error: {e}"


async def _cmd_status(
    manager, args: list[str], send
) -> str:
    try:
        pending = await manager.list_orders(state="pending_approval", limit=10)
        filled = await manager.list_orders(state="filled", limit=10)
    except Exception as e:
        await send(f"Status failed: {e}")
        return f"error: {e}"
    lines = ["<b>Order Status</b>"]
    if pending:
        lines.append(f"\n<b>Pending approval ({len(pending)})</b>")
        for o in pending:
            price = o.price_american or 0
            price_str = f"+{price}" if price > 0 else str(price)
            lines.append(
                f"  {o.order_id[-6:]}: {o.sport or '?'} {o.side or '?'} {price_str} "
                f"{o.stake_units:.1f}u"
            )
    else:
        lines.append("\nNo orders pending approval.")
    if filled:
        lines.append(f"\n<b>Awaiting settlement ({len(filled)})</b>")
        for o in filled:
            lines.append(f"  {o.order_id[-6:]}: {o.side or '?'} ${o.stake_dollars or 0:.0f}")
    await send("\n".join(lines))
    return "ok"


async def _cmd_pause_all(
    manager, args: list[str], send, bet_executor=None
) -> str:
    manager.disable()
    if bet_executor is not None:
        try:
            bet_executor.disable()
        except Exception:
            pass
    await send("PAUSED: order_manager + bet_executor disabled. Use /resume_all to resume.")
    return "paused"


async def _cmd_resume_all(
    manager, args: list[str], send, bet_executor=None
) -> str:
    manager.enable()
    if bet_executor is not None:
        try:
            bet_executor.enable()
        except Exception:
            pass
    await send("RESUMED: order_manager + bet_executor enabled.")
    return "resumed"


COMMANDS = {
    "/approve": _cmd_approve,
    "/reject": _cmd_reject,
    "/submitted": _cmd_submitted,
    "/fill": _cmd_fill,
    "/order_status": _cmd_status,
    "/pause_all": _cmd_pause_all,
    "/resume_all": _cmd_resume_all,
}


async def handle_order_command(
    text: str,
    manager,
    send: Callable[[str], Awaitable[None]],
    bet_executor=None,
) -> Optional[str]:
    """Parse a Telegram message; if it's an order command, dispatch.

    Returns the handler's summary string, or None if the message isn't an
    order command (caller should fall through to other handlers).
    """
    text = (text or "").strip()
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    if not parts:
        return None
    cmd = parts[0].lower()
    args = parts[1:]
    handler = COMMANDS.get(cmd)
    if handler is None:
        return None
    try:
        if cmd in ("/pause_all", "/resume_all"):
            return await handler(manager, args, send, bet_executor=bet_executor)
        return await handler(manager, args, send)
    except Exception as e:
        logger.exception(f"order command {cmd} crashed")
        await send(f"{cmd} crashed: {e}")
        return f"crash: {e}"
