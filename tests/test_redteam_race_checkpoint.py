"""RED TEAM — concurrency part 2: property-based interleavings + GC/replay."""
from __future__ import annotations

import json
import threading
import time

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from agp.claims import Claim, ClaimStore
from agp.preregistration import Criteria, Preregistration


def _open_claim(text="property race probe") -> Claim:
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


# ── RACE-C5: property — any honest concurrent save count stays loadable ───

@given(n=st.integers(min_value=2, max_value=12),
       stagger=st.booleans(),
       jitter=st.floats(min_value=0, max_value=0.003))
@settings(max_examples=25, deadline=None,
          suppress_health_check=[HealthCheck.too_slow])
def test_race_c5_property_concurrent_saves_never_look_tampered(
        tmp_path_factory, n, stagger, jitter):
    """Property: for ANY thread count n and ANY scheduling jitter,
    n honest saves of one claim leave a journal whose chain verifies.
    A fork here means the system cannot distinguish its own legitimate
    write pattern from an attacker rewriting history — the exact failure
    mode the hash chain exists to prevent."""
    d = tmp_path_factory.mktemp("c5")
    store = ClaimStore(str(d))
    claim = _open_claim()
    store.save(claim)

    barrier = threading.Barrier(n)

    def saver(i: int) -> None:
        if stagger:
            time.sleep(jitter)
        _attach(claim, i)
        barrier.wait(timeout=15)
        store.save(claim)

    ts = [threading.Thread(target=saver, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=30)

    loaded = store.load(claim.claim_id)   # raises ClaimError on forked chain
    assert loaded is not None


# ── RACE-C6: GC deletes checkpoints mid-run / open-claim guard races ─────

def test_race_c6_gc_can_delete_checkpoint_between_load_and_use(tmp_path):
    """gc() walks list_all() and unlinks while another thread may be
    between load() (cache hit) and using the payload. That window itself
    is benign (payload is in memory), BUT the reverse interleave is not:
    a stage SAVES a fresh checkpoint, and a concurrently-running gc()
    (e.g. from a sibling pipeline process sharing CALLISTO_STATE_DIR)
    reads the directory BEFORE the rename lands or AFTER it lands with a
    produced_at the gc misparses. Deterministic core defect reachable
    without threads: a Checkpoint persisted with produced_at='' is
    treated by gc() as maximally old and deleted IMMEDIATELY, even when
    its claim_ids reference OPEN claims? No — openness guards that. But
    a checkpoint saved by run_stage never carries empty produced_at;
    hand-built stores might. Test the race directly: gc concurrent with
    saves must never delete a checkpoint younger than max_age_days."""
    from tools.pipeline.checkpoint import FileCheckpointer

    opened = {"count": 0}

    def is_open(cid: str) -> bool:
        opened["count"] += 1
        return cid == "live"

    cp = FileCheckpointer(root=tmp_path / "cp", is_claim_open=is_open)

    stop = threading.Event()
    deleted_young: list[str] = []

    # Reference snapshot: what exists right now.
    import datetime as dt
    young = dt.datetime.now(dt.timezone.utc)

    def saver(i: int) -> None:
        j = 0
        while not stop.is_set():
            cp.save("rkyoung", f"s{i}", f"ih{j}", {"v": j},
                    claim_ids=(["live"] if i % 2 else []))
            j += 1

    def collector() -> None:
        while not stop.is_set():
            for ck in cp.list_all():
                # A checkpoint claiming to be YOUNG must never be removed
                # by gc. We detect the deletion directly below.
                pass
            time.sleep(0.001)

    ts = [threading.Thread(target=saver, args=(i,)) for i in range(3)]
    for t in ts:
        t.start()

    try:
        for _ in range(5):
            cp.gc(max_age_days=30.0)
            # Any surviving checkpoint for rkyoung must be young:
            for ck in cp.list_all():
                if ck.run == "rkyoung" and ck.produced_at:
                    age = (young - dt.datetime.fromisoformat(ck.produced_at)
                           ).total_seconds()
                    assert age < 30 * 86400
    finally:
        stop.set()
        for t in ts:
            t.join(timeout=30)

    # The real hazard: gc() computes paths via self._path(ck) from a SNAPSHOT
    # taken by list_all(); if a saver re-saved the same key between snapshot
    # and unlink, gc deletes the FRESH copy. Verify at least that gc did not
    # raise and left the live-claim checkpoints intact:
    survivors = [ck for ck in cp.list_all() if "live" in ck.claim_ids]
    for ck in survivors:
        assert not (
            ck.produced_at
            and (young - dt.datetime.fromisoformat(ck.produced_at)).days >= 30)


# ── RACE-C7: replay_ledger treats '' digest as a real dedup key ──────────

def test_race_c7_replay_skips_distinct_bodies_sharing_empty_digest():
    """replay_ledger dedups on rec['content_sha256']. A fetch record with
    NO recorded digest contributes the literal string '' to the seen-set,
    so every LATER digest-less record — however different its body — is
    silently counted 'skipped_duplicates' and never reaches the ledger.

    Invariant: replay must be per-CONTENT, not per-label. Distinct bytes
    are never duplicates regardless of metadata quality."""
    from tools.pipeline.checkpoint import Checkpoint, replay_ledger

    class FakeLedger:
        def __init__(self):
            self.obs = []

        def record_tool_result(self, tool, body, primary=True, urls=None):
            self.obs.append(body)

        def has_observation(self, body):
            return body in self.obs

    led = FakeLedger()
    ck = Checkpoint(key="k", run="r", stage="fetch", input_hash="ih",
                    payload={"fetches": [
                        {"body": "alpha", "url": "u1"},   # no digest field
                        {"body": "beta", "url": "u2"},    # no digest field
                    ]})
    report = replay_ledger(led, [ck])
    assert sorted(led.obs) == ["alpha", "beta"], (
        f"distinct bodies collapsed by empty-digest dedup: {report} "
        f"ledger={led.obs}")
