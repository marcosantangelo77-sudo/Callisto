"""Review pass 2026-08-23 — independent reproduction of two defects found while
auditing recent red-team fixes on this branch (ee549f8 K1, a7b951e C1-C4).

The reviewer owns only this file; no production code was edited.
Both tests below FAIL against current code — they are the deliverable.
"""
import hashlib
import json
import tempfile
from pathlib import Path

from tools.pipeline.checkpoint import (
    FileCheckpointer,
    RunTrace,
    StageOutcome,
    replay_ledger,
    seal_guard,
)
from tools.retrodiction.batch import BatchResult, build_report


class _FakeLedger:
    """Minimal ledger surface for the guard/replay path."""

    def __init__(self):
        self.bodies = set()

    def record_tool_result(self, tool, body, primary=True, urls=None):
        self.bodies.add(body)

    def has_observation(self, body):
        return body in self.bodies


def _resume_trace(run):
    return RunTrace(run=run, stages=[StageOutcome(
        stage="fetch_leaf", resumed=True, payload={},
        produced_at="2026-08-23T00:00:00+00:00")])


# ── Defect 1: the resume anti-laundering guard never verifies the HMAC ──────

def test_seal_guard_refuses_checkpoint_whose_signature_fails():
    """replay_ledger/seal_guard verify sha256(body)==content_sha256, but BOTH
    fields live inside the unsigned-by-check payload: an attacker (or a buggy
    writer) can rewrite the body AND recompute its hash on disk. Each
    checkpoint carries an HMAC over the canonical record (`sig`), and
    load_by_key faithfully loads it — yet nothing in the resume path ever
    calls verify_signature. Demonstrated end to end below: a checkpoint whose
    signature does NOT verify still yields SEAL."""
    tmp = tempfile.mkdtemp()
    store = FileCheckpointer(Path(tmp))
    run = "a" * 64
    store.save(run, "fetch_leaf", json.dumps({"qid": "q1"}),
               {"fetches": [{"body": "REAL BYTES", "url": "https://x/1",
                             "content_sha256":
                                 hashlib.sha256(b"REAL BYTES").hexdigest(),
                             "source_name": "openalex", "primary": True}]})
    trace = _resume_trace(run)

    # Tamper: rewrite body and its recorded hash; `sig` untouched -> now invalid.
    path = next(Path(tmp).rglob("*.json"))
    d = json.loads(path.read_text())
    assert d["sig"], "fixture must carry a signature to be meaningful"
    nb = "FABRICATED EVIDENCE"
    d["payload"]["fetches"][0]["body"] = nb
    d["payload"]["fetches"][0]["content_sha256"] = \
        hashlib.sha256(nb.encode()).hexdigest()
    path.write_text(json.dumps(d))

    ck = store.load(run, "fetch_leaf", json.dumps({"qid": "q1"}))
    # Sanity: the loaded record's signature really is invalid...
    from tools.pipeline.checkpoint import _harness_key
    key = _harness_key()
    if key:
        assert not ck.verify_signature(key), \
            "tampered record unexpectedly verifies"

    ledger = _FakeLedger()
    replay_ledger(ledger, [ck])
    verdict, _reason = seal_guard(trace, [ck], ledger)
    assert verdict != "SEAL", (
        "seal_guard SEALED a resumed run whose checkpoint signature does "
        "not verify — the HMAC exists but nothing checks it, so C1/C2 "
        "integrity reduces to attacker-writable fields")


# ── Defect 2: K1 fix left mean_brier/_verdict trusting unverifiable briers ──

def test_verdict_does_not_praise_batches_with_zero_real_ground_truth():
    """ee549f8 fixed _implied_outcome (truth-less rows are now excluded from
    bins and disclosed via n_no_truth) — correct as far as it goes. But
    mean_brier still averages r.brier over ALL scored rows regardless of
    whether any row carries answer_binary, and _verdict grades on that mean.
    Ten rows with fabricated briers and zero ground truth still produce the
    headline verdict 'strongly better than chance' — the exact forgery K1
    documented, surviving one level up in the same report."""
    rows = [BatchResult(question_id=str(i), status="scored",
                        predicted_probability=round(0.2 + 0.01 * i, 3),
                        answer_binary=None, brier=0.001)
            for i in range(10)]
    rep = build_report({r.question_id: r for r in rows})
    n_truth = sum(b["n"] for b in rep["calibration_overall"])
    assert n_truth == 0, "no row carries ground truth; no bin may score"
    verdict = rep["verdict"].lower()
    assert "better than chance" not in verdict, (
        f"a batch with ZERO ground-truth rows got verdict {rep['verdict']!r} "
        "— mean_brier over unverifiable briers still drives the headline")
