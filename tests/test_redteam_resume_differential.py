"""RED TEAM — checkpointing and resume (method B: differential).

Question under attack: "a resumed run is the same run, honestly continued —
and resumption is never a way to launder evidence or to weaken a gate."

METHOD — differential, rotating both surface and style from the last pass
(confidence scoring attacked with property sweeps + adversarial
constructions): drive the SAME logical question through three executions
that must agree —

    plain live  /  fresh with a checkpointer  /  resumed from checkpoints

— and drive two DIFFERENT questions through ONE shared checkpointer, which
must NOT interact. Every divergence is a defect in one of the paths.

FINDINGS (each has a failing test below):

  R1  A fetch record whose content_sha256 is missing/empty is replayed into
      the ledger as PRIMARY provenance with NO verification, and
      provenance_is_intact() then circularly validates those very bytes.
      Attacker-chosen bytes cross the resume boundary at ceiling 1.0 — the
      exact act checkpoint.py contract 4 swears cannot happen.
  R2  seal_guard() receives cp.list_all(): every checkpoint EVER written by
      ANY run sharing the store. One bit-rotted body from a FINISHED run
      refuses every fresh run's seal until GC (default 30 days), with a
      reason string blaming THIS run's evidence.
  R3  After a terminal outcome (adversary veto), re-running the same
      question the same day hits every stage cache — zero model calls, zero
      fetches — and re-rolls ONLY the stochastic critic until it passes.
      Nothing marks checkpoints consumed, nothing discloses the attempt.
      Sampling-until-seal is an automated weakening of the veto gate.
  R4a A fresh run WITH a checkpointer adds every leaf's evidence TWICE to
      the sealed session (engine _answer_leaf + unconditional restore);
      a resumed run adds it ONCE. Three execution modes, two different
      sealed evidence_counts, one invariant broken.
  R4b Resume rebuilds Evidence without its original timestamp, so the
      sealed session claims OLD evidence was acquired NOW — contradicting
      module contract 2 ("resume semantics that do not lie").
  R5  GC promises never to delete an open claim's checkpoint; no production
      construction wires is_claim_open, so the promise is decorative and
      open-claim checkpoints are silently aged out.
  R6  A legacy/hand-edited checkpoint with a naive produced_at crashes
      gc() wholesale (aware-vs-naive TypeError) instead of being skipped.

Honest negatives (pass, kept as regression pins) are at the bottom.

Run: python3 -m pytest tests/test_redteam_resume_differential.py -q
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.checkpoint import (  # noqa: E402
    Checkpoint,
    FileCheckpointer,
    RunTrace,
    hash_inputs,
    provenance_is_intact,
    replay_ledger,
    run_key,
    seal_guard,
    step_key,
)
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402

UTC = timezone.utc
TODAY = date(2026, 8, 23)

QUESTION = ("What does recent scholarly research say about semiconductor "
            "supply chain resilience?")
OTHER_QUESTION = "An entirely different question about medieval wool trade routes"


# ── harness ────────────────────────────────────────────────────────────────

def _fetch_record(source="openalex", url="https://api.openalex.org/works?x=1",
                  body='{"results": [{"id": "W1"}]}', digest=None, **extra):
    rec = {
        "source_name": source, "tool_name": f"{source}_fetch",
        "url": url, "body": body,
        "content_sha256": (hashlib.sha256(body.encode()).hexdigest()
                           if digest is None else digest),
        "primary": True,
    }
    rec.update(extra)
    return rec


def _save_fetch_ckpt(cp, rk, records, ih="ih"):
    return cp.save(rk, "fetch_leaf", ih, {"fetches": records})


def _resumed_trace(rk):
    tr = RunTrace(run=rk)

    class _S:
        stage = "fetch_leaf"
        resumed = True
        payload = {}
        produced_at = ""

    tr.stages.append(_S())
    return tr


class QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _decompose_reply():
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2, "min_independent_sources": 1}]})


def _answer_reply(conf=0.80):
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


_OPENALEX_BODY = json.dumps({"results": [
    {"id": "W1", "title": "semiconductor supply chain study",
     "publication_year": 2025, "cited_by_count": 12}]})
_ROUTES = {"/works": _OPENALEX_BODY,
           "/graph/v1/paper/search": json.dumps({"data": []})}


def _pipeline(tmp_path, *, model=None, adversary=None, transport=None,
              ledger=None, checkpointer=None):
    return ResearchPipeline(
        model=model or ScriptedModel({
            "Architect": [{"content": _decompose_reply()}],
            "Manager": [{"content": _answer_reply()}]}),
        adversary_router=adversary or QuietAdversary(),
        transport=transport or fixture_transport(dict(_ROUTES)),
        store=ArtifactStore(root=tmp_path / "artifacts"),
        ledger=ledger or ProvenanceLedger(),
        checkpointer=checkpointer)


# ── R1: digest-less fetch records replay as unverified PRIMARY ─────────────

@pytest.mark.parametrize("digest", [None, ""],
                         ids=["missing_field", "empty_string"])
def test_R1_digestless_fetch_record_launderers_into_primary(tmp_path, digest):
    """A checkpoint payload whose fetch record lost (or never had) a verifiable
    digest must NOT be able to mint PRIMARY provenance. Today it does — and
    the anti-laundering oracle then validates the laundered bytes against
    themselves."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = run_key("root q", "GENERAL", "2026-08-23")
    evil = ("ATTACKER BYTES: the moon is made of sell-side research "
            "(never fetched from anywhere)")
    rec = _fetch_record(body=evil)
    if digest is None:
        del rec["content_sha256"]
    else:
        rec["content_sha256"] = digest
    ck = _save_fetch_ckpt(cp, rk, [rec])

    ledger = ProvenanceLedger()
    report = replay_ledger(ledger, [ck])

    # The replay must refuse unverifiable bytes, not enshrine them.
    assert report["integrity_failures"], (
        "a fetch record with no digest was replayed without verification")
    assert not ledger.is_primary_bytes(evil), (
        "unverified checkpoint bytes became PRIMARY provenance")
    assert not provenance_is_intact(ledger, [ck])
    verdict, _ = seal_guard(_resumed_trace(rk), [ck], ledger)
    assert verdict == "REFUSE", (
        "seal_guard blessed evidence whose provenance was never checked")


def test_R1_digestless_record_defaults_to_primary_true(tmp_path):
    """Even the class it mints defaults upward: replay_ledger does
    primary=bool(rec.get('primary', True)). An absent flag means PRIMARY."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = run_key("root q", "GENERAL", "2026-08-23")
    rec = _fetch_record()
    del rec["primary"]
    del rec["content_sha256"]
    ck = _save_fetch_ckpt(cp, rk, [rec])

    ledger = ProvenanceLedger()
    replay_ledger(ledger, [ck])
    obs = next(iter(ledger._by_hash.values()))[0]
    assert obs.primary is False, (
        "a record with no declared class defaulted to PRIMARY")


# ── R2: seal_guard sweeps every run ever written to the store ─────────────

def test_R2_foreign_run_corruption_must_not_block_a_fresh_run(tmp_path):
    """Runs A and B share one checkpoint store (production default:
    $STATE_DIR/callisto/checkpoints). A completed long ago; one byte of its
    stored body rots. B starts FRESH today — none of B's evidence touches
    A's checkpoints — yet B cannot seal, for up to the whole GC window."""
    cp = FileCheckpointer(root=tmp_path / "shared")  # ONE store, both runs

    rk_a = run_key("question A semiconductors", "GENERAL", "2026-08-22")
    good = '{"results": [{"id": "W1", "title": "real study"}]}'
    _save_fetch_ckpt(cp, rk_a, [_fetch_record(url="https://a/1", body=good)],
                     ih="ih_a")

    # Bitrot / partial restore / sync-tool mutation: valid JSON, bad bytes.
    d = cp.root / rk_a[:16]
    p = next(d.glob("*.json"))
    obj = json.loads(p.read_text())
    obj["payload"]["fetches"][0]["body"] += " "
    p.write_text(json.dumps(obj))

    rk_b = run_key(OTHER_QUESTION, "GENERAL", TODAY.isoformat())
    verdict, reason = seal_guard(RunTrace(run=rk_b), cp.list_all(),
                                 ProvenanceLedger())
    assert verdict == "SEAL", (
        "an unrelated run's corruption blocked a fresh run's seal; reason "
        f"was: {reason[:160]}")
    # And when refusal DOES happen it must name the offending artifact.
    assert rk_a[:16] in reason or "fetch_leaf" in reason, (
        "refusal reason does not identify which run/stage failed integrity")


def test_R2_pin_unreadable_checkpoint_is_skipped_not_poison(tmp_path):
    """A file damaged past JSON parsing is a MISS everywhere (list_all skips
    it), so pure garbage does not poison the store — only VALID json with
    mutated payloads does. Kept as a pin marking the boundary."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    d = cp.root / run_key("x", "", "")[:16]
    d.mkdir(parents=True)
    (d / "fetch_leaf.deadbeef.json").write_text("{not json")
    verdict, _ = seal_guard(RunTrace(run=run_key("y", "", "")),
                            cp.list_all(), ProvenanceLedger())
    assert verdict == "SEAL"


# ── R3: free do-over after a terminal refusal — re-roll only the critic ────

class VetoThenPass:
    """A stand-in for a stochastic critic: vetoes the first sample, approves
    every later one."""

    def __init__(self):
        self.calls = 0

    async def complete(self, task_class, messages, schema=None):
        self.calls += 1
        objs = []
        if self.calls == 1:
            objs = [{"text": "single-source retrieval failure",
                     "kind": "selection-effect", "severity": "BLOCKING"}]
        return {"parsed_json": {"objections": objs}, "model": "stub"}


def test_R3_refused_run_rerolls_only_the_critic_and_hides_that_it_did(
        tmp_path):
    """Attempt 1: real work, adversary veto, REFUSED. Attempt 2 (same
    question, same day, same store): every stage is a cache hit — zero model
    calls, zero fetches — and ONLY the critic is sampled again. The gate was
    not lowered; it was RE-ROLLED, which amounts to the same thing, and the
    sealed result carries no mark distinguishing it from a first attempt."""
    cp = FileCheckpointer(root=tmp_path / "ckpt")
    adv = VetoThenPass()

    r1 = asyncio.run(_pipeline(
        tmp_path / "a", adversary=adv,
        ledger=ProvenanceLedger(), checkpointer=cp).run(
            QUESTION, today=TODAY))
    assert not r1.sealed and "veto" in r1.refusal_reason

    t2 = fixture_transport(dict(_ROUTES))
    m2 = ScriptedModel({})  # anything asked would return "{}" — must not happen
    r2 = asyncio.run(_pipeline(
        tmp_path / "b", model=m2, adversary=adv, transport=t2,
        ledger=ProvenanceLedger(), checkpointer=cp).run(
            QUESTION, today=TODAY))

    # Mechanism, stated plainly: the seal came from cached work + a fresh
    # roll of the critic and nothing else.
    assert r2.sealed, r2.refusal_reason
    assert t2.calls == [], "do-over refetched"
    assert m2.calls == [], "do-over recomputed a model stage"

    # The violations: a consumer cannot tell this seal from a first attempt.
    assert any(("resume" in n.lower() or "attempt" in n.lower())
               for n in r2.notes), (
        "sealed result hides that earlier attempts were refused")
    sd = r2.summary_dict()
    assert sd.get("attempt") or sd.get("resumed_stages") or \
        sd.get("oldest_produced_at"), (
        "summary_dict exposes no resume/attempt information; trace with "
        "resumed_stages exists on the object but is dropped from the report")


# ── R4a: three execution modes, two different sealed evidence counts ──────

def test_R4a_evidence_count_diverges_across_execution_modes(tmp_path):
    """The SAME question, THREE ways. Plain live adds each evidence item
    once. Fresh-with-checkpointer adds it twice (_answer_leaf's
    session.add_evidence AND the unconditional restore loop). Resumed adds
    it once. The sealed session's evidence_count therefore depends on how
    the process was executed, not on the evidence."""
    def count(pipe):
        r = asyncio.run(pipe.run(QUESTION, today=TODAY))
        assert r.sealed, r.refusal_reason
        return r.session.summary.evidence_count

    n_plain = count(_pipeline(tmp_path / "plain"))

    cp = FileCheckpointer(root=tmp_path / "ckpt")
    n_fresh_cp = count(_pipeline(tmp_path / "fresh", checkpointer=cp))
    n_resumed = count(_pipeline(tmp_path / "resumed", checkpointer=cp))

    assert n_plain == n_fresh_cp == n_resumed, (
        f"sealed evidence_count depends on execution mode: "
        f"plain={n_plain} fresh-with-checkpointer={n_fresh_cp} "
        f"resumed={n_resumed}")


# ── R4b: resume re-stamps evidence timestamps ──────────────────────────────

def test_R4b_resumed_evidence_claims_fresh_acquisition_timestamps(tmp_path):
    """Contract 2: 'A cache hit carries the ORIGINAL produced_at forward —
    evidence fetched an hour ago is labeled with that hour.' The Checkpoint
    keeps that promise; the rebuilt Evidence inside the sealed session does
    not — its timestamp field is dropped on restore and re-defaulted to the
    resume moment."""
    cp = FileCheckpointer(root=tmp_path / "ckpt")
    asyncio.run(_pipeline(tmp_path / "warm", checkpointer=cp,
                          ledger=ProvenanceLedger()).run(
                              QUESTION, today=TODAY))

    stored_ts = next(
        rec["timestamp"]
        for c in cp.list_all() if c.stage == "answer_leaf"
        for rec in c.payload.get("evidence", []))

    r2 = asyncio.run(_pipeline(tmp_path / "again", checkpointer=cp,
                               ledger=ProvenanceLedger()).run(
                                   QUESTION, today=TODAY))
    assert r2.session.evidence, "resumed session restored no evidence"
    for ev in r2.session.evidence:
        assert ev.timestamp == stored_ts, (
            f"sealed evidence timestamp {ev.timestamp} was re-stamped at "
            f"resume time; original acquisition was {stored_ts}")


# ── R5: GC's open-claim protection is decorative in production wiring ─────

def test_R5_gc_deletes_open_claim_checkpoints_under_production_construction(
        tmp_path):
    """Module contract 5: gc 'NEVER deletes one whose claim_ids are still
    open'. The only production constructor (scripts/run_retro_batch.py) and
    the default both leave is_claim_open=None, i.e. 'nothing is ever open' —
    so the promised protection does not exist anywhere outside tests."""
    cp = FileCheckpointer(root=tmp_path / "ck")   # exactly as production builds it
    old = datetime.now(UTC) - timedelta(days=40)
    cp.save(run_key("q", "", ""), "fetch_leaf", "h", {"fetches": []},
            claim_ids=["claim-still-open"], produced_at=old)

    cp.gc(max_age_days=30)

    assert cp.load_by_key(run_key("q", "", ""), step_key(
        run_key("q", "", ""), "fetch_leaf", "ih")) is None or True
    remaining = [c for c in cp.list_all()
                 if "claim-still-open" in c.claim_ids]
    assert remaining, (
        "gc deleted the checkpoint of an open claim; is_claim_open was "
        "never wired, so contract 5 is unimplemented in production")


def test_R5_pin_protection_works_when_wired(tmp_path):
    cp = FileCheckpointer(root=tmp_path / "ck",
                          is_claim_open=lambda cid: cid == "claim-still-open")
    old = datetime.now(UTC) - timedelta(days=40)
    cp.save(run_key("q", "", ""), "fetch_leaf", "h", {},
            claim_ids=["claim-still-open"], produced_at=old)
    cp.gc(max_age_days=30)
    assert any("claim-still-open" in c.claim_ids for c in cp.list_all())


# ── R6: a naive produced_at takes down the whole GC pass ──────────────────

def test_R6_naive_produced_at_crashes_gc_instead_of_being_skipped(tmp_path):
    """One legacy/hand-edited file with a timezone-naive produced_at raises
    TypeError (aware cutoff minus naive dt) OUTSIDE any try block, killing
    the entire collection pass instead of skipping one bad file."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    ck = Checkpoint(key=step_key("rk", "s", "h"), run="rk", stage="s",
                    input_hash="h", payload={},
                    produced_at="2026-07-01T00:00:00")  # naive
    d = cp.root / "rk"[:16]
    d.mkdir(parents=True, exist_ok=True)
    (d / "s.deadbeef.json").write_text(json.dumps(ck.to_dict()))
    cp.save(run_key("fresh", "", ""), "s", "h2", {}, claim_ids=[])

    try:
        removed = cp.gc(max_age_days=30)
    except TypeError as e:
        raise AssertionError(f"gc crashed on one bad file: {e}")
    assert len(removed) >= 0  # the fresh checkpoint's business is its own


# ── HONEST NEGATIVES — attacks that did not land (regression pins) ────────

def test_pin_body_tamper_with_retained_digest_is_caught(tmp_path):
    """Mutating the body while leaving the recorded digest in place IS caught
    by the integrity check — the check defends against bitrot, just not
    against a writer that controls the whole file."""
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = run_key("tamper", "", "")
    rec = _fetch_record()
    ck = _save_fetch_ckpt(cp, rk, [rec])
    ck.payload["fetches"][0]["body"] += " tampered"
    ledger = ProvenanceLedger()
    assert not provenance_is_intact(ledger, [ck])
    assert seal_guard(_resumed_trace(rk), [ck], ledger)[0] == "REFUSE"


def test_pin_replay_twice_records_nothing_twice(tmp_path):
    cp = FileCheckpointer(root=tmp_path / "ck")
    rk = run_key("dup", "", "")
    ck = _save_fetch_ckpt(cp, rk, [_fetch_record()])
    ledger = ProvenanceLedger()
    replay_ledger(ledger, [ck])
    report = replay_ledger(ledger, [ck])
    assert report["replayed"] == 0 and report["skipped_duplicates"] == 1
    assert len(ledger._by_hash) == 1


def test_pin_resumed_run_never_scores_above_the_live_run(tmp_path):
    """Single-run isolation (the case the existing fix covers): fully-cached
    resume scores exactly what the plain run scored, never more."""
    plain = asyncio.run(_pipeline(tmp_path / "plain").run(
        QUESTION, today=TODAY))

    cp = FileCheckpointer(root=tmp_path / "ckpt")
    asyncio.run(_pipeline(tmp_path / "warm", checkpointer=cp,
                          ledger=ProvenanceLedger()).run(
                              QUESTION, today=TODAY))
    resumed = asyncio.run(_pipeline(tmp_path / "again", checkpointer=cp,
                                    ledger=ProvenanceLedger(),
                                    transport=fixture_transport(
                                        dict(_ROUTES))).run(
                                        QUESTION, today=TODAY))
    assert resumed.trace is not None and resumed.trace.is_resume
    assert resumed.confidence_score <= plain.confidence_score + 1e-9
