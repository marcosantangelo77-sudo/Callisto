"""Shared utilities for tools.lanalysis."""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("callisto.line_analysis")


def _parse_timestamp(ts) -> float:
    """Convert various timestamp formats to Unix epoch float."""
    if isinstance(ts, (int, float)):
        # Already numeric — assume Unix epoch
        # If it's in milliseconds (> year 2100 in seconds), convert
        if ts > 4_102_444_800:
            return float(ts) / 1000.0
        return float(ts)
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            pass
        # Try common formats
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                return dt.timestamp()
            except ValueError:
                continue
    # Fallback: return 0 (will sort to beginning)
    logger.warning(f"Could not parse timestamp: {ts}")
    return 0.0
