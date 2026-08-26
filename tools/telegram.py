"""
Telegram notification system for Callisto.

This module is now a thin compatibility facade over the ``tools.tg`` package:

- tools/tg/config.py   — env-driven bot token / chat id / API base
- tools/tg/client.py   — low-level send_alert with rate limiting
- tools/tg/alerts.py   — formatted alert builders
- tools/tg/listener.py — TelegramListener for bidirectional commands

All public names are re-exported below so existing callers of
``tools.telegram`` keep working unchanged. Sends alerts to Marco's phone
for edges, sharp moves, line alerts, bet resolutions and system status.
Uses httpx (already a dependency) — no extra packages needed.
"""

import logging

from tools.tg import (  # noqa: F401
    API_BASE,
    BOT_TOKEN,
    CHAT_ID,
    MIN_INTERVAL_SECONDS,
    TelegramListener,
    alert_bet_result,
    alert_edge,
    alert_prop_edges,
    alert_sharp_move,
    alert_system,
    reset_throttle_cache,
    send_alert,
)
from tools.tg.client import _last_sent  # noqa: F401

logger = logging.getLogger("callisto.telegram")  # noqa: F821
