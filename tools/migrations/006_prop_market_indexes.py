"""Migration 006: prop-market indexes for MLB/NHL coverage expansion.

The props-expansion adds 13 MLB and 11 NHL prop markets to
``DK_PROP_CATEGORIES``. The edge scanner, fair-value model, and hypothesis
generator all filter ``prop_snapshots`` by ``(sport, market)`` to assemble
per-market line histories. The existing indexes cover
``(player, market, line, book, snapshot_time)`` and
``(sport, snapshot_time)`` — neither is selective for "all pitcher_strikeouts
rows for MLB in the last week", which is the hot query once MLB/NHL props
start landing.

Before:
    SCAN prop_snapshots USING INDEX idx_prop_snap_sport_time  (full sport scan)
After:
    SEARCH prop_snapshots USING INDEX idx_prop_snap_sport_market_time
        (sport=? AND market=? range on snapshot_time)

Also adds a covering ``(sport, market, player)`` index so the fair-value
model's "give me every pitcher_strikeouts row for Gerrit Cole this month"
query stays on an index.

Zero data-migration risk — indexes only, IF NOT EXISTS, no lock concerns
past the initial build (a few seconds on the ~100k-row live DB).
"""

from __future__ import annotations

import sqlite3


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,),
        ).fetchone()
        is not None
    )


def up(conn: sqlite3.Connection) -> None:
    # prop_snapshots may not exist on a fresh DB that hasn't run the prop
    # scraper yet. Skip gracefully.
    if not _table_exists(conn, "prop_snapshots"):
        return

    # Hot query: "edge scanner wants every MLB pitcher_strikeouts row from
    # the last N minutes". Without this index it was a full scope-scan of
    # the (sport, snapshot_time) index filtered by market post-hoc.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_snap_sport_market_time "
        "ON prop_snapshots(sport, market, snapshot_time)"
    )

    # Fair-value model hot query: per-player, per-market rolling window.
    # The existing idx_prop_snap_player covers (player, market, line, book,
    # snapshot_time) but has (line, book) eating selectivity before
    # snapshot_time. A leaner (sport, market, player) covering index
    # lets the model's "most recent 10 starts for pitcher X" query stay
    # on index.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prop_snap_sport_market_player "
        "ON prop_snapshots(sport, market, player, snapshot_time)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_prop_snap_sport_market_time")
    conn.execute("DROP INDEX IF EXISTS idx_prop_snap_sport_market_player")
