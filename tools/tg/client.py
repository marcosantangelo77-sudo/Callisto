"""Low-level Telegram Bot API client: send_alert with throttling."""

import logging
import time
from typing import Optional

import httpx

from tools.tg.config import API_BASE, BOT_TOKEN, CHAT_ID

logger = logging.getLogger("callisto.telegram")

# Rate limiting — don't spam
_last_sent: dict[str, float] = {}  # key -> timestamp
_LAST_SENT_MAX = 200  # Cap throttle cache to prevent unbounded growth
MIN_INTERVAL_SECONDS = 900  # Same edge throttled to 15 min minimum


def _throttle(key: str) -> bool:
    """
    Check/update the throttle cache for a key.

    Returns True if the send should proceed, False if throttled.
    """
    now = time.time()
    last = _last_sent.get(key, 0)
    if now - last < MIN_INTERVAL_SECONDS:
        logger.debug(f"Throttled alert: {key}")
        return False
    _last_sent[key] = now
    # Evict stale throttle keys to prevent unbounded growth
    if len(_last_sent) > _LAST_SENT_MAX:
        stale = sorted(_last_sent, key=_last_sent.get)[: len(_last_sent) - _LAST_SENT_MAX // 2]
        for k in stale:
            del _last_sent[k]
    return True


async def send_alert(
    message: str,
    silent: bool = False,
    parse_mode: str = "HTML",
    throttle_key: Optional[str] = None,
) -> bool:
    """
    Send an alert to Telegram.

    Args:
        message: The message text (supports HTML formatting).
        silent: If True, delivers without notification sound.
        parse_mode: "HTML" or "MarkdownV2" (empty string disables parsing).
        throttle_key: Optional key for rate limiting. Same key won't send
                     more than once per MIN_INTERVAL_SECONDS.

    Returns:
        True if sent successfully, False otherwise.
    """
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("Telegram not configured — skipping alert")
        return False

    # Throttle check
    if throttle_key and not _throttle(throttle_key):
        return False

    payload: dict = {
        "chat_id": CHAT_ID,
        "text": message,
        "disable_notification": silent,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(f"{API_BASE}/sendMessage", json=payload)
            if resp.status_code == 200:
                logger.info(f"Telegram alert sent ({len(message)} chars)")
                return True
            else:
                logger.error(f"Telegram API error: {resp.status_code} {resp.text}")
                return False
    except Exception as e:
        logger.error(f"Telegram send failed: {e}")
        return False


def reset_throttle_cache() -> None:
    """Clear the throttle cache (useful for tests)."""
    _last_sent.clear()
