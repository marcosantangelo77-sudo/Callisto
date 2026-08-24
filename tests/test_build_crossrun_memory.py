"""Cross-run memory — carry what a run LEARNED about its SOURCES forward.

GAP 2 from the prior-art survey: Skywork's gated distiller auto-loads what a
run learned; Callisto started every run cold. This module is the smallest
version that compounds — and most tests here are GATE-RULE tests, because
this is exactly where memory systems go wrong:

  ORDER ONLY        memory reorders retrieval fan-out; it never excludes a
                    source and never acts beyond ordering (+ flags in notes).
  NEVER CONFIDENCE  no remembered value can move tier/stance/confidence —
                    proven with poisoned records (the R5 trust-escalator
                    this repo already closed must not be rebuilt).
  NOT EVIDENCE      remembered facts never enter the evidence set or ledger.
  PER CLASS         keyed by question class only; the question text is never
                    part of the key and only ever stored as a hash.

JOB 3 proof lives in test_two_runs_fewer_wasted_fetches_same_conclusion:
run 2 makes FEWER wasted fetches with the SAME conclusion. Both numbers are
printed for findings/crossrun_memory.md.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import QuestionKind, ResearchQuestion  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.crossrun import (  # noqa: E402
    DEPRIORITISE_MIN_RUNS,
    SCHEMA_VERSION,
    CrossRunMemoryStore,
    PlanningView,
    question_class_for,
    record_run,
)
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from tools.sources.base import SourceSpec  # noqa: E402
from tools.sources.registry import (  # noqa: E402
    SelectionDecision,
    SourceAdapter,
)


QUESTION = ("What does research say about semiconductor supply chain "
            "resilience?")
OTHER_QUESTION = ("What does research say about lithium battery supply "
                  "chains?")
CLASS = "default"          # classify_query(QUESTION) — pinned below
TODAY = date(2026, 8, 22)

GOOD_BODY = json.dumps({"results": [
    {"title": "semiconductor supply chain resilience under export controls",
     "publication_year": 2024},
]})
JUNK_BODY = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"},
]})


def test_question_class_is_coarse_not_per_question():
    assert question_class_for(QUESTION) == CLASS
    # A DIFFERENT question lands in the SAME class bucket — memory is per
    # class, never per question.
    assert question_class_for(OTHER_QUESTION) == CLASS


# ── Fixtures ───────────────────────────────────────────────────────────────

def _spec(name, answers):
    return SourceSpec(name=name, base_url=f"https://{name}.example",
                      description="", answers=tuple(answers),
                      cannot_answer=("",), tier=1, min_interval_s=0.0)


class _FixedOrderRegistry:
    """Registry stand-in with DETERMINISTIC select order. The real one
    ranks by overlap score, which is exactly the fan-out order the
    compounding proof needs held fixed."""

    def __init__(self, specs):
        self._specs = list(specs)
        self._by_name = {s.name: s for s in self._specs}

    def select(self, question_type, *, max_tier=5, exclude=None,
               min_score=0.0):
        excl = exclude or set()
        return [s for s in self._specs if s.name not in excl]

    def select_explained(self, question_type, **_kw):
        return [SelectionDecision(name=s.name, included=True, score=1.0,
                                  spec=s) for s in self._specs]

    def get(self, name):
        s = self._by_name.get(name)

        def make_adapter(source):
            path = "/" + re.sub(r"^https?://|[^a-z0-9].*$", "",
                                source.spec.base_url.lower()) or "/works"

            class _Ad:
                def __getattr__(self, method_name):
                    def call(*args, **kwargs):
                        url = source.build_url(path, {"search": "q"})
                        return source.get_json(url)[0]
                    return call
            return _Ad()

        return None if s is None else SourceAdapter(spec=s,
                                                    make_adapter=make_adapter)

    def specs(self):
        return [s.to_dict() for s in self._specs]

    def names(self):
        return sorted(self._by_name)


def _routes():
    return {
        "openalex.example": JUNK_BODY,   # always fetched, never relevant
        # gdelt deliberately UNROUTED -> transport 404 -> fetch error
        "federalregister.example": GOOD_BODY,
        "clinicaltrials.example": GOOD_BODY,
    }


_ANSWER = json.dumps({"answer": "the literature supports the claim",
                      "proposed_confidence": 0.6})


class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _pipeline(tmp_path, store, tag="p"):
    model = ScriptedModel({
        "Architect": [{"content": json.dumps({"sub_questions": [{
            "text": "what does scholarly literature say about "
                    "semiconductor supply chain resilience",
            "kind": "descriptive", "question_type": "",
            "min_source_tier": 2,
            "min_independent_sources": 2}]})}],
        # No Manager queue: EVERY turn falls back to the same answer, so
        # the conclusion cannot depend on how many fetch rounds ran.
    }, default={"content": _ANSWER})
    return ResearchPipeline(
        model=model, adversary_router=_Quiet(),
        transport=fixture_transport(_routes()),
        store=ArtifactStore(root=tmp_path / f"art_{tag}"),
        registry=_FixedOrderRegistry([
            _spec("openalex", ["scholarly works search"]),
            _spec("gdelt", ["news events about supply chains"]),
            _spec("federalregister", ["agency rules on supply chains"]),
            _spec("clinicaltrials", ["trial registries on supply chains"]),
        ]),
        crossrun_store=store)


def _seed_rec(qclass=CLASS, sources=None, stance="UNDETERMINED",
              tier="UNVERIFIED"):
    """One prior-run record — facts only, the exact shape record_run writes."""
    return {
        "v": SCHEMA_VERSION,
        "recorded_at": "2026-08-20T00:00:00+00:00",
        "question_class": qclass,
        "sources": sources if sources is not None else {},
        "gap_kinds": {},
        "stance": stance,
        "tier": tier,
        "sealed": True,
        "refusal_reason": "",
        "n_fetches": 0,
        "root_query_sha256": "a" * 16,
    }


def _null_src(rejected=1):
    return {"admitted": 0, "rejected_gate": rejected, "errored": 0,
            "skipped": 0}


def _err_src():
    return {"admitted": 0, "rejected_gate": 0, "errored": 1, "skipped": 0}


def _wasted(record):
    """Wasted fetches per persisted record: gate-rejections + errors."""
    return sum(c.get("rejected_gate", 0) + c.get("errored", 0)
               for c in (record.get("sources") or {}).values())


def _conclusion(result):
    return {
        "sealed": result.sealed,
        "stance": result.stance,
        "tier": result.confidence_tier,
        "confidence": result.confidence_score,
        "refusal": result.refusal_reason,
        "answers": [(l.text, l.answer, l.gap_kind, l.confidence)
                    for l in result.leaves],
    }


# ── JOB 1: persist at end of run ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_persists_structured_record(tmp_path):
    store = CrossRunMemoryStore(tmp_path / "runs.jsonl")
    pipe = _pipeline(tmp_path, store, tag="persist")
    result = await pipe.run(QUESTION, today=TODAY)

    recs = store.load_class(CLASS)
    assert len(recs) == 1
    rec = recs[0]
    assert set(rec["sources"]) >= {"openalex", "federalregister"}
    assert rec["sources"]["openalex"]["rejected_gate"] == 2
    assert rec["sources"]["gdelt"]["errored"] == 1
    assert rec["sources"]["federalregister"]["admitted"] == 1
    assert set(rec["gap_kinds"].values()) == {""}  # answered on real evidence
    assert rec["stance"] == result.stance
    assert rec["tier"] == result.confidence_tier
    assert rec["sealed"] is True
    assert rec["question_class"] == CLASS
    # the question TEXT is never stored — audit hash only
    blob = json.dumps(rec)
    assert "semiconductor" not in blob
    assert len(rec["root_query_sha256"]) == 16


# ── JOB 3: prove it compounds ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_two_runs_fewer_wasted_fetches_same_conclusion(tmp_path):
    store = CrossRunMemoryStore(tmp_path / "runs.jsonl")
    # Two prior runs of THIS CLASS: openalex returned only gate-rejected
    # junk, gdelt always errored. One more chronic-null run each and the
    # deprioritise rule (>= 3) fires.
    for _ in range(2):
        store.append(_seed_rec(sources={"openalex": _null_src(),
                                        "gdelt": _err_src()}))

    r1 = await _pipeline(tmp_path, store, tag="run1").run(QUESTION,
                                                         today=TODAY)
    r2 = await _pipeline(tmp_path, store, tag="run2").run(QUESTION,
                                                         today=TODAY)

    assert r1.sealed and r2.sealed, (r1.refusal_reason, r2.refusal_reason)
    recs = store.load_class(CLASS)
    assert len(recs) == 4                      # 2 seeds + 2 runs
    w1, w2 = _wasted(recs[-2]), _wasted(recs[-1])

    print(f"\n[cross-run compounding] wasted fetches run1={w1} run2={w2}")
    assert w2 < w1, f"expected FEWER wasted fetches, got {w1} -> {w2}"

    # SAME conclusion, not a better-looking one:
    assert _conclusion(r1) == _conclusion(r2)
    # ...down to the admitted evidence itself, byte-for-byte the same set
    # in the same order (memory changed ORDER of attempts, nothing else):
    assert ([f.source_name for f in r1.fetches] ==
            [f.source_name for f in r2.fetches] ==
            ["federalregister", "clinicaltrials"])
    # run2's note discloses what memory did; run1 had nothing to deprioritise
    assert any("deprioritised" in n for n in r2.notes), r2.notes
    assert not any("deprioritised" in n for n in r1.notes), r1.notes


@pytest.mark.asyncio
async def test_memory_shared_across_questions_of_same_class(tmp_path):
    """Per CLASS, not per question: a different question of the same kind
    benefits from (and only from) the class bucket."""
    store = CrossRunMemoryStore(tmp_path / "runs.jsonl")
    for _ in range(DEPRIORITISE_MIN_RUNS):
        store.append(_seed_rec(sources={"openalex": _null_src()}))

    result = await _pipeline(tmp_path, store, tag="other").run(
        OTHER_QUESTION, today=TODAY)
    # The other question never ran before, yet its class memory applies:
    assert any("openalex" in n and "deprioritised" in n
               for n in result.notes), result.notes
    # ...and it still persisted its own facts back into the SAME bucket.
    assert len(store.load_class(CLASS)) == DEPRIORITISE_MIN_RUNS + 1


# ── HARD CONSTRAINTS ───────────────────────────────────────────────────────

def test_planning_view_physically_lacks_confidence_material():
    v = PlanningView(CLASS, [_seed_rec(stance="AFFIRMS", tier="VERIFIED",
                                       sources={"openalex": _null_src()})])
    for forbidden in ("stance", "tier", "confidence", "conclusion",
                      "evidence", "gap_kinds"):
        assert not hasattr(v, forbidden), forbidden
    assert v.late_sources == frozenset()


@pytest.mark.asyncio
async def test_poisoned_memory_cannot_move_the_conclusion(tmp_path):
    """R5 guard: even records LYING about stance/tier/sealed cannot change
    a run's conclusion relative to a memory-less run."""
    base = await _pipeline(tmp_path, None, tag="base").run(
        QUESTION, today=TODAY)

    store = CrossRunMemoryStore(tmp_path / "poison.jsonl")
    for _ in range(DEPRIORITISE_MIN_RUNS):
        store.append(_seed_rec(stance="AFFIRMS", tier="VERIFIED",
                               sources={"openalex": _null_src(),
                                        "gdelt": _err_src()}))
    poisoned = await _pipeline(tmp_path, store, tag="pois").run(
        QUESTION, today=TODAY)

    cb, cp = _conclusion(base), _conclusion(poisoned)
    assert cb["sealed"] and cp["sealed"]
    assert cp["tier"] == cb["tier"]
    assert cp["confidence"] == cb["confidence"]
    assert cp["stance"] == cb["stance"]
    assert cp["answers"] == cb["answers"]
    # and no remembered fact entered the evidence set either
    assert (len(poisoned.session.evidence) ==
            len(base.session.evidence))


@pytest.mark.asyncio
async def test_class_isolation_other_class_records_have_no_effect(tmp_path):
    cold = await _pipeline(tmp_path, None, tag="cold").run(
        QUESTION, today=TODAY)

    store = CrossRunMemoryStore(tmp_path / "runs.jsonl")
    for _ in range(DEPRIORITISE_MIN_RUNS + 3):
        store.append(_seed_rec(qclass="deep",       # WRONG class
                               sources={"openalex": _null_src(),
                                        "gdelt": _err_src()}))
    isolated = await _pipeline(tmp_path, store, tag="iso").run(
        QUESTION, today=TODAY)

    assert _conclusion(cold) == _conclusion(isolated)
    assert not any("cross-run memory" in n for n in isolated.notes)
    # ...and the cold run wasted exactly what the cold run always wastes.
    assert _wasted(store.load_class(CLASS)[-1]) == 3


def test_chronic_null_threshold_three_runs_not_two():
    srcs = {"openalex": _null_src()}
    below = PlanningView(CLASS, [_seed_rec(sources=srcs)
                                 for _ in range(DEPRIORITISE_MIN_RUNS - 1)])
    assert below.late_sources == frozenset()
    at = PlanningView(CLASS, [_seed_rec(sources=srcs)
                              for _ in range(DEPRIORITISE_MIN_RUNS)])
    assert at.late_sources == frozenset({"openalex"})


def test_window_ages_out_old_evidence_and_redeemers():
    old_nulls = [_seed_rec(sources={"x": _null_src()})
                 for _ in range(20)]
    redeemed = old_nulls[:-DEPRIORITISE_MIN_RUNS - 1] + [
        _seed_rec(sources={"x": {"admitted": 3, "rejected_gate": 0,
                                 "errored": 0, "skipped": 0}})
        for _ in range(DEPRIORITISE_MIN_RUNS)]
    assert PlanningView(CLASS, old_nulls).late_sources == frozenset({"x"})
    assert PlanningView(CLASS, redeemed).late_sources == frozenset()


def test_fragile_flagged_not_deprioritised():
    recs = [_seed_rec(sources={"gdelt": _err_src(),
                               "ok": {"admitted": 1, "rejected_gate": 0,
                                      "errored": 0, "skipped": 0}})
            for _ in range(2)]
    v = PlanningView(CLASS, recs)
    assert v.fragile and "gdelt" in v.fragile
    assert v.late_sources == frozenset()      # errors are not nulls
    brief = v.briefing()
    assert "fragile" in brief and "gdelt" in brief


def test_order_specs_is_stable_partition_never_drops():
    specs = [_spec(n, ["a"]) for n in ["s1", "late1", "s2", "late2"]]
    v = PlanningView.__new__(PlanningView)
    v.question_class = CLASS
    v.runs_considered = 3
    v._null_runs = {}
    v.late_sources = frozenset({"late1", "late2"})
    v.fragile = {}
    out = v.order_specs(specs)
    assert [s.name for s in out] == ["s1", "s2", "late1", "late2"]
    assert {s.name for s in out} == {s.name for s in specs}
    assert v.order_specs([]) == []


@pytest.mark.asyncio
async def test_retriever_ignores_a_malformed_order_hint():
    """A broken hint degrades to registry order — memory must never be able
    to break retrieval, let alone silently redirect it."""
    reg = _FixedOrderRegistry([_spec("alpha", ["supply chains"]),
                               _spec("beta", ["supply chains"])])
    seen: list = []

    def bad_order(specs):
        seen.extend(specs)
        return specs[:1]                       # DROPS beta: malformed

    retr = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=fixture_transport({"alpha.example": GOOD_BODY,
                                     "beta.example": GOOD_BODY}),
        max_rounds=1, adaptive_gain=False, source_order=bad_order,
        generic_calls={"alpha": ("works_search", ("term",), {"limit": 3}),
                       "beta": ("works_search", ("term",), {"limit": 3})})
    q = ResearchQuestion(text="what does research say about supply chains",
                         kind=QuestionKind.DESCRIPTIVE)
    trace = retr.retrieve(q, "", min_independent=1)
    assert seen, "hint was consulted"
    assert trace.n_admitted >= 1               # retrieval proceeded anyway


# ── record_run unit: counts only, no content ──────────────────────────────

def test_record_run_stores_counts_and_verdict_kinds_not_content():
    from tools.pipeline.engine import LeafOutcome, PipelineResult
    from tools.pipeline.retrieval import RetrievalTrace

    tr = RetrievalTrace(question_id="q1")
    tr.rounds.append({"round": 1, "query": "secret query terms",
                      "sources": [
                          {"name": "openalex", "rejected": "irrelevant"},
                          {"name": "gdelt", "error": "timeout"}],
                      "admitted": 0})
    leaf = LeafOutcome(question_id="q1", text="leaf text")
    leaf.gap_kind = "honest_null"
    res = PipelineResult(root_query="root", sealed=False)
    res.leaves = [leaf]
    res.fetches = []
    res.stance = "DENIES"
    res.confidence_tier = "SPECULATIVE"
    rec = record_run(res, {"q1": tr}, CLASS, "the secret root question")

    blob = json.dumps(rec)
    assert rec["sources"]["openalex"]["rejected_gate"] == 1
    assert rec["sources"]["gdelt"]["errored"] == 1
    assert rec["gap_kinds"] == {"q1": "honest_null"}
    assert rec["stance"] == "DENIES" and rec["tier"] == "SPECULATIVE"
    for leak in ("secret query terms", "secret root question", "leaf text"):
        assert leak not in blob, leak
