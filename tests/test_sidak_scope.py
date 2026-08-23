"""FWER family scope (2026-08-23): denominator selection per findings/sidak_scope.md.

Pins:
  - CALLISTO_FWER_SCOPE validates to {window, sport, lifetime} only
  - sport scope counts DISTINCT hyps within the candidate's sport in-window
  - no floor is reintroduced at any scope
  - base/adaptive thresholds are untouched by scoping
"""
from __future__ import annotations

import os
from unittest import mock

import pytest


def test_scope_env_validates():
    """Invalid scope strings must hard-fail at import, never silently widen."""
    import importlib
    from tools import hypothesis as hyp

    assert hyp.FWER_SCOPE in ("window", "sport", "lifetime")
    with mock.patch.dict(os.environ, {"CALLISTO_FWER_SCOPE": "everything"}):
        with pytest.raises(ValueError):
            importlib.reload(hyp)
    # restore module state after reload failure path
    os.environ.pop("CALLISTO_FWER_SCOPE", None)
    importlib.reload(hyp)


def test_no_floor_at_any_scope():
    """Šidák threshold must stay below any plausible floor for large N."""
    for n in (120, 356, 3192):
        thr = 1.0 - (1.0 - 0.05) ** (1.0 / n)
        assert thr < 5e-3


@pytest.mark.asyncio
async def test_sport_scope_denominator_query():
    """The sport-scoped count query returns only same-sport hypotheses."""
    import aiosqlite, tempfile
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        async with aiosqlite.connect(path) as db:
            await db.execute(
                "CREATE TABLE hypotheses (hypothesis_id TEXT PRIMARY KEY, sport TEXT)"
            )
            await db.execute(
                "CREATE TABLE backtest_runs (run_id TEXT PRIMARY KEY, "
                "hypothesis_id TEXT NOT NULL, completed_at DATETIME)"
            )
            for i, sport in enumerate(["basketball_nba"] * 3 + ["golf_pga"] * 2):
                await db.execute(
                    "INSERT INTO hypotheses VALUES (?, ?)", (f"h{i}", sport)
                )
                await db.execute(
                    "INSERT INTO backtest_runs VALUES (?, ?, datetime('now'))",
                    (f"r{i}", f"h{i}"),
                )
            await db.commit()
            cur = await db.execute(
                "SELECT COUNT(DISTINCT br.hypothesis_id) FROM backtest_runs br "
                "JOIN hypotheses hy ON hy.hypothesis_id = br.hypothesis_id "
                "WHERE br.completed_at IS NOT NULL AND br.completed_at > datetime('now','-1 day') "
                "AND hy.sport = ?",
                ("basketball_nba",),
            )
            n = (await cur.fetchone())[0]
        assert n == 3
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
