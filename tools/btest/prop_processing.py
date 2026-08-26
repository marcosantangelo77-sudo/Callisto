"""Player-prop processing pipelines extracted from tools/backtest.py (slice 4).

Two paths feed player-prop events:
  - ``_process_game_props``: props embedded in a game's bookmakers payload
    (legacy inline format), devigged across non-target books.
  - ``_process_prop_snapshots``: rows fetched from the ``prop_snapshots``
    table by HistoricalOddsFetcher.fetch_prop_snapshots (multi-book,
    per player/market/line).

Both are pure with respect to state: the DB handle is passed in and used
only for batch inserts of pending event rows. ``tools/backtest.py`` remains
the public facade — BacktestEngine re-binds these as thin async methods so
call sites and signatures are unchanged.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from tools.btest.events_io import insert_pending_rows
from tools.btest.market_processing import (
    build_event_row as _build_event_row,
    clean_outliers as _clean_outliers,
    devig_pair as _devig_pair,
    index_props as _index_props,
)
from tools.devig import devig_american
from tools.ev import ev_binary
from tools.math_utils import american_to_decimal, american_to_implied
from tools.sizing import kelly_binary

logger = logging.getLogger("callisto.backtest")

# Hard cap on recorded edge magnitude — mirrors the game-lines path in
# market_processing. Phantom edges from stale one-sided quotes get clipped.
MAX_EDGE_MAGNITUDE = 0.15

# Require at least this many non-target books before flagging an inline-prop
# row as a signal — prevents 1-2 book phantom edges (same gate as game lines).
MIN_BOOKS_FOR_SIGNAL = 4

# Relaxed book requirement for prop_snapshots — prop markets are thinner
# and fewer books carry per-player lines.
MIN_BOOKS_FOR_PROP_SIGNAL = 2

# Heavy-favorite guard for moneyline-style props: fair prob above this is
# almost never a real edge regardless of implied odds.
MAX_FAIR_PROB_FOR_SIGNAL = 0.80


def clip_edge(edge: float) -> float:
    """Clamp |edge| to MAX_EDGE_MAGNITUDE, preserving sign."""
    if abs(edge) > MAX_EDGE_MAGNITUDE:
        return MAX_EDGE_MAGNITUDE if edge > 0 else -MAX_EDGE_MAGNITUDE
    return edge


async def process_game_props(
    db,
    run_id: str,
    hypothesis_id: str,
    game: dict,
    game_date: str,
    snapshot_time: str,
    market_type: str,
    target_book: str,
    edge_threshold: float,
    devig_method: str,
    min_books: int,
    config: dict,
    filters: Optional[dict] = None,
) -> tuple[int, int]:
    """
    Process player props for a game.
    For props, we need per-event prop data which may require separate API calls.
    If prop data is embedded in the game object, process directly.

    Returns (events_processed, signals_generated).
    """
    bookmakers = game.get("bookmakers", [])
    events = 0
    signals = 0
    _pending_rows: list[tuple] = []  # Collect rows for batch INSERT

    # Organize props: (player, market, line) -> book -> {Over, Under}
    prop_lines, book_names = _index_props(bookmakers, market_type)

    # Process each prop line
    for (player, mkt_key, line), books in prop_lines.items():
        if target_book not in books:
            continue
        target_data = books[target_book]
        if "Over" not in target_data or "Under" not in target_data:
            continue

        # Devig all books with both sides at this line
        # Track (fair_prob, book_key) for cross-book best-line detection
        fair_overs = []   # (fair_prob, book_key)
        fair_unders = []  # (fair_prob, book_key)
        for bk_key, bk_data in books.items():
            if bk_key == target_book:
                continue  # exclude target book from consensus
            if "Over" not in bk_data or "Under" not in bk_data:
                continue
            try:
                fo, fu = _devig_pair(bk_data["Over"], bk_data["Under"], devig_method)
                fair_overs.append((fo, bk_key))
                fair_unders.append((fu, bk_key))
            except (ValueError, ZeroDivisionError) as e:
                logger.warning(
                    f"Devig failed for book={bk_key}, market={mkt_key}, "
                    f"prices=(Over={bk_data['Over']}, Under={bk_data['Under']}): {e}"
                )
                continue

        non_target_count = len(fair_overs)
        if non_target_count < min_books:
            continue

        consensus_over = sum(v[0] for v in fair_overs) / non_target_count
        consensus_under = sum(v[0] for v in fair_unders) / non_target_count

        # ── Outlier filter (same logic as the game-lines path) ──
        OUTLIER_THRESHOLD = 0.15
        clean_overs = _clean_outliers(fair_overs, consensus_over)
        clean_unders = _clean_outliers(fair_unders, consensus_under)

        # Cross-book best line: sharpest devigged fair prob for each side
        best_over_val, best_over_book = max(clean_overs, key=lambda x: x[0])
        best_under_val, best_under_book = max(clean_unders, key=lambda x: x[0])

        use_crossbook = non_target_count >= 3
        contributing_books = [bk for _, bk in fair_overs]

        for side, consensus, best_val, best_book, target_price in [
            ("Over", consensus_over, best_over_val, best_over_book, target_data["Over"]),
            ("Under", consensus_under, best_under_val, best_under_book, target_data["Under"]),
        ]:
            # Apply side_filter from hypothesis filters (e.g. "Over" or "Under")
            if filters and "side_filter" in filters:
                if side.lower() != filters["side_filter"].lower():
                    continue

            fair_val = best_val if use_crossbook else consensus
            edge_method = "cross_book_best_line" if use_crossbook else "consensus_devig"

            target_implied = american_to_implied(target_price)
            ev = ev_binary(fair_val, american_to_decimal(target_price))
            kelly = kelly_binary(fair_val, american_to_decimal(target_price))
            edge = fair_val - target_implied  # Probability edge (not EV)
            edge = clip_edge(edge)

            heavy_fav = (mkt_key == "h2h"
                         and fair_val > MAX_FAIR_PROB_FOR_SIGNAL)
            is_signal = (edge >= edge_threshold
                         and non_target_count >= MIN_BOOKS_FOR_SIGNAL
                         and not heavy_fav)

            events += 1
            if is_signal:
                signals += 1

            event_id = game.get("id", "")

            _pending_rows.append(_build_event_row(
                run_id=run_id,
                event_id=event_id,
                hypothesis_id=hypothesis_id,
                sport=game.get("sport_key", ""),
                player=player,
                market=mkt_key,
                line=line,
                side=side,
                book=target_book,
                target_price=target_price,
                target_implied=round(target_implied, 6),
                fair_val=fair_val,
                factors={
                    "edge_method": edge_method,
                    "books_used": non_target_count,
                    "devig_method": devig_method,
                    "best_line_book": best_book,
                    "best_line_fair_prob": round(best_val, 6),
                    "consensus_fair_prob": round(consensus, 6),
                    "contributing_books": contributing_books,
                },
                edge=round(edge, 6),
                ev=round(ev, 6),
                kelly=round(kelly, 6),
                is_signal=is_signal,
                game_date=game_date,
                snapshot_time=snapshot_time,
            ))

    # Batch INSERT all rows in one transaction
    await insert_pending_rows(
        db, _pending_rows, operation="backtest props_batch_insert"
    )
    return events, signals


async def process_prop_snapshots(
    db,
    run_id: str,
    hypothesis_id: str,
    prop_lines: list[dict],
    target_book: str,
    edge_threshold: float,
    devig_method: str,
    config: dict,
    h_sport: str,
    filters: Optional[dict] = None,
) -> tuple[int, int]:
    """Process prop_snapshots data for player prop backtesting.

    Each prop_line is a dict with multi-book data for one player/market/line.
    We devig the non-target books to get fair probability, then compute
    edge vs target book.

    Returns (total_events, total_signals).
    """
    events = 0
    signals = 0
    _pending_rows = []

    for prop in prop_lines:
        player = prop["player"]
        market = prop["market"]
        line = prop["line"]
        event_id = prop["event_id"]
        game_date = prop["game_date"]
        books_data = prop["books"]

        # Side filter from hypothesis
        side_filter = None
        if filters and "side_filter" in filters:
            side_filter = filters["side_filter"].lower()

        # Group books by side
        over_books = [b for b in books_data if b["side"].lower() == "over"]
        under_books = [b for b in books_data if b["side"].lower() == "under"]

        # Need at least Over + Under from different books for devig
        if not over_books or not under_books:
            continue

        # Find target book entries
        target_over = [b for b in over_books if b["book"].lower() == target_book]
        target_under = [b for b in under_books if b["book"].lower() == target_book]

        # Non-target books for consensus
        non_target_over = [b for b in over_books if b["book"].lower() != target_book]
        non_target_under = [b for b in under_books if b["book"].lower() != target_book]
        non_target_count = len(set(b["book"] for b in non_target_over + non_target_under))

        # Skip if no target book data
        if not target_over and not target_under:
            continue

        # Devig non-target books for fair probability
        fair_overs = []
        for bo in non_target_over:
            # Find matching under from same book
            matching_under = [bu for bu in non_target_under if bu["book"] == bo["book"]]
            if matching_under:
                try:
                    # NOTE: fixed during slice-4 extraction — the facade copy
                    # read a nonexistent ``fair_prob_1`` key here (silently
                    # swallowed by the except below), so every lookup failed
                    # and prop_snapshots produced 0 events. Use the real
                    # side_a.fair_prob contract of devig_american.
                    result = devig_american(bo["price_american"], matching_under[0]["price_american"])
                    fair_overs.append((result["side_a"]["fair_prob"], bo["book"]))
                except Exception:
                    continue

        if not fair_overs:
            continue

        consensus_over = sum(f for f, _ in fair_overs) / len(fair_overs)
        consensus_under = 1.0 - consensus_over

        for side, consensus_fair, target_entries in [
            ("Over", consensus_over, target_over),
            ("Under", consensus_under, target_under),
        ]:
            if side_filter and side.lower() != side_filter:
                continue
            if not target_entries:
                continue

            target_price = target_entries[0]["price_american"]
            target_implied = american_to_implied(target_price)
            edge = clip_edge(consensus_fair - target_implied)
            ev = ev_binary(consensus_fair, american_to_decimal(target_price))
            kelly = kelly_binary(consensus_fair, american_to_decimal(target_price))

            is_signal = (edge >= edge_threshold
                         and non_target_count >= MIN_BOOKS_FOR_PROP_SIGNAL)

            events += 1
            if is_signal:
                signals += 1

            _pending_rows.append((
                run_id, event_id, hypothesis_id, h_sport,
                player, market, line, side, target_book,
                target_price, round(target_implied, 6),
                round(consensus_fair, 6),
                json.dumps({
                    "edge_method": "consensus_devig",
                    "books_used": non_target_count,
                    "devig_method": devig_method,
                    "target_book": target_book,
                    "consensus_fair_prob": round(consensus_fair, 6),
                    "contributing_books": [bk for _, bk in fair_overs],
                    "data_source": "prop_snapshots",
                }),
                round(edge, 6), round(ev, 6), round(kelly, 6),
                is_signal, game_date, game_date,
            ))

    # Batch INSERT
    await insert_pending_rows(
        db, _pending_rows, operation="backtest prop_snapshots_batch_insert"
    )
    return events, signals
