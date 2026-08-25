"""Integration-seam reproductions at ENGINE level: features in combination.

Companion to tests/test_integration_seams.py (retrieval level). Every test
here was run against the unmodified feature code and reproduces behaviour
documented in findings/integration_seams.md.

Run: python3 -m pytest tests/test_integration_seam_engine.py -q
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import json
import ssl  # must precede the socket guard
import sys
import tempfile

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, \
    _trace_from_payload, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.pipeline.stasis_stop import StasisStop  # noqa: E402

GOOD = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review"}]})
IRRELEVANT = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})

DECOMPOSE_ONE = json.dumps({"sub_questions": [{
    "text": "what does scholarly research say about semiconductor supply "
            "chain resilience",
    "kind": "descriptive",
    "question_type": "scholarly literature about semiconductor supply chains",
    "min_source_tier": 2, "min_independent_sources": 2}]})


class _Quiet:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _default_specs():
    return {
        "alpha": ("scholarly works on semiconductor supply chain resilience",
                  "https://api.openalex.org"),
        "beta": ("news events about semiconductor supply chains",
                 "https://api.gdeltproject.org"),
        "gamma": ("agency rules about supply chains", "https://c.example"),
    }


def _registry(specs_dict=None):
    from tools.sources.registry import SourceRegistry, SourceAdapter, \
        SourceSpec
    specs_dict = specs_dict or _default_specs()
    reg = SourceRegistry()

    def make_adapter(source):
        path = "/fetch_" + source.spec.name

        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    term = next((a for a in args if isinstance(a, str)),
                                kwargs.get("query_term", "q"))
                    url = source.build_url(
                        path, {"search": term.replace(" ", "+")})
                    return source.get_json(url)[0]
                return call
        return _Ad()

    calls = {}
    for name, (answers, url) in specs_dict.items():
        reg.register(SourceAdapter(
            spec=SourceSpec(name=name, base_url=url, description="",
                            answers=(answers,), tier=1,
                            min_interval_s=0.0),
            make_adapter=make_adapter))
        calls[name] = ("works_search", ("term",), {"limit": 3})
    return reg, calls


def _model(decompose, answer=""):
    return ScriptedModel({
        "Architect": [{"content": decompose}],
        "Manager": [{"content": json.dumps(
            {"answer": answer, "proposed_confidence": 0.7})}],
    })


def _pipeline(registry=None, routes=None, model=None, checkpointer=None):
    reg, calls = registry or _registry({
        "alpha": ("scholarly works on semiconductor supply chain resilience",
                  "https://api.openalex.org"),
        "beta": ("news events about semiconductor supply chains",
                 "https://api.gdeltproject.org"),
        "gamma": ("agency rules about supply chains", "https://c.example"),
    })
    pipe = ResearchPipeline(
        model=model or _model(DECOMPOSE_ONE), adversary_router=_Quiet(),
        transport=fixture_transport(routes or {}), store=None,
        ledger=ProvenanceLedger(), registry=reg, checkpointer=checkpointer)
    return pipe, reg, calls


def _run(pipe, reg, calls, *, adaptive_gain=True, stasis=False,
         max_rounds=3, max_spq=3, gate_cov=None):
    from tools.pipeline import retrieval as R
    orig_init = R.IterativeRetriever.__init__
    traces = []

    def patched(self, *a, **kw):
        kw["generic_calls"] = calls
        kw["adaptive_gain"] = adaptive_gain
        kw["max_rounds"] = max_rounds
        kw["max_sources_per_leaf"] = max_spq
        if gate_cov is not None:
            kw["gate"] = R.RelevanceGate(min_coverage=gate_cov)
        orig_init(self, *a, **kw)
        if stasis:
            self.stasis_stop = StasisStop()

    R.IterativeRetriever.__init__ = patched
    orig_ret = R.IterativeRetriever.retrieve

    def ret_wrap(self, *a2, **k2):
        tr = orig_ret(self, *a2, **k2)
        traces.append(tr)
        return tr

    R.IterativeRetriever.retrieve = ret_wrap
    try:
        result = asyncio.run(pipe.run(
            "What does research say about semiconductor supply chains?",
            today=datetime.date(2026, 8, 22)))
    finally:
        R.IterativeRetriever.__init__ = orig_init
        R.IterativeRetriever.retrieve = orig_ret
    return result, traces


# ── F1 at engine level ──────────────────────────────────────────────────────

def test_f1_engine_all_junk_stops_with_misattributed_route_reason():
    """Every source returns junk -> stop reason claims missing fetch routes
    although every source had a route and was JUDGED irrelevant. Stasis
    never gets the chance to fire."""
    reg, calls = _registry()
    routes = {"/fetch_alpha": IRRELEVANT, "/fetch_beta": IRRELEVANT,
              "/fetch_gamma": IRRELEVANT}
    pipe, _, _ = _pipeline((reg, calls), routes)
    result, traces = _run(pipe, reg, calls, adaptive_gain=False, stasis=True)
    assert traces, "no retrieval ran"
    for tr in traces:
        assert tr.stop_reason.startswith(
            "selected sources lack generic fetch routes"), tr.stop_reason


# ── F4 at engine level ──────────────────────────────────────────────────────

def test_f4_answered_leaf_unmet_requirements_marked_unprovable():
    """One admitted voice against min_independent=2: leaf answers on thin
    evidence and MUST carry the unprovable verdict with the requirement
    reason. (The empty-answer sibling of this case gets gap_kind='' — see
    F4 in findings/integration_seams.md.)"""
    reg, calls = _registry()
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": IRRELEVANT,
              "/fetch_gamma": IRRELEVANT}
    mdl = _model(DECOMPOSE_ONE,
                 answer="the literature suggests resilience improved")
    pipe, _, _ = _pipeline((reg, calls), routes, model=mdl)
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=2, gate_cov=0.25)
    leaf = result.leaves[0]
    # D2 seal-contract fix: an all-unprovable parent now REFUSES instead of
    # sealing a non-answer — see tests/test_seal_unprovable.py.
    assert not result.sealed
    assert "unprovable" in result.refusal_reason
    assert leaf.gap_kind == "unprovable", (leaf.gap_kind,
                                           leaf.requirement_reasons)


def test_f4_gap_classification_skipped_when_answer_empty_but_fetches_exist():
    """F4 core defect shape: fetches exist, requirements unmet, model answer
    EMPTY -> neither classification branch runs; gap_kind stays ''. The run
    then dies with 'every leaf came back unanswered', discarding the per-leaf
    verdict entirely."""
    reg, calls = _registry()
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": IRRELEVANT,
              "/fetch_gamma": IRRELEVANT}
    mdl = _model(DECOMPOSE_ONE, answer="")
    pipe, _, _ = _pipeline((reg, calls), routes, model=mdl)
    result, _ = _run(pipe, reg, calls, adaptive_gain=True, max_rounds=2,
                     max_spq=2, gate_cov=0.25)
    leaf = result.leaves[0]
    assert not result.sealed
    # Fail-closed fix: admitted fetches + unmet requirements -> unprovable,
    # even when the model's answer is empty. Never a silent fall-through.
    assert leaf.gap_kind == "unprovable", leaf.gap_kind
    assert "declared standard" in (leaf.gap_explanation or "")
    assert result.refusal_reason.startswith("every leaf came back unanswered")
    assert "unprovable" in result.refusal_reason


def test_f4_control_empty_answer_zero_admitted_gets_classification():
    """Control proving the branch exists when ZERO fetches were admitted:
    all-junk leaf with empty answer DOES classify (honest_null /
    retrieval_failure path). Only the partial-evidence case falls through."""
    reg, calls = _registry()
    routes = {"/fetch_alpha": IRRELEVANT, "/fetch_beta": IRRELEVANT}
    mdl = _model(DECOMPOSE_ONE, answer="")
    pipe, _, _ = _pipeline((reg, calls), routes, model=mdl)
    result, _ = _run(pipe, reg, calls, adaptive_gain=True)
    # D2 fix: refusal names the structured kinds.
    assert result.refusal_reason.startswith("every leaf came back unanswered (")
    assert "honest_null" in result.refusal_reason or \
        "retrieval_failure" in result.refusal_reason
    for leaf in result.leaves:
        assert leaf.gap_kind != "", leaf.gap_kind


# ── F5: checkpoint restore drops rounds ─────────────────────────────────────

def test_f5_checkpoint_restores_rounds_null_and_crossrun_fidelity():
    """Regression (was: checkpoint restore dropped rounds). A modern fetch
    checkpoint restores the full retrieval audit state, so a resumed run's
    classify_null_kind discloses partial/error coverage and crossrun.
    record_run retains beta's errored count — same facts as the live run."""
    from tools.pipeline.crossrun import record_run
    from tools.gaps import classify_null_kind

    payload = {
        "fetches": [],
        "rejections": [{"source_name": "alpha", "url": "u",
                        "reason": "irrelevant", "relevance_score": 0.1,
                        "content_sha256": "x"}],
        "admitted_fetches": [],
        "rounds": [
            {"round": 1, "query": "q", "admitted": 0, "sources": [
                {"name": "alpha", "rejected": "irrelevant"},
                {"name": "beta", "error": "HTTP 503"}]},
        ],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": ["q"],
        "stop_reason": "round budget exhausted"}
    tr = _trace_from_payload("q1", payload)
    assert tr.rounds and tr.rounds[0]["sources"][1] == \
        {"name": "beta", "error": "HTTP 503"}

    kind, expl = classify_null_kind(tr)
    assert "errored" in expl.lower() or "partial" in expl.lower()

    class _L:
        fetches = []
        leaves = []
    rec = record_run(_L(), {"q1": tr}, "default", "Q")
    assert rec["sources"]["beta"]["errored"] == 1


def test_f5_legacy_checkpoint_without_rounds_stays_safe():
    """Legacy-absence control: a checkpoint with no new fields degrades to
    empty audit state — no invented rounds, admissions, or outcomes."""
    tr = _trace_from_payload("q1", {
        "fetches": [],
        "rejections": [{"source_name": "alpha", "url": "u",
                        "reason": "irrelevant", "relevance_score": 0.1,
                        "content_sha256": "x"}],
        "independent_keys": [], "queries": ["q"],
        "stop_reason": "round budget exhausted"})
    assert tr.rounds == [] and tr.admitted == []
    assert tr.skipped_sources == [] and tr.gain_skipped == []


def test_trace_roundtrip_preserves_full_audit_state():
    """Modern payload round trip: rounds, planner skips, gain skips, stop
    reason survive; `admitted` reflects only restored admitted fetches."""
    from tools.pipeline.engine import FetchResult
    from tools.pipeline.retrieval import RetrievalTrace

    def _FR(name, url):
        return FetchResult(source_name=name, url=url,
                           content_sha256=name + "-sha",
                           body="body-" + name, parsed=None,
                           question_id="q1",
                           fetched_at="2026-08-25T00:00:00Z")
    live = RetrievalTrace(question_id="q1")
    fr_beta = _FR("beta", "https://b/1")
    live.admitted.append(fr_beta)
    live.rounds = [
        {"round": 1, "query": "q", "admitted": 1, "sources": [
            {"name": "beta", "admitted": True, "relevance": 0.9},
            {"name": "gamma", "skipped": "no route"}]},
        {"round": 2, "query": "q2", "admitted": 0, "sources": []},
    ]
    live.skipped_sources = [{"name": "delta", "reason": "planner gap"}]
    live.gain_skipped = [{"round": 2, "source": "epsilon",
                          "reason": "duplicate independent voice"}]
    trace_q = live
    # serialize exactly as the engine does
    payload = {
        "fetches": [dataclasses.asdict(f) for f in trace_q.admitted],
        "rejections": [dataclasses.asdict(r)
                       for r in trace_q.rejected],
        "admitted_fetches": [
            {"source_name": f.source_name, "url": f.url}
            for f in trace_q.admitted],
        "rounds": list(trace_q.rounds),
        "skipped_sources": list(trace_q.skipped_sources),
        "gain_skipped": list(trace_q.gain_skipped),
        "independent_keys": sorted(trace_q.independent_keys),
        "queries": list(trace_q.queries),
        "stop_reason": trace_q.stop_reason}
    restored = _trace_from_payload("q1", json.loads(json.dumps(payload)))
    assert restored.rounds == live.rounds
    assert restored.skipped_sources == live.skipped_sources
    assert restored.gain_skipped == live.gain_skipped
    assert [f.source_name for f in restored.admitted] == ["beta"]
    assert restored.admitted[0].url == fr_beta.url
    assert restored.independent_keys == set()


# ── Seam 5: crossrun store round-trip, all-on ───────────────────────────────

def test_s5_crossrun_roundtrip_stable_and_wellformed():
    """Two identical runs through one shared CrossRunMemoryStore: conclusions
    stable, records well-formed, no leakage of stance into ordering on thin
    samples."""
    from tools.pipeline.crossrun import CrossRunMemoryStore
    tmp = tempfile.mkdtemp()
    store = CrossRunMemoryStore(path=__import__("os").path.join(
        tmp, "runs.jsonl"))
    reg, calls = _registry()
    routes = {"/fetch_alpha": GOOD, "/fetch_beta": GOOD}

    def once(label):
        pipe, r2, c2 = _pipeline((reg, calls), routes,
                                 model=_model(DECOMPOSE_ONE))
        # inject store
        pipe.crossrun_store = store
        return _run(pipe, r2, c2)

    r1, t1 = once("run1")
    r2, t2 = once("run2")
    lines = open(store.path).read().strip().splitlines()
    assert len(lines) == 2
    recs = [json.loads(x) for x in lines]
    for rec in recs:
        # alpha admitted once per run; beta/gamma appear with their
        # gate verdict counts and nothing leaks beyond counts.
        assert rec["sources"]["alpha"]["admitted"] == 1
        assert all(set(c) == {"admitted", "rejected_gate", "errored",
                              "skipped"}
                   for c in rec["sources"].values())
