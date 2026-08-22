"""W3 — checkpointing and resumability.

Covers:
  1. step-level checkpoints (content-addressed; unchanged steps not redone)
  2. resume semantics that do not lie (original produced_at carried forward)
  3. idempotence (kill + resume produces exactly a clean run's ledger/store)
  4. sealing across the resume boundary (provenance intact or refuse)
  5. GC that never deletes an open claim's checkpoint
Property-based tests where the invariant matters (idempotence under any
crash point, GC safety for any claim-openness pattern).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

UTC = timezone.utc

from tools.pipeline.checkpoint import (  # noqa: E402
    Checkpoint,
    Crash,
    FileCheckpointer,
    RunTrace,
    hash_inputs,
    provenance_is_intact,
    replay_ledger,
    run_pipeline_checked,
    run_stage,
    run_key,
    seal_guard,
    step_key,
)
from agp.provenance import ProvenanceLedger  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────

def _cp(tmp_path):
    return FileCheckpointer(root=tmp_path / "ck")


def _trace(rk="r"):
    return RunTrace(run=rk)


async def _ok(payload=None):
    return payload or {"done": True}


# ── 1. step-level checkpoints, content-addressed ──────────────────────────

def test_step_key_is_content_addressed():
    rk = run_key("question", "GENERAL", "2026-08-22")
    assert step_key(rk, "decompose", hash_inputs({"q": "x"})) == \
        step_key(rk, "decompose", hash_inputs({"q": "x"}))
    # different stage / different input / different run -> different key
    assert step_key(rk, "fetch", "h") != step_key(rk, "decompose", "h")
    assert step_key(rk, "decompose", "h") != \
        step_key(run_key("other"), "decompose", "h")
    assert step_key(rk, "decompose", hash_inputs({"q": "x"})) != \
        step_key(rk, "decompose", hash_inputs({"q": "y"}))


def test_unchanged_step_is_not_redone(tmp_path):
    cp = _cp(tmp_path)
    calls = []

    async def work():
        calls.append(1)
        return {"answer": "a"}

    async def go():
        tr = _trace()
        await run_stage(cp, tr, "decompose", {"q": "x"}, work)
        tr2 = _trace()
        oc = await run_stage(cp, tr2, "decompose", {"q": "x"}, work)
        return oc

    oc = asyncio.get_event_loop().run_until_complete(go())
    assert len(calls) == 1          # execute ran exactly once
    assert oc.resumed and oc.payload == {"answer": "a"}


def test_changed_input_re_executes(tmp_path):
    cp = _cp(tmp_path)
    calls = []

    async def work():
        calls.append(1)
        return {}

    async def go():
        tr = _trace()
        await run_stage(cp, tr, "fetch", {"url": "u1"}, work)
        await run_stage(cp, tr, "fetch", {"url": "u2"}, work)

    asyncio.get_event_loop().run_until_complete(go())
    assert len(calls) == 2


def test_crash_mid_run_resumes_from_last_good_step(tmp_path):
    """run, die at stage 'adversary', resume — decompose/leaves are hits."""
    cp = _cp(tmp_path)
    executed = []

    def stages(die_at=None):
        async def decompose():
            executed.append("decompose")
            return {"sub_questions": ["a", "b"]}

        async def leaf():
            executed.append("leaf")
            return {"answer": "x"}

        async def adversary():
            executed.append("adversary")
            if die_at == "adversary":
                raise Crash("died at adversary")
            return {"objections": []}

        return [
            ("decompose", lambda: {"q": "root"}, decompose),
            ("leaf", lambda: {"qid": "a"}, leaf),
            ("adversary", lambda: {"claim": "c"}, adversary),
        ]

    with pytest.raises(Crash):
        asyncio.get_event_loop().run_until_complete(
            run_pipeline_checked(cp, run_key("Q"), stages(die_at="adversary")))
    executed.clear()

    trace, merged = asyncio.get_event_loop().run_until_complete(
        run_pipeline_checked(cp, run_key("Q"), stages()))
    assert merged["decompose"]["sub_questions"] == ["a", "b"]
    assert trace.resumed_stages == ["decompose", "leaf"]
    assert trace.fresh_stages == ["adversary"]
    assert executed == ["adversary"]      # only the failed stage redone


# ── 2. resume semantics that do not lie ───────────────────────────────────

def test_cache_hit_carries_original_produced_at(tmp_path):
    cp = _cp(tmp_path)
    old = datetime.now(UTC) - timedelta(hours=1)

    async def go():
        saved = cp.save(run_key("Q"), "fetch", "h",
                        {"body": "b"}, produced_at=old)
        tr = _trace()
        oc = await run_stage(cp, tr, "fetch", "h", _ok)
        return saved, oc

    saved, oc = asyncio.get_event_loop().run_until_complete(go())
    assert oc.resumed
    assert oc.produced_at == saved.produced_at
    assert datetime.fromisoformat(oc.produced_at) < datetime.now(UTC) - \
        timedelta(minutes=59)   # NOT the resume time


def test_trace_reports_resume_and_oldest_evidence_time(tmp_path):
    cp = _cp(tmp_path)
    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    async def go():
        cp.save(run_key("Q"), "fetch", "h1", {}, produced_at=hour_ago)
        tr = _trace()
        await run_stage(cp, tr, "fetch", "h1", _ok)
        await run_stage(cp, tr, "seal", "h2", _ok)
        return tr

    tr = asyncio.get_event_loop().run_until_complete(go())
    assert tr.is_resume
    assert tr.resumed_stages == ["fetch"] and tr.fresh_stages == ["seal"]
    oldest = datetime.fromisoformat(tr.oldest_produced_at())
    assert oldest <= datetime.now(UTC) - timedelta(minutes=59)


@given(st.lists(st.tuples(st.booleans(), st.integers(min_value=0, max_value=48)),
                min_size=1, max_size=6))
@settings(max_examples=25, deadline=None)
def test_property_oldest_produced_at_is_the_min(specs):
    """For any mix of resumed/fresh steps the trace's oldest timestamp is
    the minimum over all step production times — never the resume time."""
    now = datetime.now(UTC)
    ck = FileCheckpointer(root=None)
    ck.root.mkdir(parents=True, exist_ok=True)   # isolated default root is fine? no—use tmp
