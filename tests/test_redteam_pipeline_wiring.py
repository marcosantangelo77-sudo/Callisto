"""RED TEAM — PIPELINE WIRING & THE RETRODICTION FEEDBACK PATH (surface pass).

Prior passes attacked components in isolation (synthesis, provenance, seal,
retrieval, loop incentives, calibration scoring itself). This pass attacks
the SEAMS between them: how engine.py composes clamps and tiers across
stages, what survives the resume boundary, who the retrodiction batch
trusts, and what the routing store does with what it is fed. A component
that is individually sound can still inflate a number by handing it to a
neighbour one stage too early or one trust level too high.

Convention (matches test_redteam_calib_scoring): each test is a deterministic
reproduction of a CONFIRMED defect and PASSES against current code; it FAILS
the moment the defect is fixed, which is the canary signal to update
findings/redteam_pipeline_wiring.md.

Findings:
  W1  result.confidence_tier is minted BEFORE adversary penalties are
      applied — a CORROBORATED label ships with a PROBABLE score.
  W2  seal_guard on a resumed run whose checkpoints were LOST (gc/crash)
      returns SEAL — the anti-laundering guard degrades to a no-op on its
      own degenerate input (sibling pattern R*/Z*).
  W3  The resume path trusts checkpointed answer payloads wholesale:
      poisoned leaf confidence/tier seals without recomputation.
  W4  On a resumed leaf, the sandbox counts as an INDEPENDENT SOURCE:
      n_indep = 1 fetch + own computation satisfies
      min_independent_sources=2, lifting the leaf past the SPECULATIVE cap.
  W5  CutoffEnforcer without a signing key admits forged proofs over any
      bytes with any date — fail-open default on the load-bearing check.
  W6  Every ResearchPipeline lazily mints its Adversary into a fresh temp
      dir — the scored track record is fragmented per pipeline instance;
      precision_of_attack never accumulates across runs.
  W7  write_routing_scores has no question-level dedupe: rerunning a batch
      appends duplicate rows, doubling n and weakening shrinkage.
  W8  ThompsonRoutingPolicy.decide pools all task_classes under a role:
      a classification specialist routes synthesis calls it was never
      measured on.
  W9  RetrodictionBatch.load_completed trusts any non-error checkpoint
      payload — a flipped status/brier feeds build_report unverified.
"""

import asyncio
import hashlib
import json
import random
import tempfile
from datetime import date, datetime
from pathlib import Path

import pytest

from agp.adversary import Adversary, AdversaryObjection
from agp.provenance import ProvenanceLedger
from tools.pipeline import checkpoint as ckpt
from tools.pipeline.engine import ResearchPipeline, fixture_transport
from tools.pipeline.model import ScriptedModel
from tools.pipeline.checkpoint import RunTrace, StageOutcome
from tools.research_program import ResolutionRecord, clamp_parent_confidence
from tools.retrodiction.batch import RetrodictionBatch, BatchResult, build_report, \
    write_routing_scores
from tools.retrodiction.cutoff import (
    CutoffEnforcer,
    EvidenceRecord,
    PublicationProof,
    ProofKind,
)
from tools.retrodiction.questions import RetrodictionQuestion


def _proof_for(body: str, published_on: date) -> PublicationProof:
    return PublicationProof(
        kind=ProofKind.SOURCE_DECLARED,
        published_on=published_on,
        locator="fixture-accession",
        content_sha256=hashlib.sha256(body.encode()).hexdigest(),
    )


# ── W1: tier minted before the adversary subtracts ─────────────────────────

def test_w1_tier_label_ignores_adversary_penalties():
    recs = [ResolutionRecord(f"q{i}", date(2025, 1, 1), "hit",
                             pinball_score=0.05,
                             best_source_class="PRIMARY") for i in range(6)]
    clamped, tier = clamp_parent_confidence(0.75, recs)
    objs = [AdversaryObjection("x", f"objection {i}", severity="MINOR")
            for i in range(3)]
    final, _ = Adversary.apply_verdict(clamped, objs)
    # engine.run reports `tier` from clamp_parent_confidence but stores the
    # post-adversary score. CORROBORATED label on a PROBABLE number.
    assert tier == "CORROBORATED"
    assert final < 0.75                      # penalties moved the score…
    from agp import ConfidenceTier
    assert ConfidenceTier.from_score(final).value != tier  # …but not the label


# ── W2: lost checkpoints = clean seal on a resumed run ─────────────────────

def test_w2_resumed_run_with_zero_surviving_checkpoints_seals():
    trace = RunTrace(run="rk")
    trace.stages.append(StageOutcome(
        stage="fetch_leaf", resumed=True, payload={},
        produced_at="2026-08-01T00:00:00"))
    assert trace.is_resume
    verdict, reason = ckpt.seal_guard(trace, [], ProvenanceLedger())
    # The run resumed, every checkpoint is gone, nothing was re-verified —
    # and the guard says SEAL. Degenerate input degrades to a pass.
    assert verdict == "SEAL" and reason == ""   # CANARY: must REFUSE when fixed


# ── W3: poisoned answer checkpoints seal without recomputation ─────────────

DECOMP = {"content": json.dumps({"sub_questions": [{
    "text": "Is X true?", "kind": "descriptive", "question_type": "papers",
    "min_source_tier": 2, "min_independent_sources": 2,
    "quant_required": False}]})}
ANSWER = {"content": json.dumps({"answer": "yes", "proposed_confidence": 0.9})}


def _fresh_model():
    m = ScriptedModel(default={"content": "{}"})
    m.script("Architect", DECOMP)
    m.script("Manager", ANSWER)
    return m


ROUTES = {"openalex": json.dumps(
    {"results": [{"title": "X is true, study shows"}]})}


def test_w3_poisoned_answer_checkpoint_seals_at_attacker_confidence(tmp_path):
    cp = ckpt.FileCheckpointer(root=tmp_path / "cp")
    pipe = ResearchPipeline(model=_fresh_model(),
                            transport=fixture_transport(ROUTES),
                            checkpointer=cp)
    asyncio.run(pipe.run("Is X true?", today=date(2026, 8, 23)))

    for c in cp.list_all():
        if c.stage == "answer_leaf":
            c.payload["leaf"]["confidence"] = 0.93
            c.payload["leaf"]["tier"] = "VERIFIED"
            cp._path(c).write_text(json.dumps(c.to_dict(), sort_keys=True))

    pipe2 = ResearchPipeline(model=_fresh_model(),
                             transport=fixture_transport(ROUTES),
                             checkpointer=ckpt.FileCheckpointer(root=tmp_path / "cp"))
    res2 = asyncio.run(pipe2.run("Is X true?", today=date(2026, 8, 23)))
    # seal_guard verifies FETCH bytes only; the stored leaf verdict/evidence
    # scores are trusted as-is.
    assert res2.sealed is True                  # defect: seals over poison
    assert res2.leaves[0].confidence == 0.93    # CANARY: must refuse/recompute
    assert res2.leaves[0].tier == "VERIFIED"


# ── W4: own sandbox counted as an independent source after resume ──────────

BODY42 = json.dumps({"results": [{"title": "X study: measured value 42 units"}]})


def _compute_model():
    m = ScriptedModel(default={"content": "{}"})
    m.script("Architect", {"content": json.dumps({"sub_questions": [{
        "text": "What is the measured value of X?", "kind": "descriptive",
        "question_type": "papers", "min_source_tier": 2,
        "min_independent_sources": 2, "quant_required": True}]})})
    m.script("Manager", {"content": json.dumps(
        {"compute": {"code": "print('value = 42')", "inputs": {}},
         "answer": None})})
    m.script("Manager", {"content": json.dumps(
        {"answer": "The measured value is 42 units.",
         "proposed_confidence": 0.9})})
    return m


def test_w4_resume_counts_own_sandbox_as_second_independent_source(tmp_path):
    cp = ckpt.FileCheckpointer(root=tmp_path / "cp")
    pipe = ResearchPipeline(model=_compute_model(),
                            transport=fixture_transport({"openalex": BODY42}),
                            checkpointer=cp)
    asyncio.run(pipe.run("What is the measured value of X?",
                         today=date(2026, 8, 23)))
    for c in cp.list_all():
        if c.stage == "answer_leaf":
            cp._path(c).unlink()

    pipe2 = ResearchPipeline(model=_compute_model(),
                             transport=fixture_transport({"openalex": BODY42}),
                             checkpointer=ckpt.FileCheckpointer(root=tmp_path / "cp"))
    res2 = asyncio.run(pipe2.run("What is the measured value of X?",
                                 today=date(2026, 8, 23)))
    # One fetched source + the pipeline's OWN calculator must NOT satisfy
    # min_independent_sources=2; the requirement gate must still cap the leaf.
    assert res2.leaves[0].requirement_reasons == []   # CANARY: gate bypassed


# ── W5: unsigned publication proofs admitted when no key is configured ─────

def test_w5_forged_proof_admitted_without_signing_key():
    body = "bytes edited long after the claimed date"
    proof = _proof_for(body, date(2019, 1, 1))   # no .sign() — unsigned
    rec = EvidenceRecord(url="u", query="q", fetched_at=datetime.now(),
                         content=body, proof=proof)
    enforcer = CutoffEnforcer(date(2024, 1, 1))  # production default: no key
    admitted, rejected = enforcer.admit([rec])
    # Fail-closed policy says an unverifiable proof must exclude. Today the
    # signature check is skipped entirely when no key is configured.
    assert admitted and not rejected   # CANARY: must exclude when fixed


# ── W6: per-pipeline temp adversary ledgers fragment the track record ──────

class _NullModel:
    name = "null"

    async def complete(self, *a, **k):
        return {"content": "{}"}


def test_w6_adversary_track_record_is_not_persistent_across_pipelines():
    p1 = ResearchPipeline(model=_NullModel())
    p2 = ResearchPipeline(model=_NullModel())
    path1 = p1.adversary.ledger.path
    path2 = p2.adversary.ledger.path
    # Both pipelines must share ONE durable dissent ledger so calibration
    # accumulates; today each mints a throwaway temp-dir ledger that dies
    # with the process.
    assert path1 != path2           # CANARY: must be one shared durable path


# ── W7/W8: the routing store and policy ────────────────────────────────────

def test_w7_batch_rerun_duplicates_rows_in_routing_store(tmp_path):
    from tools.routing.scores import ModelScoreStore
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    r = BatchResult(question_id="q1", status="scored", brier=0.05,
                    predicted_probability=0.9)
    write_routing_scores({"q1": r}, store)
    write_routing_scores({"q1": r}, store)   # resume/rerun of the same batch
    agg = store.summary("pipeline")["hermes-cli"]
    assert agg["n"] == 2                     # CANARY: dedupe -> n==1, basis unchanged


def test_w8_routing_policy_pools_task_classes_within_a_role(tmp_path):
    from tools.routing.scores import ModelScoreStore
    from tools.routing.policy import ThompsonRoutingPolicy, CandidateModel
    store = ModelScoreStore(path=tmp_path / "s.jsonl")
    for i in range(20):
        store.record(role="pipeline", model="A",
                     task_class="research_synthesis",
                     question_id=f"r{i}", brier=0.45)
        store.record(role="pipeline", model="B", task_class="classification",
                     question_id=f"c{i}", brier=0.05)

    wins = {"A": 0, "B": 0}
    for seed in range(50):
        pol = ThompsonRoutingPolicy(store=store, rng=random.Random(seed))
        d = pol.decide("pipeline", [CandidateModel(name="A", tier="t"),
                                    CandidateModel(name="B", tier="t")])
        wins[d.model] += 1
    # Routing a SYNTHESIS call: B has never answered one synthesis question,
    # yet wins every draw because task_class is not part of the comparison.
    assert wins["B"] > wins["A"]             # CANARY: must route per task_class


# ── W9: batch resume trusts any non-error checkpoint payload ───────────────

def test_w9_flipped_status_and_brier_feed_build_report(tmp_path):
    cp = ckpt.FileCheckpointer(root=tmp_path / "cp")
    q = RetrodictionQuestion(question_id="q1", text="t?", domain="GENERAL",
                             claim_date=date(2024, 1, 1),
                             resolution_date=date(2024, 3, 1),
                             answer_binary=True)
    batch = RetrodictionBatch(questions=[q], researcher_factory=lambda: None,
                              checkpointer=cp,
                              results_path=tmp_path / "r.jsonl")
    rk, ih = batch._run_key(q), batch._inputs_hash(q)
    cp.save(rk, RetrodictionBatch.STAGE, ih, {
        "question_id": "q1", "status": "scored",
        "predicted_probability": 1.0, "answer_binary": False, "brier": 0.0})
    done = batch.load_completed()
    results = {qid: BatchResult(**{k: v for k, v in rec.items()
                                   if k in BatchResult.__dataclass_fields__})
               for qid, rec in done.items()}
    rep = build_report(results)
    # A stored row with no integrity binding to the question's real outcome
    # mints "strongly better than chance". The batch record must be bound to
    # the question (answer hash) or re-verified before scoring.
    assert rep["verdict"] == "strongly better than chance (Brier < 0.20)"
    assert rep["mean_brier"] == 0.0          # CANARY: must verify/reject


# ── HONEST NEGATIVES — attacks that did NOT land (regression pins) ─────────

def test_neg_harness_min_claim_date_is_the_strictest_cutoff():
    """harness._run_arm uses min(claim_date): initially suspected as a leak,
    but strictly-before semantics make MIN the most conservative choice."""
    body = "b"
    rec = EvidenceRecord(url="u", query="q", fetched_at=datetime.now(),
                         content=body,
                         proof=_proof_for(body, date(2023, 1, 1)).sign("k"))
    mixed = [date(2024, 6, 1), date(2020, 1, 1)]
    admitted, _ = CutoffEnforcer(min(mixed), signing_key="k").admit([rec])
    assert not admitted  # 2023 bytes excluded even for the 2024 question


def test_neg_magnitude_edge_is_signed_by_direction_not_magnitude():
    from tools.retrodiction.batch import magnitude_score
    right = magnitude_score(0.8, True, market_implied=0.4)
    wrong = magnitude_score(0.8, False, market_implied=0.4)
    assert right["directional_edge"] > 0 > wrong["directional_edge"]
    zero = magnitude_score(0.4, True, market_implied=0.4)
    assert zero["directional_edge"] == 0.0


def test_neg_floor_conf_never_raises_any_clamp_input():
    from agp.thresholds import floor_conf
    rng = random.Random(1234)
    for _ in range(5000):
        x = rng.random()
        assert floor_conf(x) <= x + 1e-12
