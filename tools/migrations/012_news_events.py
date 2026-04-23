"""Migration 012: ``news_events`` table for injury / lineup / coaching signals.

Why this exists
---------------
Late-breaking news moves lines. A starting pitcher getting scratched 40min
before first pitch, a surprise coaching-rest decision, or a 30-minutes-to-tip
lineup swap can invalidate a model's priors within seconds. Callisto had
**zero** ingestion of this class of signal before this migration — every
non-odds edge detector was running on stale roster / health assumptions.

This table is the single destination for all news-sourced events regardless
of provider. The ingestion layer (``tools.news_ingestion``) writes here after
cross-source deduplication, and ``tools.news_impact`` correlates rows with
``line_movements`` to spot under-reactions (news out, line hasn't moved yet
→ edge candidate).

Schema choices
--------------
* ``event_id`` is nullable — most injury headlines arrive before any specific
  game is matched. A second pass (game association) backfills it when the
  scheduler can identify the affected game.
* ``first_seen_at`` vs ``confirmed_at``: the dedup layer sets
  ``confirmed_at`` when a second independent source reports the same
  ``(sport, player_name, body_part)``. Single-source rows stay with
  ``confirmed_at IS NULL`` and are treated as lower-confidence by downstream
  filters.
* ``raw_json`` stores the per-source payload verbatim for forensic replay —
  source HTML/JSON shapes drift and the raw blob is our only way to re-derive
  fields after a scraper fix.
* ``local_game_date`` mirrors the pattern from migration 010 — every date
  that matters to joining/filtering is the venue-local date, never UTC-sliced.

Indexing
--------
Two lookup paths dominate:
  1. "what's current for today's slate" → (sport, local_game_date)
  2. "has this player shown up recently?" → (player_name, first_seen_at DESC)
Both are covered. A third implied lookup — "events we haven't matched to a
game yet" — is cheap against the small unmatched subset and doesn't warrant
its own index.

Down migration
--------------
Provided but guarded. The table carries forensic history; callers that
drop it lose the ability to audit past line reactions against news flow.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT,
            event_id TEXT,                 -- may be NULL if not yet associated
            player_name TEXT,
            event_type TEXT,               -- 'injury', 'lineup_change',
                                           -- 'coaching_decision', 'roster_move'
            severity TEXT,                 -- 'minor', 'moderate', 'severe',
                                           -- 'out_indefinite'
            body_part TEXT,                -- 'lower_body', 'upper_body', etc.
            status TEXT,                   -- 'questionable', 'probable',
                                           -- 'doubtful', 'out', 'inactive'
            first_seen_at TIMESTAMP,
            confirmed_at TIMESTAMP,        -- set when 2nd source confirms
            source TEXT,
            source_url TEXT,
            raw_json TEXT,
            local_game_date DATE,          -- correlates to game affected
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_sport_date "
        "ON news_events(sport, local_game_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_player "
        "ON news_events(player_name, first_seen_at DESC)"
    )
    # Dedup support: fast lookup by (sport, player_name, body_part, event_type)
    # — the four-tuple that identifies "same underlying event, different
    # source". The ingestion layer hits this index when deciding whether a
    # newly scraped headline is a dup of something seen in the last ~12h.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_news_events_dedup "
        "ON news_events(sport, player_name, body_part, event_type, "
        "first_seen_at DESC)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_news_events_dedup")
    conn.execute("DROP INDEX IF EXISTS idx_news_events_player")
    conn.execute("DROP INDEX IF EXISTS idx_news_events_sport_date")
    conn.execute("DROP TABLE IF EXISTS news_events")
