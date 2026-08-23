"""RED TEAM — checkpoint/resume (surface: checkpointing and resume, W3 module).

Method: DIFFERENTIAL + adversarial input. The module's own docstring states the
invariants under attack:

  - "Resumption must never become a way to launder evidence whose provenance
    was lost — when we cannot guarantee provenance, we refuse to seal."
  - "Integrity is checked, not assumed: if a checkpointed fetch's body no
    longer matches its recorded hash ... seal_guard() says REFUSE."
  - "A cache hit carries the ORIGINAL produced_at forward — evidence fetched
    an hour ago is labeled with that hour."

Every failing test below is a differential between the documented contract and
actual behaviour. Run:
    python3 -m pytest tests/test_redteam_checkpoint_resume.py -q
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agp.provenance import ProvenanceLedger, Evidence, SourceClass
from tools.pipeline.checkpoint import (
    FileCheckpointer,
    StageOutcome,
    RunTrace,
    hash_inputs,
    provenance_is_intact,
    replay_ledger,
    run_key,
    run_stage,
    seal_guard,
)

UTC = timezone.utc


# ── helpers ────────────────────────────────────────────────────────────────

def _cp(tmp_path) -> FileCheckpointer:
    return FileCheckpointer(root=tmp_path / "ck")


def _rk(q="Q"):
    return run_key(q, "GENERAL", "2026-08-23")


def _fetch(body="BODY", url="https://u/1", digest=None, **over):
    rec = {
        "source_name": "openalex",
        "tool_name": "openalex_fetch",
        "url": url,
        "body": body,
        "content_sha256": digest if digest is not None else
            hashlib.sha256(body.encode()).hexdigest(),
        "primary": True,
    }
    rec.update(over)
    return rec


def _resumed_trace(rk):
    tr = RunTrace(run=rk)
    tr.stages.append(StageOutcome(stage="decompose", resumed=True,
                                  payload={}, produced_at="2026-08-23T00:00:00+00:00"))
    return tr


def _fresh_trace(rk):
    return RunTrace(run=rk)


# ── F1: empty/missing digest bypasses the integrity check entirely ─────────

def test_f1_missing_digest_replays_fabrication_as_primary(tmp_path):
    """A fetch record with NO content_sha256 skips the integrity check
    (`if digest and ...` — falsy guard), is replayed into the ledger as
    primary=True bytes, and seal_guard returns SEAL.

    Anything that can write a checkpoint file (same machine, another agent,
    the GC-crash path) can mint PRIMARY provenance for arbitrary bytes with
    one missing JSON field."""
    cp = _cp(tmp_path)
    rk = _rk()
    ck = cp.save(rk, "fetch_leaf", hash_inputs({"qid": "q1"}), {
        "fetches": [_fetch(body="FABRICATED BYTES", digest=None)]})
    ledger = ProvenanceLedger()

    rep = replay_ledger(ledger, [ck])
    # CONTRACT: bytes whose integrity was never checked must not enter the
    # ledger as primary observations.
    assert not ledger.has_observation("FABRICATED BYTES")

    ev = Evidence(content="FABRICATED BYTES",
                  source_class=SourceClass.INFERRED, confidence_score=0.30,
                  domain=None, origin_agent="pipeline")
    assert ledger.assign_source_class(ev) != SourceClass.PRIMARY
    verdict, reason = seal_guard(_resumed_trace(rk), [ck], ledger)
    assert verdict == "REFUSE", reason


def test_f1_empty_string_digest_same_hole_and_breaks_dedup(tmp_path):
    """Empty-string digest: same bypass, plus the dedup key becomes '' so the
    SECOND distinct body is skipped as a 'duplicate' of the first — silently
    dropping real records while admitting fabrications."""
    cp = _cp(tmp_path)
    rk = _rk()
    ck = cp.save(rk, "fetch_leaf", hash_inputs({"qid": "q2"}), {
        "fetches": [_fetch(body="FAB ONE", digest=""),
                    _fetch(body="FAB TWO", digest="")]})
    ledger = ProvenanceLedger()
    rep = replay_ledger(ledger, [ck])
    assert rep["integrity_failures"], (
        "records without digests must count as integrity failures, not passes")
    assert not ledger.has_observation("FAB ONE"), (
        "unverified bytes must never be replayed into the ledger")
    # The resumed-run guard happens to REFUSE here only as a side effect of
    # FAB TWO having no ledger entry (has_observation False) — i.e. the one
    # record that WAS verified-looking gets refused while FAB ONE, never
    # integrity-checked, sits in the ledger as PRIMARY. Invert the pairing to
    # see the laundering: a checkpoint whose ONLY records carry empty digests
    # and which are all replayed seals fine (test_f1_missing_digest_...).


# ── F2: seal_guard on a fresh run contaminates the ledger cross-run ────────

def test_f2_seal_guard_pollutes_ledger_with_other_runs_checkpoints(tmp_path):
    """engine.py:611 calls `seal_guard(trace, cp.list_all(), self.ledger)` —
    ALL checkpoints in the store, every run ever made on this machine, not
    just this trace's. Two consequences:

      1. Run B's guard replays Run A's fetches into Run B's ledger, so Run A's
         bytes become PRIMARY inside Run B even though Run B never fetched them.
         Any later INFERRED claim in Run B echoing those bytes re-classes
         SECONDARY→PRIMARY off evidence its own run never collected.
      2. The guard MUTATES the ledger as a side effect of checking it — even
         the fresh branch, which then returns SEAL based on bytes it just
         laundered in itself."""
    cp = _cp(tmp_path)
    rkA = _rk("semiconductor supply chains")
    bodyA = '{"openalex": "chip paper"}'
    cp.save(rkA, "fetch_leaf", hash_inputs({"qid": "a1"}),
            {"fetches": [_fetch(body=bodyA, url="https://openalex/1")]})

    rkB = _rk("unemployment rate")   # different question, never crashed
    ledgerB = ProvenanceLedger()
    assert not ledgerB.has_observation(bodyA)

    verdict, _ = seal_guard(_fresh_trace(rkB), cp.list_all(), ledgerB)
    assert not ledgerB.has_observation(bodyA), (
        "FAILS: the guard itself replayed run A's bytes into run B's ledger")
    # And the seal decision was made over checkpoints from other runs:
    assert verdict == "REFUSE", (
        "guard consulted checkpoints belonging to other runs")


# ── F3: no-fetch checkpoints make the guard vacuous ────────────────────────

def test_f3_guard_passes_when_checkpoints_carry_no_fetch_records(tmp_path):
    """provenance_is_intact iterates payload['fetches']; a checkpoint whose
    payload has none (answer_leaf, decompose, or a fetch record stripped by a
    schema change / older writer) trivially passes. 'Nothing to verify'
    collapses to 'verified'."""
    cp = _cp(tmp_path)
    rk = _rk()
    ck = cp.save(rk, "answer_leaf", hash_inputs({"qid": "z"}), {"leaf": {}})
    ledger = ProvenanceLedger()
    assert not provenance_is_intact(ledger, [ck]), (
        "a checkpoint whose fetches cannot be verified must not read as intact")
    verdict, _ = seal_guard(_resumed_trace(rk), [ck], ledger)
    assert verdict == "REFUSE"


# ── F4: produced_at is attacker-writable; staleness is cosmetic ────────────

def test_f4_produced_at_forgery_uncovered(tmp_path):
    """The docstring promises resumed runs are 'honest about evidence age'.
    produced_at is a plain JSON field written by whoever touches the store;
    nothing authenticates it. Rewriting it to now() makes 40-day-old
    checkpointed evidence report age 0 — and gc() will then spare it forever
    by the same mechanism."""
    cp = _cp(tmp_path)
    rk = _rk()
    old = datetime.now(UTC) - timedelta(days=40)
    ck = cp.save(rk, "fetch_leaf", hash_inputs({"qid": "t"}),
                 {"fetches": []}, produced_at=old)
    assert ck.age_seconds() > 39 * 86400

    p = cp._path(ck)
    d = json.loads(p.read_text())
    d["produced_at"] = datetime.now(UTC).isoformat()
    p.write_text(json.dumps(d))

    forged = cp.load_by_key(rk, ck.key)
    assert forged.age_seconds() > 39 * 86400, (
        "produced_at was rewritten to now() and the forgery went undetected")


# ── F5: cache key ignores everything except question_id ────────────────────

@pytest.mark.asyncio
async def test_f5_fetch_cache_key_ignores_question_text_and_day(tmp_path):
    """run_stage's fetch_leaf inputs are {"qid": q.question_id}. The cached
    payload binds neither the leaf TEXT nor today's date nor the registry
    state. On resume after a regenerated decomposition (model nondeterminism),
    a leaf that kept its id but changed meaning is served fetches collected
    for a different question — and seals them."""
    cp = _cp(tmp_path)
    rk = "r" * 64
    calls = []

    async def execute():
        calls.append(hash_inputs({"served_for": "old text"}))
        return {"fetches": [], "rejections": [], "independent_keys": [],
                "queries": [], "stop_reason": ""}

    await run_stage(cp, RunTrace(run=rk), "fetch_leaf",
                    {"qid": "L1"}, execute)
    # Same question_id, but the live leaf TEXT changed (regenerated
    # decomposition). A content-addressed cache MUST miss here.
    oc = await run_stage(cp, RunTrace(run=rk), "fetch_leaf",
                         {"qid": "L1"}, execute)
    assert not oc.resumed, (
        "cache hit served fetches collected under different stage inputs "
        "(key binds only qid, not the leaf text / date / registry state)")


# ── honest negatives (regression pins) ─────────────────────────────────────

def test_negative_wrong_digest_is_caught(tmp_path):
    """A tampered body WITH a recorded good digest IS refused — the check
    exists and works when the field is present and honest."""
    cp = _cp(tmp_path)
    rk = _rk()
    good = '{"results": [1]}'
    ck = cp.save(rk, "fetch_leaf", hash_inputs({"qid": "q9"}),
                 {"fetches": [_fetch(body="TAMPERED", digest=good)]})
    ledger = ProvenanceLedger()
    rep = replay_ledger(ledger, [ck])
    assert rep["integrity_failures"] == [ck.key]
    assert not ledger.has_observation("TAMPERED")
    assert seal_guard(_resumed_trace(rk), [ck], ledger)[0] == "REFUSE"


def test_negative_double_replay_does_not_duplicate(tmp_path):
    cp = _cp(tmp_path)
    rk = _rk()
    ck = cp.save(rk, "fetch_leaf", hash_inputs({"qid": "d"}),
                 {"fetches": [_fetch()]})
    l1, l2 = ProvenanceLedger(), ProvenanceLedger()
    r1 = replay_ledger(l1, [ck])
    r2 = replay_ledger(l1, [ck])           # same ledger twice
    assert (r1["replayed"], r2["replayed"]) == (1, 0)
