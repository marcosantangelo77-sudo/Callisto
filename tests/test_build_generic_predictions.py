"""The generic prediction store — predictions/outcomes are real tables.

Before this pass SqlitePredictionResolver read ``predictions``/``outcomes``
tables that NO migration created and NOTHING wrote: a resolver wired to a
real database reported zero evidence forever, silently. These tests pin the
completed seam against a REAL migrated database (ensure_schema +
apply_pending_migrations), plus the writer contracts.
"""

import os

import pytest

from tools.resolvers.base import (
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
)
from tools.resolvers.generic import (
    SqlitePredictionResolver,
    record_outcome,
    record_prediction,
)


async def _migrated_db(tmpdir: str):
    from tools.migrations import apply_pending_migrations
    from tools.schema import ensure_schema

    db_path = os.path.join(tmpdir, "pred.db")
    await ensure_schema(db_path)
    apply_pending_migrations(db_path)

    from tools.db_utils import open_db
    return await open_db(db_path)


@pytest.mark.asyncio
async def test_core_schema_creates_prediction_tables(tmp_path):
    db = await _migrated_db(str(tmp_path))
    try:
        for table in ("predictions", "outcomes"):
            cur = await db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,))
            assert await cur.fetchone(), f"{table} missing after ensure_schema"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_and_read_back_round_trip(tmp_path):
    db = await _migrated_db(str(tmp_path))
    try:
        pid = await record_prediction(
            db, claim_id="claim1", event_id="halving_2028",
            predicted_prob=0.62, book_implied_prob=0.55,
            odds_american=-120, context_key="bull",
        )
        assert isinstance(pid, int)
        # nothing resolved yet: unresolved, no raise
        r = SqlitePredictionResolver(db)
        records = [rec async for rec in r.iter_evidence("claim1")]
        assert len(records) == 1
        rec = records[0]
        assert rec.resolved_outcome == "unresolved"
        assert rec.predicted_prob == 0.62
        assert rec.book_implied_prob == 0.55
        assert rec.odds_american == -120
        assert not rec.is_decided and rec.binary_outcome is None

        await record_outcome(db, prediction_id=pid,
                             resolved_outcome="yes", payoff=0.83)
        records = [x async for x in r.iter_evidence("claim1")]
        assert records[0].resolved_outcome == OUTCOME_POSITIVE
        assert records[0].is_decided and records[0].binary_outcome == 1
        summary = await r.summarize("claim1")
        assert summary.total == 1 and summary.positive == 1
        assert summary.hit_rate == 1.0
        assert summary.fully_resolved is True
        assert await r.has_resolved("claim1") is True
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_first_committed_probability_stands(tmp_path):
    """Re-recording the same (claim, event) must NOT displace the original
    probability — that is the laundering shape preregistration exists to
    prevent."""
    db = await _migrated_db(str(tmp_path))
    try:
        await record_prediction(db, claim_id="c", event_id="e",
                                predicted_prob=0.40)
        await record_prediction(db, claim_id="c", event_id="e",
                                predicted_prob=0.90)
        cur = await db.execute(
            "SELECT predicted_prob FROM predictions WHERE claim_id='c'")
        rows = await cur.fetchall()
        assert len(rows) == 1
        assert rows[0][0] == 0.40
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_same_event_never_double_counts_a_claim(tmp_path):
    """UNIQUE(claim_id, event_id): one event, one observation, however many
    times the pipeline retries."""
    db = await _migrated_db(str(tmp_path))
    try:
        for _ in range(5):
            await record_prediction(db, claim_id="c", event_id="e",
                                    predicted_prob=0.5)
        cur = await db.execute("SELECT COUNT(*) FROM predictions")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_unknown_outcome_token_raises(tmp_path):
    db = await _migrated_db(str(tmp_path))
    try:
        pid = await record_prediction(db, claim_id="c", event_id="e")
        with pytest.raises(ValueError, match="scoreable"):
            await record_outcome(db, prediction_id=pid,
                                 resolved_outcome="sorta-won")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_official_correction_replaces_stale_truth(tmp_path):
    """An overturned result replaces the stale outcome, loudly logged."""
    db = await _migrated_db(str(tmp_path))
    try:
        pid = await record_prediction(db, claim_id="c", event_id="e")
        await record_outcome(db, prediction_id=pid,
                             resolved_outcome=OUTCOME_NEGATIVE, payoff=-1.0)
        await record_outcome(db, prediction_id=pid,
                             resolved_outcome=OUTCOME_POSITIVE, payoff=1.5)
        r = SqlitePredictionResolver(db)
        records = [x async for x in r.iter_evidence("c")]
        assert len(records) == 1
        assert records[0].resolved_outcome == OUTCOME_POSITIVE
        assert records[0].payoff == 1.5
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_resolver_tolerates_pre_seam_db(tmp_path):
    """The honest contract kept: on a DB without the tables the resolver
    reports zero evidence instead of raising."""
    import aiosqlite

    db = await aiosqlite.connect(os.path.join(str(tmp_path), "old.db"))
    try:
        r = SqlitePredictionResolver(db)
        records = [x async for x in r.iter_evidence("anything")]
        assert records == []
        summary = await r.summarize("anything")
        assert summary.total == 0
    finally:
        await db.close()
