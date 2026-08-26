"""Persistence: write qualifying arb rows into ev_opportunities.

We use raw sqlite3 here (the tests do too) so we don't depend on the async
aiosqlite pool at test time. Callers inside the live pipeline can wrap
``persist_opportunity`` inside their own async transaction.
"""

from __future__ import annotations

import sqlite3

from tools.arb.models import ArbOpportunity

_PERSIST_COLS = (
    "detected_at", "sport", "game_id", "team", "market", "bookmaker",
    "american_odds", "implied_probability", "estimated_true_prob", "edge",
    "expected_value", "kelly_fraction", "status", "source", "thesis_tag",
    "expires_at",
)


def persist_opportunity(
    conn: sqlite3.Connection,
    opp: ArbOpportunity,
) -> list[int]:
    """Write one ArbOpportunity to ev_opportunities — one row per leg.

    Returns the list of inserted row ids. Caller is responsible for commit.
    """
    # Make sure the thesis_tag / expires_at columns exist on the target DB.
    # This lets persist work against older installs that haven't run the
    # migration yet; no-op on DBs that already have them.
    for col, decl in (("thesis_tag", "TEXT"),
                      ("expires_at", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE ev_opportunities ADD COLUMN {col} {decl}")
        except sqlite3.OperationalError:
            pass  # already exists

    ids: list[int] = []
    for leg in opp.legs:
        cur = conn.execute(
            f"INSERT INTO ev_opportunities "
            f"({', '.join(_PERSIST_COLS)}) "
            f"VALUES ({', '.join('?' * len(_PERSIST_COLS))})",
            (
                opp.detected_at,
                opp.sport,
                opp.game_id,
                leg.outcome,
                opp.market_type,
                leg.bookmaker,
                leg.american_odds,
                leg.implied_prob,
                None,               # estimated_true_prob — N/A for arbs
                round(1.0 - opp.total_implied, 6),   # edge = the "gap"
                opp.profit_pct,
                # kelly_fraction slot repurposed as stake-fraction-of-budget
                round(leg.stake / opp.effective_budget, 6)
                if opp.effective_budget > 0 else 0.0,
                "open",
                "arbitrage",
                opp.thesis_tag,
                opp.expires_at,
            ),
        )
        ids.append(cur.lastrowid)
    return ids
