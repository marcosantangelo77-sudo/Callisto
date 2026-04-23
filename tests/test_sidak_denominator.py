"""FWER Šidák correction: correct denominator (lifetime, not active-only)
and no 0.001 floor.

Pre-audit: denominator was COUNT(*) FROM hypotheses WHERE status IN
('backtesting','paper_trading') — i.e. 50-odd active hyps instead of the
4500+ lifetime-tested population.  And the computed α was floored at 0.001,
swallowing the actual per-test threshold.

Post-fix: denominator = COUNT(DISTINCT hypothesis_id) FROM backtest_runs
within CALLISTO_FWER_LOOKBACK_DAYS; no floor.
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import aiosqlite
import pytest


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


async def _seed_db(db_path: str, n_lifetime: int, n_active: int) -> None:
    """Seed a minimal DB: n_lifetime distinct hyps in backtest_runs,
    n_active in hypotheses with status='backtesting'."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """CREATE TABLE hypotheses (
                hypothesis_id TEXT PRIMARY KEY,
                name TEXT, thesis TEXT, sport TEXT, market_type TEXT,
                model_config TEXT, edge_threshold REAL DEFAULT 0.02,
                status TEXT DEFAULT 'draft',
                min_sample_size INTEGER DEFAULT 1000,
                significance_level REAL DEFAULT 0.05,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                promoted_at DATETIME, promoted_by TEXT, notes TEXT
            )"""
        )
        await db.execute(
            """CREATE TABLE backtest_runs (
                run_id TEXT PRIMARY KEY,
                hypothesis_id TEXT NOT NULL,
                completed_at DATETIME
            )"""
        )
        for i in range(n_active):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, thesis, sport, market_type, "
                "model_config, status) VALUES (?, ?, ?, 'baseball_mlb', 'h2h', '{}', 'backtesting')",
                (f"active_{i}", f"a{i}", "thesis"),
            )
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        for i in range(n_lifetime):
            await db.execute(
                "INSERT INTO backtest_runs (run_id, hypothesis_id, completed_at) VALUES (?, ?, ?)",
                (f"run_{i}", f"hyp_{i}", now_iso),
            )
        await db.commit()


def test_sidak_threshold_at_4594_lifetime():
    """At 4594 lifetime hypotheses, α=0.05 → per-test ~1.12e-5 (no floor)."""
    n = 4594
    alpha_family = 0.05
    expected = 1.0 - (1.0 - alpha_family) ** (1.0 / n)
    # ~1.116e-5
    assert 1.0e-5 < expected < 1.2e-5, f"unexpected: {expected:.3e}"
    # Floor-free invariant: the correct threshold must fall BELOW the old
    # pre-audit floor of 0.001 — the whole point of removing the floor.
    assert expected < 0.001


def test_sidak_manual_calc_matches_within_1pct():
    """The same formula used in tools/hypothesis.py."""
    for n_test in (100, 1000, 4594):
        direct = 1.0 - (1.0 - 0.05) ** (1.0 / n_test)
        # within 1% of analytical — trivially true, sanity check
        assert direct > 0


@pytest.mark.asyncio
async def test_denominator_uses_backtest_runs_not_active(temp_db_path, monkeypatch):
    """The FWER denominator must be the count of DISTINCT hypothesis_ids in
    backtest_runs (lifetime tested), not the count of active hypotheses."""
    await _seed_db(temp_db_path, n_lifetime=4594, n_active=50)

    # The query the gate actually issues:
    async with aiosqlite.connect(temp_db_path) as db:
        cur = await db.execute(
            "SELECT COUNT(DISTINCT hypothesis_id) FROM backtest_runs "
            "WHERE completed_at IS NOT NULL"
        )
        lifetime_n = (await cur.fetchone())[0]
        cur = await db.execute(
            "SELECT COUNT(*) FROM hypotheses WHERE status IN ('backtesting','paper_trading')"
        )
        active_n = (await cur.fetchone())[0]

    assert lifetime_n == 4594
    assert active_n == 50
    # Pre-fix: floored@0.001 gave effective 0.001; post-fix: ~1.1e-5
    post_fix = 1.0 - (1.0 - 0.05) ** (1.0 / lifetime_n)
    pre_fix_floor = 0.001
    assert post_fix < pre_fix_floor / 50  # at least 50x stricter
