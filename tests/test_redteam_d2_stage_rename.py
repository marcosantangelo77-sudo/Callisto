"""D2 — the mandatory-fetches rule keyed on the stage NAME, which is
attacker-controlled state on disk.

_is_fetch_stage() used `"fetch" in stage`. `stage` is a plain string in an
editable JSON file, so renaming a fetch-bearing checkpoint to "decompose"
hid its records from every structural check while replay_ledger (which
reads only the payload) still minted their bytes PRIMARY — and seal_guard
sealed. A label is not evidence.

Fix under test: _is_fetch_stage is CONTENT-BASED. Any checkpoint whose
payload carries fetch records is subject to the provenance rules whatever
it is named; and a genuinely fetch-named stage still owes its `fetches`
key (the C3 rule).
"""
import hashlib

import pytest

from agp.provenance import ProvenanceLedger
from tools.pipeline.checkpoint import (
    Checkpoint,
    FileCheckpointer,
    RunTrace,
    StageOutcome,
    hash_inputs,
    provenance_is_intact,
    replay_ledger,
    run_key,
    seal_guard,
    step_key,
)


def _rk(q="Q"):
    return run_key(q, "GENERAL", "2026-08-23")


def _rec(body='{"real": 1}', url="http://x/1"):
    return {
        "source_name": "openalex", "tool_name": "openalex_fetch",
        "url": url, "body": body,
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "primary": True,
    }


def _resumed_trace(rk, stage="fetch_leaf"):
    tr = RunTrace(run=rk)
    tr.stages.append(StageOutcome(stage=stage, resumed=True,
                                  payload={}, produced_at=""))
    return tr


def test_d2_stage_rename_evades_mandatory_fetch_check(tmp_path, monkeypatch):
    """The ported reproduction: fetch records stored under a non-fetch stage
    label are invisible to the OLD name-based rule. With the content-based
    rule they are judged like any fetch checkpoint — an unverifiable record
    (corrupt digest) must refuse the seal instead of minting PRIMARY bytes."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("stagehide")
    cp = FileCheckpointer(root=tmp_path / "ck")
    rec = _rec()
    rec["content_sha256"] = "0" * 64          # body no longer matches digest
    cp.save(rk, "decompose", hash_inputs({"qid": "a"}), {"fetches": [rec]})
    ck = cp.load_by_key(rk, step_key(rk, "decompose",
                                     hash_inputs({"qid": "a"})))

    ledger = ProvenanceLedger()
    rep = replay_ledger(ledger, [ck])
    assert rep["replayed"] == 0 and rep["integrity_failures"], (
        "premise: corrupt record fails integrity")

    verdict, reason = seal_guard(_resumed_trace(rk, stage="decompose"),
                                 [ck], ledger)
    assert verdict == "REFUSE", (
        f"fetch records smuggled under a non-fetch stage name sealed: "
        f"{verdict} {reason}")


def test_d2e_renamed_stage_with_intact_records_still_mints_and_verifies(
        tmp_path):
    """The content rule is about COVERAGE, not punishment: a renamed stage
    whose records genuinely verify passes provenance_is_intact — the same
    verdict the identical payload would earn under its real fetch name."""
    import tempfile, pathlib
    rk = _rk("stageok")
    cp = FileCheckpointer(root=pathlib.Path(tempfile.mkdtemp()) / "ck")
    cp.save(rk, "decompose", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    ck = cp.load_by_key(rk, step_key(rk, "decompose",
                                     hash_inputs({"qid": "a"})))
    ledger = ProvenanceLedger()
    assert replay_ledger(ledger, [ck])["replayed"] == 1

    tr = RunTrace(run=rk)
    tr.stages.append(StageOutcome(stage="decompose", resumed=True,
                                  payload={}, produced_at=""))
    verdict, reason = seal_guard(tr, [ck], ledger)
    assert verdict == "SEAL", (
        f"intact renamed-stage evidence refused: {reason}")


def test_d2f_signature_covers_stage_rename_but_nothing_verifies_it():
    """The C4 HMAC covers `stage`, so a rename IS detectable IN PRINCIPLE —
    but only in a keyed deployment, and nothing on the load/replay/seal path
    calls verify_signature() (that seam is D1). The content rule stays as the
    always-on floor; this pin documents the keyed-regime layer."""
    import os
    ck = Checkpoint(key="k", run="r", stage="fetch_leaf", input_hash="ih",
                    payload={"fetches": []})
    signed = ck.signed("test-key")
    d = dict(signed.to_dict())
    d["stage"] = "decompose"                    # rename in-file
    renamed = Checkpoint.from_dict(d)
    assert not renamed.verify_signature("test-key"), (
        "HMAC must fail on a renamed stage")
    # ...and with no key configured there is no signature at all:
    assert not Checkpoint.from_dict(d).verify_signature(""), (
        "unkeyed regime has no authentication; content rule is the floor")


def test_d2b_content_rule_catches_smuggled_records_directly(tmp_path):
    """The predicate itself, without any ledger: a payload carrying fetches
    is a fetch checkpoint regardless of what the file calls itself."""
    ck = Checkpoint(key="k", run="r", stage="decompose", input_hash="ih",
                    payload={"fetches": [{"body": "b"}]})
    from tools.pipeline.checkpoint import _is_fetch_stage
    assert _is_fetch_stage(ck), (
        "payload content, not the stage label, must drive the rule")


def test_d2c_nonfetch_stage_without_fetches_still_clean():
    """No false positives: decompose legitimately has no fetch records."""
    ck = Checkpoint(key="k", run="r", stage="decompose", input_hash="ih",
                    payload={"subquestions": ["a"]})
    assert provenance_is_intact(type("L", (), {
        "record_tool_result": lambda *a, **k: None,
        "has_observation": lambda self, b: True,
    })(), [ck])


def test_d2d_legitimate_fetch_checkpoint_under_odd_name_passes(tmp_path):
    """A fetch-bearing checkpoint with an odd name verifies fine when its
    bytes are actually in the ledger — the rule is about coverage, not
    punishment."""
    body = '{"real": 1}'
    ck = Checkpoint(key="k", run="r", stage="synthesis_step",
                    input_hash="ih",
                    payload={"fetches": [{"body": body, "url": "https://ok",
                                          "content_sha256":
                                          hashlib.sha256(
                                              body.encode()).hexdigest()}]})

    class L:
        def __init__(self): self.b = set()
        def record_tool_result(self, t, body, primary=True, urls=None):
            self.b.add(body)
        def has_observation(self, body): return body in self.b

    led = L()
    assert replay_ledger(led, [ck])["replayed"] == 1
    assert provenance_is_intact(led, [ck])
