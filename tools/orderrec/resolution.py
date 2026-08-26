"""Per-market settlement resolution (split from ``tools/order_reconciler``)."""

from __future__ import annotations

from typing import Optional

from tools.orderrec.odds import _team_matches
from tools.orderrec.markets import (
    _normalise_market,
    _parse_side_for_total,
)
from tools.orderrec.results import (
    _lookup_game_result,
    _lookup_player_stat,
)


def _resolve_moneyline(side: str, game: dict) -> Optional[str]:
    """Return ``win|loss|push`` or None if undecidable."""
    winner = (game.get("winner") or "").strip()
    if not winner:
        return None
    if winner.lower() == "push":
        return "push"
    if _team_matches(side, winner):
        return "win"
    # Make sure the other team actually appeared on the card before
    # declaring loss — protects against partial data.
    home = game.get("home_team") or ""
    away = game.get("away_team") or ""
    if _team_matches(side, home) or _team_matches(side, away):
        return "loss"
    return None


def _resolve_spread(side: str, line: Optional[float], game: dict) -> Optional[str]:
    """Standard spread settlement.

    ``line`` is signed from the bettor's perspective (e.g. -7.5 means
    "side laid 7.5 points"). ``game.spread_result`` is stored as
    ``away_score - home_score`` by data_collector; we recompute from
    (home_score, away_score) so we don't depend on that convention.
    """
    if line is None:
        return None
    home = game.get("home_team") or ""
    away = game.get("away_team") or ""
    h, a = game.get("home_score"), game.get("away_score")
    if h is None or a is None:
        return None
    if _team_matches(side, home):
        margin = float(h) - float(a)
    elif _team_matches(side, away):
        margin = float(a) - float(h)
    else:
        return None
    diff = margin + float(line)
    if abs(diff) < 1e-9:
        return "push"
    return "win" if diff > 0 else "loss"


def _resolve_total(side: str, line: Optional[float], game: dict) -> Optional[str]:
    ou = _parse_side_for_total(side)
    if ou is None or line is None:
        return None
    total = game.get("total_score")
    if total is None:
        h, a = game.get("home_score"), game.get("away_score")
        if h is None or a is None:
            return None
        total = int(h) + int(a)
    diff = float(total) - float(line)
    if abs(diff) < 1e-9:
        return "push"
    if ou == "over":
        return "win" if diff > 0 else "loss"
    return "win" if diff < 0 else "loss"


def _resolve_player_prop(
    side: str, line: Optional[float], stat_value: Optional[float]
) -> Optional[str]:
    ou = _parse_side_for_total(side)
    if ou is None or line is None or stat_value is None:
        return None
    diff = float(stat_value) - float(line)
    if abs(diff) < 1e-9:
        return "push"
    if ou == "over":
        return "win" if diff > 0 else "loss"
    return "win" if diff < 0 else "loss"


async def _resolve_sgp(db, notes: Optional[str], sport: str) -> Optional[str]:
    """All-legs-must-hit resolution for same-game parlays.

    Legs persisted as JSON in ``notes`` under ``legs=[...]``, each leg
    a dict with keys: ``market``, ``event_id``, ``side``, ``line``,
    ``player``, ``stat_type``. One unresolved leg -> None (skip).
    One losing leg -> loss immediately. All winners -> win. Any push on
    a still-winning ticket degrades to win on the pushed legs dropping.
    """
    from tools.orderrec.markets import _parse_legs

    if not notes:
        return None
    legs = _parse_legs(notes)
    if not legs:
        return None
    outcomes: list[str] = []
    for leg in legs:
        mkt = _normalise_market(leg.get("market"))
        side = leg.get("side") or ""
        line = leg.get("line")
        eid = leg.get("event_id") or ""
        if mkt == "player_props":
            stat_value = await _lookup_player_stat(
                db, sport, eid, leg.get("player", ""), leg.get("stat_type", ""),
            )
            outcome = _resolve_player_prop(side, line, stat_value)
        else:
            game = await _lookup_game_result(db, sport, eid)
            if game is None:
                return None
            if mkt == "h2h":
                outcome = _resolve_moneyline(side, game)
            elif mkt == "spreads":
                outcome = _resolve_spread(side, line, game)
            elif mkt == "totals":
                outcome = _resolve_total(side, line, game)
            else:
                return None
        if outcome is None:
            return None
        if outcome == "loss":
            return "loss"  # one dead leg -> whole ticket loses
        outcomes.append(outcome)
    # No losing legs. Pure wins -> win. Pushes reduce the parlay but
    # since we don't recompute price, any all-push ticket -> push.
    if all(o == "push" for o in outcomes):
        return "push"
    return "win"
