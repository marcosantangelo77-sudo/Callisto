"""Tests for adaptive orchestrator timeouts + task_classifier heuristics."""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from tools.task_classifier import (
    TaskType,
    classify_and_budget,
    classify_query,
    get_budget_s,
    get_hard_ceiling_s,
)


# ───────────────────────── classifier unit tests ──────────────────────────


CLASSIFIER_CASES = [
    # QUICK — simple price/score lookups
    ("What's the current odds on Lakers tonight?", TaskType.QUICK),
    ("Current score for Yankees vs Red Sox", TaskType.QUICK),
    ("Who's winning the Celtics game?", TaskType.QUICK),
    ("Price check on Mahomes MVP futures", TaskType.QUICK),
    # NEWS — injuries/lineups/weather
    ("Are there any injuries for the Cowboys this week?", TaskType.NEWS),
    ("What's the weather forecast for the Packers game Sunday?", TaskType.NEWS),
    ("Who is the starting pitcher for the Orioles tomorrow?", TaskType.NEWS),
    ("Is Jokic in the starting lineup tonight?", TaskType.NEWS),
    # HYPGEN — edge/strategy generation
    ("Generate a new hypothesis on MLB run totals", TaskType.HYPGEN),
    ("Find new edges in NBA first-quarter unders", TaskType.HYPGEN),
    ("Brainstorm new signals for WNBA identity factors", TaskType.HYPGEN),
    # DEEP — investigation/analysis
    ("Analyze the home underdog edge in NFL divisional games", TaskType.DEEP),
    ("Deep dive on referee-whistled pace in NBA playoffs", TaskType.DEEP),
    ("Investigate contradictions in the blowout-under thesis", TaskType.DEEP),
    ("Evaluate whether live O/U overcorrection holds in 2025 data", TaskType.DEEP),
    ("Verify the Celtics 3rd-quarter surge pattern", TaskType.DEEP),
    ("Audit the CLV calculation for paper trades since April", TaskType.DEEP),
    # DEEP should beat NEWS when both appear (order matters)
    ("Analyze the injury report for the Ravens defense", TaskType.DEEP),
    # DEFAULT — things that don't match any bucket
    ("Tell me about the Kelly criterion", TaskType.DEFAULT),
    ("Hello, what can you do?", TaskType.DEFAULT),
    ("Summarize last night's MLB slate", TaskType.DEFAULT),
]


@pytest.mark.parametrize("query,expected", CLASSIFIER_CASES)
def test_classifier_buckets(query, expected):
    got = classify_query(query)
    assert got == expected, f"{query!r} classified as {got}, expected {expected}"


def test_explicit_task_type_overrides_heuristic():
    # Query says "deep", explicit says quick → explicit wins.
    tt, budget = classify_and_budget(
        "Deep analysis of NBA refs", explicit_task_type="quick"
    )
    assert tt == TaskType.QUICK
    assert budget == 60.0


def test_invalid_explicit_task_type_falls_back():
    tt, budget = classify_and_budget(
        "Analyze NBA refs", explicit_task_type="not-a-type"
    )
    assert tt == TaskType.DEEP


# ───────────────────────── budget resolution tests ────────────────────────


def test_default_budgets(monkeypatch):
    # Clear all env overrides so defaults are seen.
    for key in [
        "CALLISTO_TIMEOUT_QUICK_S", "CALLISTO_TIMEOUT_NEWS_S",
        "CALLISTO_TIMEOUT_HYPGEN_S", "CALLISTO_TIMEOUT_DEEP_S",
        "CALLISTO_TIMEOUT_DEFAULT_S", "CALLISTO_TIMEOUT_HARD_CEILING_S",
        "CALLISTO_TASK_TIMEOUT_S",
    ]:
        monkeypatch.delenv(key, raising=False)
    assert get_budget_s(TaskType.QUICK) == 60.0
    assert get_budget_s(TaskType.NEWS) == 180.0
    assert get_budget_s(TaskType.HYPGEN) == 600.0
    assert get_budget_s(TaskType.DEEP) == 900.0
    assert get_budget_s(TaskType.DEFAULT) == 300.0
    assert get_hard_ceiling_s() == 1800.0


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CALLISTO_TIMEOUT_QUICK_S", "45")
    monkeypatch.setenv("CALLISTO_TIMEOUT_DEEP_S", "1234")
    monkeypatch.setenv("CALLISTO_TIMEOUT_HARD_CEILING_S", "7200")
    assert get_budget_s(TaskType.QUICK) == 45.0
    assert get_budget_s(TaskType.DEEP) == 1234.0
    assert get_hard_ceiling_s() == 7200.0


def test_legacy_env_var_honored_as_default(monkeypatch):
    """CALLISTO_TASK_TIMEOUT_S (legacy) still sets DEFAULT bucket."""
    for key in ["CALLISTO_TIMEOUT_DEFAULT_S"]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CALLISTO_TASK_TIMEOUT_S", "420")
    assert get_budget_s(TaskType.DEFAULT) == 420.0


def test_classify_and_budget_quick():
    tt, budget = classify_and_budget("What's the current score for Yankees?")
    assert tt == TaskType.QUICK
    assert budget == 60.0


def test_classify_and_budget_deep():
    tt, budget = classify_and_budget("Investigate the home-favorite edge in MLB")
    assert tt == TaskType.DEEP
    assert budget == 900.0


# ─────────────────────── adaptive timeout logic tests ─────────────────────
#
# These test the _run_session_with_adaptive_timeout loop directly. We mock
# the orchestrator by patching api.orchestrator_instance with a stub that
# exposes the same active_session_for() / run_session() contract.

class _StubSession:
    """Minimal AGPSession-shape for the watchdog to inspect."""

    def __init__(self):
        self.current_step = type("S", (), {"name": "PRIMARY_COLLECTION"})()
        self.evidence: list = []
        self.contradictions: list = []
        self.filtered_evidence_count = 0
        self.progress_events = 0
        self.last_progress_at = time.monotonic()
        self.session_id = "stub"

    def bump(self):
        self.progress_events += 1
        self.last_progress_at = time.monotonic()
        self.evidence.append(object())


class _StubOrchestrator:
    def __init__(self):
        self._active: dict = {}
        self.session = _StubSession()

    def active_session_for(self, task):
        return self._active.get(task)

    async def run_session(self, query: str, skip_search: bool = False):
        """Fake orchestrator: calls a hook every second, finishes when told.

        The hook is installed by the test.
        """
        cur = asyncio.current_task()
        self._active[cur] = self.session
        try:
            # Test controls lifecycle via self.session.done_event.
            while not getattr(self.session, "done_event", asyncio.Event()).is_set():
                # Simulate: bump progress every _tick_s (set by test).
                tick = getattr(self.session, "_tick_s", 20.0)
                await asyncio.sleep(tick)
                self.session.bump()
            return {"session_id": self.session.session_id, "summary": {"conclusion": "done"}}
        finally:
            self._active.pop(cur, None)


@pytest.mark.asyncio
async def test_adaptive_extends_past_initial_budget_on_live_progress(monkeypatch):
    """Session makes progress every tick for longer than initial_budget → extends."""
    import api

    stub = _StubOrchestrator()
    stub.session.done_event = asyncio.Event()
    stub.session._tick_s = 0.2  # 5 progress events/sec

    monkeypatch.setattr(api, "orchestrator_instance", stub)
    # Small numbers so the test is fast. initial 1s, ceiling 4s.
    # progress window 0.5s (recent); extension 1s.
    monkeypatch.setattr(api, "_ADAPTIVE_PROGRESS_WINDOW_S", 0.5)
    monkeypatch.setattr(api, "_ADAPTIVE_STALL_WINDOW_S", 2.0)
    monkeypatch.setattr(api, "_ADAPTIVE_EXTENSION_S", 1.0)
    monkeypatch.setattr(api, "_ADAPTIVE_POLL_S", 0.1)

    async def finisher():
        # Let the session run ~2.5s (past initial_budget=1s) then finish.
        await asyncio.sleep(2.5)
        stub.session.done_event.set()

    asyncio.create_task(finisher())

    t0 = time.monotonic()
    result, telemetry = await api._run_session_with_adaptive_timeout(
        "q", skip_search=True, initial_budget_s=1.0, hard_ceiling_s=4.0,
    )
    elapsed = time.monotonic() - t0
    assert result["session_id"] == "stub"
    # Should have outlived the initial 1s budget by extending.
    assert elapsed > 1.2, f"elapsed={elapsed:.2f}s; extension failed"
    assert telemetry["extensions"] >= 1
    assert not telemetry["stalled"]


@pytest.mark.asyncio
async def test_adaptive_terminates_on_stall(monkeypatch):
    """Session makes NO progress → terminates at stall window."""
    import api

    stub = _StubOrchestrator()
    stub.session.done_event = asyncio.Event()
    stub.session._tick_s = 999.0  # effectively never bumps

    monkeypatch.setattr(api, "orchestrator_instance", stub)
    monkeypatch.setattr(api, "_ADAPTIVE_PROGRESS_WINDOW_S", 0.3)
    monkeypatch.setattr(api, "_ADAPTIVE_STALL_WINDOW_S", 0.8)
    monkeypatch.setattr(api, "_ADAPTIVE_EXTENSION_S", 1.0)
    monkeypatch.setattr(api, "_ADAPTIVE_POLL_S", 0.1)

    t0 = time.monotonic()
    with pytest.raises(api._AdaptiveTimeout) as exc_info:
        await api._run_session_with_adaptive_timeout(
            "q", skip_search=True, initial_budget_s=5.0, hard_ceiling_s=30.0,
        )
    elapsed = time.monotonic() - t0
    # Should have terminated well before the 5s initial budget (stall triggers faster).
    assert elapsed < 2.0, f"stall didn't fire fast enough, elapsed={elapsed:.2f}s"
    assert exc_info.value.telemetry["stalled"] is True


@pytest.mark.asyncio
async def test_adaptive_hard_ceiling_respected(monkeypatch):
    """Session keeps making progress forever → hard ceiling still cuts it off."""
    import api

    stub = _StubOrchestrator()
    stub.session.done_event = asyncio.Event()  # never set
    stub.session._tick_s = 0.1  # bumps constantly

    monkeypatch.setattr(api, "orchestrator_instance", stub)
    monkeypatch.setattr(api, "_ADAPTIVE_PROGRESS_WINDOW_S", 0.5)
    monkeypatch.setattr(api, "_ADAPTIVE_STALL_WINDOW_S", 2.0)
    monkeypatch.setattr(api, "_ADAPTIVE_EXTENSION_S", 0.5)
    monkeypatch.setattr(api, "_ADAPTIVE_POLL_S", 0.1)

    t0 = time.monotonic()
    with pytest.raises(api._AdaptiveTimeout) as exc_info:
        await api._run_session_with_adaptive_timeout(
            "q", skip_search=True, initial_budget_s=0.5, hard_ceiling_s=2.0,
        )
    elapsed = time.monotonic() - t0
    # Should be bounded by hard_ceiling (2s) + a tiny slack.
    assert 1.5 < elapsed < 3.5, f"elapsed={elapsed:.2f}s, hard ceiling violated"
    # At least one extension fired before the ceiling cut it off.
    assert exc_info.value.telemetry["extensions"] >= 1
    assert exc_info.value.telemetry["timeout_reason"] == "hard ceiling"


@pytest.mark.asyncio
async def test_adaptive_completes_within_initial_budget(monkeypatch):
    """Sanity: short, fast session finishes before the deadline."""
    import api

    stub = _StubOrchestrator()
    stub.session.done_event = asyncio.Event()
    stub.session._tick_s = 0.1

    monkeypatch.setattr(api, "orchestrator_instance", stub)
    monkeypatch.setattr(api, "_ADAPTIVE_POLL_S", 0.05)

    async def finisher():
        await asyncio.sleep(0.3)
        stub.session.done_event.set()

    asyncio.create_task(finisher())

    result, telemetry = await api._run_session_with_adaptive_timeout(
        "q", skip_search=True, initial_budget_s=5.0, hard_ceiling_s=30.0,
    )
    assert result["session_id"] == "stub"
    assert telemetry["extensions"] == 0
    assert not telemetry["stalled"]


# ───────────────────── task_worker per-bucket budget tests ────────────────
#
# We don't exercise the whole task_worker loop (would need a live DB); we
# just confirm the classifier + budget table produces the right numbers for
# the buckets the task description calls out.

def test_quick_bucket_is_60s():
    tt, budget = classify_and_budget("What's the current score for Yankees?")
    assert tt == TaskType.QUICK
    assert budget == 60.0


def test_news_bucket_is_180s():
    tt, budget = classify_and_budget(
        "Injuries for Baltimore Orioles April 22"
    )
    assert tt == TaskType.NEWS
    assert budget == 180.0


def test_hypgen_bucket_is_600s():
    tt, budget = classify_and_budget(
        "Generate a new hypothesis about MLB home dogs"
    )
    assert tt == TaskType.HYPGEN
    assert budget == 600.0


def test_deep_bucket_is_900s():
    tt, budget = classify_and_budget(
        "Investigate contradictions in the referee pace thesis"
    )
    assert tt == TaskType.DEEP
    assert budget == 900.0


def test_default_bucket_is_300s(monkeypatch):
    for key in ["CALLISTO_TIMEOUT_DEFAULT_S", "CALLISTO_TASK_TIMEOUT_S"]:
        monkeypatch.delenv(key, raising=False)
    tt, budget = classify_and_budget("Tell me about Kelly criterion")
    assert tt == TaskType.DEFAULT
    assert budget == 300.0


# ──────────────────────── migration + DB round-trip ───────────────────────

@pytest.mark.asyncio
async def test_timeout_status_migration_and_roundtrip(tmp_path):
    """Migration 005 allows TIMEOUT; timeout_task() writes and reads back."""
    import sqlite3
    from task_queue import TaskQueue, TASK_SCHEMA_SQL
    from tools.migrations import apply_pending_migrations

    db_path = str(tmp_path / "test_timeout.db")
    # Simulate a pre-migration DB by creating the OLD schema manually.
    old_schema = TASK_SCHEMA_SQL.replace(
        "'FAILED', 'TIMEOUT'", "'FAILED'"
    )
    conn = sqlite3.connect(db_path)
    for stmt in old_schema.split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    # Seed a row under the old constraint.
    conn.execute(
        "INSERT INTO task_queue (query, status) VALUES (?, ?)",
        ("legacy-query", "PENDING"),
    )
    conn.commit()
    conn.close()

    # Apply migrations — should upgrade the CHECK to allow TIMEOUT.
    apply_pending_migrations(db_path)

    # Verify schema now allows TIMEOUT.
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='task_queue'"
    ).fetchone()
    assert "TIMEOUT" in row[0]
    # Legacy row survived.
    row = conn.execute(
        "SELECT query FROM task_queue WHERE task_id=1"
    ).fetchone()
    assert row[0] == "legacy-query"
    conn.close()

    # End-to-end: TaskQueue.timeout_task writes TIMEOUT status.
    q = TaskQueue(db_path=db_path)
    await q.initialize()
    try:
        tid = await q.submit_task("adaptive-timeout-test", priority=1)
        # Claim it first so status is PROCESSING.
        claimed = await q.get_next()
        assert claimed["task_id"] == tid
        await q.timeout_task(
            tid, "timeout: type=deep reason=budget elapsed=900s",
            result={"task_type": "deep", "telemetry": {"evidence_count": 3}},
        )
        task = await q.get_task(tid)
        assert task["status"] == "TIMEOUT", f"got {task['status']}"
        assert "timeout: type=deep" in task["error"]
    finally:
        await q.close()
