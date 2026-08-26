"""Kill-switch test: _phase_refresh_signals must NOT rewrite evidence by default.

Default env: signal_generated stays 0 even when edge >= edge_threshold.
CALLISTO_ALLOW_SIGNAL_REFRESH=1 (operator-explicit): the legacy upgrade runs.
In-memory-ish sqlite via tmp_path; no network, no Claude.
"""

import asyncio
import os
import sys
import types

import aiosqlite
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Stub polars before importing tools.autonomous (same as tier1 gate-policy test).
if "polars" not in sys.modules:
    try:
        import polars  # noqa: F401
    except ModuleNotFoundError:
        _pl = types.ModuleType("polars")
        _pl.DataFrame = type("DataFrame", (), {})
        _pl.Series = object
        _pl.read_parquet = lambda *a, **k: None
        sys.modules["polars"] = _pl

import tools.autonomous as auto

SCHEMA = """
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    edge_threshold REAL,
    notes TEXT
);
CREATE TABLE backtest_events (
    id TEXT PRIMARY KEY,
    hypothesis_id TEXT,
    run_id TEXT,
    edge REAL,
    signal_generated INTEGER DEFAULT 0
);
CREATE TABLE backtest_runs (
    run_id TEXT PRIMARY KEY,
    signals_generated INTEGER DEFAULT 0
);
"""


async def _seed(db_path):
    async with aiosqlite.connect(str(db_path)) as db:
        await db.executescript(SCHEMA)
        await db.execute(
            "INSERT INTO hypotheses VALUES ('h1', 0.010, '')"
        )
        await db.execute(
            "INSERT INTO backtest_events VALUES ('e1', 'h1', 'r1', 0.05, 0)"
        )
        await db.execute("INSERT INTO backtest_runs VALUES ('r1', 0)")
        await db.commit()


def _stub_loop(db_path):
    """Minimal stand-in exposing only what _phase_refresh_signals touches."""

    class _StubEngine:
        def __init__(self, p):
            self.db_path = str(p)

        def recalculate_run_stats(self, rid):
            return None

    class _StubLoop:
        def __init__(self, p):
            self.backtest_engine = _StubEngine(p)
            self.backtest_engine.db_path = str(p)

    return _StubLoop(db_path)


async def _signal_value(db_path):
    async with aiosqlite.connect(str(db_path)) as db:
        cur = await db.execute(
            "SELECT signal_generated FROM backtest_events WHERE id='e1'"
        )
        return (await cur.fetchone())[0]


class TestSignalRefreshGate:
    @pytest.fixture(autouse=True)
    def _env_restore(self):
        saved = os.environ.get("CALLISTO_ALLOW_SIGNAL_REFRESH")
        yield
        if saved is None:
            os.environ.pop("CALLISTO_ALLOW_SIGNAL_REFRESH", None)
        else:
            os.environ["CALLISTO_ALLOW_SIGNAL_REFRESH"] = saved

    @pytest.mark.asyncio
    async def test_default_env_does_not_write(self, tmp_path):
        os.environ.pop("CALLISTO_ALLOW_SIGNAL_REFRESH", None)
        await _seed(tmp_path / "t.db")
        loop = _stub_loop(tmp_path / "t.db")

        await auto.ResearchLoop._phase_refresh_signals(loop)  # noqa: SLF001

        assert await _signal_value(tmp_path / "t.db") == 0

    @pytest.mark.asyncio
    async def test_env_other_than_1_does_not_write(self, tmp_path):
        os.environ["CALLISTO_ALLOW_SIGNAL_REFRESH"] = "true"
        await _seed(tmp_path / "t.db")
        loop = _stub_loop(tmp_path / "t.db")

        await auto.ResearchLoop._phase_refresh_signals(loop)  # noqa: SLF001

        assert await _signal_value(tmp_path / "t.db") == 0

    @pytest.mark.asyncio
    async def test_explicit_env_1_allows_upgrade(self, tmp_path):
        os.environ["CALLISTO_ALLOW_SIGNAL_REFRESH"] = "1"
        await _seed(tmp_path / "t.db")
        loop = _stub_loop(tmp_path / "t.db")

        await auto.ResearchLoop._phase_refresh_signals(loop)  # noqa: SLF001

        assert await _signal_value(tmp_path / "t.db") == 1

    @pytest.mark.asyncio
    async def test_default_env_is_read_only_no_runs_write(self, tmp_path):
        os.environ.pop("CALLISTO_ALLOW_SIGNAL_REFRESH", None)
        await _seed(tmp_path / "t.db")
        loop = _stub_loop(tmp_path / "t.db")

        await auto.ResearchLoop._phase_refresh_signals(loop)  # noqa: SLF001

        import aiosqlite as sq
        async with sq.connect(str(tmp_path / "t.db")) as db:
            cur = await db.execute(
                "SELECT signals_generated FROM backtest_runs WHERE run_id='r1'"
            )
            assert (await cur.fetchone())[0] == 0
