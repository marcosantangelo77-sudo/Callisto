"""Schema-shape helpers for consumers of the hypotheses seam.

Migration 013 (the schema seam) moved the sports columns off
``hypotheses`` into the plugin-owned ``hypothesis_sports_ext`` side
table. Both shapes exist in the wild:

  * WELDED (pre-013): ``sport``/``market_type`` are NOT NULL columns on
    ``hypotheses`` itself.
  * SEAM (post-013, and every fresh core-schema DB): ``hypotheses``
    carries only domain-general columns; the sports fields live in the
    ext table.

Every consumer that hand-writes SQL against ``hypotheses`` must resolve
the sports fields across BOTH shapes or it breaks on one of them. This
module is the single place that knows how to do that, so eight call
sites do not each re-derive it (and drift — which is exactly what the
2026-08-23 audit of the uncommitted seam work found: four sites fixed,
four left referencing a column that no longer exists).
"""

from __future__ import annotations

# The FROM clause that reads the sports field when the ext table exists.
# NOTE: on the seam shape the ext value IS the only sport — SQLite rejects
# ``COALESCE(e.sport, h.sport)`` outright because h.sport does not exist as
# a column there (column resolution is static, not row-by-row). Consumers
# on a welded pre-013 DB must query ``hypotheses.sport`` directly instead;
# gate on tools.schema.seam.has_ext_table / is_welded_shape, never on a
# COALESCE over both shapes.
SPORT_FROM_CLAUSE = (
    "FROM {table} h "
    "JOIN hypothesis_sports_ext e ON e.hypothesis_id = h.hypothesis_id"
)

SPORT_EXPR = "e.sport"


async def has_ext_table(db) -> bool:
    """Does this connection have the plugin's ext table?"""
    cur = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='hypothesis_sports_ext'"
    )
    return bool(await cur.fetchone())


async def is_welded_shape(db) -> bool:
    """True when ``hypotheses`` still carries its own sport column."""
    cur = await db.execute("SELECT name FROM pragma_table_info('hypotheses')")
    cols = {r[0] for r in await cur.fetchall()}
    return "sport" in cols
