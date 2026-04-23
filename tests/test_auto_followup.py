"""Tests for feat/auto-followup-hardening.

Covers the guards in ``tools.followup_guard``:
  - depth cap
  - fan-out cap
  - quality gate (vague / verbatim / entity-free)
  - semantic dedup (string + embedding paths)
  - chain budget
  - chain tree traversal

Each test spins up a throwaway SQLite DB, creates the ``task_queue``
table, runs the column migration, and exercises the guard functions
directly. ``api._maybe_auto_followup`` is not imported here — we want
the guards testable without FastAPI/lifespan dependencies. The
integration path is covered by the final test which simulates the
exact call pattern ``api.py`` uses.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import aiosqlite
import pytest


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_db(monkeypatch):
    """Throwaway DB with the minimal task_queue schema + followup columns."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    path = tmp.name

    async def _init():
        async with aiosqlite.connect(path) as db:
            await db.execute(
                """
                CREATE TABLE task_queue (
                    task_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    priority INTEGER NOT NULL DEFAULT 0,
                    result TEXT,
                    error TEXT,
                    session_id TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')),
                    started_at TEXT,
                    completed_at TEXT
                )
                """
            )
            await db.commit()
            # Apply the followup column migration on top of the bare table.
            from tools.followup_guard import ensure_followup_columns
            await ensure_followup_columns(db)

    asyncio.run(_init())
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


async def _seed_task(
    db_path: str,
    query: str,
    *,
    depth: int = 0,
    parent: int | None = None,
    root: int | None = None,
    cost: float = 0.0,
    status: str = "COMPLETED",
    created_at: str | None = None,
) -> int:
    async with aiosqlite.connect(db_path) as db:
        # When created_at is explicit (for dedup-window tests) we pass
        # it through; else SQLite stamps 'now'.
        if created_at is None:
            cur = await db.execute(
                "INSERT INTO task_queue "
                "(query, status, followup_depth, parent_task_id, root_task_id, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (query, status, depth, parent, root, cost),
            )
        else:
            cur = await db.execute(
                "INSERT INTO task_queue "
                "(query, status, created_at, followup_depth, parent_task_id, "
                "root_task_id, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (query, status, created_at, depth, parent, root, cost),
            )
        await db.commit()
        task_id = int(cur.lastrowid)
        # Back-fill root=self for depth=0 seeds.
        if root is None and depth == 0:
            await db.execute(
                "UPDATE task_queue SET root_task_id = ? WHERE task_id = ?",
                (task_id, task_id),
            )
            await db.commit()
        return task_id


# ── Schema migration ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_followup_columns_is_idempotent(tmp_db):
    """Running ensure_followup_columns twice is a no-op."""
    from tools.followup_guard import ensure_followup_columns
    async with aiosqlite.connect(tmp_db) as db:
        # Fixture already ran it once; running again must not raise.
        await ensure_followup_columns(db)
        cur = await db.execute("PRAGMA table_info(task_queue)")
        cols = {r[1] for r in await cur.fetchall()}
    assert {"followup_depth", "parent_task_id", "root_task_id", "cost_usd"} <= cols


# ── Depth cap ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_depth_cap_rejects_6th_level(tmp_db, monkeypatch):
    """A parent at depth=5 cannot spawn a depth=6 followup."""
    monkeypatch.setenv("CALLISTO_MAX_FOLLOWUP_DEPTH", "5")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    root = await _seed_task(tmp_db, "root query about Jayson Tatum stats")
    parent = await _seed_task(
        tmp_db, "depth-5 task Jayson Tatum stats",
        depth=5, parent=root, root=root,
    )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 99: inspect the Celtics bench rotations specifically",
        )
    assert not decision.allowed
    assert decision.reason == "followup_depth_exceeded"


@pytest.mark.asyncio
async def test_depth_cap_allows_within_limit(tmp_db, monkeypatch):
    """A parent at depth=4 CAN spawn a depth=5 followup (still at cap)."""
    monkeypatch.setenv("CALLISTO_MAX_FOLLOWUP_DEPTH", "5")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    root = await _seed_task(tmp_db, "root query")
    parent = await _seed_task(
        tmp_db, "depth-4 node", depth=4, parent=root, root=root,
    )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 2: look at Atlanta Braves hitting vs LHP in Q4",
        )
    assert decision.allowed
    assert decision.depth == 5


# ── Fan-out cap ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_fanout_cap_rejects_fourth_child(tmp_db, monkeypatch):
    """3 direct children already exist → 4th must be rejected."""
    monkeypatch.setenv("CALLISTO_MAX_FOLLOWUP_FANOUT", "3")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    parent = await _seed_task(tmp_db, "root query about NBA rebounds")
    for i in range(3):
        await _seed_task(
            tmp_db, f"child {i} Jayson Tatum rebounds angle",
            depth=1, parent=parent, root=parent,
        )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 1: examine Jalen Brown defensive matchup vs Pacers",
        )
    assert not decision.allowed
    assert decision.reason == "followup_fanout_exceeded"


# ── Quality gate ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quality_gate_rejects_vague(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    parent = await _seed_task(tmp_db, "original query about MLB pitchers")
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 1: investigate further and look into this",
        )
    assert not decision.allowed
    assert decision.reason.startswith("quality_gate:")


@pytest.mark.asyncio
async def test_quality_gate_rejects_verbatim_parent(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    parent_q = "Analyze Jayson Tatum performance against Pacers tonight"
    parent = await _seed_task(tmp_db, parent_q)
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent, f"AUTO-FOLLOWUP from task {parent}: {parent_q}",
        )
    assert not decision.allowed
    assert "quality_gate" in decision.reason


@pytest.mark.asyncio
async def test_quality_gate_accepts_concrete_entity(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    parent = await _seed_task(tmp_db, "general NBA edge research")
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 1: examine hypothesis_id=mlb_early_home_fav "
            "edge vs post-clock regime specifically",
        )
    assert decision.allowed, f"Rejected with {decision.reason}"


# ── Dedup ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dedup_exact_string_match(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "1")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    parent = await _seed_task(tmp_db, "parent query")
    # Seed an existing recent task identical to what the followup will try.
    existing = await _seed_task(
        tmp_db,
        "investigate event_id=abc123 Celtics game total line movement",
        status="PROCESSING",
    )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 99: investigate event_id=abc123 Celtics "
            "game total line movement",
        )
    assert not decision.allowed
    assert decision.reason == "dedup_merge"
    assert decision.merge_target_id == existing


@pytest.mark.asyncio
async def test_dedup_respects_time_window(tmp_db, monkeypatch):
    """A near-dup outside the window is NOT rejected."""
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "1")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP_WINDOW_S", "3600")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    parent = await _seed_task(tmp_db, "parent")
    # Seed an identical query but dated 2 hours ago.
    await _seed_task(
        tmp_db,
        "probe event_id=xyz789 Knicks rebounding split",
        created_at="2020-01-01 00:00:00",  # ancient
    )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, parent,
            "AUTO-FOLLOWUP from task 1: probe event_id=xyz789 Knicks rebounding split",
        )
    assert decision.allowed, f"Expected allowed, got {decision.reason}"


# ── Chain budget ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_budget_rejects_when_spent(tmp_db, monkeypatch):
    monkeypatch.setenv("CALLISTO_MAX_CHAIN_BUDGET_USD", "1.00")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")

    root = await _seed_task(tmp_db, "root query", cost=0.60)
    # Two existing children at $0.30 each — total chain now $1.20 > $1.00
    await _seed_task(
        tmp_db, "child 1", depth=1, parent=root, root=root, cost=0.30,
    )
    await _seed_task(
        tmp_db, "child 2", depth=1, parent=root, root=root, cost=0.30,
    )
    from tools.followup_guard import evaluate_followup
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, root,
            "AUTO-FOLLOWUP from task 1: look at Atlanta Braves pitching splits vs LHP",
        )
    assert not decision.allowed
    assert decision.reason == "chain_budget_exceeded"


# ── Chain tree ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_chain_tree_walks_ancestry(tmp_db):
    """Given a chain root→c1→gc1, /chain should return all 3 with correct depth."""
    root = await _seed_task(tmp_db, "root", cost=0.10)
    c1 = await _seed_task(
        tmp_db, "child", depth=1, parent=root, root=root, cost=0.10,
    )
    gc1 = await _seed_task(
        tmp_db, "grandchild", depth=2, parent=c1, root=root, cost=0.10,
    )
    from tools.followup_guard import get_chain_tree
    async with aiosqlite.connect(tmp_db) as db:
        # Query by the deepest task — should still return the full tree.
        tree = await get_chain_tree(db, gc1)
    assert tree["root_task_id"] == root
    assert tree["task_count"] == 3
    assert tree["max_depth"] == 2
    assert abs(tree["total_cost_usd"] - 0.30) < 1e-6
    ids = [t["task_id"] for t in tree["tasks"]]
    assert ids == sorted([root, c1, gc1], key=lambda x: x)


# ── Integration: simulate the api.py call pattern ────────────────────────

@pytest.mark.asyncio
async def test_integration_depth_5_then_blocked(tmp_db, monkeypatch):
    """Walk the chain root → d1 → d2 → d3 → d4 → d5, then 6th should block.

    This simulates the exact flow ``api._maybe_auto_followup`` runs:
    evaluate, insert with followup columns stamped, then evaluate again
    with the next parent.
    """
    monkeypatch.setenv("CALLISTO_MAX_FOLLOWUP_DEPTH", "5")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_DEDUP", "0")
    monkeypatch.setenv("CALLISTO_FOLLOWUP_QUALITY_GATE", "0")
    monkeypatch.setenv("CALLISTO_MAX_CHAIN_BUDGET_USD", "100")  # huge

    from tools.followup_guard import evaluate_followup, insert_followup

    current_parent = await _seed_task(tmp_db, "root: investigate Celtics vs Pacers")
    chain = [current_parent]

    for step in range(1, 6):  # depth 1..5
        q = (
            f"AUTO-FOLLOWUP from task {current_parent}: step {step} examine "
            f"Jayson Tatum usage in Q{step} vs specific defensive sets"
        )
        async with aiosqlite.connect(tmp_db) as db:
            decision = await evaluate_followup(db, current_parent, q)
            assert decision.allowed, f"step {step} rejected: {decision.reason}"
            new_id = await insert_followup(
                db, q, priority=1,
                parent_task_id=decision.parent_task_id,
                root_task_id=decision.root_task_id,
                depth=decision.depth,
            )
        chain.append(new_id)
        current_parent = new_id

    assert len(chain) == 6  # root + 5 followups
    # Next one must blow the cap.
    async with aiosqlite.connect(tmp_db) as db:
        decision = await evaluate_followup(
            db, current_parent,
            "AUTO-FOLLOWUP from task X: step 6 beyond cap Jayson Tatum deep dive",
        )
    assert not decision.allowed
    assert decision.reason == "followup_depth_exceeded"
