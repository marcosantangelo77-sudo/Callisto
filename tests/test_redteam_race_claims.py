"""RED TEAM — concurrency and races, part 1: ClaimStore.

Invariant under attack: an HONEST concurrent workload cannot corrupt,
strand, or falsely flag a claim's calibration record. Tamper-evidence
that fires on legitimate use is itself a defect: it trains the operator
to ignore the alarm, and a claim whose journal fails to load is a claim
whose belief history is gone.

Every test here uses only the public API — no fabricated state.
"""
from __future__ import annotations

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


# ── RACE-C1: concurrent saves interleave past the hash chain ──────────────

def test_race_c1_concurrent_saves_keep_chain_loadable(tmp_path):
    """N threads save the SAME claim concurrently (e.g. evidence arriving
    from parallel fetchers, or the loop and a dashboard both persisting).
    Each save reads the last line's hash THEN appends — nothing serializes
    the read-append pair. Two savers that read the same tail both write a
    line referencing the same predecessor, and the chain forks.

    Invariant: after any number of legitimate concurrent saves, load()
    must succeed and return the newest state. A forked chain raises
    'tampering detected' — an honest write pattern must not be
    indistinguishable from tampering."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim = _open_claim()

    BARRIER = threading.Barrier(8)
    errors: list[Exception] = []

    def saver(i: int) -> None:
        try:
            _attach(claim, i)          # distinct evidence per thread
            BARRIER.wait(timeout=10)
            store.save(claim)
        except Exception as e:          # noqa: BLE001 - collect everything
            errors.append(e)

    threads = [threading.Thread(target=saver, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"savers raised: {errors!r}"
    loaded = store.load(claim.claim_id)   # default verify=True
    assert loaded is not None, "claim vanished"
    # The journal must contain every save (nothing dropped):
    import os
    n_lines = sum(
        1 for ln in open(store._journal_path(claim.claim_id),
                         encoding="utf-8") if ln.strip())
    assert loaded.status.value == "open"


def test_race_c1b_concurrent_saves_do_not_drop_entries(tmp_path):
    """Append-mode writes under POSIX are line-atomic only below PIPE_BUF;
    a full claim state blob is far larger, so two interleaved appends can
    tear. Property: whatever else happens, every byte written by a save
    survives, and load() either returns valid state or raises loudly —
    never a SILENTLY TRUNCATED history presented as truth."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim = _open_claim()
    store.save(claim)                     # genesis entry

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
    # Every line must be parseable JSON — a torn write leaves a line that
    # is not, which load() turns into 'journal line N corrupt'.
    import json
    bad = []
    for i, ln in enumerate(lines):
        try:
            json.loads(ln)
        except json.JSONDecodeError as e:
            bad.append((i + 1, str(e)))
    assert not bad, f"torn/corrupt journal lines from concurrent appends: {bad}"


# ── RACE-C2: lost update — read-modify-write of claim state ───────────────

def test_race_c2_last_writer_wins_silently_drops_evidence(tmp_path):
    """Two processes load the same claim, attach DIFFERENT evidence, and
    save. There is no merge, no version counter, no compare-and-swap in
    ClaimStore.save — the second save overwrites the first's evidence set
    while still APPENDING to the journal, so the journal happily records
    the loss as if it were a legitimate transition.

    Invariant: evidence that was persisted by a successful save() must
    still be present after any subsequent save/load round-trip. Evidence
    may be superseded, never silently erased by a peer writer."""
    store = ClaimStore(str(tmp_path / "claims"))
    claim_a = _open_claim("lost update probe")
    claim_b = Claim.from_dict(claim_a.to_dict())   # same id, second process

    _attach(claim_a, 1)
    store.save(claim_a)
    # process B loads what A wrote:
    loaded_b = Claim.from_dict(store.load(claim_a.claim_id).to_dict())
    assert len(loaded_b.evidence) == 1

    # Both attach new, DIFFERENT evidence concurrently-ish and save:
    _attach(loaded_b, 2)
    store.save(loaded_b)
    _attach(claim_a, 3)                    # A never saw B's evidence
    store.save(claim_a)

    final = store.load(claim_a.claim_id)
    texts = {e.evidence.content for e in final.evidence}
    assert "ev 2" in texts, (
        f"B's persisted evidence was silently erased by A's save; "
        f"final evidence set: {sorted(texts)}")


# ── RACE-C3: list_ids during a save sees a half-written world ────────────

@pytest.mark.parametrize("n_writers", [4])
def test_race_c3_list_ids_never_sees_corrupt_id(tmp_path, n_writers):
    """list_ids() derives claim ids by slicing filenames. Concurrent saves
    create files atomically? No — save() opens the journal with 'a' and
    writes directly; the FILE exists from the moment of open. But the
    directory scan itself races nothing here. What DOES race: a reader
    calling list_ids()+load() while writers work must never get an id
    whose load() reports TAMPERING (as opposed to clean state)."""
    store = ClaimStore(str(tmp_path / "claims"))
    claims = [_open_claim(f"c{i}") for i in range(n_writers)]
    stop = threading.Event()
    failures: list[str] = []

    def writer(c: Claim) -> None:
        i = 0
        while not stop.is_set():
            _attach(c, i)
            store.save(c)
            i += 1

    threads = [threading.Thread(target=writer, args=(c,)) for c in claims]
    for t in threads:
        t.start()

    try:
        for _ in range(200):
            for cid in store.list_ids():
                try:
                    store.load(cid)
                except ClaimError as e:
                    failures.append(f"{cid}: {e}")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=30)

    assert not failures, (
        f"honest concurrent writes flagged as tampering/corruption "
        f"{len(failures)} times, e.g.: {failures[:3]}")
