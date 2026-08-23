"""Migration 016 — the domain-general ``predictions`` / ``outcomes`` tables.

Why this exists
---------------
SqlitePredictionResolver (tools/resolvers/generic.py) has, since B1 landed,
documented two tables:

    predictions(id, claim_id, event_id, predicted_prob, context_key, created_at)
    outcomes(prediction_id, resolved_outcome, payoff, resolved_at)

No migration ever created them and nothing writes to them — verified
2026-08-23 by grep over tools/, agp/, plugins/ and migrations/. The resolver
deliberately tolerates their absence ("reports zero evidence rather than
raising"), which is failure family 3: absence treated as success. The
domain-general resolution path — the thing that makes Callisto a research
engine rather than a betting engine with a general vocabulary — could store
nothing and silently reported "not yet tested" forever.

This migration creates only the tables. It does NOT change any reader's
behaviour: SqlitePredictionResolver already queries exactly these names and
columns; before this migration it saw zero rows, after it it sees whatever
the ingest path records. Sports stays green by construction.

Schema choices
-------------
* ``predictions.claim_id`` is the lifecycle join key (hypotheses.hypothesis_id
  today; any claim id tomorrow). Indexed — every resolver query filters on it.
* ``predicted_prob`` nullable: some claims resolve without a numeric
  probability at claim time; the record is still worth keeping.
* ``outcomes.resolved_outcome`` stores raw domain tokens ("won", "yes",
  "positive", ...). Normalisation onto hit/miss/stale happens in
  tools/research_program.ResolutionRecord, ONE place, so adding a new domain
  token never requires a schema edit.
* ``outcomes.source`` records which resolver produced the ground truth —
  provenance of outcomes, not just of evidence.
* One outcome row per prediction enforced by UNIQUE(prediction_id): a claim
  resolves once; corrections should UPDATE, not duplicate.
* ``raw_json`` keeps the resolver payload verbatim for forensic replay,
  mirroring migration 012's pattern.

Down migration: provided but destructive (drops recorded ground truth), same
guard note as 012.
"""

from __future__ import annotations

import sqlite3

MIGRATION_VERSION = 20260824


def up(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS predictions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,          -- hypotheses.hypothesis_id or any claim id
            event_id TEXT NOT NULL,          -- stable id of the predicted event
            predicted_prob REAL,             -- claim's probability at prediction time
            context_key TEXT,                -- free-form regime/diversity bucket
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(claim_id, event_id)
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
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prediction_id INTEGER NOT NULL REFERENCES predictions(id),
            resolved_outcome TEXT NOT NULL,  -- raw domain token; normalised downstream
            payoff REAL,                     -- per-unit return; NULL if not applicable
            resolved_at TIMESTAMP,
            source TEXT,                     -- which resolver produced this
            raw_json TEXT,                   -- verbatim resolver payload
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(prediction_id)            -- a claim resolves once
        )
        """
    )


def down(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS outcomes")
    conn.execute("DROP TABLE IF EXISTS predictions")
