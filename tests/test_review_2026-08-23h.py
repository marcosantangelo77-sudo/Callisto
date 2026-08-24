"""Review run 10 reproductions (reviewer: ox-alpha).

Each test documents a defect found while reviewing build/cli-front-door and
its interaction with master. All three FAIL against the current bytes —
that failure IS the deliverable. Do not "fix" them by loosening assertions;
fix the production code they point at, or merge the branch that already did.

RV10-A (HIGH): HypothesisManager.create_hypothesis dies on every post-013
(seam-shaped) database. api.py applies pending migrations at EVERY startup,
and migration 013 rebuilds ``hypotheses`` without sport/market_type, so one
restart turns hypothesis creation into ``OperationalError: no such column:
sport``. The writer-side fix exists — e4edcca "HypothesisManager works on
both sides of the schema seam" — but it is stranded on
origin/review/rotating-0823-155500 and merged nowhere. Meanwhile
build/cli-front-door's own test_improve_schema_seam.py pins this exact
contract AND FAILS 10/24 on its own branch bytes (verified by git archive +
pytest). Family #2 across branches; family #7 (tests committed without ever
being run against their own subject).

RV10-B (MEDIUM): ThompsonRoutingPolicy (the empirical router, incl. the K2
coverage gate) has ZERO production callers — nothing outside its own module
and tests constructs it. Measurements accumulate in model_scores.jsonl and
influence no decision. Inert component presented as routing behaviour.

RV10-C (HIGH): the cli-front-door line branched BEFORE ba0a63c (the
floor_conf downward-only quantisation fix) landed on master, and its
pipeline/engine.py still uses plain ``round(x, 2)`` on confidence scores —
round() can RAISE a score (0.269 -> 0.27), the exact family-#6 defect the
red team found six instances of. Merging cli-front-door into master as-is
would re-import the laundering bug through the synthesis/agreement path.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── RV10-A ────────────────────────────────────────────────────────────────

LEGACY_WELDED_DDL = """
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


@pytest.mark.asyncio
async def test_create_hypothesis_survives_migration_013(tmp_path):
    """The exact restart sequence: legacy DB -> apply_pending_migrations
    (as api.py does at every startup) -> create_hypothesis must WORK."""
    import sqlite3

    import aiosqlite

    from tools.migrations import apply_pending_migrations

    path = str(tmp_path / "restart.db")
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_WELDED_DDL)
    conn.commit()
    conn.close()

    apply_pending_migrations(path)

    from tools.hypothesis import HypothesisManager

    hm = HypothesisManager(path)
    await hm.initialize()
    try:
        hid = await hm.create_hypothesis(
            "post-restart-create", "thesis", "basketball_nba",
            "moneyline", {})
        assert hid, "create_hypothesis returned nothing"
        h = await hm.get_hypothesis(hid)
        assert h is not None
        assert h.get("sport") == "basketball_nba"
    finally:
        await hm.close()


@pytest.mark.asyncio
async def test_dup_guard_query_runs_on_seam_shape(tmp_path):
    """Same restart sequence; the duplicate game_filters guard issues its
    own raw SQL naming hypotheses.sport — it must not raise."""
    import sqlite3

    from tools.migrations import apply_pending_migrations

    path = str(tmp_path / "dupguard.db")
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_WELDED_DDL)
    conn.commit()
    conn.close()
    apply_pending_migrations(path)

    from tools.hypothesis import HypothesisManager

    hm = HypothesisManager(path)
    await hm.initialize()
    try:
        a = await hm.create_hypothesis(
            "dup-a", "t", "nba", "ml", {"game_filters": {"min_odds": -150}})
        b = await hm.create_hypothesis(
            "dup-b-different-name", "t", "nba", "ml",
            {"game_filters": {"min_odds": -150}})
        assert a == b, "dup guard did not fire post-seam"
    finally:
        await hm.close()


# ── RV10-B ────────────────────────────────────────────────────────────────

def test_thompson_routing_policy_has_a_production_caller():
    """The empirical router must be reachable from the serving path.
    Grep-level wiring check: some non-test, non-self module must import
    ThompsonRoutingPolicy. Today none does."""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = subprocess.run(
        ["git", "grep", "-l", "ThompsonRoutingPolicy", "HEAD", "--",
         "tools/", "api.py", "orchestrator.py", "scripts/"],
        capture_output=True, text=True, cwd=root)
    hits = [l.split(":", 1)[1] for l in out.stdout.splitlines() if l.strip()]
    prod = [h for h in hits
            if not h.startswith("tests/")
            and h != "tools/routing/policy.py"]
    assert prod, (
        "ThompsonRoutingPolicy has no production caller — empirical routing "
        "is inert; scores are recorded and never consulted")


# ── RV10-C ────────────────────────────────────────────────────────────────

def test_pipeline_engine_does_not_round_confidence_upward():
    """Confidence quantisation must be downward-only (agp.thresholds.
    floor_conf). tools/pipeline/engine.py rounds with round(), which can
    raise a score — family #6, the defect ba0a63c fixed everywhere else."""

    src_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools", "pipeline", "engine.py")
    with open(src_path, encoding="utf-8") as f:
        lines = f.read().splitlines()

    offenders = [(i + 1, l.strip()) for i, l in enumerate(lines)
                 if "confidence" in l.lower() and "round(" in l]
    assert not offenders, (
        f"pipeline/engine.py quantises confidence with round(), which can "
        f"RAISE it (0.269 -> 0.27); use agp.thresholds.floor_conf. Sites: "
        f"{offenders}")
