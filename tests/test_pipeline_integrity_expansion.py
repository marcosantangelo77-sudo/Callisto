"""Tests for the feat/pipeline-integrity-expansion check set.

Each new check is exercised against an isolated temp SQLite DB seeded
with synthetic rows that should trigger the check (plus a "clean" case
that should not).

The live memory/callisto.db is never touched — ``CALLISTO_DB_PATH`` is
monkeypatched per-test, and ``tools.pipeline_integrity.DB_PATH`` is
patched as well because the module reads it at import time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest
import pytest_asyncio

import tools.pipeline_integrity as pi


_MIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    model_config TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    date_range_start DATE NOT NULL,
    date_range_end DATE NOT NULL,
    total_events INTEGER DEFAULT 0,
    signals_generated INTEGER DEFAULT 0,
    actual_win INTEGER DEFAULT 0,
    actual_loss INTEGER DEFAULT 0,
    actual_push INTEGER DEFAULT 0,
    unresolved INTEGER DEFAULT 0,
    completed_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS backtest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    hypothesis_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    player TEXT,
    market TEXT NOT NULL,
    line REAL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    book_odds_american INTEGER NOT NULL,
    book_implied_prob REAL NOT NULL,
    model_fair_prob REAL NOT NULL,
    edge REAL NOT NULL,
    ev_pct REAL NOT NULL,
    kelly_fraction REAL,
    signal_generated BOOLEAN DEFAULT FALSE,
    actual_result TEXT,
    actual_stat REAL,
    closing_odds INTEGER,
    closing_implied REAL,
    clv_implied REAL,
    game_date DATE NOT NULL,
    local_game_date DATE,
    snapshot_time DATETIME NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS paper_trades (
    trade_id TEXT PRIMARY KEY,
    hypothesis_id TEXT NOT NULL,
    event_id TEXT,
    sport TEXT NOT NULL,
    player TEXT,
    market TEXT NOT NULL,
    line REAL,
    side TEXT NOT NULL,
    book TEXT NOT NULL,
    signal_time DATETIME NOT NULL,
    signal_odds_american INTEGER NOT NULL,
    signal_implied_prob REAL NOT NULL,
    model_fair_prob REAL NOT NULL,
    edge REAL NOT NULL,
    ev_pct REAL NOT NULL,
    kelly_fraction REAL,
    recommended_stake REAL,
    closing_odds INTEGER,
    closing_implied REAL,
    clv_implied REAL,
    actual_result TEXT,
    actual_stat REAL,
    hypothetical_pnl REAL,
    game_date DATE NOT NULL,
    local_game_date DATE,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS live_edge_surface (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_at TEXT NOT NULL,
    sport TEXT NOT NULL,
    event_id TEXT NOT NULL,
    market TEXT NOT NULL,
    outcome TEXT NOT NULL,
    placement_book TEXT NOT NULL,
    placement_implied REAL NOT NULL,
    placement_fair REAL NOT NULL,
    consensus_fair REAL NOT NULL,
    raw_edge REAL NOT NULL,
    effective_edge REAL NOT NULL,
    penalty_total REAL NOT NULL,
    penalty_breakdown TEXT NOT NULL,
    n_books INTEGER NOT NULL,
    decision TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT,
    checksum TEXT,
    bootstrap INTEGER NOT NULL DEFAULT 0
);
"""


@pytest_asyncio.fixture
async def db_path(tmp_path, monkeypatch):
    """Isolated temp DB + module-level DB_PATH patch."""
    path = str(tmp_path / "pi_expansion.db")
    monkeypatch.setenv("CALLISTO_DB_PATH", path)
    monkeypatch.setattr(pi, "DB_PATH", path, raising=True)
    async with aiosqlite.connect(path) as db:
        await db.executescript(_MIN_SCHEMA)
        await db.commit()
    return path


@pytest_asyncio.fixture
async def checker(db_path):
    """Fresh singleton-independent checker."""
    c = pi.PipelineIntegrityChecker()
    await c.ensure_table()
    return c


def _find_result(checker_obj, name):
    for r in checker_obj._check_results:
        if r["name"] == name:
            return r
    return None


# ─────────────────────────────────────────────
# 1. hypothesis_status_distribution
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_distribution_flags_dominant_status(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        # 95 in 'draft', 5 scattered elsewhere → draft dominates 95%
        for i in range(95):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'draft')",
                (f"H_draft_{i}", f"h{i}"),
            )
        for i in range(3):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'backtesting')",
                (f"H_bt_{i}", f"bt{i}"),
            )
        for i in range(2):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'paper_trading')",
                (f"H_pt_{i}", f"pt{i}"),
            )
        await db.commit()

    await checker._check_status_distribution()
    r = _find_result(checker, "hypothesis_status_distribution")
    assert r is not None
    assert r["severity"] == "warn"
    assert r["metric_value"] >= 0.90
    assert any(i.check_name == "hypothesis_status_distribution" for i in checker._issues)


@pytest.mark.asyncio
async def test_status_distribution_healthy_spread(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        for i in range(10):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'draft')",
                (f"H_d_{i}", f"d{i}"),
            )
        for i in range(10):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'backtesting')",
                (f"H_b_{i}", f"b{i}"),
            )
        for i in range(5):
            await db.execute(
                "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES (?, ?, 'paper_trading')",
                (f"H_p_{i}", f"p{i}"),
            )
        await db.commit()

    await checker._check_status_distribution()
    r = _find_result(checker, "hypothesis_status_distribution")
    assert r is not None
    assert r["severity"] == "ok"


# ─────────────────────────────────────────────
# 2. backtest_staleness
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_backtest_staleness_flags_old_runs(db_path, checker):
    old = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H1', 'h1', 'paper_trading')"
        )
        await db.execute(
            "INSERT INTO backtest_runs (run_id, hypothesis_id, date_range_start, "
            "date_range_end, completed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("run_old", "H1", "2026-01-01", "2026-01-15", old, old),
        )
        await db.commit()

    await checker._check_backtest_staleness()
    r = _find_result(checker, "backtest_staleness")
    assert r is not None
    assert r["severity"] == "warn"
    assert r["metric_value"] == 1


@pytest.mark.asyncio
async def test_backtest_staleness_fresh_run(db_path, checker):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H2', 'h2', 'paper_trading')"
        )
        await db.execute(
            "INSERT INTO backtest_runs (run_id, hypothesis_id, date_range_start, "
            "date_range_end, completed_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("run_recent", "H2", "2026-04-01", "2026-04-15", recent, recent),
        )
        await db.commit()

    await checker._check_backtest_staleness()
    r = _find_result(checker, "backtest_staleness")
    assert r is not None
    assert r["severity"] == "ok"


# ─────────────────────────────────────────────
# 3. signal_resolution_lag
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_signal_resolution_lag_flags_old_unresolved(db_path, checker):
    old_date = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H3', 'h3', 'backtesting')"
        )
        for i in range(3):
            await db.execute(
                "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
                "sport, market, side, book, book_odds_american, book_implied_prob, "
                "model_fair_prob, edge, ev_pct, signal_generated, actual_result, "
                "game_date, local_game_date, snapshot_time) "
                "VALUES ('r1', ?, 'H3', 'mlb', 'totals', 'Over', 'dk', -110, "
                "0.524, 0.55, 0.02, 0.05, 1, NULL, ?, ?, ?)",
                (f"E{i}", old_date, old_date, f"{old_date}T12:00:00"),
            )
        await db.commit()

    await checker._check_signal_resolution_lag()
    r = _find_result(checker, "signal_resolution_lag")
    assert r is not None
    assert r["severity"] in ("warn", "critical")
    assert r["metric_value"] == 3


@pytest.mark.asyncio
async def test_signal_resolution_lag_healthy(db_path, checker):
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H4', 'h4', 'backtesting')"
        )
        await db.execute(
            "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
            "sport, market, side, book, book_odds_american, book_implied_prob, "
            "model_fair_prob, edge, ev_pct, signal_generated, actual_result, "
            "game_date, local_game_date, snapshot_time) "
            "VALUES ('r1', 'E1', 'H4', 'mlb', 'totals', 'Over', 'dk', -110, "
            "0.524, 0.55, 0.02, 0.05, 1, 'WIN', ?, ?, ?)",
            (recent, recent, f"{recent}T12:00:00"),
        )
        await db.commit()

    await checker._check_signal_resolution_lag()
    r = _find_result(checker, "signal_resolution_lag")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] == 0


# ─────────────────────────────────────────────
# 4. closing_line_coverage
# ─────────────────────────────────────────────

async def _insert_paper_trade(db, trade_id, hypothesis_id, *, actual_result=None, closing_odds=None):
    await db.execute(
        "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport, "
        "market, side, book, signal_time, signal_odds_american, "
        "signal_implied_prob, model_fair_prob, edge, ev_pct, game_date, "
        "actual_result, closing_odds) "
        "VALUES (?, ?, 'E1', 'mlb', 'totals', 'Over', 'dk', "
        "'2026-04-10T12:00:00', -110, 0.524, 0.55, 0.02, 0.05, '2026-04-10', ?, ?)",
        (trade_id, hypothesis_id, actual_result, closing_odds),
    )


@pytest.mark.asyncio
async def test_closing_line_coverage_flags_low(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H5', 'h5', 'paper_trading')"
        )
        # 30 settled trades, only 5 with closing line → ~17% coverage
        for i in range(25):
            await _insert_paper_trade(db, f"t_nc_{i}", "H5", actual_result="WIN", closing_odds=None)
        for i in range(5):
            await _insert_paper_trade(db, f"t_c_{i}", "H5", actual_result="WIN", closing_odds=-105)
        await db.commit()

    await checker._check_closing_line_coverage()
    r = _find_result(checker, "closing_line_coverage")
    assert r is not None
    assert r["severity"] == "critical"
    assert r["metric_value"] < 0.50


@pytest.mark.asyncio
async def test_closing_line_coverage_healthy(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H6', 'h6', 'paper_trading')"
        )
        for i in range(25):
            await _insert_paper_trade(db, f"t_ok_{i}", "H6", actual_result="WIN", closing_odds=-110)
        await db.commit()

    await checker._check_closing_line_coverage()
    r = _find_result(checker, "closing_line_coverage")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] >= 0.99


# ─────────────────────────────────────────────
# 5. edge_bet_conversion
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_edge_bet_conversion_low_ratio(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H7', 'h7', 'paper_trading')"
        )
        # 200 actionable edges, 1 trade → 0.5% conversion
        for i in range(200):
            await db.execute(
                "INSERT INTO live_edge_surface (computed_at, sport, event_id, "
                "market, outcome, placement_book, placement_implied, placement_fair, "
                "consensus_fair, raw_edge, effective_edge, penalty_total, "
                "penalty_breakdown, n_books, decision) "
                "VALUES ('2026-04-10T12:00:00', 'mlb', ?, 'h2h', 'home', 'dk', "
                "0.5, 0.52, 0.52, 0.02, 0.02, 0.0, '{}', 5, 'take')",
                (f"E{i}",),
            )
        await _insert_paper_trade(db, "pt_x", "H7")
        await db.commit()

    await checker._check_edge_bet_conversion()
    r = _find_result(checker, "edge_bet_conversion")
    assert r is not None
    assert r["severity"] == "warn"
    assert r["metric_value"] < 0.01


@pytest.mark.asyncio
async def test_edge_bet_conversion_healthy(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H8', 'h8', 'paper_trading')"
        )
        # 100 edges, 10 trades → 10% conversion
        for i in range(100):
            await db.execute(
                "INSERT INTO live_edge_surface (computed_at, sport, event_id, "
                "market, outcome, placement_book, placement_implied, placement_fair, "
                "consensus_fair, raw_edge, effective_edge, penalty_total, "
                "penalty_breakdown, n_books, decision) "
                "VALUES ('2026-04-10T12:00:00', 'mlb', ?, 'h2h', 'home', 'dk', "
                "0.5, 0.52, 0.52, 0.02, 0.02, 0.0, '{}', 5, 'take')",
                (f"Eh{i}",),
            )
        for i in range(10):
            await _insert_paper_trade(db, f"pth_{i}", "H8")
        await db.commit()

    await checker._check_edge_bet_conversion()
    r = _find_result(checker, "edge_bet_conversion")
    assert r is not None
    assert r["severity"] == "ok"


# ─────────────────────────────────────────────
# 6. migration_drift
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_migration_drift_detects_missing_rows(db_path, checker, monkeypatch):
    # Simulate: disk has versions [1,2,3] but DB only recorded [1,2].
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _FakeMig:
        version: int
        name: str = "fake"
        module_name: str = "fake"
        up: object = None
        down: object = None
        source_checksum: str = ""

    fake_migs = [_FakeMig(v) for v in (1, 2, 3)]

    def fake_discover():
        return fake_migs

    import tools.migrations as migs_pkg
    monkeypatch.setattr(migs_pkg, "discover_migrations", fake_discover)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'init', '2026-01-01T00:00:00Z')"
        )
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (2, 'two', '2026-01-02T00:00:00Z')"
        )
        await db.commit()

    await checker._check_migration_drift()
    r = _find_result(checker, "migration_drift")
    assert r is not None
    assert r["severity"] == "critical"
    assert r["metric_value"] == 1  # exactly 1 drifted (version 3)


@pytest.mark.asyncio
async def test_migration_drift_healthy(db_path, checker, monkeypatch):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _FakeMig:
        version: int
        name: str = "fake"
        module_name: str = "fake"
        up: object = None
        down: object = None
        source_checksum: str = ""

    fake_migs = [_FakeMig(v) for v in (1, 2)]

    def fake_discover():
        return fake_migs

    import tools.migrations as migs_pkg
    monkeypatch.setattr(migs_pkg, "discover_migrations", fake_discover)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'init', '2026-01-01T00:00:00Z')"
        )
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (2, 'two', '2026-01-02T00:00:00Z')"
        )
        await db.commit()

    await checker._check_migration_drift()
    r = _find_result(checker, "migration_drift")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] == 0


# ─────────────────────────────────────────────
# 7. db_index_coverage
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_db_index_coverage_flags_missing(db_path, checker):
    # Our _MIN_SCHEMA deliberately created signals/hypotheses/paper_trades
    # WITHOUT leading indexes on the hot columns — check should flag them.
    await checker._check_db_index_coverage()
    r = _find_result(checker, "db_index_coverage")
    assert r is not None
    assert r["severity"] == "warn"
    assert r["metric_value"] > 0


@pytest.mark.asyncio
async def test_db_index_coverage_healthy(db_path, checker):
    # Add leading indexes on every HOT_INDEX_COLUMNS entry that exists.
    async with aiosqlite.connect(db_path) as db:
        stmts = [
            "CREATE INDEX idx_be_created ON backtest_events(created_at)",
            "CREATE INDEX idx_be_event ON backtest_events(event_id)",
            "CREATE INDEX idx_pt_created ON paper_trades(created_at)",
            "CREATE INDEX idx_pt_hypo ON paper_trades(hypothesis_id)",
            "CREATE INDEX idx_sig_created ON signals(created_at)",
            "CREATE INDEX idx_hyp_status ON hypotheses(status)",
            "CREATE INDEX idx_hyp_updated ON hypotheses(updated_at)",
        ]
        for s in stmts:
            await db.execute(s)
        await db.commit()

    await checker._check_db_index_coverage()
    r = _find_result(checker, "db_index_coverage")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] == 0


# ─────────────────────────────────────────────
# 8. orphaned_records
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orphaned_records_critical_when_missing_parent(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        # No hypothesis 'H_GONE' in hypotheses, but events + trades reference it
        await db.execute(
            "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
            "sport, market, side, book, book_odds_american, book_implied_prob, "
            "model_fair_prob, edge, ev_pct, signal_generated, game_date, "
            "snapshot_time) VALUES ('r', 'E1', 'H_GONE', 'mlb', 'h2h', 'home', "
            "'dk', -110, 0.524, 0.55, 0.02, 0.05, 1, '2026-04-10', "
            "'2026-04-10T12:00:00')"
        )
        await _insert_paper_trade(db, "pt_orphan", "H_GONE")
        await db.commit()

    await checker._check_orphaned_records()
    r = _find_result(checker, "orphaned_records")
    assert r is not None
    assert r["severity"] == "critical"
    assert r["metric_value"] >= 2


@pytest.mark.asyncio
async def test_orphaned_records_healthy(db_path, checker):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO hypotheses (hypothesis_id, name, status) VALUES "
            "('H_OK', 'ok', 'paper_trading')"
        )
        await db.execute(
            "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
            "sport, market, side, book, book_odds_american, book_implied_prob, "
            "model_fair_prob, edge, ev_pct, signal_generated, game_date, "
            "snapshot_time) VALUES ('r', 'E1', 'H_OK', 'mlb', 'h2h', 'home', "
            "'dk', -110, 0.524, 0.55, 0.02, 0.05, 1, '2026-04-10', "
            "'2026-04-10T12:00:00')"
        )
        await _insert_paper_trade(db, "pt_ok", "H_OK")
        await db.commit()

    await checker._check_orphaned_records()
    r = _find_result(checker, "orphaned_records")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] == 0


# ─────────────────────────────────────────────
# 9. schema_version_lag
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_schema_version_lag_detected(db_path, checker, monkeypatch):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _FakeMig:
        version: int
        name: str = "fake"
        module_name: str = "fake"
        up: object = None
        down: object = None
        source_checksum: str = ""

    fake_migs = [_FakeMig(v) for v in (1, 2, 3, 4, 5)]

    def fake_discover():
        return fake_migs

    import tools.migrations as migs_pkg
    monkeypatch.setattr(migs_pkg, "discover_migrations", fake_discover)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'init', '2026-01-01T00:00:00Z')"
        )
        await db.commit()

    await checker._check_schema_version_lag()
    r = _find_result(checker, "schema_version_lag")
    assert r is not None
    assert r["severity"] == "critical"
    assert r["metric_value"] == 4


@pytest.mark.asyncio
async def test_schema_version_lag_none(db_path, checker, monkeypatch):
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _FakeMig:
        version: int
        name: str = "fake"
        module_name: str = "fake"
        up: object = None
        down: object = None
        source_checksum: str = ""

    fake_migs = [_FakeMig(v) for v in (1, 2)]

    def fake_discover():
        return fake_migs

    import tools.migrations as migs_pkg
    monkeypatch.setattr(migs_pkg, "discover_migrations", fake_discover)

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (1, 'init', '2026-01-01T00:00:00Z')"
        )
        await db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) "
            "VALUES (2, 'two', '2026-01-02T00:00:00Z')"
        )
        await db.commit()

    await checker._check_schema_version_lag()
    r = _find_result(checker, "schema_version_lag")
    assert r is not None
    assert r["severity"] == "ok"
    assert r["metric_value"] == 0


# ─────────────────────────────────────────────
# Integration: run_all_checks surfaces new checks
# ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_all_checks_includes_expansion_checks(db_path, checker):
    summary = await checker.run_all_checks()
    assert "check_results" in summary
    names = {r["name"] for r in summary["check_results"]}
    expected = {
        "hypothesis_status_distribution",
        "backtest_staleness",
        "signal_resolution_lag",
        "closing_line_coverage",
        "edge_bet_conversion",
        "migration_drift",
        "db_index_coverage",
        "orphaned_records",
        "schema_version_lag",
    }
    assert expected.issubset(names), f"Missing checks: {expected - names}"
    # Each check_result has the required spec keys
    for r in summary["check_results"]:
        assert set(r.keys()) >= {"name", "severity", "detail", "metric_value"}
        assert r["severity"] in {"ok", "warn", "critical"}


@pytest.mark.asyncio
async def test_check_result_format(db_path, checker):
    """Verify the {name, severity, detail, metric_value} spec exactly."""
    r = pi._check_result("foo", "warn", "detail msg", 42)
    assert r == {
        "name": "foo",
        "severity": "warn",
        "detail": "detail msg",
        "metric_value": 42,
    }
