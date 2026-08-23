"""C3 — "nothing to verify" collapses into "verified".

provenance_is_intact iterates payload["fetches"]. A checkpoint carrying no
fetch records trivially reads as intact, so a resumed run whose fetch payloads
were restructured — by tampering, or simply by an older/newer schema — seals
with ZERO verified provenance while the guard reports success.

The fix must distinguish two genuinely different things:
  - a stage that never fetches (decompose) legitimately has no fetch records
  - a FETCH stage with no `fetches` key is schema drift or tampering
"""
import hashlib
import pytest

from tools.pipeline.checkpoint import (
    Checkpoint, RunTrace, StageOutcome, provenance_is_intact, seal_guard,
)


class _Ledger:
    def __init__(self):
        self._b = set()

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self._b.add(body)

    def has_observation(self, body):
        return body in self._b


def _sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


def _trace(run="run-1", resumed=True):
    t = RunTrace(run=run)
    t.stages.append(StageOutcome(stage="fetch_leaf", resumed=resumed,
                                 payload={}, produced_at="2026-08-23T00:00:00"))
    return t


def test_fetch_stage_without_fetches_key_is_not_intact():
    """A fetch checkpoint missing its fetches key must not read as verified."""
    ck = Checkpoint(key="k", run="run-1", stage="fetch_leaf",
                    input_hash="ih", payload={"queries": ["q"]})  # no fetches
    assert not provenance_is_intact(_Ledger(), [ck]), \
        "a fetch checkpoint with no fetch records read as intact"


def test_resumed_run_refuses_to_seal_on_vacuous_provenance():
    ck = Checkpoint(key="k", run="run-1", stage="fetch_leaf",
                    input_hash="ih", payload={"queries": ["q"]})
    verdict, reason = seal_guard(_trace(), [ck], _Ledger())
    assert verdict == "REFUSE", f"sealed with zero verified provenance: {reason}"


def test_non_fetch_stage_without_fetches_is_fine():
    """decompose legitimately carries no fetch records — not a failure."""
    ck = Checkpoint(key="k", run="run-1", stage="decompose",
                    input_hash="ih", payload={"subquestions": ["a", "b"]})
    assert provenance_is_intact(_Ledger(), [ck]), \
        "a non-fetch stage was wrongly treated as missing provenance"


def test_legitimate_fetch_checkpoint_still_passes():
    body = "REAL BYTES"
    ck = Checkpoint(key="k", run="run-1", stage="fetch_leaf", input_hash="ih",
                    payload={"fetches": [{"body": body, "url": "https://ok",
                                          "content_sha256": _sha(body)}]})
    assert provenance_is_intact(_Ledger(), [ck])
