"""Migration 016: domain-general ``predictions`` and ``outcomes`` tables.

Why this exists
---------------
BUILD_MANDATE queue item 1 called the OutcomeResolver seam "the single
highest-value change in the repo" — it is what turns a betting engine into
a research engine. The read side landed (tools/resolvers/generic.py,
``SqlitePredictionResolver``), but the two tables it reads existed in NO
migration: they were created only inside a test's ad-hoc SQL, and nothing
in production code wrote to them. A non-sports claim therefore had no way
to durably enter the lifecycle scoring path — its evidence died with the
process (in-memory resolver) or required hand-writing DDL against the live
database.

This migration formalises the table shapes the resolver already reads:

    predictions(id, claim_id, event_id, predicted_prob,
                context_key, created_at)
    outcomes(prediction_id UNIQUE, resolved_outcome, payoff, resolved_at)

Semantics
---------
* ``claim_id`` joins ``hypotheses.hypothesis_id`` by convention, not FK:
  claims may also live outside that table (retrodiction questions, model-
  registry predictions) and a hard FK would weld generality back to sports.
* One outcome row per prediction (UNIQUE on prediction_id): resolution is
  a fact, not a stream. Corrections should UPDATE resolved_outcome with an
  audit note, not insert a second verdict.
* ``predicted_prob`` nullable: some claims resolve directionally without
  a numeric probability; scoring paths that need one skip those rows.
* No index beyond the primary key and the unique constraint was added —
  the dominant lookup is exactly what the resolver already issues
  (predictions WHERE claim_id = ?, LEFT JOIN outcomes). Add indexes when
  a measured query needs them, not speculatively.

Sports stays green: these tables are new and additive; no existing reader,
writer or gate touches them.
"""

from __future__ import annotations

import sqlite3


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            predicted_prob REAL,
            context_key TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_predictions_claim "
        "ON predictions(claim_id)"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS outcomes (
            prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
            resolved_outcome TEXT NOT NULL,
            payoff REAL,
            resolved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS outcomes")
    conn.execute("DROP INDEX IF EXISTS idx_predictions_claim")
    conn.execute("DROP TABLE IF EXISTS predictions")
