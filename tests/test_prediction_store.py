"""Migration 016 + PredictionStore — the write side of the generic evidence
path finally exists.

Before this pass, ``SqlitePredictionResolver`` (the read side) read
``predictions``/``outcomes`` tables that existed in NO migration and were
created only inside a test's ad-hoc SQL; nothing in production code wrote
to them. These tests pin the closed loop:

    migrate → record prediction → resolve outcome → resolver scores it

using the REAL migration module, not hand-written DDL.
"""

from __future__ import annotations

import importlib
import sqlite3

import aiosqlite
import pytest

from tools.resolvers import GenericPredictionResolver, PredictionStore

M016 = importlib.import_module("tools.migrations.016_generic_predictions")


class TestMigration016:
    def test_up_creates_tables(self):
        conn = sqlite3.connect(":memory:")
        M016.up(conn)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {"predictions", "outcomes"} <= names

    def test_up_is_idempotent(self):
        conn = sqlite3.connect(":memory:")
        M016.up(conn)
        M016.up(conn)  # must not raise

    def test_down_reverses(self):
        conn = sqlite3.connect(":memory:")
        M016.up(conn)
        M016.down(conn)
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "predictions" not in names and "outcomes" not in names


@pytest.mark.asyncio
async def test_record_resolve_score_round_trip():
    """The loop that was impossible before this pass: a non-sports claim
    recorded through the real migration's tables and scored by the real
    resolver."""
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(M016_PREDICTIONS_DDL)
        await db.execute(M016_OUTCOMES_DDL)
        store = PredictionStore(db)

        p1 = await store.record("mat-science", "exp-42",
                                predicted_prob=0.15,
                                context_key="low-base-rate")
        p2 = await store.record("mat-science", "exp-43", predicted_prob=0.15)
        assert p1 != p2

        # domain vocabulary normalises: 'confirmed' -> positive
        assert await store.resolve(p1, "confirmed", payoff=4.0) is True

        r = GenericPredictionResolver.Sqlite(db)
        recs = [x async for x in r.iter_evidence("mat-science")]
        assert len(recs) == 2
        hit = [x for x in recs if x.is_decided]
        assert len(hit) == 1 and hit[0].binary_outcome == 1
        s = await r.summarize("mat-science")
        assert s.hit_rate == pytest.approx(1.0) and not s.fully_resolved
    finally:
        await db.close()


# The exact DDL the migration issues — kept as strings so the round-trip
# test can run on an aiosqlite connection without going through the stdlib
# migration runner.
M016_PREDICTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS predictions ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "claim_id TEXT NOT NULL,"
    "event_id TEXT NOT NULL,"
    "predicted_prob REAL,"
    "context_key TEXT,"
    "created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)
M016_OUTCOMES_DDL = (
    "CREATE TABLE IF NOT EXISTS outcomes ("
    "prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),"
    "resolved_outcome TEXT NOT NULL,"
    "payoff REAL,"
    "resolved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)"
)


@pytest.mark.asyncio
async def test_double_resolve_is_rejected_without_correction_flag():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(M016_PREDICTIONS_DDL)
        await db.execute(M016_OUTCOMES_DDL)
        store = PredictionStore(db)
        pid = await store.record("btc-hashrate", "2027-q1", predicted_prob=0.3)
        assert await store.resolve(pid, "yes") is True
        # second verdict silently ignored without overwrite=True
        assert await store.resolve(pid, "no") is False
        row = await store.get(pid)
        assert row["resolved_outcome"] == "positive"  # first verdict stands
        # explicit correction goes through
        assert await store.resolve(pid, "no", overwrite=True) is True
        row = await store.get(pid)
        assert row["resolved_outcome"] == "negative"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_outcome_token_raises():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(M016_PREDICTIONS_DDL)
        await db.execute(M016_OUTCOMES_DDL)
        store = PredictionStore(db)
        pid = await store.record("x", "e1")
        with pytest.raises(ValueError):
            await store.resolve(pid, "probaly won")  # typo must not vanish
        row = await store.get(pid)
        assert row["resolved_outcome"] is None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_requires_claim_and_event():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(M016_PREDICTIONS_DDL)
        store = PredictionStore(db)
        with pytest.raises(ValueError):
            await store.record("", "e1")
    finally:
        await db.close()
