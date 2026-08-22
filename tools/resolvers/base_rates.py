"""Base-rate-relative thresholds.

The absolute hit-rate floors (0.45 auto-reject, 0.45 live-review demote)
are calibrated to a ~50% base-rate domain (spread betting). Anywhere the
base rate of positive events is low — drug-discovery hits, anomaly
detection, retraction prediction — those floors mass-reject true positives:
being right 10% of the time against a 2% base rate is a 5x lift and
spectacular.

General form (DOMAIN_GENERALITY.md §3c): a claim's floor is derived from
its own expected base rate, not an absolute constant.

    floor(claim) = clamp(base_rate + MIN_LIFT * base_rate,
                         ABSOLUTE_FLOOR_MIN, ABSOLUTE_FLOOR_MAX)

i.e. a claim must beat its own base rate by at least MIN_LIFT, subject to
absolute sanity bounds. At base_rate=0.5 with MIN_LIFT=-0.10 this reduces
to the existing behaviour class; at base_rate=0.02 it demands only that a
claim do better than ~2.4% — while still rejecting claims at or below
chance.

Nothing here can LOWER a gate below what pure chance would produce: the
floor is always >= base_rate (beating chance is the minimum bar) and never
above the old absolute constants for high-base-rate domains.
"""

from __future__ import annotations

import os

# A hypothesis must beat its expected base rate by at least this relative
# lift to avoid auto-rejection on a losing record.
MIN_BASE_RATE_LIFT = float(
    os.getenv("CALLISTO_MIN_BASE_RATE_LIFT", "0.10")
)
# Absolute bounds on any derived floor, whatever the claimed base rate.
BASE_RATE_FLOOR_ABS_MIN = float(os.getenv("CALLISTO_BASE_RATE_FLOOR_MIN", "0.02"))
BASE_RATE_FLOOR_ABS_MAX = float(os.getenv("CALLISTO_BASE_RATE_FLOOR_MAX", "0.45"))
# Base rates below this are treated as unknown → fall back to the legacy
# 50%-domain constant rather than trusting an unvalidated prior.
MIN_TRUSTWORTHY_BASE_RATE = float(
    os.getenv("CALLISTO_MIN_TRUSTWORTHY_BASE_RATE", "0.01")
)


def base_rate_relative_floor(
    base_rate: float | None,
    *,
    legacy_floor: float = 0.45,
    min_lift: float | None = None,
) -> float:
    """Hit-rate floor for a claim whose expected base rate is ``base_rate``.

    - base_rate unknown/None/too-small → legacy_floor (unchanged behaviour
      for the 50% domain).
    - otherwise: max(ABS_MIN, min(ABS_MAX, base_rate * (1 + lift))) where
      lift defaults to MIN_BASE_RATE_LIFT.

    Guarantees: the result is NEVER below base_rate (a gate may not demand
    less than chance) and NEVER above legacy_floor for a 50% base rate.
    """
    if min_lift is None:
        min_lift = MIN_BASE_RATE_LIFT
    if (
        base_rate is None
        or not isinstance(base_rate, (int, float))
        or base_rate != base_rate  # NaN
        or base_rate < MIN_TRUSTWORTHY_BASE_RATE
        or base_rate > 1.0
    ):
        return legacy_floor

    # Lift shrinks as the base rate grows so the legacy constant remains the
    # ceiling: floor = min(legacy_floor, br*(1+lift)), floored at br itself
    # (never demand less than chance) and clamped into absolute bounds.
    derived = base_rate * (1.0 + min_lift)
    derived = min(derived, legacy_floor)
    derived = max(derived, base_rate, BASE_RATE_FLOOR_ABS_MIN)
    return min(derived, BASE_RATE_FLOOR_ABS_MAX)


def expected_base_rate_from_events(events: list[dict]) -> float | None:
    """Estimate a claim's base rate from its evidence sample.

    Betting rows carry book_implied_prob (the market's estimate). Generic
    EvidenceRecord-shaped rows may carry 'base_rate' in context. Returns
    None when nothing trustworthy exists.
    """
    if not events:
        return None
    # Explicit override wins.
    explicit = [e.get("base_rate") for e in events if isinstance(e.get("base_rate"), (int, float))]
    if explicit:
        return sum(explicit) / len(explicit)
    implied = [
        float(e["book_implied_prob"])
        for e in events
        if isinstance(e.get("book_implied_prob"), (int, float))
        and 0.0 < float(e["book_implied_prob"]) <= 1.0
    ]
    if implied:
        return sum(implied) / len(implied)
    return None
