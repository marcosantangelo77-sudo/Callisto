"""Standing review, run 2 — 2026-08-23 (branch review/rotating-0823-150721).

Findings in findings/review_2026-08-23b.md. Convention from run 1:
defects are xfail(strict=True) so the suite stays green while the repro
provably fails for the stated reason; controls prove the xfails fail for
the RIGHT reason. Regression pins for fixed code are plain tests.

Reviewed here: a7b951e (C1+C2 resume anti-laundering), 96be252/ba0a63c
(six inflation paths -> floor_conf), and continuity re-checks of run 1's
R1/R2 against current master.
"""
import hashlib
import inspect

import pytest

from agp import Domain, Evidence, SourceClass
from agp.claims import AttachedEvidence, recompute_confidence
from agp.thresholds import floor_conf
from tools.pipeline.checkpoint import (
    Checkpoint,
    RunTrace,
    StageOutcome,
    seal_guard,
)
from tools.pipeline.engine import _trace_from_payload


# ── fixtures ───────────────────────────────────────────────────────────────

def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _ck(run, body, key):
    return Checkpoint(
        key=key, run=run, stage="fetch_leaf", input_hash="ih",
        payload={"fetches": [{"body": body, "url": "https://a/x",
                              "content_sha256": _sha(body)}]})


class _RecordingLedger:
    """Real-enough ledger: records what it is given, answers has_observation."""

    def __init__(self):
        self.records = []

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self.records.append((body, primary))

    def has_observation(self, body):
        return any(b == body for b, _ in self.records)


def _resumed_trace(run):
    t = RunTrace(run=run)
    t.stages.append(StageOutcome(stage="fetch_leaf", resumed=True,
                                 payload={}, produced_at="2026-08-23T00:00:00"))
    return t


def _attached(cls):
    ev = Evidence(content="x", source_class=cls, confidence_score=0.30,
                  domain=Domain.GENERAL, origin_agent="review")
    return AttachedEvidence(evidence=ev, assigned_class=cls)


# ── PIN-P1: C2 scope fix on the RESUMED branch (the shipped suite's gap) ────
#
# tests/test_redteam_c2_crossrun_laundering.py exercises only FRESH runs,
# where replay goes to the scratch ledger and the run filter is dead weight.
# On a RESUMED run the replay goes into the REAL ledger — the only branch
# where `checkpoints = [ck ... ck.run == trace.run]` is load-bearing. This
# pin passes at master and FAILS if that filter line is deleted (verified by
# simulation during review): without it, run A's bytes enter run B's ledger
# as PRIMARY and no shipped test notices.

FOREIGN = "RUN A SECRET BYTES"
OWN = "run b bytes"


class TestC2ScopePinOnResumedBranch:
    def test_foreign_checkpoints_never_enter_resumed_ledger(self):
        led = _RecordingLedger()
        store = [_ck("run-A", FOREIGN, "kA"), _ck("run-B", OWN, "kB")]
        verdict, reason = seal_guard(_resumed_trace("run-B"), store, led)
        assert verdict == "SEAL", reason
        bodies = [b for b, _ in led.records]
        assert FOREIGN not in bodies, (
            "deleting the seal_guard run-filter launders another run's "
            "evidence into this resumed run's ledger")
        assert OWN in bodies  # own evidence still earns its class

    def test_control_filter_present_in_source(self):
        # The thing P1 pins must actually exist; if someone deletes it this
        # control fails even before the behaviour pin above does.
        src = inspect.getsource(seal_guard)
        assert "ck.run == trace.run" in src


# ── R5: claims.recompute_confidence rounds claimed confidence UPWARD ───────
#
# ba0a63c's headline: six inflation paths, all closed by ONE shared
# quantiser "used by every site". The clamp inside agp/claims.py was not one
# of the six and still uses round(). round(min(max(claimed, .30), ceiling), 2)
# can land ABOVE claimed — at exactly the evidence ceiling. Verified live:
#   SECONDARY ceiling 0.75, claimed 0.7499 -> 0.75
#   INFERRED  ceiling 0.55, claimed 0.5451 -> 0.55
# Its docstring says "identical behavior to orchestrator's clamp" — the
# orchestrator-side clamp is precisely one of the paths that WAS floored.
# Reachable: Claim.attach() feeds evidence.confidence_score straight in.

class TestR5ClaimsClampRoundsUp:
    @pytest.mark.xfail(strict=True, reason="R5: claims clamp rounds up")
    @pytest.mark.parametrize("claimed,cls,ceiling", [
        (0.7499, SourceClass.SECONDARY, 0.75),
        (0.5451, SourceClass.INFERRED, 0.55),
    ])
    def test_clamped_confidence_never_exceeds_claimed(self,
                                                      claimed, cls, ceiling):
        out = recompute_confidence([_attached(cls)], claimed)
        assert out <= claimed, (
            f"clamp raised {claimed} to {out} (ceiling {ceiling}) with no "
            "new evidence — an automated actor raising confidence")

    def test_control_the_shared_quantiser_floors(self):
        # Proves R5 fails because of ROUND, not because raising is impossible:
        # the shared quantiser floors the same input downward.
        assert floor_conf(0.7499) == 0.74 < 0.7499

    def test_control_low_claims_pass_through_unraised(self):
        # A claim already below/at the band must come back unchanged;
        # documents that the path works when round() happens not to raise.
        out = recompute_confidence([_attached(SourceClass.INFERRED)], 0.54)
        assert out == 0.54


# ── R6 (carry-forward of run 1's R1, still open at 5e88b05): trace restore
#     never populates `admitted`, while its docstring claims it does ─────────

class TestR6ResumedTraceStillDropsAdmitted:
    @pytest.mark.xfail(strict=True, reason="R6/run-1 R1: admitted not restored")
    def test_trace_restore_populates_admitted(self):
        payload = {"fetches": [{"question_id": "q1",
                                "source_name": "openalex",
                                "url": "https://a/x", "body": "bytes",
                                "query": "q", "content_sha256": "d"}],
                   "rejections": [], "independent_keys": ["openalex"],
                   "queries": ["q"], "stop_reason": "info_gain"}
        tr = _trace_from_payload("q1", payload)
        assert tr.admitted, (
            "_trace_from_payload restores keys/queries/rejections but leaves "
            "admitted empty on every resumed run; docstring claims otherwise")

    def test_control_payload_actually_carries_fetches(self):
        # The data exists in the payload; only the restore drops it.
        payload = {"fetches": [{"source_name": "openalex"}],
                   "rejections": [], "independent_keys": [],
                   "queries": [], "stop_reason": ""}
        assert len(payload["fetches"]) == 1
