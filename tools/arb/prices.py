"""Timestamp freshness helpers and best-price collection for the arb scanner."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from tools.book_keys import canonicalize_book
from tools.math_utils import american_to_decimal


# ---------------------------------------------------------------------------
# Timestamp helpers — mirror edge_scanner semantics but strict.
# ---------------------------------------------------------------------------
def _parse_ts(s: object) -> Optional[datetime]:
    if s is None:
        return None
    try:
        if isinstance(s, datetime):
            return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
        txt = str(s)
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_seconds(line_ts: Optional[str], now: datetime) -> Optional[float]:
    dt = _parse_ts(line_ts)
    if dt is None:
        return None
    return (now - dt).total_seconds()


def _extract_line_ts(outcome: dict, bm: dict) -> Optional[str]:
    """Pick the best available freshness stamp for an outcome.

    Preference order:
      1. outcome.fetched_at  (our own ingest stamp, most meaningful)
      2. bm.fetched_at       (bookmaker-level stamp from line_monitor)
      3. bm.last_update      (the book's self-reported stamp)
    """
    for cand in (outcome.get("fetched_at"), bm.get("fetched_at"), bm.get("last_update")):
        if cand:
            return cand
    return None


# ---------------------------------------------------------------------------
# Best price per outcome per market, across all books in a game dict.
# ---------------------------------------------------------------------------
def _collect_best_prices(
    game: dict,
    market_type: str,
    point_value: Optional[float] = None,
) -> dict[str, dict]:
    """Return {outcome_name: {price, bookmaker, bookmaker_canonical, point,
    fetched_at, decimal}} — the highest decimal price per outcome.

    If ``point_value`` is given (for spreads/totals), only outcomes at that
    exact point are considered. This matters: a +3 spread at DK and a +3.5
    spread at BetMGM are different bets and averaging them together creates
    phantom arbs.
    """
    best: dict[str, dict] = {}
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            for outcome in mkt.get("outcomes", []):
                name = outcome.get("name")
                price = outcome.get("price")
                if name is None or price is None:
                    continue
                pt = outcome.get("point")
                if point_value is not None and pt != point_value:
                    continue
                try:
                    dec = american_to_decimal(int(price))
                except (TypeError, ValueError):
                    continue
                if dec <= 1.0:
                    continue
                entry = {
                    "american": int(price),
                    "decimal": dec,
                    "implied": 1.0 / dec,
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "bookmaker_canonical": canonicalize_book(
                        bm.get("key") or bm.get("title") or ""
                    ),
                    "point": pt,
                    "fetched_at": _extract_line_ts(outcome, bm),
                }
                prev = best.get(name)
                if prev is None or dec > prev["decimal"]:
                    best[name] = entry
    return best


def _collect_point_groups(game: dict, market_type: str) -> dict[Optional[float], dict]:
    """Group outcomes by point value for spreads/totals. Returns
    {point: {outcome_name: best_entry}}. For h2h there is no point, so we
    return {None: {...}}.
    """
    if market_type == "h2h":
        return {None: _collect_best_prices(game, market_type)}

    # Collect every distinct point we've seen across all books so we can
    # iterate and call the single-point version. This keeps the invariant
    # "all legs at the same point" clean.
    points: set = set()
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            for o in mkt.get("outcomes", []):
                pt = o.get("point")
                if pt is not None:
                    points.add(pt)

    out: dict[Optional[float], dict] = {}
    for pt in points:
        grp = _collect_best_prices(game, market_type, point_value=pt)
        if grp:
            out[pt] = grp
    return out


def _best_at(game: dict, market: str, team: str, point: float) -> Optional[dict]:
    """Return the best (highest-decimal) offer for ``team`` at exactly ``point``.

    Used by the spread arb pairing logic. Returns None if no book has an
    offer for that team/point combo.
    """
    best = None
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt.get("key") != market:
                continue
            for o in mkt.get("outcomes", []):
                if o.get("name") != team or o.get("point") != point:
                    continue
                try:
                    dec = american_to_decimal(int(o.get("price")))
                except (TypeError, ValueError):
                    continue
                if dec <= 1.0:
                    continue
                entry = {
                    "american": int(o["price"]),
                    "decimal": dec,
                    "implied": 1.0 / dec,
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "bookmaker_canonical": canonicalize_book(
                        bm.get("key") or bm.get("title") or ""
                    ),
                    "point": point,
                    "fetched_at": _extract_line_ts(o, bm),
                    "outcome": team,
                }
                if best is None or dec > best["decimal"]:
                    best = entry
    return best
