"""ULID generation for order ids (no external dependency)."""

from __future__ import annotations

import secrets
import time
from threading import Lock

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_lock = Lock()
_last_value = 0


def new_ulid() -> str:
    """Monotonic ULID implementation (no external dep).

    48 bits ms timestamp + 80 bits randomness, Crockford base32. A lock +
    last-value guard guarantees strictly non-decreasing ids even for two
    calls within the same millisecond, so the timestamp prefix really does
    mean default index order = creation order.
    """
    global _last_value
    with _lock:
        ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)
        rnd = secrets.randbits(80)
        v = (ts_ms << 80) | rnd
        if v <= _last_value:
            v = _last_value + 1
        _last_value = v
    out = [""] * 26
    for i in range(25, -1, -1):
        out[i] = _CROCKFORD[v & 0x1F]
        v >>= 5
    return "".join(out)
