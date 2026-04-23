"""Tests for truthful /health demotion logic.

Verifies that _evaluate_health_signals downgrades healthy=False with the
correct severity and an explanatory reason for every silent-failure class
the live system cares about: write failures, queue backlog, watchdog
staleness, task-queue backlog, stalled phases, pipeline integrity, and
tripped subsystem breakers.
"""
from __future__ import annotations

import os
import sys

# Ensure the worktree root is importable before touching api.py.
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pytest


def _import_eval():
    """Import just the pure function from api.py without booting the app."""
    # Lazy import — api.py has heavy side-effects on import. We need the
    # helper only, so stub it out via importlib and pick the symbol.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "callisto_api_under_test", os.path.join(ROOT, "api.py")
    )
    # Loading api.py end-to-end would require DB + env; instead, read the
    # source and exec just the _evaluate_health_signals function.
    src = open(os.path.join(ROOT, "api.py"), "r", encoding="utf-8").read()

    # Extract the function block
    needle = "def _evaluate_health_signals("
    start = src.index(needle)
    # End at the next top-level `async def` or `def` or `@app.` after start.
    # Simpler: find the next `\n\n\nasync def` which conventionally separates blocks.
    rest = src[start:]
    # Find end: first occurrence of "\nasync def _build_health_report"
    end_rel = rest.index("\nasync def _build_health_report")
    func_src = rest[:end_rel]

    ns: dict = {}
    exec(func_src, ns)
    return ns["_evaluate_health_signals"]


_evaluate_health_signals = _import_eval()


def test_happy_state_is_healthy():
    report = {
        "subsystems": {
            "ollama": {"is_open": False},
            "sqlite": {"is_open": False},
        },
        "write_coordinators": [
            {"db_path": "main.db", "writes_total": 1000, "writes_failed": 0, "queue_depth": 2}
        ],
        "watchdog_monitoring": {"last_ping_ago_seconds": 5.0, "total_pings": 200},
        "task_queue": {"depth": 3, "oldest_pending_seconds": 10},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is True
    assert severity == "ok"
    assert reasons == []


def test_writes_failed_rate_over_1pct_demotes_to_warning():
    report = {
        "subsystems": {},
        "write_coordinators": [
            {"db_path": "main.db", "writes_total": 100, "writes_failed": 50, "queue_depth": 0}
        ],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "warning"
    assert any("writes_failed_rate" in r for r in reasons)
    assert any("50/100" in r for r in reasons)


def test_writes_failed_rate_under_threshold_is_healthy():
    # 1 failure in 10000 is well under 1%
    report = {
        "subsystems": {},
        "write_coordinators": [
            {"db_path": "main.db", "writes_total": 10000, "writes_failed": 1, "queue_depth": 0}
        ],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, _, reasons = _evaluate_health_signals(report)
    assert healthy is True
    assert reasons == []


def test_queue_depth_over_100_demotes_to_warning():
    report = {
        "subsystems": {},
        "write_coordinators": [
            {"db_path": "main.db", "writes_total": 1000, "writes_failed": 0, "queue_depth": 200}
        ],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "warning"
    assert any("writer_queue_depth" in r and "200" in r for r in reasons)


def test_watchdog_ping_stale_is_critical():
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 120.0, "total_pings": 500},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "critical"
    assert any("watchdog_last_ping_ago" in r for r in reasons)
    assert any("120" in r for r in reasons)


def test_watchdog_ping_stale_ignored_on_boot():
    # During the first few checks after boot, staleness is expected —
    # external pinger may not have started yet.
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 120.0, "total_pings": 2},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, _, _ = _evaluate_health_signals(report)
    assert healthy is True


def test_task_queue_depth_over_50_demotes():
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 75, "oldest_pending_seconds": 30},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "warning"
    assert any("task_queue_depth: 75" in r for r in reasons)


def test_task_queue_oldest_pending_over_10min_demotes():
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 5, "oldest_pending_seconds": 900},  # 15 min
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "warning"
    assert any("task_queue_oldest_pending" in r and "15." in r for r in reasons)


def test_stalled_phases_demotes():
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": ["hypothesis_gen", "backtest"],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "warning"
    assert any("stalled_phases" in r for r in reasons)


def test_pipeline_integrity_failure_is_critical():
    report = {
        "subsystems": {},
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": False, "issues": [{"name": "foo"}]},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "critical"
    assert any("pipeline_broken" in r for r in reasons)


def test_subsystem_breaker_open_is_critical():
    report = {
        "subsystems": {
            "ollama": {"is_open": True, "last_error": "connection refused"},
            "sqlite": {"is_open": False},
        },
        "write_coordinators": [],
        "watchdog_monitoring": {"last_ping_ago_seconds": 1.0, "total_pings": 50},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "critical"
    assert any("breaker_open[ollama]" in r for r in reasons)


def test_multiple_signals_promote_severity_to_max():
    # warning + critical -> critical
    report = {
        "subsystems": {},
        "write_coordinators": [
            {"db_path": "x", "writes_total": 100, "writes_failed": 25, "queue_depth": 0}
        ],
        "watchdog_monitoring": {"last_ping_ago_seconds": 120.0, "total_pings": 100},
        "task_queue": {"depth": 0},
        "stalled_phases": [],
        "pipeline_integrity": {"healthy": True},
    }
    healthy, severity, reasons = _evaluate_health_signals(report)
    assert healthy is False
    assert severity == "critical"
    assert len(reasons) >= 2


# --- Fast-path breaker + memory warning regression ---


def test_fast_path_breaker_trips_quickly():
    from tools.health import CircuitBreaker
    b = CircuitBreaker("sqlite", fast=True, fast_fail_threshold=3, fast_min_interval_s=20)
    assert b.record_failure("boom") is False
    assert b.record_failure("boom") is False
    tripped = b.record_failure("boom")
    assert tripped is True
    assert b.is_open is True


def test_slow_path_breaker_still_works():
    from tools.health import CircuitBreaker
    b = CircuitBreaker("network", fast=False, fail_threshold=5)
    for _ in range(4):
        assert b.record_failure("boom") is False
    assert b.record_failure("boom") is True
    assert b.is_open is True


def test_record_intermediate_does_not_reset_counter():
    """Memory 'warning' should not suppress a genuine failure trajectory."""
    from tools.health import CircuitBreaker
    b = CircuitBreaker("memory", fail_threshold=3)
    b.record_failure("growing")
    b.record_failure("growing")
    assert b.consecutive_failures == 2
    b.record_intermediate()
    # Counter preserved — next real failure trips as expected.
    assert b.consecutive_failures == 2
    assert b.record_failure("oom") is True
    assert b.is_open is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
