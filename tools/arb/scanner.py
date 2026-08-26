"""Per-market pure-arb scan and dutch-book scan."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tools.arb.models import (
    ArbOpportunity,
    DEFAULT_BUDGET,
    DEFAULT_EPSILON,
    DEFAULT_STALE_SECONDS,
)
from tools.arb.prices import _collect_point_groups
from tools.arb.stakes import _build_arb_from_pair, _scan_spread_arbs


# ---------------------------------------------------------------------------
# Main per-market pure-arb scan.
# ---------------------------------------------------------------------------
def scan_pure_arb(
    game: dict,
    market_type: str,
    *,
    epsilon: float = DEFAULT_EPSILON,
    stale_seconds: float = DEFAULT_STALE_SECONDS,
    budget: float = DEFAULT_BUDGET,
    now: Optional[datetime] = None,
    allow_missing_ts: bool = False,
    sport: str = "",
) -> list[ArbOpportunity]:
    """Find pure arbs for one ``market_type`` in one ``game``.

    Returns a list because spreads/totals can have multiple valid point values
    simultaneously (e.g. DK on +2.5, FD on +3 for one team → we get one
    candidate per point value).

    ``allow_missing_ts=True`` is used by the backtest, because historical
    snapshot outcomes don't always have a fetched_at at the per-outcome level.
    In live scanning this stays False so we reject unstamped data.
    """
    if now is None:
        now = datetime.now(timezone.utc)

    arbs: list[ArbOpportunity] = []
    point_groups = _collect_point_groups(game, market_type)

    # For spreads: instead of grouping "all outcomes at point=X", we need to
    # pair each team's BEST price at their respective spread with the OTHER
    # team's BEST price at the OPPOSING spread. Team A at +X cover-bet is
    # the logical complement of Team B at -X cover-bet. A spread "arb" where
    # both legs are at the same absolute point value but the teams disagree
    # about who's favored is a data-contamination artifact (the feed has
    # BetMGM saying Team A is -1.5 while Bovada says Team B is -1.5).
    if market_type == "spreads":
        home_team = game.get("home_team", "")
        away_team = game.get("away_team", "")
        # Find distinct absolute-point values, then for each |X| pair Home@X
        # with Away@(-X) and Home@(-X) with Away@X — the latter covers home
        # favorite / home underdog cases symmetrically.
        abs_pts = {abs(pt) for pt in point_groups if pt is not None}
        spread_arbs = _scan_spread_arbs(
            game, home_team, away_team, abs_pts,
            epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
            now=now, allow_missing_ts=allow_missing_ts, sport=sport,
        )
        return spread_arbs

    for pt, outcomes in point_groups.items():
        # Need at least 2 outcomes (binary) or more for multi-way to compute
        # an arb.
        if len(outcomes) < 2:
            continue

        # Convert outcomes dict into the entry list shape _build_arb_from_pair
        # expects (each entry carries its own 'outcome' name).
        entries = []
        for name, entry in outcomes.items():
            e = dict(entry)
            e["outcome"] = name
            entries.append(e)

        arb = _build_arb_from_pair(
            game=game, entries=entries, market_type=market_type,
            epsilon=epsilon, stale_seconds=stale_seconds, budget=budget,
            now=now, allow_missing_ts=allow_missing_ts, sport=sport,
        )
        if arb is not None:
            arbs.append(arb)

    return arbs


def scan_dutch_book(
    game: dict,
    market_type: str,
    **kwargs,
) -> list[ArbOpportunity]:
    """Dutch-book = pure arb on a 3+-outcome market.

    The math is identical; we just tag it differently and let scan_pure_arb
    do the work. 3-way soccer moneyline is the canonical case.
    """
    arbs = scan_pure_arb(game, market_type, **kwargs)
    for a in arbs:
        if len(a.legs) >= 3:
            a.thesis_tag = "dutch"
            a.notes = (
                f"dutch book on {len(a.legs)}-way {market_type} "
                f"(total_implied={a.total_implied:.4f})"
            )
    return arbs
