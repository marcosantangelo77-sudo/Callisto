"""Snapshot-level orchestration: full_arbitrage_scan."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tools.arb.models import (
    ArbOpportunity,
    DEFAULT_BUDGET,
    DEFAULT_EPSILON,
    DEFAULT_STALE_SECONDS,
)
from tools.arb.scanner import scan_pure_arb
from tools.arb.synthetic import scan_cross_market_synthetic


# ---------------------------------------------------------------------------
# Snapshot-level orchestrator.
# ---------------------------------------------------------------------------
def full_arbitrage_scan(
    snapshot: dict,
    *,
    epsilon: float = DEFAULT_EPSILON,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    budget: float = DEFAULT_BUDGET,
    now: Optional[datetime] = None,
    include_synthetic: bool = True,
    allow_missing_ts: bool = False,
) -> dict:
    """Run all arbitrage scans over one snapshot dict.

    Returns a dict with per-thesis arb lists plus a summary.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    games = snapshot.get("games", [])
    sport = snapshot.get("sport", "")

    pure: list[ArbOpportunity] = []
    dutch: list[ArbOpportunity] = []
    synthetic: list[ArbOpportunity] = []

    for game in games:
        for mkt in ("h2h", "spreads", "totals"):
            arbs = scan_pure_arb(
                game, mkt,
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, allow_missing_ts=allow_missing_ts, sport=sport,
            )
            for a in arbs:
                if a.thesis_tag == "dutch":
                    dutch.append(a)
                else:
                    pure.append(a)

        if include_synthetic:
            synthetic.extend(scan_cross_market_synthetic(
                game,
                epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
                now=now, sport=sport,
            ))

    limited_count = sum(1 for a in pure + dutch if a.limited_by_book_caps)
    return {
        "sport": sport,
        "game_count": len(games),
        "pure_arbs": [a.to_dict() for a in pure],
        "dutch_books": [a.to_dict() for a in dutch],
        "synthetic_arbs": [a.to_dict() for a in synthetic],
        "summary": {
            "pure_count": len(pure),
            "dutch_count": len(dutch),
            "synthetic_count": len(synthetic),
            "limited_by_book_caps": limited_count,
            "scan_time": now.isoformat(),
            "params": {
                "epsilon": epsilon,
                "stale_seconds": stale_seconds,
                "budget": budget,
            },
        },
    }
