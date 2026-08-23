"""RED TEAM D3 (ported) — seal_guard and the ledger inspect different worlds.

engine.py replays each leaf's checkpoint into the live ledger via
load_by_key with NO run filter, NO signature check, NO integrity check;
seal_guard then scopes cp.list_all() to ck.run == trace.run before judging.
Relabeling ck.run in the file makes the guard blind to a record whose bytes
the ledger has already absorbed. Fix (one codepath): admissible_checkpoints()
is THE single predicate — run scope + verified HMAC — consumed by BOTH
seal_guard() and engine.py's replay path. A signature that fails is never
replayed into the ledger at all.
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
    admissible_checkpoints,
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


# ── D3: the guard and the ledger see different checkpoint sets ────────────

def test_d3_run_relabel_blinds_guard_but_not_the_ledger(
        tmp_path, monkeypatch):
    """engine.py replays each leaf checkpoint into self.ledger via
    load_by_key(...) with NO run filter; seal_guard judges only checkpoints
    whose .run matches THIS run. Relabel ck.run on disk and the guard stops
    looking at a record whose bytes the ledger has already absorbed. With the
    fix, the replay path applies the SAME shared predicate, so a record the
    guard cannot see never enters the ledger either."""
    monkeypatch.delenv("CALLISTO_CUTOFF_KEY", raising=False)
    monkeypatch.delenv("CALLISTO_SEAL_KEY", raising=False)
    rk = _rk("crossrun")
    other = _rk("other-question-run")

    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(other, "fetch_leaf", hash_inputs({"qid": "a"}),
            {"fetches": [_rec()]})
    ck = cp.load_by_key(other, step_key(other, "fetch_leaf",
                                        hash_inputs({"qid": "a"})))

    # exactly what engine.py does now: replay only what the SHARED
    # predicate admits — same function seal_guard calls.
    ledger = ProvenanceLedger()
    admissible = admissible_checkpoints(rk, [ck])
    assert admissible == [], (
        "foreign-run checkpoint must be inadmissible for this run")
    if admissible:
        replay_ledger(ledger, admissible)
    assert not ledger.is_primary_bytes('{"real": 1}'), (
        "ledger absorbed a checkpoint the guard would scope away")

    verdict, _ = seal_guard(RunTrace(run=rk), [ck], ledger)
    # THE INVARIANT: guard and ledger agree about which evidence exists.
    # A SEAL is only legitimate when the ledger holds none of the bytes
    # the guard scoped away; before the fix the ledger had already
    # absorbed '{"real": 1}' while the guard returned SEAL.
    assert verdict != "SEAL" or not ledger.is_primary_bytes('{"real": 1}'), (
        "guard sealed while the ledger held evidence it never inspected "
        "— guard and seal disagree about which evidence exists")


def test_d3b_guard_verdict_and_ledger_state_diverge_on_integrity_failure(
        tmp_path, monkeypatch):
    """Same divergence without relabelling: a corrupt record is skipped by
    replay (integrity failure -> bytes NOT in ledger) but seal_guard on a
    FRESH trace must refuse over it."""
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


def test_d3c_bad_signature_checkpoint_is_never_replayed(tmp_path, monkeypatch):
    """THE keyed-regime half of D3: under CALLISTO_SEAL_KEY a checkpoint
    whose bytes were edited on disk fails its HMAC. It must be inadmissible
    through the SAME shared predicate seal_guard uses, so replay_ledger
    never mints its bytes PRIMARY — one codepath decides what evidence the
    run has."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "prod-secret-key")
    rk = _rk("d3c")
    cp = FileCheckpointer(root=tmp_path / "ck")
    cp.save(rk, "fetch_leaf", hash_inputs({"qid": "a"}), {"fetches": [_rec()]})

    # on-disk attacker edits the body AND recomputes content_sha256
    p = next((cp.root / rk[:16]).glob("*.json"))
    d = json.loads(p.read_text())
    evil = '{"fabricated": true}'
    d["payload"]["fetches"][0]["body"] = evil
    d["payload"]["fetches"][0]["content_sha256"] = \
        hashlib.sha256(evil.encode()).hexdigest()
    p.write_text(json.dumps(d))

    ck = cp.load_by_key(rk, d["key"])
    assert not ck.verify_signature("prod-secret-key"), "premise"

    # the ONE predicate both consumers share:
    assert admissible_checkpoints(rk, [ck]) == [], (
        "HMAC-failing checkpoint is inadmissible everywhere")

    # ...so the engine-style replay cannot absorb the fabricated bytes...
    ledger = ProvenanceLedger()
    admissible = admissible_checkpoints(rk, [ck])
    if admissible:
        replay_ledger(ledger, admissible)
    assert not ledger.is_primary_bytes(evil)

    # ...and the guard refuses rather than sealing the unverifiable.
    verdict, reason = seal_guard(
        RunTrace(run=rk), [ck], ledger)
    # trace reports no resumed stages -> fresh path still checks integrity
    # via the scratch-ledger branch; either way no SEAL over forged bytes.
    tr = RunTrace(run=rk)
    tr.stages.append(type("S", (), {"stage": "fetch_leaf", "resumed": True,
                                    "payload": {}, "produced_at": ""})())
    verdict, reason = seal_guard(tr, [ck], ledger)
    assert verdict == "REFUSE", f"sealed over forged evidence: {reason}"
