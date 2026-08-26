"""Game-line processing for the backtest engine.

Extracted from tools/backtest.py (slice 5). These are the last two big
bodies in the facade module:

  - process_game       — market resolution + multi-book gating
  - process_game_lines — cross-book edge detection over spreads/totals/h2h

Both take the BacktestEngine instance (only used for ``_db``-free helpers
and hypothesis-filter matching) so call sites and signatures are unchanged:
``BacktestEngine._process_game`` / ``._process_game_lines`` remain thin
async delegators.
"""

from __future__ import annotations

import logging
from typing import Optional

from tools.btio.filters import matches_hypothesis_conditions
from tools.btest.market_processing import (
    SHARP_BOOKS,
    OUTLIER_THRESHOLD,
    build_event_row,
    clean_outliers,
    collect_book_snapshot_quality,
    devig_pair,
    effective_game_market,
    evaluate_side,
    group_sides,
    index_lines_by_key,
)

logger = logging.getLogger("callisto.backtest")


async def process_game(
    engine,
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
    h_sport: str = "",
    thesis: str = "",
    filters: Optional[dict] = None,
) -> tuple[int, int, list]:
    """
    Process a single game: devig, compare, record predictions.
    Returns (events_processed, signals_generated).

    Cross-book edge detection requires the target book AND at least one
    other book in the data. When only a "consensus" book exists (old
    historical data without the target book), we skip — there's no
    cross-book edge to detect without pricing from both sides.

    Falls back to game-level markets (spreads/h2h/totals) when
    player prop data isn't available, since our free historical
    data is consensus game lines, not per-player props.
    """
    # Determine available markets in this game
    available_markets = set()
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            available_markets.add(mkt["key"])

    # If hypothesis wants player props but we only have game lines,
    # fall back to the closest game-level market
    effective_market = effective_game_market(market_type, available_markets)
    if effective_market is None:
        return 0, 0, []

    available_books = {bm.get("key", "").lower() for bm in game.get("bookmakers", [])}
    bookmaker_count = len(available_books)

    # Multi-book edge detection: need at least 3 books total.
    # Need at least min_books+1 total (min_books for consensus + 1 target).
    # For thin markets (NCAAW, NWSL) with consensus_min_books=2, allow 2 total.
    required_total = max(2, min_books + 1)
    if bookmaker_count < required_total:
        return 0, 0, []

    # target_book is now just a hint — process_game_lines evaluates
    # ALL soft books against the consensus. No single-book dependency.
    effective_target = target_book
    effective_min_books = max(2, min(min_books, bookmaker_count - 1))

    return await process_game_lines(
        engine, run_id, hypothesis_id, game, game_date, snapshot_time,
        effective_market, effective_target, edge_threshold, devig_method,
        effective_min_books, config, h_sport=h_sport, filters=filters,
    )


async def process_game_lines(
    engine,
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
    h_sport: str = "",
    filters: Optional[dict] = None,
) -> tuple[int, int, list]:
    """Process spreads/totals/h2h lines for a game.

    Uses cross-book edge detection when multi-book data is available:
    1. Devig each non-target book to get fair probabilities
    2. Find the BEST (sharpest) devigged line across non-target books
    3. Also compute consensus (average) devigged fair value
    4. Use the best line as the fair value — edges exist BETWEEN books
    5. Fall back to consensus-only when only 1-2 non-target books exist
    """
    home = game.get("home_team", "")
    away = game.get("away_team", "")
    bookmakers = game.get("bookmakers", [])

    events = 0
    signals = 0
    _pending_rows: list[tuple] = []  # Collect rows for batch INSERT

    # Per-book snapshot_quality: 'pre_commence' | 'closing_fallback' |
    # 'closing_mode'. Gets embedded into model_factors on every event row
    # so the promotion gate can aggregate on the hypothesis level without
    # re-fetching the upstream snapshot. Defaults to 'pre_commence' for
    # books that don't emit it (legacy / synthetic test data) — the
    # promotion gate only rejects when the sample is >=20% fallback.
    book_snapshot_quality = collect_book_snapshot_quality(bookmakers)
    lines_by_key = index_lines_by_key(bookmakers, market_type)

    # For each unique line, find the opposite side and devig
    # Group by (market, point); spreads pair by abs(point) so
    # -7.5/+7.5 group correctly
    sides_by_line, signed_points = group_sides(lines_by_key)

    for (mkt_key, point), sides in sides_by_line.items():
        side_names = list(sides.keys())
        if len(side_names) != 2:
            continue

        side_a_name, side_b_name = side_names[0], side_names[1]
        side_a_books = sides[side_a_name]
        side_b_books = sides[side_b_name]

        # Find books that have both sides
        common_books = set(side_a_books.keys()) & set(side_b_books.keys())
        if len(common_books) < min_books + 1:  # Need min_books for consensus + 1 target
            continue

        # Devig ALL books to get fair values
        all_fair_a = {}  # book_key -> fair_prob_a
        all_fair_b = {}  # book_key -> fair_prob_b
        for bk in common_books:
            price_a = side_a_books[bk]["price"]
            price_b = side_b_books[bk]["price"]
            try:
                fa, fb = devig_pair(price_a, price_b, devig_method)
                all_fair_a[bk] = fa
                all_fair_b[bk] = fb
            except (ValueError, ZeroDivisionError) as e:
                logger.warning(
                    f"Devig failed for book={bk}, market={mkt_key}, "
                    f"prices=({price_a}, {price_b}): {e}"
                )
                continue

        # Need at least min_books devigged books for a reliable consensus.
        required_devigged = max(2, min_books)
        if len(all_fair_a) < required_devigged:
            continue

        # --- Multi-book edge detection ---
        # For EACH book as potential target, compute consensus from all
        # other books and measure the edge. This finds the best mispricing
        # across ALL books, not just DraftKings.
        #
        # Sharp books (Pinnacle, Circa, etc.) are excluded as targets —
        # they set the true line. Only soft/retail books are tested.
        for eval_target in common_books:
            # Only evaluate retail/soft books as targets
            if eval_target in SHARP_BOOKS:
                continue
            if eval_target not in all_fair_a:
                continue

            # Build consensus from all books EXCEPT this target
            others_a = [(v, bk) for bk, v in all_fair_a.items() if bk != eval_target]
            others_b = [(v, bk) for bk, v in all_fair_b.items() if bk != eval_target]

            non_target_count = len(others_a)
            if non_target_count < min_books:
                continue

            consensus_a = sum(v for v, _ in others_a) / non_target_count
            consensus_b = sum(v for v, _ in others_b) / non_target_count

            # Filter outliers before computing best-line
            clean_a = clean_outliers(others_a, consensus_a)
            clean_b = clean_outliers(others_b, consensus_b)

            best_a_val, best_a_book = max(clean_a, key=lambda x: x[0])
            best_b_val, best_b_book = max(clean_b, key=lambda x: x[0])

            use_crossbook = non_target_count >= 3
            if use_crossbook:
                fair_a = best_a_val
                fair_b = best_b_val
                edge_method = "cross_book_best_line"
            else:
                fair_a = consensus_a
                fair_b = consensus_b
                edge_method = "consensus_devig"

            contributing_books_a = [bk for _, bk in others_a]
            contributing_books_b = [bk for _, bk in others_b]

            # Evaluate both sides against this target book
            for side_name, fair_val, consensus_val, best_val, best_book, side_books, contrib_books in [
                (side_a_name, fair_a, consensus_a, best_a_val, best_a_book, side_a_books, contributing_books_a),
                (side_b_name, fair_b, consensus_b, best_b_val, best_b_book, side_b_books, contributing_books_b),
            ]:
                side_signed_point = signed_points.get((mkt_key, point, side_name), point)
                if not matches_hypothesis_conditions(
                    side_name=side_name,
                    market_type=mkt_key,
                    point=side_signed_point,
                    home_team=home,
                    away_team=away,
                    filters=filters or {},
                    fair_prob=fair_val,
                ):
                    continue

                if eval_target not in side_books:
                    continue
                target_price = side_books[eval_target]["price"]
                verdict = evaluate_side(
                    fair_val, target_price, edge_threshold,
                    non_target_count, mkt_key,
                )
                if verdict["skip"]:
                    continue

                events += 1
                if verdict["is_signal"]:
                    signals += 1

                team = side_name
                event_id = game.get("id") or f"{game_date}|{home}|{away}"
                event_sport = game.get("sport_key") or h_sport

                _pending_rows.append(build_event_row(
                    run_id=run_id,
                    event_id=event_id,
                    hypothesis_id=hypothesis_id,
                    sport=event_sport,
                    player=None,
                    market=mkt_key,
                    line=side_signed_point,
                    side=side_name,
                    book=eval_target,
                    target_price=target_price,
                    target_implied=verdict["target_implied"],
                    fair_val=fair_val,
                    factors={
                        "edge_method": edge_method,
                        "books_used": non_target_count,
                        "target_excluded": True,
                        "devig_method": devig_method,
                        "target_book": eval_target,
                        "best_line_book": best_book,
                        "best_line_fair_prob": round(best_val, 6),
                        "consensus_fair_prob": round(consensus_val, 6),
                        "contributing_books": contrib_books,
                        "home_team": home,
                        "away_team": away,
                        # Lookahead provenance — per-row so the
                        # promotion gate can count fallbacks vs
                        # pre-commence without joining upstream.
                        "snapshot_quality": book_snapshot_quality.get(
                            eval_target, "pre_commence"
                        ),
                    },
                    edge=verdict["edge"],
                    ev=verdict["ev"],
                    kelly=verdict["kelly"],
                    is_signal=verdict["is_signal"],
                    game_date=game_date,
                    snapshot_time=snapshot_time,
                ))

    # Return pending rows to caller for batch INSERT at end of backtest.
    # Per-game commits caused 274× write lock contention with line_monitor.
    return events, signals, _pending_rows
