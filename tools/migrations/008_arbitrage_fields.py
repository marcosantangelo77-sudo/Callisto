"""Migration 008: arbitrage-specific partial index on ``ev_opportunities``.

Originally this migration also added ``thesis_tag`` and ``expires_at``
columns to ``ev_opportunities``. Those two columns (plus ``is_live``) are
now added by migration 007 (``live_game_states``) because the live-in-game
detector also needs them and its migration landed first in the renumbered
round-2 sequence. Keeping both ADD COLUMNs would be a harmless duplicate
(each uses an idempotent guard) but leaves readers wondering which
migration "owns" each column. So the ADD COLUMNs are centralized in 007
and this migration now only adds the arbitrage-specific partial index.

The arbitrage scanner (``tools/arbitrage_scanner.py``) writes one row per
leg with ``source='arbitrage'``. Consumers (executor, Telegram alerts)
filter "hot open arbs" via
``source='arbitrage' AND status='open'`` and want the freshest rows first;
the partial index ``idx_ev_open_arbs`` on
``(source, status, expires_at)`` keeps that query cheap as the table grows.

Column semantics, for reference (created by 007):
    - ``thesis_tag``   — ``'arb'`` | ``'dutch'`` | ``'synthetic_arb'`` | ...
    - ``expires_at``   — ISO timestamp. Arbs evaporate fast; a row detected
                         at T should refuse to execute past T+60s.
    - ``is_live``      — 1 iff emitted from the live-in-game detector.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='ev_opportunities'"
    ).fetchone()
    if not row:
        # Table is created by tools/line_monitor.py at runtime; migration is
        # a no-op if it hasn't run yet. The runtime DDL includes the new
        # columns via persist_opportunity's defensive ALTERs, so this only
        # matters for long-lived DBs already past that code path.
        return

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ev_open_arbs "
        "ON ev_opportunities(source, status, expires_at)"
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_ev_open_arbs")
