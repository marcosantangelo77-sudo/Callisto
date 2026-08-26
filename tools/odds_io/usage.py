"""Usage tracking for odds-api.io — persisted hourly window.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from tools.odds_io.config import ODDS_API_IO_KEY, HOURLY_LIMIT

logger = logging.getLogger("callisto.odds_api_io")

_TRACKER_PATH = Path(os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")).parent / "odds_api_io_usage.json"

# Request tracking — sliding window within the current hour
_hourly_requests: int = 0
_hour_key: str = ""
_lifetime_requests: int = 0


def current_hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")


def load_usage() -> None:
    """Load hourly request count from disk."""
    global _hourly_requests, _hour_key, _lifetime_requests
    current_hour = current_hour_key()

    if _TRACKER_PATH.exists():
        try:
            data = json.loads(_TRACKER_PATH.read_text())
            _lifetime_requests = data.get("lifetime", 0)
            if data.get("hour") == current_hour:
                _hourly_requests = data.get("count", 0)
                _hour_key = current_hour
                return
        except Exception as e:
            logger.info(f"Could not load odds-api.io usage tracker (resetting): {e}")

    # New hour or no file — reset hourly counter
    _hourly_requests = 0
    _hour_key = current_hour
    save_usage()


def save_usage() -> None:
    """Persist usage tracker to disk."""
    try:
        _TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TRACKER_PATH.write_text(json.dumps({
            "hour": _hour_key,
            "count": _hourly_requests,
            "lifetime": _lifetime_requests,
            "updated": datetime.now(timezone.utc).isoformat(),
        }))
    except Exception as e:
        logger.warning(f"Failed to save odds-api.io usage tracker: {e}")


def increment_usage() -> None:
    """Increment and persist request count."""
    global _hourly_requests, _lifetime_requests
    _hourly_requests += 1
    _lifetime_requests += 1
    save_usage()


def get_usage_status() -> dict:
    """Return current Odds-API.io usage status."""
    load_usage()
    return {
        "hour": _hour_key,
        "requests_used_this_hour": _hourly_requests,
        "requests_remaining_this_hour": max(0, HOURLY_LIMIT - _hourly_requests),
        "hourly_limit": HOURLY_LIMIT,
        "lifetime_requests": _lifetime_requests,
        "api_key_set": bool(ODDS_API_IO_KEY),
    }


def check_budget(cost: int = 1) -> Optional[str]:
    """Check if we have budget for a request. Returns error string or None."""
    load_usage()
    if not ODDS_API_IO_KEY:
        return "ODDS_API_IO_KEY not set in .env — get a free key at https://odds-api.io"
    if _hourly_requests + cost > HOURLY_LIMIT:
        return (
            f"Odds-API.io hourly limit reached ({_hourly_requests}/{HOURLY_LIMIT}). "
            f"Resets at the top of the next UTC hour."
        )
    return None


def hourly_remaining() -> int:
    """Requests remaining in the current hourly window."""
    load_usage()
    return max(0, HOURLY_LIMIT - _hourly_requests)
