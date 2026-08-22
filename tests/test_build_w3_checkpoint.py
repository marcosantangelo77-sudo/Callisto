"""W3 — checkpointing and resumability.

Covers:
  1. step-level checkpoints (content-addressed; unchanged steps not redone)
  2. resume semantics that do not lie (original produced_at carried forward)
  3. idempotence (kill + resume produces exactly a clean run's ledger/store)
  4. sealing across the resume boundary (provenance intact or refuse)
  5. GC that never deletes an open claim's checkpoint
Property-based tests where the invariant matters: idempotence under ANY
crash point, and GC safety for any claim-openness pattern.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

UTC = timezone.utc

from agp.provenance import ProvenanceLedger          # noqa: E402
from tools.pipeline.checkpoint import (              # noqa: E402
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


# ── helpers ────────────────────────────────────────────────────────────────

def _cp(tmp_path):
    return FileCheckpointer(root=tmp_path / "ck")


def _trace(rk=None):
    return RunTrace(run=rk or _run())


async def _ok(payload=None):
    return payload or {"done": True}


def _run(q="Q"):
    return run_key(q, "GENERAL", "2026-08-22")


def loop():
    return asyncio.get_event_loop()


def _fetch_record(source="openalex", url="http://x/works?q=t",
                  body='{"results": [{"id": "W1"}]}'):
    digest = hashlib.sha256(body.encode()).hexdigest()
    return {
        "source_name": source, "tool_name": f"{source}_fetch",
        "url": url, "body": body, "content_sha256": digest, "primary": True,
    }


# ── 1. step-level checkpoints, content-addressed ──────────────────────────

def test_step_key_is_content_addressed():
    rk = _run()
    assert step_key(rk, "decompose", hash_inputs({"q": "x"})) == \
        step_key(rk, "decompose", hash_inputs({"q": "x"}))
    assert step_key(rk, "fetch", "h") != step_key(rk, "decompose", "h")
    assert step_key(rk, "decompose", "h") != \
        step_key(_run("other"), "decompose", "h")
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
        return await run_stage(cp, tr2, "decompose", {"q": "x"}, work)

    oc = loop().run_until_complete(go())
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

    loop().run_until_complete(go())
    assert len(calls) == 2


def test_crash_mid_run_resumes_from_last_good_step(tmp_path):
    """Run, die at 'adversary', resume — decompose/leaf are hits."""
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
        loop().run_until_complete(
            run_pipeline_checked(cp, _run(), stages(die_at="adversary")))
    executed.clear()

    trace, merged = loop().run_until_complete(
        run_pipeline_checked(cp, _run(), stages()))
    assert merged["decompose"]["sub_questions"] == ["a", "b"]
    assert trace.resumed_stages == ["decompose", "leaf"]
    assert trace.fresh_stages == ["adversary"]
    assert executed == ["adversary"]      # only the failed stage redone


# ── 2. resume semantics that do not lie ───────────────────────────────────

def test_cache_hit_carries_original_produced_at(tmp_path):
    cp = _cp(tmp_path)
    old = datetime.now(UTC) - timedelta(hours=1)

    async def go():
        saved = cp.save(_run(), "fetch", hash_inputs({"h": "h"}),
                        {"body": "b"}, produced_at=old)
        tr = _trace()
        return saved, await run_stage(cp, tr, "fetch", {"h": "h"}, _ok)

    saved, oc = loop().run_until_complete(go())
    assert oc.resumed
    assert oc.produced_at == saved.produced_at
    # NOT the resume time: still reads as an hour old.
    assert datetime.fromisoformat(oc.produced_at) <= \
        datetime.now(UTC) - timedelta(minutes=59)


def test_trace_reports_resume_and_oldest_evidence_time(tmp_path):
    cp = _cp(tmp_path)
    hour_ago = datetime.now(UTC) - timedelta(hours=1)

    async def go():
        cp.save(_run(), "fetch", hash_inputs({"h": "h1"}), {},
                produced_at=hour_ago)
        tr = _trace()
        await run_stage(cp, tr, "fetch", {"h": "h1"}, _ok)
        await run_stage(cp, tr, "seal", {"h": "h2"}, _ok)
        return tr

    tr = loop().run_until_complete(go())
    assert tr.is_resume
    assert tr.resumed_stages == ["fetch"] and tr.fresh_stages == ["seal"]
    oldest = datetime.fromisoformat(tr.oldest_produced_at())
    assert oldest <= datetime.now(UTC) - timedelta(minutes=59)


@given(st.integers(min_value=0, max_value=4))
@settings(max_examples=5, deadline=None)
def test_property_kill_and_resume_equals_clean_run(crash_after_n):
    """For ANY crash point in the 4-stage chain, resuming to completion
    yields exactly what a clean run yields: same ledger observations, same
    artifact hashes, same payloads — nothing duplicated, nothing lost.
    (The earlier instance shipped a correct assertion over inputs that never
    hit the failing boundary; this sweeps every boundary.)"""
    root = Path(tempfile.mkdtemp(prefix="w3prop_"))
    try:
        stages_seen: list[str] = ["decompose", "leaf_a", "leaf_b", "seal"]

        async def attempt(crash_at, fresh_root):
            """crash_at=None -> clean; else Crash raised AT that index."""
            cp = FileCheckpointer(root=fresh_root / "ck")
            ledger = ProvenanceLedger()
            store: list[str] = []
            rk = _run(f"prop")

            def mk(crash_idx):
                out = []
                for i, name in enumerate(stages_seen):
                    if name == "leaf_a":
                        rec = _fetch_record()

                        async def fetch(rec=rec):
                            ledger.record_tool_result(
                                rec["tool_name"], rec["body"], primary=True,
                                urls=[rec["url"]])
                            store.append(rec["content_sha256"])
                            return {"fetches": [rec]}

                        fn, ins = fetch, {"qid": name}
                    else:
                        async def generic(name=name):
                            return {"done": name}

                        fn, ins = generic, {"in": name}
                    if crash_idx is not None and i == crash_idx:
                        async def die(*a, **k):
                            raise Crash("boom")
                        fn = die
                    out.append((name, (lambda i=i, ins=ins: dict(ins)), fn))
                return out

            trace = RunTrace(run=rk)
            crashed = False
            for name, ins_fn, fn in mk(crash_at):
                try:
                    await run_stage(cp, trace, name, ins_fn(), fn)
                except Crash:
                    crashed = True
                    break
            # second pass to completion (resume) if we crashed
            for name, ins_fn, fn in mk(None):
                await run_stage(cp, trace, name, ins_fn(), fn)
            return crashed, ledger, store, trace

        clean = loop().run_until_complete(attempt(None, root))
        assert clean[0] is False
        crashed, ledger, store, trace = loop().run_until_complete(
            attempt(crash_after_n, root / "crash"))
        assert crashed is True
        assert trace.is_resume

        # IDEMPOTENCE: resumed run's ledger equals clean run's exactly.
        assert len(store) == 1, "artifact/fetch recorded more than once"
        assert ledger.observed_urls() == clean[1].observed_urls()
        # payloads equal: same number of checkpoints, identical payloads
        cps = {c.stage: c.payload for c in
               FileCheckpointer(root=root / "crash" / "ck").list_all()}
        ref = {c.stage: c.payload for c in
               FileCheckpointer(root=root / "ck").list_all()}
        assert len(cps) == 4 == len(ref)
        assert {k: v for k, v in cps.items()} == \
            {k: v for k, v in ref.items()}
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── 3b. idempotence, concrete kill/resume (mirrors the property test) ─────

def test_kill_resume_produces_exactly_clean_run_state(tmp_path):
    cp = _cp(tmp_path)
    ledger = ProvenanceLedger()
    store: list[str] = []
    rec = _fetch_record()
    rk = _run("kill")

    def stages(die_at=None):
        async def decompose():
            return {"sub_questions": ["a"]}

        async def fetch():
            ledger.record_tool_result(rec["tool_name"], rec["body"],
                                      primary=True, urls=[rec["url"]])
            store.append(rec["content_sha256"])
            return {"fetches": [rec]}

        async def seal():
            return {"sealed": True}

        out = [("decompose", lambda: {}, decompose),
               ("fetch", lambda: {"qid": "a"}, fetch),
               ("seal", lambda: {"c": "c"}, seal)]
        if die_at:
            async def die(*a, **k):
                raise Crash("died")
            out[die_at] = (out[die_at][0], out[die_at][1], die)
        return out

    # kill during fetch
    with pytest.raises(Crash):
        loop().run_until_complete(
            run_pipeline_checked(cp, rk, stages(die_at=1)))
    assert store == []                       # died before recording

    trace, merged = loop().run_until_complete(run_pipeline_checked(cp, rk, stages()))
    assert merged["seal"] == {"sealed": True}
    # exactly one ledger observation, one artifact hash, three checkpoints
    assert len(store) == 1
    assert len(ledger._by_hash) == 1
    assert len(cp.list_all()) == 3


# ── 4. sealing across the resume boundary ─────────────────────────────────

def _fetch_checkpoint(tmp_path, rk, body='{"ok": 1}', url="http://x/1",
                      corrupt=False):
    rec = _fetch_record(body=body, url=url)
    if corrupt:
        rec["content_sha256"] = "0" * 64
    ck = Checkpoint(key=step_key(rk, "fetch_leaf", rec["content_sha256"]),
                    run=rk, stage="fetch_leaf", input_hash="ih",
                    payload={"fetches": [rec]},
                    produced_at=datetime.now(UTC).isoformat())
    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(rk, ck.stage, ck.input_hash, ck.payload)
    stored = cp.load(rk, ck.stage, ck.input_hash)
    if corrupt:
        stored.payload["fetches"][0]["content_sha256"] = "0" * 64
    return stored


def test_replay_ledger_restores_provenance(tmp_path):
    rk = _run("replay")
    ck = _fetch_checkpoint(tmp_path, rk)
    ledger = ProvenanceLedger()
    report = replay_ledger(ledger, [ck])
    assert report["replayed"] == 1 and not report["integrity_failures"]
    body = ck.payload["fetches"][0]["body"]
    assert ledger.is_primary_bytes(body)


def test_replay_is_idempotent_no_duplicate_observations(tmp_path):
    rk = _run("dup")
    ck = _fetch_checkpoint(tmp_path, rk)
    ledger = ProvenanceLedger()
    replay_ledger(ledger, [ck])
    report = replay_ledger(ledger, [ck])
    assert report["skipped_duplicates"] == 1
    assert len(ledger._by_hash) == 1


def test_seal_guard_refuses_when_provenance_broken(tmp_path):
    rk = _run("broken")
    ck = _fetch_checkpoint(tmp_path, rk, corrupt=True)
    ledger = ProvenanceLedger()
    verdict, reason = seal_guard(_trace(rk), [ck], ledger)
    assert verdict == "REFUSE"
    assert "refusing to seal" in reason
    # and a FRESH run is never blocked by the guard
    assert seal_guard(RunTrace(run=rk), [], ledger)[0] == "SEAL"


def test_seal_guard_allows_intact_resumed_run(tmp_path):
    rk = _run("intact")
    ck = _fetch_checkpoint(tmp_path, rk)
    ledger = ProvenanceLedger()
    tr = _trace(rk)
    tr.stages.append(type("S", (), {"stage": "fetch_leaf", "resumed": True,
                                    "payload": {}, "produced_at": ""})())
    verdict, reason = seal_guard(tr, [ck], ledger)
    assert verdict == "SEAL", reason
    assert provenance_is_intact(ledger, [ck])


def test_tampered_body_detected_even_with_matching_hash_field(tmp_path):
    """An attacker (or corruption) editing the body but leaving the recorded
    hash must fail the integrity check."""
    rk = _run("tamper")
    ck = _fetch_checkpoint(tmp_path, rk)
    # tamper AFTER hashing was recorded
    ck.payload["fetches"][0]["body"] = '{"ok": EVIL}'
    ledger = ProvenanceLedger()
    assert not provenance_is_intact(ledger, [ck])
    verdict, _ = seal_guard(_trace(rk), [ck], ledger)
    assert verdict == "REFUSE"


# ── 5. garbage collection spares open claims ──────────────────────────────

def _aged_cp(tmp_path, days, claims=None):
    cp = _cp(tmp_path)
    old = datetime.now(UTC) - timedelta(days=days)
    cp.save(_run("gc"), "stage", f"h{days}", {"v": days},
            claim_ids=claims or [], produced_at=old)
    return cp


def test_gc_deletes_old_checkpoints(tmp_path):
    cp = _aged_cp(tmp_path, 40)
    removed = cp.gc(max_age_days=30)
    assert len(removed) == 1 and cp.list_all() == []


def test_gc_keeps_recent_checkpoints(tmp_path):
    cp = _aged_cp(tmp_path, 5)
    assert cp.gc(max_age_days=30) == []
    assert len(cp.list_all()) == 1


@given(st.lists(st.tuples(st.floats(min_value=0.1, max_value=100),
                          st.lists(st.text(min_size=1, max_size=5),
                                   max_size=2)),
                min_size=1, max_size=8),
       st.sets(st.text(min_size=1, max_size=5), max_size=6))
@settings(max_examples=30, deadline=None)
def test_property_gc_never_deletes_open_claim_checkpoint(specs, open_claims):
    """INVARIANT: after gc, every checkpoint belonging to an open claim
    still exists, whatever the ages and openness pattern."""
    root = Path(tempfile.mkdtemp(prefix="w3gc_"))
    try:
        cp = FileCheckpointer(root=root / "ck",
                              is_claim_open=lambda c: c in open_claims)
        now = datetime.now(UTC)
        expected_keep, expected_gone = set(), set()
        for i, (age_days, claims) in enumerate(specs):
            produced = now - timedelta(days=age_days)
            cp.save(_run("gcp"), "stage", f"key{i}", {"i": i},
                    claim_ids=list(claims), produced_at=produced)
            is_open = any(c in open_claims for c in claims)
            old = age_days > 30.0
            (expected_keep if (is_open or not old) else expected_gone).add(i)

        cp.gc(max_age_days=30.0, now=now)
        remaining_keys = {c.input_hash for c in cp.list_all()}
        kept = {f"key{i}" for i in expected_keep} & remaining_keys
        gone = {f"key{i}" for i in expected_gone} & remaining_keys
        assert gone == set(), f"stale unclaimed checkpoints survived: {gone}"
        assert kept == {f"key{i}" for i in expected_keep}, (
            f"open-claim checkpoint deleted! missing="
            f"{ {f'key{i}' for i in expected_keep} - kept }")
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ── misc robustness ────────────────────────────────────────────────────────

def test_corrupt_checkpoint_file_is_a_miss_not_a_crash(tmp_path):
    cp = _cp(tmp_path)
    d = cp.root / _run()[:16]
    d.mkdir(parents=True)
    (d / "decompose.deadbeef.json").write_text("{not json")
    assert cp.load_by_key(_run(), "deadbeef") is None


def test_atomic_write_leaves_no_tmp_files(tmp_path):
    cp = _cp(tmp_path)
    cp.save(_run(), "stage", "h", {})
    leftovers = list((cp.root / _run()[:16]).glob("*.tmp"))
    assert leftovers == []
