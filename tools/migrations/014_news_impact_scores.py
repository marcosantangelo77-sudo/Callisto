"""Migration 014: ``news_impact_scores`` — persisted scoring for downstream JOINs.

Why this exists
---------------
Migration 012 introduced ``news_events`` (the raw-row journal). Migration 013
landed the ML backtest reports. Neither provides a persistent, queryable
surface for *scored* news impact — everything ``tools.news_impact`` computes
lives in memory and is recomputed every loop tick. That means:

  * Other modules (bet_executor sizing, hypothesis generator context,
    dashboard) can't JOIN against "latest impact score for this team/player
    in the last N minutes".
  * Decay/recency logic has to be duplicated everywhere the score is read.
  * Audit trail is limited to the ``ev_opportunities`` rows we emit, which
    only capture the *actionable* subset.

This table is the one-row-per-scored-news-event ledger with:
  * raw projected_impact (model output)
  * decayed_impact (linear decay 0→24h baked in at compute-time)
  * line_moved flag (whether the book already reacted)
  * actionability flags so downstream consumers can filter cheaply

Schema is deliberately narrow: one row per (news_event_id) unique key so
re-scoring a news event UPSERTs rather than accumulating. The
``computed_at`` column lets consumers filter "fresh" scores vs "stale"
without re-running ``process_news_events``.

Indexes
-------
1. ``(sport, computed_at DESC)`` — dashboard / recent endpoint
2. ``(team, computed_at DESC)`` — bet_executor JOIN on team
3. ``(player_name, computed_at DESC)`` — prop-sizing JOIN

Down migration
--------------
Drops cleanly. The source-of-truth journal (``news_events``) is untouched,
so re-running ``process_news_events`` rebuilds the scores.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_impact_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_event_id INTEGER NOT NULL,
            sport TEXT,
            team TEXT,
            player_name TEXT,
            first_seen_at TIMESTAMP,
            computed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            projected_impact REAL NOT NULL DEFAULT 0.0,
            decayed_impact REAL NOT NULL DEFAULT 0.0,
            age_minutes REAL NOT NULL DEFAULT 0.0,
            line_moved INTEGER NOT NULL DEFAULT 0,
            is_under_reaction INTEGER NOT NULL DEFAULT 0,
            is_actionable INTEGER NOT NULL DEFAULT 0,
            is_stale INTEGER NOT NULL DEFAULT 0,
            severity TEXT,
            confirmed INTEGER NOT NULL DEFAULT 0,
            UNIQUE (news_event_id)
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_impact_sport_time "
        "ON news_impact_scores(sport, computed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_impact_team_time "
        "ON news_impact_scores(team, computed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_impact_player_time "
        "ON news_impact_scores(player_name, computed_at DESC)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_impact_actionable "
        "ON news_impact_scores(is_actionable, computed_at DESC)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_news_impact_actionable")
    conn.execute("DROP INDEX IF EXISTS idx_news_impact_player_time")
    conn.execute("DROP INDEX IF EXISTS idx_news_impact_team_time")
    conn.execute("DROP INDEX IF EXISTS idx_news_impact_sport_time")
    conn.execute("DROP TABLE IF EXISTS news_impact_scores")
