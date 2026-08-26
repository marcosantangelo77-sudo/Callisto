"""Tests for the tools.pipeint split of pipeline_integrity.

Covers:
- Facade re-exports (tools.pipeline_integrity keeps its import surface)
- Module structure of the split
- Real checker behavior against an in-memory-shaped temp SQLite DB:
  paper trade flow, temporal isolation, signal pipeline, zero/stale
  metrics, rejection rate, calibration health
- Fail-closed semantics: broken pipelines yield CRITICAL issues, never
  downgraded to warnings.
"""

import asyncio
import os
import sys
import tempfile

import aiosqlite
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ── Facade / split structure ─────────────────────────────────────────


def test_facade_reexports_public_surface():
    import tools.pipeline_integrity as pi

    assert hasattr(pi, "PipelineIntegrityChecker")
    assert hasattr(pi, "IntegrityIssue")
    assert callable(pi.get_checker)
    assert callable(pi.initialize)
    for name in [
        "SEVERITY_CRITICAL",
        "SEVERITY_WARNING",
        "SEVERITY_INFO",
        "PAPER_TRADE_STALL_HOURS",
        "HYPOTHESIS_STALL_HOURS",
        "BACKTEST_MIN_EVENTS_FOR_EDGE_CHECK",
        "ODDS_SNAPSHOT_STALE_HOURS",
        "SIGNAL_PIPELINE_MIN_HYPOTHESES",
        "REJECTION_RATE_BROKEN_THRESHOLD",
        "PHASE_ERROR_RATE_THRESHOLD",
        "METRIC_STALE_HOURS",
        "INTEGRITY_CHECK_INTERVAL_CYCLES",
        "DB_PATH",
    ]:
        assert hasattr(pi, name), f"facade missing {name}"


def test_split_modules_exist_and_are_importable():
    from tools.pipeint import (
        checker,
        checks_data_flow,
        checks_quality,
        core,
    )

    assert checker.PipelineIntegrityChecker is not None
    assert checks_data_flow.DataFlowChecks is not None
    assert checks_quality.QualityChecks is not None
    assert core.IntegrityIssue is not None


def test_checker_composes_check_mixins():
    from tools.pipeint.checker import PipelineIntegrityChecker
    from tools.pipeint.checks_data_flow import DataFlowChecks
    from tools.pipeint.checks_quality import QualityChecks

    assert issubclass(PipelineIntegrityChecker, DataFlowChecks)
    assert issubclass(PipelineIntegrityChecker, QualityChecks)


def test_singleton_returns_same_instance():
    import tools.pipeline_integrity as pi

    c1 = pi.get_checker()
    c2 = pi.get_checker()
    assert c1 is c2


# ── IntegrityIssue basics ────────────────────────────────────────────


def test_integrity_issue_to_dict():
    import tools.pipeline_integrity as pi

    issue = pi.IntegrityIssue("check_x", pi.SEVERITY_CRITICAL, "broken", details={"a": 1})
    d = issue.to_dict()
    assert d["check"] == "check_x"
    assert d["severity"] == "CRITICAL"
    assert d["details"] == {"a": 1}
    assert "timestamp" in d


# ── DB-backed behavioral tests ───────────────────────────────────────

SCHEMA = """
CREATE TABLE hypotheses (
    hypothesis_id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT,
    model_config TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE paper_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT,
    created_at TEXT
);
CREATE TABLE backtest_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT,
    edge REAL,
    signal_generated INTEGER,
    created_at TEXT
);
CREATE TABLE backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    completed_at TEXT,
    total_events INTEGER DEFAULT 0,
    unresolved INTEGER DEFAULT 0,
    signals_generated INTEGER DEFAULT 0
);
CREATE TABLE signals (id INTEGER PRIMARY KEY AUTOINCREMENT);
CREATE TABLE game_contexts (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT);
CREATE TABLE odds_snapshots (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT);
CREATE TABLE odds_snapshots_v2 (id INTEGER PRIMARY KEY AUTOINCREMENT, snapshot_time TEXT);
CREATE TABLE hypothesis_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT,
    brier_score REAL,
    information_coefficient REAL,
    sortino REAL,
    signals_n INTEGER,
    computed_at TEXT
);
"""


@pytest.fixture()
def checker(tmp_path, monkeypatch):
    """A PipelineIntegrityChecker pointed at a fresh temp DB."""
    db_path = str(tmp_path / "test.db")
    import tools.pipeint.checker as checker_mod
    import tools.pipeint.checks_data_flow as df_mod
    import tools.pipeint.checks_quality as q_mod

    for mod in (checker_mod, df_mod, q_mod):
        monkeypatch.setattr(mod, "DB_PATH", db_path)

    async def _setup():
        async with aiosqlite.connect(db_path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    asyncio.run(_setup())

    # Fresh singleton instance per test
    import tools.pipeint.checker as checker_pkg

    old = checker_pkg._checker
    checker_pkg._checker = None
    import tools.pipeline_integrity as pi

    chk = pi.get_checker()

    yield chk

    checker_pkg._checker = old


def run(coro):
    return asyncio.run(coro)


def seed_hypothesis(db_path, h_id="h1", status="paper_trading", updated_at=None,
                     model_config=None, name="Test Hypo"):
    async def _seed():
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                "INSERT OR REPLACE INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                (h_id, name, status, model_config, updated_at, updated_at),
            )
            await db.commit()
    import tools.pipeint.checks_data_flow as df_mod
    asyncio.run(_seed())


def get_db_path(checker):
    import tools.pipeint.checks_data_flow as df_mod
    return df_mod.DB_PATH


def test_paper_trade_stall_is_critical(checker):
    """Paper-trading hypotheses + zero trades after the stall window => CRITICAL."""
    from datetime import datetime, timedelta, timezone

    db = get_db_path(checker)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    seed_hypothesis(db, status="paper_trading", updated_at=old)

    run(checker._check_paper_trade_flow())
    criticals = [i for i in checker._issues if i.severity == "CRITICAL"]
    assert criticals, "expected a CRITICAL issue for stalled paper trade flow"
    assert criticals[0].check_name == "paper_trade_flow"
    assert "0 paper trades" in criticals[0].message


def test_paper_trade_flow_healthy_when_trades_exist(checker):
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()
    seed_hypothesis(db, status="paper_trading", updated_at=now)

    async def _add_trade():
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "INSERT INTO paper_trades (hypothesis_id, created_at) VALUES (?, ?)",
                ("h1", now),
            )
            await conn.commit()

    asyncio.run(_add_trade())
    run(checker._check_paper_trade_flow())
    assert all(i.severity != "CRITICAL" for i in checker._issues)


def test_temporal_overlap_is_critical_fail_closed(checker):
    """Circular testing (backtest starts before training ends) must stay CRITICAL."""
    mc = '{"training_period_end": "2025-06-01", "backtest_period_start": "2025-05-01"}'
    from datetime import datetime, timezone

    db = get_db_path(checker)
    seed_hypothesis(db, status="backtesting", updated_at=datetime.now(timezone.utc).isoformat(),
                    model_config=mc)

    run(checker._check_temporal_isolation())
    criticals = [i for i in checker._issues if i.severity == "CRITICAL"]
    assert len(criticals) == 1
    assert "CIRCULAR TESTING" in criticals[0].message


def test_proper_isolation_no_issue(checker):
    mc = '{"training_period_end": "2025-05-01", "backtest_period_start": "2025-06-01", "temporal_isolation": true}'
    from datetime import datetime, timezone

    db = get_db_path(checker)
    seed_hypothesis(db, status="backtesting", updated_at=datetime.now(timezone.utc).isoformat(),
                    model_config=mc)

    run(checker._check_temporal_isolation())
    assert not any(i.severity == "CRITICAL" for i in checker._issues)


def test_signal_pipeline_zero_signals_is_critical(checker):
    """40+ backtesting hypotheses, events exist, but 0 signals => CRITICAL."""
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            for i in range(45):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", f"Hypo {i}", "backtesting", None, now, now),
                )
            await conn.execute(
                "INSERT INTO backtest_events (hypothesis_id, edge, signal_generated) VALUES (?, ?, ?)",
                ("h0", -0.02, 0),
            )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_signal_pipeline())
    criticals = [i for i in checker._issues if i.severity == "CRITICAL"]
    assert len(criticals) == 1
    assert criticals[0].check_name == "signal_pipeline"


def test_backtest_edge_rate_circular_when_not_isolated(checker):
    """0% positive edges across 50+ non-isolated events => CRITICAL."""
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                ("h1", "H1", "backtesting", '{"temporal_isolation": false}', now, now),
            )
            for _ in range(60):
                await conn.execute(
                    "INSERT INTO backtest_events (hypothesis_id, edge, signal_generated) "
                    "VALUES (?, ?, ?)",
                    ("h1", -0.05, 1 if False else 0),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_backtest_edge_rate())
    criticals = [i for i in checker._issues
                 if i.severity == "CRITICAL" and i.check_name == "backtest_edge_rate"]
    assert len(criticals) == 1
    assert "temporal isolation" in criticals[0].message


def test_backtest_edge_rate_info_when_all_isolated(checker):
    """0% edges WITH proper temporal isolation on every hypothesis => INFO, not CRITICAL."""
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            await conn.execute(
                "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                ("h1", "H1", "backtesting", '{"temporal_isolation": true}', now, now),
            )
            for _ in range(60):
                await conn.execute(
                    "INSERT INTO backtest_events (hypothesis_id, edge, signal_generated) "
                    "VALUES (?, ?, ?)",
                    ("h1", -0.05, 0),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_backtest_edge_rate())
    severities = {i.severity for i in checker._issues if i.check_name == "backtest_edge_rate"}
    assert severities == {"INFO"}


def test_zero_metric_detection_flags_missing_paper_trades(checker):
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()
    seed_hypothesis(db, status="paper_trading", updated_at=now)

    run(checker._check_zero_metrics())
    warnings = [i for i in checker._issues if i.check_name == "zero_metric_detection"]
    assert warnings, "expected zero-metric warning"
    joined = "; ".join(
        line for w in warnings for line in w.details.get("zero_checks", [])
    )
    assert "paper_trades=0" in joined


def test_rejection_rate_broken_evaluation(checker):
    """>95% rejection across 20+ evaluated hypotheses => WARNING."""
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            for i in range(30):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", f"H{i}", "rejected", None, now, now),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_rejection_rate())
    warns = [i for i in checker._issues if i.check_name == "rejection_rate"]
    assert len(warns) == 1
    assert warns[0].severity == "WARNING"


def test_rejection_rate_healthy_population_no_issue(checker):
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            for i in range(10):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", f"H{i}", "rejected", None, now, now),
                )
            for i in range(15):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"p{i}", f"P{i}", "paper_trading", None, now, now),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_rejection_rate())
    assert not [i for i in checker._issues if i.check_name == "rejection_rate"]


def test_calibration_health_poor_brier_is_warning_below_four(checker):
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            for i in range(2):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", f"H{i}", "backtesting", None, now, now),
                )
                await conn.execute(
                    "INSERT INTO hypothesis_stats (hypothesis_id, brier_score, "
                    "information_coefficient, sortino, signals_n, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", 0.42, 0.1, 1.0, 20, now),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_calibration_health())
    warns = [i for i in checker._issues if i.check_name == "calibration_health"]
    assert warns and warns[0].severity == "WARNING"


def test_calibration_health_many_poor_is_critical(checker):
    """4+ poorly calibrated hypotheses escalate beyond warning."""
    from datetime import datetime, timezone

    db = get_db_path(checker)
    now = datetime.now(timezone.utc).isoformat()

    async def _seed():
        async with aiosqlite.connect(db) as conn:
            for i in range(5):
                await conn.execute(
                    "INSERT INTO hypotheses VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", f"H{i}", "backtesting", None, now, now),
                )
                await conn.execute(
                    "INSERT INTO hypothesis_stats (hypothesis_id, brier_score, "
                    "information_coefficient, sortino, signals_n, computed_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (f"h{i}", 0.45, 0.1, 1.0, 20, now),
                )
            await conn.commit()

    asyncio.run(_seed())
    run(checker._check_calibration_health())
    crits = [i for i in checker._issues
             if i.check_name == "calibration_health" and i.severity == "CRITICAL"]
    assert crits


def test_run_all_checks_summary_shape(checker):
    summary = run(checker.run_all_checks())
    assert summary["healthy"] is True
    assert summary["issues"]["total"] == 0
    assert summary["issue_details"] == []
    assert summary["run_number"] == 1
    report = checker.get_latest_report()
    assert report["status"] == "ok"
    assert report["healthy"] is True


def test_issues_logged_to_database(checker):
    from datetime import datetime, timedelta, timezone

    db = get_db_path(checker)
    old = (datetime.now(timezone.utc) - timedelta(hours=96)).isoformat()
    seed_hypothesis(db, status="paper_trading", updated_at=old)

    run(checker.ensure_table())
    summary = run(checker.run_all_checks())
    assert summary["issues"]["critical"] >= 1

    history = run(checker.get_history(limit=10))
    assert len(history) >= 1
    assert any(h["severity"] == "CRITICAL" for h in history)
    assert all("check_name" in h and "message" in h for h in history)


def test_phase_error_tracking(checker):
    for _ in range(9):
        checker.record_phase_result("research", success=False)
    checker.record_phase_result("research", success=False)

    rates = checker.get_phase_error_rates()
    assert rates["research"]["error_rate"] == 1.0
    assert rates["research"]["is_broken"] is True

    issues = checker.check_phase_error_rates()
    assert len(issues) == 1
    assert issues[0].severity == "CRITICAL"
    assert issues[0].check_name == "phase_error_rate"


def test_phase_history_capped_at_twenty(checker):
    for i in range(30):
        checker.record_phase_result("phase_a", success=i % 2 == 0)
    assert len(checker._phase_errors["phase_a"]) == 20


def test_initialize_ensures_table(tmp_path, monkeypatch):
    import tools.pipeline_integrity as pi
    import tools.pipeint.checker as checker_mod

    db_path = str(tmp_path / "init.db")
    monkeypatch.setattr(checker_mod, "DB_PATH", db_path)

    old = checker_mod._checker
    checker_mod._checker = None
    try:
        chk = run(pi.initialize())
        assert isinstance(chk, pi.PipelineIntegrityChecker)

        async def _tables():
            async with aiosqlite.connect(db_path) as db:
                cur = await db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='integrity_checks'"
                )
                return await cur.fetchone()

        assert run(_tables()) is not None
    finally:
        checker_mod._checker = old
