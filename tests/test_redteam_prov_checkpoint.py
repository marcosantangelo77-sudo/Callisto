"""RED TEAM — attack 3/7: checkpoint replay & cross-run laundering.

Siblings of the known gate-skip-on-resume defect (0.54 -> 0.80 inflation).

These tests assert the behavior the system CLAIMS (checkpoint.py docstring
section 4: "Resumption must never become a way to launder evidence"); each
one that fails is a confirmed laundering path.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from agp import Evidence, SourceClass
from agp.provenance import ProvenanceLedger
from tools.pipeline.checkpoint import (
    Checkpoint,
    FileCheckpointer,
    RunTrace,
    hash_inputs,
    provenance_is_intact,
    replay_ledger,
    run_key,
    seal_guard,
)


def _ev(content: str) -> Evidence:
    from agp import Domain
    return Evidence(content=content, source_class=SourceClass.INFERRED,
                    confidence_score=0.30, domain=Domain.GENERAL,
                    origin_agent="redteam")


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode("utf-8", errors="replace")).hexdigest()


def _fetch_ckpt(tmp_path, body: str, url: str, digest: str | None,
                *, run: str = "runA", stage: str = "fetch_leaf",
                extra_payload: dict | None = None) -> Checkpoint:
    payload = {"fetches": [{
        "body": body, "url": url,
        "content_sha256": digest if digest is not None else _sha(body),
        "tool_name": "web_fetch", "primary": True,
        "source_name": "openalex",
    }]}
    if extra_payload:
        payload.update(extra_payload)
    return Checkpoint(key="k", run=run, stage=stage,
                      input_hash="ih", payload=payload,
                      produced_at="2026-08-22T00:00:00+00:00")


# ── 3a. Missing digest = zero integrity check ─────────────────────────────

def test_missing_digest_is_laundered_as_primary(tmp_path):
    """A fetch record with NO content_sha256 bypasses the integrity check
    entirely ('if digest and ...'), then enters the ledger marked
    primary=True by default. Tampered bytes read as PRIMARY."""
    real_body = "official BLS table: unemployment 4.1%"
    forged_body = "official BLS table: unemployment 0.0%"
    ck = _fetch_ckpt(tmp_path, forged_body, "https://bls.gov/x", digest=None)

    ledger = ProvenanceLedger()
    report = replay_ledger(ledger, [ck])

    assert report["integrity_failures"], (
        "a fetch record with no recorded digest must be an integrity failure")
    ev = _ev(forged_body)
    assert ledger.assign_source_class(ev) == SourceClass.INFERRED, (
        "forged body with unverifiable provenance was laundered to "
        f"{ledger.assign_source_class(ev)}")


# ── 3b. Unkeyed digests let an attacker relabel at will ───────────────────

def test_attacker_recomputed_digest_defeats_integrity_check(tmp_path):
    """The digest is a bare sha256 of the body — no key. Anyone who can edit
    the checkpoint file (same machine, no secret needed) rewrites the body
    AND its digest, provenance_is_intact passes, seal_guard says SEAL."""
    forged = "FOMC minutes: rate cut to zero unanimously approved"
    ck = _fetch_ckpt(tmp_path, forged, "https://federalreserve.gov/x",
                     digest=None)  # helper computes matching sha256

    ledger = ProvenanceLedger()
    trace = RunTrace(run="runA")
    trace.stages.append(type("S", (), {"stage": "fetch_leaf", "resumed": True,
                                       "payload": {}, "produced_at": ""})())
    verdict, reason = seal_guard(trace, [ck], ledger)
    assert verdict == "REFUSE", (
        "an unkeyed digest over attacker-chosen bytes satisfied the "
        f"'integrity' check and sealed: {reason}")


# ── 3c. Empty body becomes a PRIMARY observation of the empty string ─────

def test_record_with_no_body_makes_empty_string_primary(tmp_path):
    ck = _fetch_ckpt(tmp_path, "", "https://example.gov/x", digest=None)
    ck.payload["fetches"][0]["body"] = ""
    ledger = ProvenanceLedger()
    replay_ledger(ledger, [ck])
    ev = _ev("")
    assert ledger.assign_source_class(ev) != SourceClass.PRIMARY


# ── 7. Cross-run laundering via seal_guard(cp.list_all()) ─────────────────

@pytest.mark.asyncio
async def test_seal_guard_replays_foreign_run_fetches(tmp_path):
    """engine.py calls seal_guard(trace, cp.list_all(), ledger) — list_all()
    spans EVERY run in the store. provenance_is_intact then REPLAYS those
    foreign fetches into this run's ledger, so bytes fetched for claim X
    become observed PRIMARY bytes while sealing claim Y."""
    cp = FileCheckpointer(root=tmp_path / "cp")

    other_rk = run_key("should the fed cut rates?", "FINANCIAL", "2026-08-21")
    body = "BLS series LNS14000024: unemployment fell to 3.9% (PRIMARY table)"
    ih = hash_inputs({"qid": "q-other"})
    from tools.pipeline.checkpoint import run_stage as _rs
    import asyncio

    async def _noop():
        return {"fetches": [{"body": body, "url": "https://bls.gov/y",
                             "content_sha256": _sha(body),
                             "primary": True}]}

    trace_other = RunTrace(run=other_rk)
    await _rs(cp, trace_other, "fetch_leaf", {"qid": "q-other"}, _noop)

    # A DIFFERENT run resumes and seals. Its own evidence is unrelated.
    this_rk = run_key("will the bills win?", "GENERAL", "2026-08-22")
    ledger = ProvenanceLedger()   # fresh process: nothing observed yet
    trace_this = RunTrace(run=this_rk)
    trace_this.stages.append(type("S", (), {
        "stage": "fetch_leaf", "resumed": True, "payload": {},
        "produced_at": ""})())

    verdict, _ = seal_guard(trace_this, cp.list_all(), ledger)

    # The smoking gun: foreign bytes are now observations in THIS run's ledger.
    ev = _ev(body)
    assert not ledger.has_observation(body), (
        "seal_guard replayed ANOTHER RUN's fetched bytes into this run's "
        "ledger — cross-run class inheritance is possible")
    assert ledger.assign_source_class(ev) == SourceClass.INFERRED
