"""Pure math/date helpers used by the CLV package (no DB, no IO)."""

import logging
from datetime import date
from typing import Optional

logger = logging.getLogger("callisto.clv_tracker")


def half_vig_devig(implied: Optional[float], vig: float) -> Optional[float]:
    """Half-vig approximation: fair = implied / (1 + vig/2). Bounded to (0,1).

    Returns the input untouched for non-positive or None values so call sites
    can safely chain it without extra guards.
    """
    try:
        if implied is None or implied <= 0:
            return implied
        return max(0.0, min(1.0, float(implied) / (1.0 + max(0.0, vig) / 2.0)))
    except (TypeError, ValueError):
        return implied


def regime_stamp(sport: str) -> Optional[str]:
    """Return a compact ``<sport>|<season_phase>`` stamp or None on failure.

    Uses ``_classify_phase`` (pure date-math, no DB) rather than
    ``detect_regime`` so the stamp computation never opens a separate DB
    connection while a write is in flight on the primary aiosqlite one —
    cross-connection contention would otherwise stall a bet resolution
    under concurrent load. Callers write this into
    ``clv_log.regime_phase_at_placement`` so downstream analysis can bucket
    CLV by regime. Any error degrades to None so CLV writes never fail
    due to regime lookup.
    """
    if not sport:
        return None
    try:
        from tools.market_regime import (
            _classify_phase as _mr_classify,
            _canonical_sport as _mr_canon,
        )
        sp_norm = _mr_canon(sport)
        phase, _win, _bounds = _mr_classify(sp_norm, date.today())
        return f"{sp_norm}|{phase}"
    except Exception as e:
        logger.debug(f"regime_stamp failed for {sport!r}: {e}")
        return None


def american_to_decimal(odds: Optional[int]) -> Optional[float]:
    """American → decimal odds. None/0 → None (can't convert)."""
    if odds is None:
        return None
    try:
        o = int(odds)
    except (TypeError, ValueError):
        return None
    if o == 0:
        return None
    if o > 0:
        return 1.0 + o / 100.0
    return 1.0 + 100.0 / abs(o)


def interpret_clv(avg_clv_implied: float) -> str:
    """Interpret CLV performance."""
    if avg_clv_implied > 0.03:
        return "STRONG EDGE — consistently beating closing lines by 3%+. Sharp-level performance."
    elif avg_clv_implied > 0.015:
        return "POSITIVE EDGE — beating closing lines. Maintain approach, scale cautiously."
    elif avg_clv_implied > 0.005:
        return "SLIGHT EDGE — marginally beating close. Edge exists but thin. Increase volume."
    elif avg_clv_implied > -0.005:
        return "BREAK EVEN — tracking close to closing lines. No clear edge yet."
    elif avg_clv_implied > -0.015:
        return "SLIGHT NEGATIVE — slightly behind closing lines. Review bet selection process."
    else:
        return "NEGATIVE — consistently worse than closing lines. Current approach is -EV."
