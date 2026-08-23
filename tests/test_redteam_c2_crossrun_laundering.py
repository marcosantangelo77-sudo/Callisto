"""C2 — seal_guard consults EVERY checkpoint in the store, and mutates.

engine.py passes `cp.list_all()` — every checkpoint ever written, from every
run — into seal_guard. seal_guard calls provenance_is_intact -> replay_ledger,
which RECORDS those bytes into the ledger via record_tool_result(primary=True).

So checking run B imports run A's bytes into run B's ledger as PRIMARY
observations. Any later INFERRED claim in run B that echoes those bytes
re-classes upward off evidence run B never collected — and the guard returns
SEAL over bytes it laundered in itself, as a side effect of *checking*.

Two independent defects:
  1. scope   — checkpoints are not filtered to trace.run
  2. purity  — a CHECK must not mutate the thing it is checking
"""
import hashlib

from tools.pipeline.checkpoint import (
    Checkpoint, RunTrace, StageOutcome, seal_guard,
)


class _Ledger:
    def __init__(self):
        self.records = []

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self.records.append({"tool": tool, "body": body, "primary": primary})


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _ck(run, body, key):
    return Checkpoint(
        key=key, run=run, stage="fetch_leaf", input_hash="ih",
        payload={"fetches": [{"body": body, "url": "https://a/x",
                              "content_sha256": _sha(body)}]})


def _trace(run, resumed=False):
    t = RunTrace(run=run)
    t.stages.append(StageOutcome(stage="fetch_leaf", resumed=resumed,
                                 payload={}, produced_at="2026-08-23T00:00:00"))
    return t


def test_other_runs_checkpoints_are_not_replayed_into_this_ledger():
    """Run B must not absorb run A's bytes just by being checked."""
    led = _Ledger()
    store = [_ck("run-A", "RUN A SECRET BYTES", "kA"),
             _ck("run-B", "run b bytes", "kB")]
    seal_guard(_trace("run-B"), store, led)
    bodies = [r["body"] for r in led.records]
    assert "RUN A SECRET BYTES" not in bodies, \
        "another run's evidence was laundered into this run's ledger"


def test_guard_does_not_mutate_the_ledger_it_checks():
    """Checking provenance must be side-effect free."""
    led = _Ledger()
    store = [_ck("run-B", "run b bytes", "kB")]
    seal_guard(_trace("run-B"), store, led)
    assert not led.records, \
        "seal_guard recorded into the ledger as a side effect of checking it"


def test_tampered_checkpoint_in_this_run_still_refuses():
    """The fix must not weaken the guard: bad bytes in THIS run still REFUSE."""
    led = _Ledger()
    bad = Checkpoint(key="kB", run="run-B", stage="fetch_leaf", input_hash="ih",
                     payload={"fetches": [{"body": "TAMPERED",
                                           "url": "https://a/x",
                                           "content_sha256": _sha("original")}]})
    verdict, reason = seal_guard(_trace("run-B", resumed=True), [bad], led)
    assert verdict == "REFUSE", f"tampered evidence sealed: {reason}"
