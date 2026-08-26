"""Formatted Telegram alert builders (edges, sharp moves, bet results, system)."""

from typing import Optional

from tools.tg.client import send_alert


async def alert_edge(
    game: str,
    team: str,
    market: str,
    edge_pct: float,
    confidence_tier: str,
    confidence_score: float,
    best_book: str,
    best_price: int,
    reasoning: str = "",
) -> bool:
    """Send edge detection alert."""
    price_str = f"+{best_price}" if best_price > 0 else str(best_price)
    msg = (
        f"<b>Edge Detected</b>\n"
        f"\n"
        f"<b>{game}</b>\n"
        f"{team} — {market}\n"
        f"\n"
        f"Edge: <b>{edge_pct:.1f}%</b>\n"
        f"Confidence: <b>{confidence_tier}</b> ({confidence_score:.0%})\n"
        f"Best line: {best_book} {price_str}\n"
    )
    if reasoning:
        msg += f"\n<i>{reasoning}</i>"

    return await send_alert(msg, throttle_key=f"edge:{game}:{team}:{market}")


async def alert_sharp_move(
    game: str,
    team: str,
    market: str,
    moved_books: list[dict],
    stale_books: list[dict],
) -> bool:
    """Send sharp money movement alert."""
    movers = ", ".join(
        f"{m['bookmaker']} ({m['old_price']}→{m['new_price']})"
        for m in moved_books
    )
    stale = ", ".join(
        f"{s['bookmaker']} {s['price']}"
        for s in stale_books[:3]
    )
    msg = (
        f"<b>Sharp Money Alert</b>\n"
        f"\n"
        f"<b>{game}</b>\n"
        f"{team} — {market}\n"
        f"\n"
        f"Moved: {movers}\n"
        f"Stale: {stale}\n"
        f"\n"
        f"<i>Stale books may offer value before adjusting.</i>"
    )
    return await send_alert(msg, throttle_key=f"sharp:{game}:{team}:{market}")


async def alert_bet_result(
    bet_id: int,
    game: str,
    team: str,
    result: str,
    placement_odds: int,
    stake: float,
    payout: Optional[float] = None,
    clv_implied: Optional[float] = None,
) -> bool:
    """Send bet resolution alert."""
    icon = {"won": "W", "lost": "L", "push": "P"}.get(result, "?")
    price_str = f"+{placement_odds}" if placement_odds > 0 else str(placement_odds)

    msg = (
        f"<b>Bet #{bet_id} — {icon} {result.upper()}</b>\n"
        f"\n"
        f"{game}\n"
        f"{team} @ {price_str}\n"
        f"Stake: ${stake:.2f}"
    )
    if result == "won" and payout:
        msg += f" → <b>${payout:.2f}</b> (+${payout - stake:.2f})"
    elif result == "lost":
        msg += f" → <b>-${stake:.2f}</b>"

    if clv_implied is not None:
        direction = "+" if clv_implied > 0 else ""
        msg += f"\nCLV: {direction}{clv_implied:.1%}"

    return await send_alert(msg)


async def alert_prop_edges(edges: list[dict], sport: str = "") -> bool:
    """Send summary of prop edges found."""
    if not edges:
        return False

    header = "<b>Prop Edges Found</b>"
    if sport:
        header += f" — {sport}"
    header += "\n\n"

    lines = []
    for e in edges[:5]:  # Top 5
        player = e.get("player", "?")
        market = e.get("market", "?")
        side = e.get("side", "?")
        edge = e.get("edge_pct", 0)
        book = e.get("target_book", "?")
        price = e.get("target_price", 0)
        price_str = f"+{price}" if price > 0 else str(price)
        conf = e.get("confidence", {})
        tier = conf.get("tier", "?") if isinstance(conf, dict) else "?"

        lines.append(
            f"<b>{player}</b> {market} {side}\n"
            f"  Edge: {edge:.1f}% | {book} {price_str} | {tier}"
        )

    msg = header + "\n\n".join(lines)
    if len(edges) > 5:
        msg += f"\n\n<i>+{len(edges) - 5} more edges</i>"

    return await send_alert(msg, throttle_key=f"props:{sport}")


async def alert_system(message: str, is_error: bool = False) -> bool:
    """Send system status alert."""
    prefix = "<b>System Error</b>" if is_error else "<b>Callisto</b>"
    return await send_alert(f"{prefix}\n\n{message}", silent=not is_error)
