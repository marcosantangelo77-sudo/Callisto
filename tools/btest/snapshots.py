"""Snapshot enrichment for historical odds.

Extracted from BacktestEngine._enrich_snapshot_with_multibook
(tools/backtest.py, slice 2).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger("callisto.backtest")


def _book_stats(games: list[dict], target_book: str) -> tuple[int, bool]:
    """Return (max book count across games, whether target book present)."""
    max_books = 0
    has_target = False
    for g in games:
        book_keys = {bm.get("key", "").lower() for bm in g.get("bookmakers", [])}
        max_books = max(max_books, len(book_keys))
        if target_book in book_keys:
            has_target = True
    return max_books, has_target


async def enrich_snapshot_with_multibook(
    db,
    sport: str,
    date_str: str,
    snapshot: dict,
    target_book: str,
) -> dict:
    """Enrich a snapshot with multi-book data from odds_snapshots.

    When the historical_odds_cache has only single-book "consensus" data
    (common for older dates), check if odds_snapshots has a richer
    multi-book snapshot for the same date and sport. If so, use that
    instead — it has the target book + comparison books needed for
    cross-book edge detection.

    Returns the original snapshot if already multi-book or no better
    data is available.
    """
    games = snapshot.get("games", [])
    if not games:
        return snapshot

    # Check if snapshot already has multi-book data with the target book
    max_books, has_target = _book_stats(games, target_book)

    if has_target and max_books >= 2:
        # Already have multi-book data with target — use as-is
        return snapshot

    # Try to find a better snapshot in odds_snapshots for this date
    # Look for snapshots on this date with the most games
    try:
        cursor = await db.execute(
            "SELECT snapshot_json FROM odds_snapshots "
            "WHERE sport = ? AND timestamp LIKE ? AND game_count > 0 "
            "ORDER BY game_count DESC LIMIT 1",
            (sport, f"{date_str}%"),
        )
        row = await cursor.fetchone()
        if not row:
            return snapshot

        better_snapshot = json.loads(row[0])
        better_games = better_snapshot.get("games", [])

        # Verify the better snapshot actually has multi-book data
        better_max_books, better_has_target = _book_stats(better_games, target_book)

        if better_has_target and better_max_books > max_books:
            # Filter out cross-sport contamination before returning
            better_snapshot["games"] = [
                g for g in better_snapshot.get("games", [])
                if not g.get("sport_key") or g["sport_key"] == sport
            ]
            for g in better_snapshot["games"]:
                if not g.get("sport_key"):
                    g["sport_key"] = sport
            logger.info(
                f"Enriched {sport} {date_str}: upgraded from {max_books} to "
                f"{better_max_books} books (from odds_snapshots)"
            )
            return better_snapshot

    except Exception as e:
        logger.warning(f"Snapshot enrichment failed for {sport} {date_str}: {e}", exc_info=True)

    return snapshot


__all__ = ["enrich_snapshot_with_multibook"]
