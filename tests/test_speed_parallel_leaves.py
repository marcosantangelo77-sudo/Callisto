"""SPEED — parallel-leaf pipeline: same answers, less wall clock.

The standing speed role (findings/speed_2026-08-23.md) identified the
strictly serial per-leaf loop in engine.run() as the dominant cost after
the transport fix. These tests pin the two properties that matter:

1. ANSWERS DID NOT CHANGE — golden fingerprints captured from the SERIAL
   engine (git HEAD before the restructure) must match the parallel engine
   byte-for-byte on every observable field.
2. THE SPEEDUP IS REAL — with simulated per-call latencies, a 5-leaf run
   must take far less than 5x a 1-leaf run.

Hard rules honored here: no caching anywhere near a cutoff; the adversary
stays its own call; nothing here can raise confidence.
"""
from __future__ import annotations

import asyncio
import json
import time
from datetime import date
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain  # noqa: E402
from agp import AGPSession  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "speed_golden"

OPENALEX_BODY = json.dumps({
    "results": [
        {"id": "W1", "title": "Scholarly study on apple earnings expectations:"
                              " analyst consensus and quarterly results",
         "publication_year": 2024, "cited_by_count": 12},
    ],
})
FR_BODY = json.dumps({
    "documents": [
        {"title": "Final agency rule published by the government: proposed "
                  "and final rules on apple earnings disclosure",
         "document_number": "2024-12345", "published_at": "2024-01-15",
         "agency": "government agency"},
    ],
})
ROUTES = {"/works": OPENALEX_BODY, "/documents.json": FR_BODY}


def _decompose(n_leaves: int) -> str:
    subs = []
    qtypes = ["scholarly work search",
              "final/proposed agency rules with dates and docket refs"]
    for i in range(n_leaves):
        subs.append({
            "text": f"leaf {i}: what does the evidence say about apple "
                    "earnings expectations and analyst consensus",
            "kind": "descriptive",
            "question_type": qtypes[i % 2],
            "min_source_tier": 1,
            "min_independent_sources": 1,
            "quant_required": False,
            "horizon_days": None,
        })
    return json.dumps({"sub_questions": subs})


def _answer(conf=0.7, answer="the evidence supports the claim") -> str:
    return json.dumps({"answer": answer,
                       "proposed_confidence": conf, "compute": None})


class _Adversary:
    def __init__(self, objections=None):
        self.objections = list(objections or [])

    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": self.objections},
                "model": "stub-adversary"}


def _fingerprint(result, ledger) -> dict:
    """Everything observable about a run EXCEPT volatile ids/timestamps."""
    fp = {
        "sealed": result.sealed,
        "refusal_reason": result.refusal_reason,
        "confidence_score": result.confidence_score,
        "confidence_tier": result.confidence_tier,
        "conclusion": result.conclusion,
        "notes": list(result.notes),
        "leaves": [{
            "text": l.text, "answer": l.answer, "confidence": l.confidence,
            "tier": l.tier, "source_classes": list(l.source_classes),
            "n_sources": l.n_sources,
            "requirement_reasons": list(l.requirement_reasons),
            "sandbox_status": l.sandbox_status,
            "artifact_sha256s": sorted(l.artifact_sha256s),
        } for l in result.leaves],
        "fetches": [{"source_name": f.source_name, "url": f.url,
                     "sha": f.content_sha256} for f in result.fetches],
        "evidence": ([e.content for e in result.session.evidence]
                     if result.session else []),
        "objections": [{"text": o.text, "kind": o.kind,
                        "severity": o.severity, "model": o.model}
                       for o in result.objections],
        "artifacts": sorted(r.sha256 for r in result.artifact_refs),
        "seal_verifies": (
            AGPSession.verify_seal(result.session.to_dict())
            if result.sealed and result.session else None),
        "ledger_observations": len(ledger._by_hash),
        "ledger_primary": sorted(
            h for h, obs in ledger._by_hash.items()
            if any(o.primary for o in obs)),
    }
    return fp


def _run_scenario(tmp_path, *, n_leaves=5, adversary=None, manager=None,
                  manager_tagged=None, decompose=None, checkpointer=None,
                  ledger=None, routes_override=None):
    model = ScriptedModel({"Architect": [decompose or _decompose(n_leaves)]})
    adversary = adversary if adversary is not None else _Adversary()
    store = ArtifactStore(root=tmp_path / "artifacts")
    led = ledger if ledger is not None else ProvenanceLedger()
    routes = dict(ROUTES)
    if routes_override:
        routes.update(routes_override)
    pipeline = ResearchPipeline(
        model=model, adversary_router=adversary,
        transport=fixture_transport(routes), store=store, ledger=led,
        checkpointer=checkpointer)
    if manager:
        model.script("Manager", *manager)
    if manager_tagged:
        # Tagged dispatch keys on the engine's deterministic leaf ordinal.
        # On a SERIAL engine (no script_for) fall back to the legacy FIFO
        # flattened in leaf order — identical arrival order there, so the
        # same scripts feed the same leaves either way. This keeps the
        # golden generator runnable against the pre-restructure engine.
        for i in range(n_leaves):
            resp = manager_tagged.get(f"leaf{i}")
            if resp is None:
                continue
            if hasattr(model, "script_for"):
                model.script_for(f"leaf{i}", "Manager", *resp)
            else:  # serial engine
                model.script("Manager", *resp)
    result = asyncio.run(pipeline.run(
        "Will Apple report quarterly results above Wall Street consensus "
        "expectations in its next earnings report?",
        domain=Domain.FINANCIAL, today=date(2026, 8, 22)))
    return result, led


SCENARIOS = {
    "leaves1_sealed": dict(n_leaves=1,
                           manager=[_answer(0.8)] * 4),
    "leaves3_sealed": dict(n_leaves=3,
                           manager=[_answer(0.75)] * 8),
    "leaves5_sealed": dict(n_leaves=5,
                           manager=[_answer(0.7)] * 12),
    "leaves5_distinct_per_leaf": dict(
        n_leaves=3,
        # Distinct responses per leaf via TAGGED scripting (the engine's
        # deterministic leaf ordinal). Confidence values differ so any
        # cross-leaf mixing is visible in the fingerprint.
        manager_tagged={"leaf0": [_answer(0.8)],
                        "leaf1": [_answer(0.6)],
                        "leaf2": [_answer(0.7)]}),
    "adversary_blocking_refuses": dict(
        n_leaves=2,
        manager=[_answer(0.7)] * 6,
        adversary=_Adversary([{"kind": "refuting_evidence",
                               "severity": "BLOCKING",
                               "text": "fixture proves nothing"}])),
    "adversary_penalizes_still_seals": dict(
        n_leaves=2,
        manager=[_answer(0.9)] * 6,
        adversary=_Adversary([
            {"kind": "scope", "severity": "MINOR",
             "text": "narrow window"}])),
    "adversary_penalizes_below_floor_refuses": dict(
        n_leaves=2,
        manager=[_answer(0.4)] * 6,
        adversary=_Adversary([
            {"kind": "selection_effect", "severity": "MAJOR",
             "text": "only successes indexed"}])),
    "compute_reask": dict(
        n_leaves=1,
        manager=[{"content": json.dumps(
                     {"answer": None,
                      "compute": {"code": "result = {'mean': sum([1.0, 2.0,"
                                  " 3.0]) / 3}", "inputs": {}}})},
                 _answer(0.75, answer="mean computed as 2.0")]),
    "two_computing_leaves": dict(
        n_leaves=2,
        # Both leaves compute, with DISTINCT codes and answers, dispatched
        # by tag so concurrency cannot cross the pairing. This pins
        # concurrent sandbox/ledger/artifact safety AND per-leaf isolation.
        manager_tagged={
            "leaf0": [{"content": json.dumps(
                {"answer": None,
                 "compute": {"code": "result = {'v': 1}", "inputs": {}}})},
                _answer(0.7, answer="computed v")],
            "leaf1": [{"content": json.dumps(
                {"answer": None,
                 "compute": {"code": "result = {'w': 2}", "inputs": {}}})},
                _answer(0.65, answer="computed w")]}),
    "all_unanswered_refuses": dict(
        n_leaves=2, manager=[_answer(0.7, answer=""), _answer(0.7, answer="")]),
    "rejected_fetches_noted": dict(
        n_leaves=2, manager=[_answer(0.7)] * 6,
        routes_override={"/documents.json": "{\"documents\": []}"}),
}


@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_parallel_matches_serial_golden(scenario, tmp_path):
    """The restructure may not move a single observable value."""
    golden_path = GOLDEN_DIR / f"{scenario}.json"
    assert golden_path.exists(), (
        f"missing golden for {scenario}; regenerate from serial engine")
    golden = json.loads(golden_path.read_text())
    result, ledger = _run_scenario(tmp_path, **dict(SCENARIOS[scenario]))
    fp = _fingerprint(result, ledger)
    assert fp == golden, (
        f"{scenario}: parallel engine diverged from serial golden")


def test_five_leaf_wall_clock_is_sublinear(tmp_path):
    """With realistic per-unit latencies the 5-leaf wall clock must sit far
    below 5x the 1-leaf wall clock — proof the leaves actually overlap."""

    class SlowModel(ScriptedModel):
        def __init__(self, delay_s, *a, **k):
            super().__init__(*a, **k)
            self.delay_s = delay_s

        async def complete(self, role, messages, *, _call_tag=None, **k):
            await asyncio.sleep(self.delay_s)
            return await super().complete(role, messages,
                                          _call_tag=_call_tag, **k)

    class SlowTransport:
        def __init__(self, inner, delay_s):
            self.inner = inner
            self.delay_s = delay_s

        def __call__(self, url, headers):
            time.sleep(self.delay_s)
            return self.inner(url, headers)

    def _timed(n_leaves):
        model = SlowModel(0.03, {"Architect": [_decompose(n_leaves)]})
        model.script("Manager", *[{"content": _answer(0.7)}
                                  for _ in range(n_leaves + 2)])
        transport = SlowTransport(fixture_transport(ROUTES), 0.02)
        pipeline = ResearchPipeline(
            model=model, adversary_router=_Adversary(),
            transport=transport, store=ArtifactStore(root=tmp_path / "art2"),
            ledger=ProvenanceLedger())
        t0 = time.monotonic()
        res = asyncio.run(pipeline.run("Q?", today=date(2026, 8, 22)))
        return time.monotonic() - t0, res

    t1, r1 = _timed(1)
    t5, r5 = _timed(5)
    assert r5.sealed and r1.sealed, "sanity: both runs sealed"
    # The sublinear property holds on the PARALLEL-LEAF engine. On the
    # serial engine (master, pre-restructure) leaves are serial by design
    # and this test would assert against the architecture, not a defect.
    import inspect
    from tools.pipeline import engine as _eng
    if "Phase A" not in inspect.getsource(_eng.ResearchPipeline.run):
        pytest.skip("serial engine: leaf parallelism lives in the "
                    "parallel-leaf restructure branch")
    # Serial would cost ~5x the leaf work; overlapped it costs ~1x plus the
    # extra decompose/adversary calls. Allow generous headroom for CI noise
    # but keep the assertion strong enough to catch serialization regressions.
    assert t5 < t1 * 3.0, (
        f"5 leaves took {t5:.3f}s vs 1 leaf {t1:.3f}s — leaves look serial")


def test_error_selection_follows_leaf_order(tmp_path):
    """A failing leaf aborts the run with THAT leaf's exception, exactly as
    the serial loop did — not whichever task happened to fail first."""

    class BoomAt:
        def __init__(self, token):
            self.token = token

        async def complete(self, role, messages, *, _call_tag=None, **k):
            if role == "Manager" and self.token in messages[-1]["content"]:
                raise ValueError(f"boom {self.token}")
            if role == "Architect":
                return {"content": _decompose(3)}
            return {"content": _answer(0.7)}

    model = BoomAt("leaf 1")
    pipeline = ResearchPipeline(
        model=model, adversary_router=_Adversary(),
        transport=fixture_transport(ROUTES),
        store=ArtifactStore(root=tmp_path / "art3"),
        ledger=ProvenanceLedger())
    with pytest.raises(ValueError, match="boom leaf 1"):
        asyncio.run(pipeline.run("Q?", today=date(2026, 8, 22)))


def test_ledger_state_identical_across_runs(tmp_path):
    """Parallel retrieval merges scratch records in leaf order; running the
    same scenario twice must produce identical ledger state."""
    fps = []
    for i in range(2):
        ledger = ProvenanceLedger()
        result, ledger = _run_scenario(tmp_path / f"run{i}", n_leaves=5,
                                       ledger=ledger)
        fps.append(_fingerprint(result, ledger)["ledger_primary"])
    assert fps[0] == fps[1]


def test_checkpoint_resume_roundtrip_identical(tmp_path):
    """A resumed run scores exactly what the fresh run scored (W3 contract,
    now under the parallel engine)."""
    from tools.pipeline.checkpoint import FileCheckpointer

    cp = FileCheckpointer(root=tmp_path / "ckpt")
    r1, _ = _run_scenario(tmp_path / "a", n_leaves=3, checkpointer=cp)
    r2, _ = _run_scenario(tmp_path / "b", n_leaves=3, checkpointer=cp,
                          ledger=ProvenanceLedger())
    f1, f2 = _fingerprint(r1, ProvenanceLedger()), None
    # compare everything except ledger internals (fresh vs replayed ledger)
    strip = lambda fp: {k: v for k, v in fp.items()
                        if k not in ("ledger_observations", "ledger_primary")}
    s1 = strip(_fingerprint(r1, ProvenanceLedger()))
    s2 = strip(_fingerprint(r2, ProvenanceLedger()))
    assert r2.sealed == r1.sealed
    assert s1 == s2, "resumed run diverged from fresh run"


def _five_question_brier():
    """Predictions for the five scored data/retro_batch questions through
    PipelineResearcher offline (scripted model, fixture transport)."""
    import sys as _sys

    repo = Path(__file__).resolve().parents[1]
    _sys.path.insert(0, str(repo / "scripts"))
    try:
        from retro_questions_i4 import load_set
        questions = load_set(str(repo / "data/retro_batch/questions.json"))
    finally:
        _sys.path.pop(0)
    questions = questions[:5]
    assert len(questions) == 5

    from tools.pipeline.retro import PipelineResearcher

    model = ScriptedModel({})
    decompose = _decompose(3)
    model.script("Architect", decompose)
    model.script("Manager", *[_answer(0.7)] * 64)

    class OfflineResearcher(PipelineResearcher):
        def __init__(self):
            super().__init__(model=model, routes=ROUTES,
                             adversary_router=_Adversary(),
                             claim_date=date(2024, 1, 3))

    researcher = OfflineResearcher()
    prompts = [q.prompt_for_researcher() for q in questions]
    preds = asyncio.run(researcher.answer_async(prompts, [], loops=1))

    from tools.retrodiction.scoring import score_brier
    brier = score_brier(preds, questions)
    return brier, [{"question_id": p.question_id,
                    "probability": p.probability} for p in preds]


def test_brier_regression_five_retro_questions(tmp_path):
    """The five scored questions in data/retro_batch/: predictions through
    PipelineResearcher offline must match the serial-engine golden Brier."""
    brier, preds = _five_question_brier()

    golden_path = GOLDEN_DIR / "five_question_brier.json"
    golden = json.loads(golden_path.read_text())
    # The golden was captured under the OLD scorer (retro._leans_yes, a
    # keyword scan defaulting to YES). Commit fa2bea9 replaced that with the
    # DECLARED stance: AFFIRMS/DENIES set the sign, UNDETERMINED is p=0.5.
    # A scripted model declares no stance, so every scripted forecast is
    # honestly 0.5 — Brier 0.25 by construction. That is an INTENTIONAL
    # semantic change in the scorer, not drift in this suite's parallelism:
    # what this test guards is that the parallel engine reproduces the
    # CURRENT serial semantics exactly, so the golden must be regenerated
    # against current code (gen_speed_golden.py) whenever the bridge changes.
    assert round(brier, 9) == golden["brier"], (
        f"Brier moved: {brier} vs golden {golden['brier']} — if the bridge "
        f"semantics changed intentionally, regenerate the golden; if not, "
        f"this is a regression")
    assert preds == golden["predictions"], (
        "per-question probabilities moved vs serial golden")
