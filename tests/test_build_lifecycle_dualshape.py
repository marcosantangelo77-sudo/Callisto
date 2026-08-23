"""The hypothesis lifecycle must work on BOTH sides of the schema seam.

Migration 013 rebuilds ``hypotheses`` without sport/market_type (they move
to hypothesis_sports_ext; a domain column is added), and api.py applies
pending migrations unconditionally at every startup. Measured 2026-08-23
on a fresh DB that ran the real ensure_schema + apply_pending_migrations
sequence: create_hypothesis died with "OperationalError: no such column:
sport". These tests pin the repaired behaviour against the REAL migrated
schema, not a hand-rolled fixture — the fixture-shaped tests are exactly
why the break was invisible.

Pre-seam databases (migrations never applied) keep their existing
behaviour byte-identically: sports creation works, general claims raise
loudly instead of writing a fake sport value.
"""

import json
import os
import tempfile

import aiosqlite
import pytest

from tools.hypothesis import HypothesisManager


async def _make_migrated_db(tmpdir: str) -> str:
    """A real database in the shape every deployment has after one startup:
    ensure_schema creates the tables, then pending migrations run (013
    included — the seam)."""
    from tools.migrations import apply_pending_migrations
    from tools.schema import ensure_schema

    db_path = os.path.join(tmpdir, "seamed.db")
    await ensure_schema(db_path)
    result = apply_pending_migrations(db_path)
    assert 13 in result["applied"], f"migration 013 did not run: {result}"
    return db_path


async def _make_pre_seam_db(tmpdir: str) -> str:
    """The welded shape: sport/market_type NOT NULL on core, as every DB
    looked before migration 013."""
    db_path = os.path.join(tmpdir, "welded.db")
    db = await aiosqlite.connect(db_path)
    await db.execute(
        """
        CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            thesis TEXT NOT NULL,
            sport TEXT NOT NULL,
            market_type TEXT NOT NULL,
            model_config TEXT NOT NULL,
            edge_threshold REAL NOT NULL DEFAULT 0.01,
            status TEXT NOT NULL DEFAULT 'draft',
            min_sample_size INTEGER NOT NULL DEFAULT 50,
            significance_level REAL NOT NULL DEFAULT 0.05,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            promoted_at DATETIME,
            promoted_by TEXT,
            notes TEXT
        )
        """
    )
    await db.commit()
    await db.close()
    return db_path


@pytest.mark.asyncio
async def test_sports_hypothesis_survives_the_seam(tmp_path):
    """The measured break, pinned: creating a SPORTS hypothesis after
    migration 013 must work, land its sports fields in the side table,
    and read back enriched."""
    db = await _make_migrated_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        hid = await mgr.create_hypothesis(
            name="nba_away_dog_v3",
            thesis="Away dogs cover more after losses",
            sport="nba",
            market_type="moneyline",
            model_config={},
        )
        h = await mgr.get_hypothesis(hid)
        assert h is not None
        assert h["sport"] == "nba"
        assert h["market_type"] == "moneyline"
        # domain column backfilled honestly by migration; new rows default sports
        assert h.get("domain") == "sports"
        cur = await mgr._db.execute(
            "SELECT sport, market_type FROM hypothesis_sports_ext "
            "WHERE hypothesis_id = ?",
            (hid,),
        )
        row = await cur.fetchone()
        assert row == ("nba", "moneyline")
        listed = await mgr.list_hypotheses(status="draft")
        assert any(x["hypothesis_id"] == hid and x["sport"] == "nba"
                   for x in listed)
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_general_hypothesis_creatable_after_the_seam(tmp_path):
    """BUILD_MANDATE item 1: a claim about anything else can finally be
    stored. No sport passed — none invented."""
    db = await _make_migrated_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        hid = await mgr.create_hypothesis(
            name="btc_halving_drift",
            thesis="BTC drifts positive in the 90 days after a halving",
            model_config={},
        )
        h = await mgr.get_hypothesis(hid)
        assert h is not None
        assert h.get("domain") == "general"
        assert h.get("sport") is None
        # no side-table row may exist for a general claim
        cur = await mgr._db.execute(
            "SELECT COUNT(*) FROM hypothesis_sports_ext WHERE hypothesis_id = ?",
            (hid,),
        )
        assert (await cur.fetchone())[0] == 0
        # _days_of_odds_data: unknown, never zero-days-of-evidence
        assert await mgr._days_of_odds_data(hid) is None
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_name_dedup_still_returns_same_id_post_seam(tmp_path):
    db = await _make_migrated_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        a = await mgr.create_hypothesis(
            name="dup_name", thesis="t", sport="mlb",
            market_type="total", model_config={})
        b = await mgr.create_hypothesis(
            name="dup_name", thesis="t", sport="mlb",
            market_type="total", model_config={})
        c = await mgr.create_hypothesis(name="dup_name", thesis="t")
        assert a == b == c
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_game_filters_guard_works_via_ext_join(tmp_path):
    """The duplicate game_filters guard must still fire post-seam when two
    sports hypotheses share sport+market+filters."""
    db = await _make_migrated_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        cfg = {"game_filters": {"min_odds": -150}}
        a = await mgr.create_hypothesis(
            name="gf_one", thesis="t", sport="nba", market_type="spread",
            model_config=cfg)
        b = await mgr.create_hypothesis(
            name="gf_two", thesis="different thesis", sport="nba",
            market_type="spread", model_config=dict(cfg))
        assert a == b, "identical game_filters must dedup to the first id"
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_pre_seam_db_sports_creation_unchanged(tmp_path):
    """Regression guard: on the welded shape everything behaves exactly as
    before this change."""
    db = await _make_pre_seam_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        assert mgr._has_core_sport is True
        assert mgr._has_domain_col is False
        hid = await mgr.create_hypothesis(
            name="legacy_nba", thesis="t", sport="nba",
            market_type="moneyline", model_config={})
        h = await mgr.get_hypothesis(hid)
        assert h["sport"] == "nba" and h["market_type"] == "moneyline"
        assert "domain" not in h
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_pre_seam_db_general_claim_raises_loudly(tmp_path):
    """No fake sport values: on a pre-seam DB a general claim cannot be
    stored honestly, so it refuses with a message naming the fix."""
    db = await _make_pre_seam_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        with pytest.raises(ValueError, match="migrations"):
            await mgr.create_hypothesis(
                name="too_early", thesis="t", model_config={})
    finally:
        await mgr.close()


@pytest.mark.asyncio
async def test_days_of_odds_data_reads_ext_sport_post_seam(tmp_path):
    db = await _make_migrated_db(str(tmp_path))
    mgr = HypothesisManager(db_path=db)
    await mgr.initialize()
    try:
        # historical_odds_cache exists on a migrated DB with more columns
        # than the count query needs; insert only what the count reads.
        await mgr._db.execute(
            "CREATE TABLE IF NOT EXISTS historical_odds_cache ("
            " snapshot_date TEXT, sport TEXT)")
        await mgr._db.executemany(
            "INSERT OR IGNORE INTO historical_odds_cache "
            "(snapshot_date, sport, response_json) VALUES (?, ?, ?)",
            [("2026-08-01", "nba", "{}"), ("2026-08-02", "nba", "{}")],
        )
        await mgr._db.commit()
        hid = await mgr.create_hypothesis(
            name="odds_days", thesis="t", sport="nba",
            market_type="spread", model_config={})
        assert await mgr._days_of_odds_data(hid) == 2
    finally:
        await mgr.close()
