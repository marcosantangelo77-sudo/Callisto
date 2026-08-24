"""RED TEAM — the source registry & query-builder seam: what happens when a
source LIES (non-200 with an error body) and when a CHECKPOINT lies
(inflated independence keys on resume).

Surface: tools/sources/base.py (RestSource._record) x tools/pipeline/retrieval.py
(_fetch_one) x tools/pipeline/engine.py (the _FetchRecorder replay) x
tools/pipeline/checkpoint.py (seal_guard / provenance_is_intact /
_trace_from_payload).

Method: adversarial constructions + corrupt-one-field replay (a recorded run
with ONE field — independent_keys — corrupted). Not previously used together
on this surface; distinct from the last pass's property sweep.

Contract under attack (the modules' own docstrings):
  P1  "RestSource.get_json ... records every successful body" — an ERROR
      response is not evidence; its bytes must never mint PRIMARY, and its
      URL must never verify a citation. (base.py module docstring;
      agp/provenance.py: "a real tool call returning real bytes".)
  P2  "sufficiency counts DISTINCT independent sources — two results from
      one publisher/index corroborate nothing" (retrieval.py header). A
      resumed run recomputes NOTHING less than a fresh one: a resumed run
      cannot beat the equivalent fresh run (resume-invariant family).
  P3  seal_guard: "resumed run may only seal when every checkpointed piece
      of evidence has verifiable provenance" — the guard must judge the
      SAME world the score was computed from, including the independence
      claim the requirement gate consumed.

Companion findings: findings/redteam_source_lies.md

STATUS ON MASTER: SL-1 and SL-2 FAIL (defects reproduced); SL-3 pins honest
behaviour. Run: python3 -m pytest tests/test_redteam_source_lies.py -q
"""
from __future__ import annotations

import hashlib
import json
import tempfile

import pytest

from agp.provenance import ProvenanceLedger
from tools.pipeline.engine import FetchResult, _FetchRecorder, _trace_from_payload
from tools.pipeline.retrieval import IterativeRetriever
from tools.sources.base import RestSource, SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry
from types import SimpleNamespace

import tools.pipeline.checkpoint as ckpt


def _spec(name="badsrc", answers=("semiconductor supply chains",)):
    return SourceSpec(
        name=name, base_url=f"https://{name}.example.org",
        description="test source", answers=answers,
        cannot_answer=("nothing",), tier=1)


class _Q:
    text = "semiconductor supply chain resilience"
    question_id = "q1"


# ── SL-1: a non-200 error body mints PRIMARY provenance ────────────────────

def test_sl1_error_body_never_mints_primary():
    """A source returns HTTP 502 WITH a body (API error JSON echoing the
    query). RestSource records the bytes as primary=True BEFORE the caller
    sees the status; _fetch_one raises on status != 200, but the scratch
    recorder already holds them — and the ENGINE replays every scratch call
    into the real ledger unconditionally (engine.py:737-738). Nothing ever
    supersedes a failed fetch: record_gate_rejection only fires on the
    gate-rejected (200-but-irrelevant) path."""
    spec = _spec()
    err_body = ('{"error":"quota exceeded while searching '
                'semiconductor supply chains"}')
    rec = _FetchRecorder()
    src = RestSource(spec, ledger=rec,
                     transport=lambda u, h: (502, err_body))
    # get_json parses the 502 body as JSON fine; the STATUS check lives in
    # the retriever, not here — so this call SUCCEEDS and hands back an
    # error payload with a clean FetchRecord. That is its own finding
    # (SL-1d): get_json itself never consults status.
    data, fr = src.get_json("https://badsrc.example.org/q?query=x")
    assert fr.status == 502
    # The scratch recorder holds the ERROR body as a PRIMARY observation...
    assert rec.calls, "precondition: failed fetch reached the recorder"
    assert rec.calls[0][2] is True

    # ...and the engine's replay loop (verbatim from engine.py:737) mints it:
    real = ProvenanceLedger()
    for tool, body, primary, urls in rec.calls:
        real.record_tool_result(tool, body, primary=primary, urls=urls or None)

    # INVARIANT P1: error bytes are not evidence.
    assert not real.is_primary_bytes(err_body), (
        "SL-1a: a 502 error BODY was minted PRIMARY in the real ledger — "
        "an API error message carries the same provenance weight as the "
        "document that was requested")
    assert "https://badsrc.example.org/q?query=x" not in real.observed_urls(), (
        "SL-1b: a URL whose fetch FAILED is registered as fetched, so any "
        "model text citing it verifies as SECONDARY via citation grounding")
    ev = SimpleNamespace(
        content="per https://badsrc.example.org/q?query=x the answer is yes",
        source_class=None)
    assert real.assign_source_class(ev).value != "SECONDARY", (
        "SL-1c: model prose merely CITING a failed URL earned SECONDARY "
        "(0.75 ceiling) instead of INFERRED (0.55)")


def test_sl1_engine_end_to_end_failed_source_laundering():
    """Same defect through the retriever exactly as the engine drives it:
    adapter raises after get_json recorded -> 'fail' branch -> scratch calls
    still replayed by the engine's leaf-order loop."""
    spec = _spec()
    err_body = '{"error":"upstream timeout semiconductor"}'

    def boom(**kw):
        raise RuntimeError("adapter exploded after fetch")

    reg = SourceRegistry()
    reg.register(SourceAdapter(spec, lambda s: SimpleNamespace(works_search=boom)))
    scratch = _FetchRecorder()
    r = IterativeRetriever(
        registry=reg, ledger=scratch,
        transport=lambda u, h: (200, err_body),  # 200 OK, adapter dies later
        use_planner=False,
        generic_calls={"badsrc": ("works_search", (), {"query": "term"})},
        max_rounds=1)
    trace = r.retrieve(_Q(), "semiconductor supply chains", min_independent=2)
    assert not trace.admitted

    # engine.py:737-738 replay, verbatim:
    real = ProvenanceLedger()
    for tool, body, primary, urls in scratch.calls:
        real.record_tool_result(tool, body, primary=primary, urls=urls or None)
    for content, urls in scratch.rejections:
        real.record_gate_rejection(content, urls)
    assert not real.has_observation(err_body) or \
        not real.is_primary_bytes(err_body), (
        "SL-1d: a fetch whose ADAPTER crashed still left its body minted "
        "PRIMARY in the real ledger")


# ── SL-2: a checkpoint lie about independence survives the seal guard ──────

def _lie_payload():
    body = json.dumps({"title": "some paper",
                       "abstract": "supply chain resilience semiconductor"})
    return {
        "fetches": [{
            "source_name": "openalex",
            "url": "https://api.openalex.org/works?query=x",
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "body": body, "parsed": json.loads(body),
            "question_id": "q1"}],
        "rejections": [],
        # THE LIE: one real fetch, TWO claimed independent voices.
        "independent_keys": ["api.openalex.org", "api.semanticscholar.org"],
        "queries": ["x"],
        "stop_reason": "sufficient: 2 independent sources >= required 2"}


def test_sl2_resume_trusts_checkpointed_independence_verbatim():
    """_trace_from_payload restores independent_keys from editable JSON with
    zero cross-checks against the fetch records being restored beside them.
    On the DOCUMENTED DEFAULT deployment (_harness_key() -> None) the
    checkpoint signature layer never runs, so nothing authenticates the
    payload either."""
    payload = _lie_payload()
    trace = _trace_from_payload("q1", payload)
    n_indep = len(trace.independent_keys)
    assert n_indep <= len(payload["fetches"]), (
        f"SL-2a: restored trace claims {n_indep} independent voices from "
        f"{len(payload['fetches'])} fetch record(s); nothing recomputed or "
        "even bounded the claim")


def test_sl2_seal_guard_seals_over_the_lie():
    """Full guard path: the bodies verify (digest matches, replay mints
    PRIMARY), provenance_is_intact -> True, seal_guard -> SEAL. Yet the
    requirement gate consumed n_indep=2 to reach this point — one real
    fetch satisfied min_independent_sources=2 on a RESUMED run while the
    identical fresh run could not."""
    cp = ckpt.FileCheckpointer(root=tempfile.mkdtemp())
    payload = _lie_payload()
    ck = cp.save("run-lie", "fetch_leaf", "inp", payload)

    ledger = ProvenanceLedger()
    report = ckpt.replay_ledger(ledger, [ck])
    assert not report["integrity_failures"]          # bodies verify...

    tr = ckpt.RunTrace(run="run-lie")
    tr.stages.append(ckpt.StageOutcome(
        stage="fetch_leaf", resumed=True, payload=payload,
        produced_at=ck.produced_at))
    assert tr.is_resume
    verdict, reason = ckpt.seal_guard(tr, [ck], ledger)
    assert verdict == "REFUSE", (
        f"SL-2b: seal_guard returned {verdict} over a resumed run whose "
        f"independence claim (2 voices) is unsupported by its own fetch "
        f"records (1); the guard verifies bodies but not the count the "
        f"requirement gate consumed")

    # And the engine's answer stage would compute:
    assert len(trace_from(payload).independent_keys) == 2, (
        "SL-2c: requirement gate sees min_independent_sources=2 SATISFIED "
        "on one real fetch — impossible for the equivalent fresh run")


def trace_from(payload):
    return _trace_from_payload("q1", payload)


# ── SL-3: honest-negative pins (these MUST keep passing) ───────────────────

def test_sl3_gate_rejection_still_supersedes_on_replay():
    """The R4/R4b fix works when the ORDERED replay runs: admit-class bytes
    rejected by the gate end superseded in the real ledger."""
    rec = _FetchRecorder()
    body = '{"results":[{"title":"cooking recipes"}]}'
    rec.record_tool_result("badsrc_fetch", body, primary=True,
                           urls=["https://u/1"])
    rec.record_gate_rejection(body, ["https://u/1"])
    real = ProvenanceLedger()
    for tool, b, primary, urls in rec.calls:
        real.record_tool_result(tool, b, primary=primary, urls=urls or None)
    for content, urls in rec.rejections:
        real.record_gate_rejection(content, urls)
    assert real.superseded(body, "https://u/1")
    assert not real.is_primary_bytes(body)
    assert not real.cites_verified_url("see https://u/1 for proof")


def test_sl3_signed_checkpoint_rename_detected(monkeypatch):
    """Under a KEYED deployment the D2/D4 defences hold: a tampered payload
    fails HMAC verification and partition_admissibility rejects it (which
    makes seal_guard REFUSE). The SL-2 hole is the UNKEYED default."""
    monkeypatch.setenv("CALLISTO_SEAL_KEY", "k")
    cp = ckpt.FileCheckpointer(root=tempfile.mkdtemp())
    payload = _lie_payload()
    cp.save("run-k", "fetch_leaf", "inp", payload)
    ck = cp.load_by_key("run-k", ckpt.step_key("run-k", "fetch_leaf", "inp"))
    ck.payload["independent_keys"] = ["fake", "fake2"]
    ok, rej = ckpt.partition_admissibility("run-k", [ck], key="k")
    assert rej and not ok, "tampered keyed checkpoint must be inadmissible"


def test_sl3_fresh_run_cannot_claim_more_voices_than_fetches():
    """The FRESH path computes keys from actual specs — the invariant the
    resume path must match (differential anchor)."""
    from tools.pipeline.retrieval import RetrievalTrace, independence_key
    t = RetrievalTrace(question_id="q")
    t.independent_keys.add(independence_key("openalex",
                                            "https://api.openalex.org"))
    assert len(t.independent_keys) == 1
