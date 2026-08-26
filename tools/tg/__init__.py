"""tools.tg — Telegram helpers for Callisto, split from tools/telegram.py.

Modules:
    config   — env-driven bot token / chat id / API base
    client   — low-level send_alert with rate limiting
    alerts   — formatted alert builders (edge, sharp move, bet result, system)
    listener — TelegramListener: bidirectional command polling
"""

from tools.tg.alerts import (
    alert_bet_result,
    alert_edge,
    alert_prop_edges,
    alert_sharp_move,
    alert_system,
)
from tools.tg.client import MIN_INTERVAL_SECONDS, reset_throttle_cache, send_alert
from tools.tg.config import API_BASE, BOT_TOKEN, CHAT_ID
from tools.tg.listener import TelegramListener

__all__ = [
    "API_BASE",
    "BOT_TOKEN",
    "CHAT_ID",
    "MIN_INTERVAL_SECONDS",
    "TelegramListener",
    "alert_bet_result",
    "alert_edge",
    "alert_prop_edges",
    "alert_sharp_move",
    "alert_system",
    "reset_throttle_cache",
    "send_alert",
]
