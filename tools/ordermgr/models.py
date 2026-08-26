"""Order dataclass + Telegram approval-message formatting."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

import aiosqlite


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


def format_approval_message(
    *,
    order_id: str,
    signal: dict,
    book: str,
    stake_units: float,
    stake_dollars: float,
    hypothesis_id: str,
    edge: Optional[float],
    clv_prior: Optional[float],
    expiry_min: int,
) -> str:
    """Build the HTML Telegram approval request for a pending order."""
    price = signal.get("price_american") or signal.get("book_odds_american") or 0
    price_str = f"+{price}" if price > 0 else str(price)
    edge_pct = (edge or 0) * 100
    clv_str = f", CLV_prior={clv_prior * 100:+.1f}%" if clv_prior is not None else ""
    return (
        f"<b>Order #{order_id[-6:]}</b>\n"
        f"{signal.get('sport', '?').upper()} "
        f"{signal.get('game_description') or signal.get('event_id', '?')}\n"
        f"{signal.get('side', '?')} {price_str} @ {book}\n"
        f"Stake: {stake_units:.2f}u (${stake_dollars:.0f})\n"
        f"hyp={hypothesis_id}{clv_str}, edge={edge_pct:.1f}%\n"
        f"\n"
        f"/approve {order_id}\n"
        f"/reject {order_id}\n"
        f"<i>Expires in {expiry_min} min.</i>"
    )
