"""Live edge scanner — glue between odds_api_io and the quant engine.

Pulls the most recent per-sport odds from the provider, reshapes each
market into ``MarketSnapshot`` rows, runs the ranker, and persists the
output to ``live_edge_surface`` so the API endpoint and Telegram
listener can serve recommendations without recomputing.

The scanner is safe to call at high frequency (once per minute is the
target cadence). It is idempotent with respect to the snapshot
timestamp — each call inserts one new set of rows; consumers join on
``MAX(computed_at)`` to get the current ranking.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.odds_api import calculate_implied_probability

from tools.devig import validate_implied_book

from .consensus_engine import BookLine
from .edge_ranker import MarketSnapshot, persist_ranked_edges, rank_edges

logger = logging.getLogger("callisto.quant.scanner")


def _snapshot_rows_from_games(
    games: list[dict],
    sport: str,
    placement_books: set[str],
) -> list[MarketSnapshot]:
    """Turn an odds-api-io ``games`` list into ``MarketSnapshot`` rows.

    For each game × market × outcome we emit one snapshot PER placement
    book when that book offers the outcome. Non-placement books feed
    the consensus calculation for every snapshot. Markets handled:
    h2h, spreads, totals. For two-way markets we supply
    ``paired_implied_prob`` so the devig math uses the exact formula.
    """
    out: list[MarketSnapshot] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    for game in games:
        event_id = str(game.get("id") or game.get("event_id") or "")
        if not event_id:
            continue
        commence = game.get("commence_time")
        home = game.get("home_team") or ""
        away = game.get("away_team") or ""

        # Pivot into (market_key, outcome_name, point) -> {book: BookLine}.
        # ``point`` is critical: DK's "Cleveland -1.5" and Pinnacle's
        # "Cleveland -2.5" are DIFFERENT bets. Grouping them together
        # feeds the consensus engine apples-vs-oranges and manufactures
        # impossible 10-25% edges on liquid markets. For h2h markets
        # every outcome has point=None, which collapses to the old
        # behavior. For spreads/totals each alt-line gets its own
        # bucket, as it should.
        books_by_market: dict[
            tuple[str, str, Optional[float]], dict[str, BookLine]
        ] = {}
        for bm in game.get("bookmakers", []) or []:
            book = (bm.get("key") or bm.get("title") or "").lower()
            if not book:
                continue
            updated_at = bm.get("last_update") or now_iso
            for market in bm.get("markets", []) or []:
                mkey = market.get("key") or ""
                outcomes = market.get("outcomes", []) or []
                if len(outcomes) != 2:
                    # Only two-way markets supported for now; spreads with
                    # alt-lines come as separate two-way pairs anyway.
                    continue
                parsed: list[tuple[str, float, Optional[float]]] = []
                for o in outcomes:
                    name = o.get("name") or ""
                    price = o.get("price")
                    point = o.get("point")
                    if not name or price is None:
                        continue
                    point_f: Optional[float]
                    try:
                        point_f = float(point) if point is not None else None
                    except (TypeError, ValueError):
                        point_f = None
                    try:
                        imp_p = calculate_implied_probability(price)
                    except Exception:
                        continue
                    parsed.append((name, imp_p, point_f))
                if len(parsed) != 2:
                    continue
                # Market-sanity gate on the SOURCE book before it can feed
                # the consensus: a zero-hold, crossed, excessive-hold, or
                # non-finite pair must never create a trusted fair value.
                try:
                    validate_implied_book([parsed[0][1], parsed[1][1]])
                except ValueError:
                    logger.debug(
                        "scanner dropping invalid book %s market %s/%s: "
                        "failed market-sanity gate", book, mkey,
                        [p[0] for p in parsed])
                    continue
                for idx in range(2):
                    name, imp_p, point_f = parsed[idx]
                    _, sib_imp, _ = parsed[1 - idx]
                    key = (mkey, name, point_f)
                    entry = books_by_market.setdefault(key, {})
                    entry[book] = BookLine(
                        book=book,
                        implied_prob=imp_p,
                        paired_implied_prob=sib_imp,
                        updated_at=updated_at,
                    )

        # Emit one MarketSnapshot per placement book per (market, outcome,
        # point) — but only when we also have at least one OTHER book to
        # build a consensus against.
        for (mkey, outcome_name, point_f), book_lines in books_by_market.items():
            if len(book_lines) < 2:
                continue
            outcome_label = outcome_name
            if point_f is not None:
                if mkey == "spreads":
                    outcome_label = f"{outcome_name} {point_f:+g}"
                else:
                    outcome_label = f"{outcome_name} {point_f:g}"
            for placement_book in placement_books:
                if placement_book not in book_lines:
                    continue
                snap = MarketSnapshot(
                    sport=sport,
                    event_id=event_id,
                    market=mkey,
                    outcome=f"{away} @ {home} | {outcome_label}",
                    placement_line=book_lines[placement_book],
                    all_lines=list(book_lines.values()),
                    commence_time=(
                        datetime.fromisoformat(commence.replace("Z", "+00:00"))
                        if commence else None
                    ),
                )
                out.append(snap)
    return out


async def scan_sport(
    sport: str,
    db,
    *,
    placement_books: Optional[set[str]] = None,
    min_recommend_edge: float = 0.02,
    top_n_per_sport: int = 25,
) -> dict:
    """Scan one sport: fetch current odds, rank, persist. Returns stats."""
    placement_books = placement_books or {"draftkings", "fanatics"}
    from tools.odds_api_io import get_odds as _get_odds_io
    try:
        payload = await _get_odds_io(sport)
    except Exception as e:
        logger.warning(f"scan_sport({sport}) odds fetch failed: {e}")
        return {"sport": sport, "error": str(e), "snapshots": 0}

    games = (payload or {}).get("games") or []
    if not games:
        return {"sport": sport, "games": 0, "snapshots": 0, "recommended": 0}

    snapshots = _snapshot_rows_from_games(games, sport, placement_books)
    if not snapshots:
        return {"sport": sport, "games": len(games), "snapshots": 0, "recommended": 0}

    ranked = rank_edges(
        snapshots,
        min_recommend_edge=min_recommend_edge,
        top_n=top_n_per_sport,
    )
    await persist_ranked_edges(db, ranked)
    rec = sum(1 for e in ranked if e.decision == "recommended")
    logger.info(
        f"quant.scanner {sport}: {len(games)} games, "
        f"{len(snapshots)} snapshots, {rec} recommended edges"
    )
    return {
        "sport": sport,
        "games": len(games),
        "snapshots": len(snapshots),
        "recommended": rec,
        "ranked": len(ranked),
    }


async def scan_all_sports(
    sports: list[str],
    db,
    *,
    placement_books: Optional[set[str]] = None,
    min_recommend_edge: float = 0.02,
    top_n_per_sport: int = 25,
) -> dict:
    """Scan every sport in the list sequentially. Returns per-sport stats."""
    results = {}
    total_rec = 0
    for sport in sports:
        r = await scan_sport(
            sport, db,
            placement_books=placement_books,
            min_recommend_edge=min_recommend_edge,
            top_n_per_sport=top_n_per_sport,
        )
        results[sport] = r
        total_rec += r.get("recommended", 0)
    return {"per_sport": results, "total_recommended": total_rec}
