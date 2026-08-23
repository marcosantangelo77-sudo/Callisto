"""RED TEAM — checkpointing & resume (method B: DIFFERENTIAL).

Claim under attack (tools/pipeline/checkpoint.py header, contract 4):
"Resumption must never become a way to launder evidence whose provenance was
lost — when we cannot guarantee provenance, we refuse to seal rather than seal
something unverifiable."  And engine.py's header: "ASYMMETRY: every confidence
adjustment in this file is min(...) or minus."

Method: differential. A live run and the resumed run that replays its
checkpoints must earn IDENTICAL provenance and identical guard verdicts from
identical bytes. Any input an on-disk attacker (or a corrupted file) can
change that flips the resumed run's outcome while leaving the live run's
outcome fixed is a divergence — a bug in the resume path by definition.

The four prior checkpoint red-team passes (c1-c4) each patched ONE named hole.
None of them ran the general differential claim above. These tests do.

Findings (all demonstrated failing below; honest negatives pinned at end):
  D1  The checkpoint HMAC is never verified anywhere in the load/replay/seal
      path — and with no CALLISTO_*_KEY configured the record is saved
      UNSIGNED entirely. Rewriting body AND its recorded content_sha256 in
      the JSON file launders arbitrary fabricated bytes to PRIMARY across
      the resume boundary; seal_guard returns SEAL. The signature is
      decorative: save() signs, nothing checks.
  D2  _is_fetch_stage() keys the mandatory-fetches rule on the stage NAME
      string ("fetch" in stage). Renaming the stage in the checkpoint file
      to "decompose" hides fetch records from the vacuous-payload check
      (the C3 fix) while replay_ledger still happily mints PRIMARY bytes
      from them. The name of a stage is attacker-controlled state on disk.
  D3  seal_guard filters checkpoints by ck.run == trace.run (the C2 fix),
      but engine.py:498 replays each leaf checkpoint into the live ledger
      via load_by_key WITHOUT any run/sig/integrity filter. Relabelling
      ck.run in the file makes the guard blind to the record while the
      ledger absorbs it anyway: the guard reasons over a different world
      than the one the seal will cover.
Companion findings: findings/redteam_resume_differential.md
"""
from __future__ import annotations

import hashlib
import json

import pytest

from agp.provenance import ProvenanceLedger
from tools.pipeline.checkpoint import (
    Checkpoint,
    FileCheckpointer,
    RunTrace,
    hash_inputs,
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
    tr.stages.append(type("S", (), {"stage": stage, "resumed": True,
                                    "payload": {}, "produced_at": ""})())
    return tr


def _save_fetch_cp(tmp_path, rk, payload, stage="fetch_leaf",
                   qid="a", monkeypatch_env=None):
    if monkeypatch_env is not None:
        monkeypatch_env()
    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(rk, stage, hash_inputs({"qid": qid}), payload)
    return cp


# ── D1: unsigned / unverified checkpoints launder fabricated bytes ────────

def test_d1a_unkeyed_save_writes_no_signature(tmp_path, monkeypatch):
    """With no key configured (the documented default deployment), the
    anti-tamper mechanism does not exist at all."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = _rk("unsigned")
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    d = json.loads(next((cp.root / rk[:16]).glob("*.json")).read_text())
    assert not d.get("sig"), (
        "checkpoint stored unsigned: on-disk tampering is undetectable")


def test_d1b_tampered_body_with_rehashed_digest_seals_as_primary(
        tmp_path, monkeypatch):
    """THE differential break: live run fetched real bytes; the file on disk
    is edited to carry fabricated bytes whose digest was recomputed to match.
    The resumed run must refuse (provenance lost) but instead seals them as
    PRIMARY — even under a keyed deployment, because NOTHING ever calls
    verify_signature() on the load path."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "prod-secret-key")
    rk = _rk("tamper")
    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}), {"fetches": [_rec()]})

    # on-disk attacker edits the JSON file directly
    p = next((cp.root / rk[:16]).glob("*.json"))
    d = json.loads(p.read_text())
    assert d["sig"], "premise: keyed save must sign"
    evil = '{"fabricated": "never fetched from anywhere"}'
    d["payload"]["fetches"][0]["body"] = evil
    d["payload"]["fetches"][0]["content_sha256"] = \
        hashlib.sha256(evil.encode()).hexdigest()
    p.write_text(json.dumps(d))

    ck = cp.load_by_key(rk, step_key(rk, "fetch_leaf",
                                     hash_inputs({"qid": "a"})))
    assert not ck.verify_signature("prod-secret-key"), (
        "premise: tampered record fails signature check — yet everything "
        "downstream proceeds anyway")

    ledger = ProvenanceLedger()
    report = replay_ledger(ledger, [ck])
    assert report["replayed"] == 1 and report["integrity_failures"] == [], (
        "replay accepted a record whose own HMAC fails")
    assert ledger.is_primary_bytes(evil), (
        "fabricated bytes minted as PRIMARY provenance")
    verdict, reason = seal_guard(_resumed_trace(rk), [ck], ledger)
    assert verdict == "REFUSE", (
        f"seal_guard sealed over forged evidence: {verdict} {reason}")


# ── D2: stage-name string matching hides fetch records from the guard ─────

def test_d2_stage_rename_evades_mandatory_fetch_check(tmp_path, monkeypatch):
    """_is_fetch_stage greps the stage NAME. The C3 fix ('fetch-stage
    checkpoint without a fetches key => not intact') only fires for stages
    whose name contains 'fetch'. A checkpoint FILE can be renamed/stored
    under any stage label; replay_ledger never looks at the stage name, so
    its fetch records still mint PRIMARY bytes while the guard's structural
    check never sees them."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("stagehide")
    cp = FileCheckpointer(root=tmp_path / "ck")
    # store genuine fetch records under a non-fetch stage label
    cp.save(rk, "decompose", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    ck = cp.load_by_key(rk, step_key(rk, "decompose", hash_inputs({"qid": "a"})))

    ledger = ProvenanceLedger()
    rep = replay_ledger(ledger, [ck])
    assert rep["replayed"] == 1
    # the record's OWN stage field still says decompose, so the C3
    # mandatory-structure check cannot apply...
    verdict, reason = seal_guard(_resumed_trace(rk, stage="decompose"),
                                 [ck], ledger)
    # ...and the guard seals even though the ONLY provenance-bearing record
    # in this run lives outside every check designed to cover it.
    # For the invariant to hold this must be REFUSE whenever a non-fetch-
    # stage checkpoint carries unverified fetch records.
    assert verdict == "REFUSE", (
        f"fetch records smuggled under a non-fetch stage name sealed: "
        f"{verdict} {reason}")


def test_d2b_stage_field_is_not_authenticated_either(tmp_path, monkeypatch):
    """Even the stage recorded INSIDE the signed payload is just a string;
    renaming it changes both the guard's rule-set and the filename glob,
    while step lookup by key still finds the record."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("stageren")
    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}), {"fetches": []})
    p = next((cp.root / rk[:16]).glob("*.json"))
    d = json.loads(p.read_text())
    d["stage"] = "synthesis"           # rename in-file
    new_name = f"synthesis.{d['key'][:24]}.json"
    (cp.root / rk[:16] / new_name).write_text(json.dumps(d))
    p.unlink()

    ck = cp.load_by_key(rk, d["key"])
    assert ck is not None and ck.stage == "synthesis"
    # the record is now invisible to every _is_fetch_stage rule everywhere


# ── D3: the guard and the ledger see different checkpoint sets ────────────

def test_d3_run_relabel_blinds_guard_but_not_the_ledger(
        tmp_path, monkeypatch):
    """engine.py replays each leaf checkpoint into self.ledger via
    load_by_key(...) with NO run filter and NO integrity check; seal_guard
    then judges only checkpoints whose .run matches THIS run. Relabel
    ck.run on disk and the guard stops looking at a record whose bytes the
    ledger has already absorbed — the seal covers a world the guard never
    inspected."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("crossrun")
    other = _rk("other-question-run")

    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(other, "fetch_leaf", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    ck = cp.load_by_key(other, step_key(other, "fetch_leaf",
                                        hash_inputs({"qid": "a"})))

    # exactly what engine.py:493-498 does on every run:
    ledger = ProvenanceLedger()
    replay_ledger(ledger, [ck])
    assert ledger.is_primary_bytes('{"real": 1}')

    # what seal_guard does: scope by run, judge the remainder
    verdict, _ = seal_guard(RunTrace(run=rk), [ck], ledger)
    assert verdict == "REFUSE", (
        "guard scoped away the foreign-run checkpoint, but the ledger had "
        "already absorbed its bytes — guard and seal disagree about which "
        "evidence exists")


def test_d3b_guard_verdict_and_ledger_state_diverge_on_integrity_failure(
        tmp_path, monkeypatch):
    """Same divergence without relabelling: a corrupt record is skipped by
    replay (integrity failure -> bytes NOT in ledger) but seal_guard on a
    FRESH trace only checks... nothing, because fresh runs pass an empty
    checkpoint list through the ScratchLedger branch only when checkpoints
    are handed to it — engine.py always hands cp.list_all(). Verify the
    fresh-path refusal actually triggers on corrupt files."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("freshcorrupt")
    cp = FileCheckpointer(root=tmp_path / "ck")
    rec = _rec()
    rec["content_sha256"] = "0" * 64          # corrupt digest
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}), {"fetches": [rec]})
    ck = cp.load_by_key(rk, step_key(rk, "fetch_leaf",
                                     hash_inputs({"qid": "a"})))
    verdict, reason = seal_guard(RunTrace(run=rk), [ck], ProvenanceLedger())
    assert verdict == "REFUSE", reason


# ── honest negatives (regression pins) ────────────────────────────────────

def test_neg_load_survives_unreadable_file(tmp_path):
    """Corrupt JSON on disk is a cache MISS, never a crash or a partial."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = _rk("neg1")
    d = cp.root / rk[:16]
    d.mkdir(parents=True)
    (d / "fetch_leaf.deadbeef.json").write_text("{broken")
    assert cp.load_by_key(rk, "deadbeef") is None


def test_neg_replay_dedup_prevents_double_primary(tmp_path):
    """Replaying the same digest twice yields one observation."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = _rk("neg2")
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    ck = cp.load_by_key(rk, step_key(rk, "fetch_leaf",
                                     hash_inputs({"qid": "a"})))
    led = ProvenanceLedger()
    replay_ledger(led, [ck])
    rep = replay_ledger(led, [ck])
    assert rep["skipped_duplicates"] == 1 and len(led._by_hash) == 1


def test_neg_body_hash_mismatch_still_refuses(tmp_path):
    """The C1 fix holds where it looks: digest mismatch refuses."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = _rk("neg3")
    rec = _rec()
    rec["content_sha256"] = "0" * 64
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}), {"fetches": [rec]})
    ck = cp.load_by_key(rk, step_key(rk, "fetch_leaf",
                                     hash_inputs({"qid": "a"})))
    led = ProvenanceLedger()
    rep = replay_ledger(led, [ck])
    assert rep["integrity_failures"] and not led._by_hash
