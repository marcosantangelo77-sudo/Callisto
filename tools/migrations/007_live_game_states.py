"""Migration 007: live_game_states table for in-game detector.

Stores ESPN live boxscore snapshots captured every ~30s while a game is
in progress. The detector in ``tools/live_edges.py`` reads the most
recent N snapshots per event to decide whether the live market has
over-reacted to a recent play.

Retention is enforced by the writer (``tools/live_state.py``) — rows
older than 6h per event are pruned on each insert, hard-capped at 10M
rows overall. This migration just ensures the table + indexes exist;
the volume-bounding logic lives with the writer because it has the
inserted-row timestamp in hand.

Schema notes
------------
- ``ts`` is an ISO-8601 UTC string (same convention as odds_snapshots).
  We index (event_id, ts DESC) because every detector read is
  ``SELECT state_json FROM live_game_states WHERE event_id=? ORDER BY ts
  DESC LIMIT N``. A composite covering index on that pair lets SQLite
  answer from the index alone.
- ``state_json`` is the raw ESPN summary payload (competitions[0],
  boxscore, drives, plays). We store it whole because individual
  detectors care about different sub-paths and re-fetching micro-fields
  would multiply our polling cost. Typical row is 20-80kB; at the 6h
  retention cap for ~30 concurrent live games that's ~60MB worst case.
- ``sport`` is denormalised for fast filter scans (kill-switch lookups
  have to scan by sport + time).
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_game_states (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id   TEXT NOT NULL,
            sport      TEXT NOT NULL,
            ts         TEXT NOT NULL,
            state_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_states_event_ts "
        "ON live_game_states(event_id, ts DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_states_sport_ts "
        "ON live_game_states(sport, ts DESC)"
    )
    # Rate-limit / kill-switch ledger. Separate table (not reusing
    # ev_opportunities) so we can enforce per-(event, market, thesis)
    # emission windows without scanning the broader ev_opportunities
    # index. Also used by the per-game kill switch to count recent
    # emissions and historical good-CLV hits.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_edge_emissions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id    TEXT NOT NULL,
            sport       TEXT NOT NULL,
            market      TEXT NOT NULL,
            thesis_tag  TEXT NOT NULL,
            emitted_at  TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            ev_opp_id   INTEGER,
            notes       TEXT
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_emit_key "
        "ON live_edge_emissions(event_id, market, thesis_tag, emitted_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_emit_event_time "
        "ON live_edge_emissions(event_id, emitted_at DESC)"
    )
    # ev_opportunities: add is_live and thesis_tag so the executor and
    # downstream filters can route live-in-game edges distinctly from
    # pre-match line-movement rows. expires_at is the edge TTL (typical
    # 60s for live rows) — consumers MUST honor it before placing.
    # Note: ev_opportunities is created lazily by tools/line_monitor.py on
    # first write. On a fresh DB that hasn't yet had line_monitor run,
    # the table may not exist. Skip gracefully in that case — line_monitor's
    # CREATE TABLE IF NOT EXISTS already includes these columns in its
    # canonical schema, so cold-starts inherit them directly.
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ev_opportunities'"
    ).fetchone()
    if row is not None:
        for col, coltype in (
            ("is_live", "INTEGER DEFAULT 0"),
            ("thesis_tag", "TEXT"),
            ("expires_at", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE ev_opportunities ADD COLUMN {col} {coltype}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_live_states_event_ts")
    conn.execute("DROP INDEX IF EXISTS idx_live_states_sport_ts")
    conn.execute("DROP TABLE IF EXISTS live_game_states")
    conn.execute("DROP INDEX IF EXISTS idx_live_emit_key")
    conn.execute("DROP INDEX IF EXISTS idx_live_emit_event_time")
    conn.execute("DROP TABLE IF EXISTS live_edge_emissions")
    # We don't drop is_live/thesis_tag/expires_at on down-migration —
    # SQLite ALTER TABLE DROP COLUMN requires 3.35+ and would orphan
    # rows already using them. Leave them.
