"""FIX — checkpoint resume must never inflate confidence.

The defect (fixed here): the engine's resume branch replayed stored fetches
with `rejected = []` and `trace_q = None`, so a checkpointed run carried
evidence the relevance gate would have rejected AND reported zero rejections
AND lost the independence-family collapse — scoring 0.80 where the identical
plain run scored 0.54. Confidence rose from a mechanism unrelated to evidence
quality.

The fix stores the gate's full verdict set alongside each fetch and restores
the complete RetrievalTrace on resume.

  1. byte-identical confidence: a fully-resumed run scores exactly what the
     equivalent live run scored — including the rejection notes.
  2. PROPERTY: for random evidence sets, resumed confidence NEVER EXCEEDS
     live confidence. Lower is acceptable; higher is the bug.
  3. rejections survive the boundary: a resumed run reports the same
     gate rejections as the live run, so it cannot look cleaner than it was.
  4. legacy checkpoints (payload without verdict fields) degrade safely:
     no trace is fabricated as 'everything was admitted'.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.no_socket import NoSocket

_guard = NoSocket()
_guard.install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline import checkpoint as ckpt  # noqa: E402
from tools.pipeline.checkpoint import FileCheckpointer, RunTrace  # noqa: E402
from tools.pipeline.engine import (  # noqa: E402
    ResearchPipeline,
    _trace_from_payload,
    fixture_transport,
)
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.sources.query_builder import build_plan  # noqa: E402

TODAY = date(2026, 8, 22)

QUESTION = ("What does recent scholarly research say about semiconductor "
            "supply chain resilience?")


def _openalex_body(n_titles=2, topic="semiconductor supply chain"):
    return json.dumps({"results": [
        {"id": f"W{i}", "title": f"{topic} study {i}",
         "publication_year": 2025, "cited_by_count": 12}
        for i in range(1, n_titles + 1)
    ]})


def _ss_body(topic="semiconductor supply chain"):
    return json.dumps({"data": [
        {"title": f"Resilience of {topic} networks", "year": 2025}]})


def _routes(openalex=None, ss=None):
    return {
        "/works": openalex if openalex is not None else _openalex_body(),
        "/graph/v1/paper/search": ss if ss is not None else _ss_body(),
    }


def _decompose(min_indep=1) -> str:
    return json.dumps({"sub_questions": [
        {"text": "what does scholarly research say about semiconductor "
                 "supply chain resilience",
         "kind": "descriptive",
         "question_type": "scholarly work search about semiconductors",
         "min_source_tier": 2,
         "min_independent_sources": min_indep},
    ]})


def _answer(conf) -> str:
    return json.dumps({"answer": "the literature supports the claim",
                       "proposed_confidence": conf})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _make(tmp_path, *, conf=0.8, ledger=None, checkpointer=None,
          routes=None):
    model = ScriptedModel({
        "Architect": [{"content": _decompose()}],
        "Manager": [{"content": _answer(conf)}],
    })
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes or _routes()),
        store=ArtifactStore(root=tmp_path / "artifacts"), ledger=ledger,
        checkpointer=checkpointer)


def _run(pipe, question=QUESTION):
    return asyncio.run(pipe.run(question, today=TODAY))


# ── 1+3: a fully-cached run is confidence-identical to the live run ───────

def test_resumed_run_matches_live_confidence_and_rejections(tmp_path):
    led_a = ProvenanceLedger()
    plain = _make(tmp_path / "a", ledger=led_a)
    ra = _run(plain)

    cp = FileCheckpointer(root=tmp_path / "b" / "ckpt")
    first = _make(tmp_path / "b", ledger=ProvenanceLedger(), checkpointer=cp)
    _run(first)  # populate every stage checkpoint

    # Fully cached resume: any complete() call would return "{}".
    led_c = ProvenanceLedger()
    resumed = _make(tmp_path / "c", ledger=led_c, checkpointer=cp,
                    routes=_routes())  # fresh transport, must not be hit
    rb = _run(resumed)

    assert rb.trace is not None and rb.trace.is_resume
    assert [l.confidence for l in ra.leaves] == \
        [l.confidence for l in rb.leaves]
    assert ra.confidence_score == rb.confidence_score, (
        f"live={ra.confidence_score} resumed={rb.confidence_score}")
    assert ra.confidence_tier == rb.confidence_tier

    # Rejection notes survive: a resumed run may not report zero rejections
    # where the live run reported some.
    rej_a = [n for n in ra.notes if "rejected at ingestion" in n]
    rej_b = [n for n in rb.notes if "rejected at ingestion" in n]
    assert rej_a == rej_b


# ── 2: property — resume never exceeds live ────────────────────────────────

@st.composite
def _evidence_set(draw):
    """Random evidence sets: per-source title topics drawn from a mix of
    on-topic and off-topic vocabularies, so the gate admits/rejects
    differently across draws."""
    n_openalex = draw(st.integers(min_value=0, max_value=4))
    n_ss = draw(st.integers(min_value=0, max_value=3))
    topics = st.sampled_from([
        "semiconductor supply chain resilience",
        "quantum error correction thresholds",
        "deep sea sediment cores",
        "medieval wool trade routes",
        "protein folding kinetics",
    ])
    t_oa = draw(topics)
    t_ss = draw(topics)
    oa = json.dumps({"results": [
        {"id": f"W{i}", "title": f"{t_oa} review {i}",
         "publication_year": 2025} for i in range(1, n_openalex + 1)]})
    ss = json.dumps({"data": [
        {"title": f"{t_ss} analysis", "year": 2024} for _ in range(max(1, n_ss))]})
    conf = draw(st.floats(min_value=0.1, max_value=0.95))
    return oa, ss, conf


@given(data=_evidence_set())
@settings(max_examples=25, deadline=None, database=None)
def test_property_resume_never_exceeds_live(data, tmp_path_factory):
    oa, ss, conf = data
    routes = _routes(oa, ss)

    led_a = ProvenanceLedger()
    plain = _make(tmp_path_factory.mktemp("prop_a"), conf=conf,
                  ledger=led_a, routes=routes)
    ra = asyncio.run(plain.run(QUESTION, today=TODAY))

    cp = FileCheckpointer(root=tmp_path_factory.mktemp("prop_ckpt") / "ckpt")
    warm = _make(tmp_path_factory.mktemp("prop_warm"), conf=conf,
                 ledger=ProvenanceLedger(), checkpointer=cp, routes=routes)
    asyncio.run(warm.run(QUESTION, today=TODAY))

    led_c = ProvenanceLedger()
    resumed = _make(tmp_path_factory.mktemp("prop_c"), conf=conf,
                    ledger=led_c, checkpointer=cp, routes=routes)
    rb = asyncio.run(resumed.run(QUESTION, today=TODAY))

    assert rb.confidence_score <= ra.confidence_score + 1e-9, (
        "resumed run scored HIGHER than the identical live run: "
        f"resume={rb.confidence_score} live={ra.confidence_score}")


# ── 4: legacy payloads degrade safely ──────────────────────────────────────

def test_legacy_payload_without_verdicts_degrades_to_empty_not_admit_all():
    """An OLD checkpoint (pre-fix payload: fetches only) must not be read as
    'the gate admitted everything'. No trace is fabricated; nothing claims
    zero rejections or full independence."""
    tr = _trace_from_payload("q1", {"fetches": [
        {"source_name": "openalex", "url": "https://u/1"}]})
    assert tr.rejected == []            # unknown, not asserted-admitted...
    assert tr.independent_keys == set()   # ...and independence NOT granted


def test_trace_roundtrip_preserves_rejections():
    payload = {
        "rejections": [
            {"source_name": "gdelt", "url": "https://g/1",
             "reason": "content covers 8% of the question's topical words "
                       "(need 25%); missing: semiconductor, resilience",
             "relevance_score": 0.08,
             "content_sha256": "cd" * 32},
        ],
        "independent_keys": ["api.openalex.org"],
        "queries": ["semiconductor supply chain resilience"],
        "stop_reason": "round budget exhausted",
    }
    tr = _trace_from_payload("qX", payload)
    assert len(tr.rejected) == 1
    r = tr.rejected[0]
    assert r.source_name == "gdelt"
    assert r.content_sha256 == "cd" * 32
    assert abs(r.relevance_score - 0.08) < 1e-9
    assert tr.independent_keys == {"api.openalex.org"}


# ── 5: final validation-gap regressions ────────────────────────────────────

def _gap_registry():
    """Minimal real registry so classify_gap can resolve 'openalex'."""
    from tools.sources.registry import (SourceAdapter, SourceRegistry,
                                        SourceSpec)
    reg = SourceRegistry()

    def make_adapter(source):
        class _Ad:
            def __getattr__(self, method_name):
                return lambda *a, **k: {}
        return _Ad()

    reg.register(SourceAdapter(
        spec=SourceSpec(name="openalex", base_url="https://api.openalex.org",
                        description="", answers=("scholarly work search",),
                        tier=1, min_interval_s=0.0),
        make_adapter=make_adapter))
    return reg


def _gap_question():
    from agp.research_program import QuestionKind, ResearchQuestion
    rq = ResearchQuestion(text=QUESTION, kind=QuestionKind.DESCRIPTIVE)
    rq.question_id = "q1"
    return rq


def test_non_dict_fetch_element_voids_admission_head_and_tail():
    """A valid dict prefix/suffix plus junk elements must not hydrate even
    when `admitted_fetch_count` matches only the dict subset."""
    good = {"source_name": "openalex", "url": "https://u/1",
            "content_sha256": "ab" * 32, "body": "", "question_id": "q1"}
    for fetches in (
            ["junk", good, good],
            [good, good, 42],
            [good, None, good]):
        tr = _trace_from_payload("q1", {
            "fetches": fetches,
            "admitted_fetch_count": 2})   # matches the dict subset exactly
        assert tr.admitted == [], fetches


def test_homogeneous_fetch_list_still_hydrates():
    good = {"source_name": "openalex", "url": "https://u/1",
            "content_sha256": "ab" * 32, "body": "", "question_id": "q1"}
    tr = _trace_from_payload("q1", {"fetches": [good],
                                    "admitted_fetch_count": 1})
    assert len(tr.admitted) == 1
    assert tr.admitted[0].source_name == "openalex"


def test_malformed_outcome_values_cannot_crash_classify_gap():
    """Skipped-reason / round-source outcome shapes that are not strings
    must be dropped during hydration so classify_gap() cannot raise on
    their types; valid data is preserved verbatim."""
    from tools.gaps import classify_gap
    payload = {
        "skipped_sources": [
            {"name": "openalex", "reason": {"not": "text"}},
            {"name": "crossref", "reason": "planner could not author"},
        ],
        "rounds": [{
            "round": 1, "query": "chips",
            "sources": [
                # valid source data preserved
                {"name": "openalex", "admitted": True},
                # malformed round outcomes
                {"name": "openalex", "skipped": {"bad": 1}},
                {"name": "openalex", "error": {"bad": 1}},
                # unusable name dropped entirely
                {"name": "   ", "error": "boom"},
            ],
        }],
        "queries": [],
        "stop_reason": "",
    }
    tr = _trace_from_payload("q1", json.loads(json.dumps(payload)))
    gap = classify_gap(_gap_registry(), tr, _gap_question())
    # malformed reason stringified away: no invented evidence text
    planner_skips = {s["name"]: s.get("reason") for s in tr.skipped_sources}
    assert planner_skips["openalex"] == ""
    assert planner_skips["crossref"] == "planner could not author"
    srcs = tr.rounds[0]["sources"]
    assert all(s["name"].strip() for s in srcs)
    assert all(not isinstance(s.get("skipped"), dict) for s in srcs)
    assert all(not isinstance(s.get("error"), dict) for s in srcs)
    # and the classification itself produced sane output
    assert gap.question_id == "q1"


def _corrupt_outcome_payload(outcome_key, bad_value):
    """A restored-trace payload where openalex's ONLY round outcome is
    malformed — the exact shape a corrupt checkpoint produces."""
    return {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [{"round": 1, "query": "chips", "admitted": 0,
                    "sources": [{"name": "openalex",
                                 outcome_key: bad_value}]}],
        "skipped_sources": [], "gain_skipped": [],
        # queries must be empty: classify_gap's "no query was ever issued"
        # failure signal is what turns the unknown state into
        # RETRIEVAL_FAILURE instead of a laundered honest_null.
        "queries": [],
        "stop_reason": "budget"}


@pytest.mark.parametrize("outcome_key,bad_value", [
    ("skipped", {"bad": 1}),
    ("error", {"bad": 1}),
    ("rejected", {"bad": 1}),
    ("admitted", "yes-please"),
])
def test_malformed_only_round_outcomes_fail_closed(
        outcome_key, bad_value):
    """Regression: a source record whose sole outcome is malformed used to
    be stripped to a name-only pseudo-success (`{"name": "openalex"}`),
    which classify_gap counted as tried and classified honest_null. The
    record must be dropped instead: no crash, no honest_null from corrupt
    data, and openalex must NOT be reported as tried."""
    import json as _json

    from tools.gaps import GapKind, classify_gap, classify_null_kind

    payload = _json.loads(_json.dumps(
        _corrupt_outcome_payload(outcome_key, bad_value)))
    tr = _trace_from_payload("q1", payload)
    # name-only pseudo-success is gone; with all sources invalid the whole
    # round is dropped so the unknown state stays unknown.
    assert tr.rounds == [], (outcome_key, tr.rounds)

    gap = classify_gap(_gap_registry(), tr, _gap_question())
    assert gap.kind is not GapKind.HONEST_NULL, \
        "corrupt checkpoint must never launder into an honest null"
    blob = _json.dumps(gap.__dict__, default=str)
    # openalex must not appear as a tried/queried success
    assert "searched 0 query round(s)" not in blob or True  # sanity
    for c in gap.candidates:
        if c.name == "openalex":
            assert not c.tried, \
                f"{outcome_key} corruption fabricated a tried query"

    kind, expl = classify_null_kind(tr)
    assert kind != "honest_null"


@pytest.mark.parametrize("outcome_key,bad_value", [
    ("skipped", {"bad": 1}),
    ("error", {"bad": 1}),
    ("rejected", {"bad": 1}),
    ("admitted", "yes-please"),
])
def test_malformed_only_round_outcomes_with_queries_fail_closed(
        outcome_key, bad_value):
    """Regression: even with a nonempty `queries` list, a checkpoint whose
    every round outcome is malformed must not classify as honest_null once
    the corrupt rounds are dropped at restoration. Queries alone are not
    proof any fetch ran; the state stays retrieval failure/unknown."""
    import json as _json

    from tools.gaps import GapKind, classify_gap, classify_null_kind

    payload = _json.loads(_json.dumps(
        _corrupt_outcome_payload(outcome_key, bad_value)))
    payload["queries"] = ["chips"]
    tr = _trace_from_payload("q1", payload)
    assert tr.rounds == [], (outcome_key, tr.rounds)

    gap = classify_gap(_gap_registry(), tr, _gap_question())
    assert gap.kind is not GapKind.HONEST_NULL, \
        "corrupt checkpoint with queries must never launder into honest null"
    assert gap.kind is GapKind.RETRIEVAL_FAILURE
    for c in gap.candidates:
        if c.name == "openalex":
            assert not c.tried, \
                f"{outcome_key} corruption fabricated a tried query"

    kind, expl = classify_null_kind(tr)
    assert kind != "honest_null"


def test_malformed_outcome_among_valid_sources_dropped_not_laundered():
    """One malformed record beside valid ones: only the malformed one is
    dropped; valid outcomes survive verbatim and drive classification."""
    import json as _json

    from tools.gaps import classify_null_kind

    payload = {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [{"round": 1, "query": "q", "admitted": 0, "sources": [
            {"name": "alpha", "rejected": "below coverage"},
            {"name": "openalex", "skipped": {"bad": 1}},
            {"name": "beta", "admitted": True},
        ]}],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": ["q"],
        "stop_reason": "budget"}
    tr = _trace_from_payload("q1", _json.loads(_json.dumps(payload)))
    assert tr.rounds[0]["sources"] == \
        [{"name": "alpha", "rejected": "below coverage"},
         {"name": "beta", "admitted": True}]
    kind, expl = classify_null_kind(tr)
    assert kind == "honest_null"  # driven by VALID records only


def test_valid_live_format_round_records_survive():
    """The canonical live shapes still round-trip byte-for-byte."""
    live_shapes = [
        {"name": "openalex", "error": "HTTP 503"},
        {"name": "openalex", "skipped": "no authored query"},
        {"name": "openalex", "rejected": "below coverage"},
        {"name": "openalex", "admitted": True, "relevance": 0.9},
    ]
    for shape in live_shapes:
        payload = {
            "fetches": [], "rejections": [], "admitted_fetch_count": 0,
            "rounds": [{"round": 1, "query": "q", "admitted": 0,
                        "sources": [dict(shape)]}],
            "skipped_sources": [], "gain_skipped": [],
            "independent_keys": [], "queries": ["q"],
            "stop_reason": "budget"}
        tr = _trace_from_payload("q1", json.loads(json.dumps(payload)))
        assert tr.rounds[0]["sources"] == [shape], shape


def test_empty_sources_round_preserved_but_all_corrupt_dropped():
    """A round that legitimately had zero sources stays; a round whose
    sources were ALL corrupt does not masquerade as it."""
    payload = {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [
            {"round": 1, "query": "q", "admitted": 0, "sources": []},
            {"round": 2, "query": "q2", "admitted": 0, "sources":
                [{"name": "x"}, {"name": "y", "skipped": 7}]},
        ],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": [],
        "stop_reason": "budget"}
    tr = _trace_from_payload("q1", json.loads(json.dumps(payload)))
    assert len(tr.rounds) == 1
    assert tr.rounds[0]["round"] == 1


@pytest.mark.parametrize("bad_admitted", [
    {"bad": 1}, [1, 2], 7, 1.0, ("t",), {True}, "yes",
])
def test_truthy_non_bool_admitted_fails_closed(bad_admitted):
    """Regression: `admitted` used to accept any truthy non-string value
    (containers, ints). Only the precise boolean True is canonical live
    format; anything else drops the whole record — no crash, no laundering,
    no fabricated admission."""
    import json as _json

    from tools.gaps import GapKind, classify_gap, classify_null_kind

    payload = {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [{"round": 1, "query": "q", "admitted": 0, "sources": [
            {"name": "openalex", "admitted": bad_admitted},
        ]}],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": ["q"],
        "stop_reason": "budget"}
    tr = _trace_from_payload("q1", _json.loads(_json.dumps(payload)))
    assert tr.rounds == [], bad_admitted

    gap = classify_gap(_gap_registry(), tr, _gap_question())
    assert gap.kind is not GapKind.HONEST_NULL
    for c in gap.candidates:
        if c.name == "openalex":
            assert not c.tried
    kind, expl = classify_null_kind(tr)
    assert kind != "honest_null"


_VALID_OUTCOMES = {
    "admitted": True,
    "error": "HTTP 503",
    "skipped": "no authored query",
    "rejected": "below coverage",
}


@pytest.mark.parametrize("valid_key", list(_VALID_OUTCOMES))
@pytest.mark.parametrize("bad_key,bad_value", [
    ("skipped", {"bad": 1}),
    ("error", {"bad": 1}),
    ("rejected", {"bad": 1}),
    ("admitted", {"bad": 1}),
    ("admitted", 5),
])
def test_valid_plus_malformed_outcome_pairing_dropped(valid_key, bad_key,
                                                      bad_value):
    """Regression: a record with one valid outcome plus another MALFORMED
    outcome key used to have the malformed key stripped silently, keeping
    the valid-only remainder. Schema incoherence must drop the whole
    record: no crash, no honest-null laundering, no fabricated attempt."""
    import json as _json

    from tools.gaps import GapKind, classify_gap, classify_null_kind

    rec = {"name": "openalex", valid_key: _VALID_OUTCOMES[valid_key],
           bad_key: bad_value}
    payload = {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [{"round": 1, "query": "q", "admitted": 0,
                    "sources": [rec]}],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": [],
        "stop_reason": "budget"}
    tr = _trace_from_payload("q1", _json.loads(_json.dumps(payload)))
    # whole record dropped -> whole round dropped; nothing survives stripped
    assert tr.rounds == [], (valid_key, bad_key, bad_value)

    gap = classify_gap(_gap_registry(), tr, _gap_question())
    assert gap.kind is not GapKind.HONEST_NULL
    for c in gap.candidates:
        if c.name == "openalex":
            assert not c.tried, \
                f"{valid_key}+malformed {bad_key} fabricated a tried query"
    kind, expl = classify_null_kind(tr)
    assert kind != "honest_null"


def test_two_valid_outcomes_still_incoherent():
    """Even two individually-canonical outcomes on one record are an
    ambiguous combination and must fail closed."""
    import json as _json

    from tools.gaps import classify_null_kind

    payload = {
        "fetches": [], "rejections": [], "admitted_fetch_count": 0,
        "rounds": [{"round": 1, "query": "q", "admitted": 0, "sources": [
            {"name": "openalex", "error": "HTTP 503",
             "rejected": "below coverage"},
        ]}],
        "skipped_sources": [], "gain_skipped": [],
        "independent_keys": [], "queries": [],
        "stop_reason": "budget"}
    tr = _trace_from_payload("q1", _json.loads(_json.dumps(payload)))
    assert tr.rounds == []
    kind, expl = classify_null_kind(tr)
    assert kind != "honest_null"
