"""RED TEAM — concurrency and races, part 1: ClaimStore + FileCheckpointer.

Invariant under attack: an HONEST concurrent workload cannot corrupt,
strand, or falsely flag a claim's calibration record, and cannot
duplicate or launder pipeline evidence through unsynchronized caches.
Tamper-evidence that fires on legitimate use is itself a defect: it
trains the operator to ignore the alarm.
"""
from __future__ import annotations

import json
import threading

import pytest

from agp.claims import Claim, ClaimError, ClaimStore
from agp.preregistration import Criteria, Preregistration


def _open_claim(text: str = "concurrent race probe") -> Claim:
    c = Claim(text=text)
    c.seal_preregistration(Preregistration(
        query="q", criteria=Criteria(confirm_markers=["up"],
                                     refute_markers=["down"])))
    return c


def _attach(c: Claim, i: int) -> None:
    from agp import Domain, Evidence, SourceClass
    c.attach_evidence(Evidence(
        content=f"ev {i}", source_class=SourceClass.SECONDARY,
        confidence_score=0.5, domain=Domain.GENERAL,
        origin_agent="redteam"))


# ── RACE-C1: concurrent saves of one claim fork the hash chain ────────────

def test_race_c1_concurrent_saves_keep_chain_loadable(tmp_path):
    """N threads save the SAME claim concurrently (evidence arriving from
    parallel fetchers; loop + dashboard both persisting). save() reads the
    last line's hash THEN appends — nothing serializes read-append. Two
    savers that read the same tail both reference the same predecessor:
    the chain forks, and load(verify=True) reports TAMPERING for honest
    writes."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim = _open_claim()

    barrier = threading.Barrier(8)
    errors: list[Exception] = []

    def saver(i: int) -> None:
        try:
            _attach(claim, i)
            barrier.wait(timeout=10)
            store.save(claim)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=saver, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"savers raised: {errors!r}"
    loaded = store.load(claim.claim_id)  # verify=True by default
    assert loaded is not None, "claim vanished after concurrent saves"


def test_race_c1b_concurrent_appends_are_not_torn(tmp_path):
    """POSIX makes appends line-atomic only below PIPE_BUF; a serialized
    claim state blob is larger, so interleaved appends can tear. Whatever
    else happens, every journal line an honest save wrote must remain
    parseable JSON — load() must never meet a half-line."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim = _open_claim()
    store.save(claim)

    def saver(i: int) -> None:
        _attach(claim, 100 + i)
        store.save(claim)

    threads = [threading.Thread(target=saver, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    path = store._journal_path(claim.claim_id)
    lines = [ln for ln in open(path, encoding="utf-8").read().splitlines()
             if ln.strip()]
    bad = []
    for i, ln in enumerate(lines):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            bad.append((i + 1, str(e)))
    assert not bad, f"torn journal lines from concurrent appends: {bad}"


# ── RACE-C2: lost update — second save silently erases evidence ───────────

def test_race_c2_lost_update_silently_drops_evidence(tmp_path):
    """Two holders of the same claim id attach DIFFERENT evidence and save.
    There is no version counter, merge, or compare-and-swap: the later save
    overwrites the earlier one's evidence set while still APPENDING to the
    journal, recording the loss as if it were a legitimate transition.

    Invariant: evidence persisted by a successful save() must still be
    present after any subsequent save/load round-trip. Evidence may be
    superseded by a claim that SAW it — never silently erased by one that
    did not."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim_a = _open_claim("lost update probe")
    _attach(claim_a, 1)
    store.save(claim_a)

    loaded_b = Claim.from_dict(store.load(claim_a.claim_id).to_dict())
    assert len(loaded_b.evidence) == 1

    _attach(loaded_b, 2)
    store.save(loaded_b)
    # A never saw B's evidence-2; A's in-memory state still has only ev 1+3:
    _attach(claim_a, 3)
    store.save(claim_a)

    final = store.load(claim_a.claim_id)
    texts = {e.evidence.content for e in final.evidence}
    assert "ev 2" in texts, (
        f"B's persisted evidence was silently erased by A's save; "
        f"final evidence set: {sorted(texts)}")


# ── RACE-C4: FileCheckpointer.run_stage check-then-act duplicates work ────

@pytest.mark.asyncio
async def test_race_c4_checkpoint_cache_miss_thundering_herd(tmp_path):
    """run_stage() is load-check then execute then save, with no lock. Two
    coroutines resuming the SAME stage concurrently both miss, both pay for
    the model call / fetch, and both save. For fetch stages this DUPLICATES
    ledger entries the module docstring promises are impossible ('re-running
    cannot duplicate evidence or ledger entries'). Worse: the two payloads
    can carry different fetched bytes for the same input_hash, and whichever
    saves LAST wins — the resumed consumer may synthesize against payload A
    while the checkpoint on disk holds payload B.

    Observable invariant here: N concurrent run_stage calls on a fresh
    stage execute the underlying work exactly once."""
    from tools.pipeline.checkpoint import FileCheckpointer, RunTrace, run_stage

    cp = FileCheckpointer(root=tmp_path / "cp")
    calls = {"n": 0}
    lock = threading.Lock()

    async def execute() -> dict:
        with lock:
            calls["n"] += 1
        return {"fetches": [{"body": "b", "url": "u", "content_sha256": ""}]}

    async def one(i: int) -> None:
        tr = RunTrace(run="racesamekey")
        await run_stage(cp, tr, "fetch", {"q": "x"}, execute)

    await asyncio.gather(*(one(i) for i in range(10)))
    assert calls["n"] == 1, (
        f"concurrent resume executed the same stage {calls['n']} times "
        f"(docstring contract: 'no duplicate fetches')")


import asyncio  # noqa: E402  (used above)
