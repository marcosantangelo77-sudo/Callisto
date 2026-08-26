"""Regime-aware sizing helpers (feat/regime-aware-sizing, 2026-04-22).

``clamped_regime_multiplier`` and ``regime_safe`` are the pure lookup/clamp
helpers extracted from ``tools/bet_executor``. They read the gate flags via
a ``gates`` mapping supplied by the caller so the facade can keep its
module-level attributes authoritative (runtime monkeypatching keeps working).
"""

import logging

from tools.betexec.config import REGIME_MIN_MULT, REGIME_MAX_MULT

logger = logging.getLogger("callisto.executor")


def clamped_regime_multiplier(
    sport: str,
    gates: dict | None = None,
) -> float:
    """Fetch current_regime_multiplier(sport) and clamp to [MIN_MULT, MAX_MULT].

    Any exception (DB missing, import error) degrades to 1.0 so sizing never
    fails closed due to the regime module. The whole feature is gated by
    CALLISTO_REGIME_SIZING so callers can disable wholesale.

    ``gates``: optional {"sizing_enabled": bool} override; defaults to True
    (i.e. consult the regime module). The facade passes its own live flag.
    """
    gates = gates or {}
    if not gates.get("sizing_enabled", True):
        return 1.0
    try:
        from tools.market_regime import current_regime_multiplier
        m = float(current_regime_multiplier(sport))
    except Exception as e:
        logger.debug(f"regime multiplier lookup failed for {sport}: {e}; using 1.0")
        return 1.0
    return max(REGIME_MIN_MULT, min(REGIME_MAX_MULT, m))


def regime_safe(sport: str, gates: dict | None = None) -> tuple[bool, str]:
    """Return (safe, phase) for ``sport``. Safe=True when gate disabled or OK.

    Second value is the season_phase string so callers can include it in log
    lines (``regime_unsafe_phase=preseason`` etc). Any error degrades to safe.

    ``gates``: optional {"safety_enabled": bool} override.
    """
    gates = gates or {}
    if not gates.get("safety_enabled", True):
        return True, ""
    try:
        from tools.market_regime import regime_safe_for_trading, detect_regime
        safe = bool(regime_safe_for_trading(sport))
        phase = ""
        if not safe:
            try:
                phase = detect_regime(sport).season_phase or ""
            except Exception:
                phase = ""
        return safe, phase
    except Exception as e:
        logger.debug(f"regime safety lookup failed for {sport}: {e}; treating as safe")
        return True, ""
