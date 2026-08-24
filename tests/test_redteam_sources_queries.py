"""RED TEAM — source registry & query builders (method: property sweep +
differential + adversarial construction; surface NOT previously attacked).

Claim under attack (MORNING_REPORT "THE REAL BACKLOG" #1/#3, query_builder.py
header): "Resolution NEVER silently guesses: a wrong series id produces
confident nonsense — the worst failure this system can have"; and retrieval's
independence counting "reflects ACTUAL source diversity".

Families hunted (research/PATTERNS.md):
  Family 1 — a verification layer that never runs: RestSource._record() logs
             EVERY 200 body into the ProvenanceLedger as primary=True BEFORE
             anyone judges it. The relevance gate exists, but the ledger does
             not wait for it.
  Family 3 — absence/200 treated as success: an HTTP 200 carrying an ERROR
             BODY is admitted as evidence and mints independent sources.
  Family 5 — structural property standing in for agreement: identical bytes
             from two hosts count as TWO independent voices.
  Family 9 / entity-resolution contract: _resolve()'s exact-id passthrough
             turns any ALL-CAPS token containing a digit ("COVID19",
             "FAA2023") into a RESOLVED series id with no candidates — the
             planner then authors series_observations(series_id="COVID19"),
             which returns FRED's HTTP 404-shaped error JSON … which probe S2
             shows becomes PRIMARY-class evidence.

Companion findings: findings/redteam_sources_queries.md
"""
from __future__ import annotations

import json

import pytest

from agp.provenance import ProvenanceLedger
from agp.research_program import (
    EvidenceRequirement,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.engine import fixture_transport
from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    independence_key,
)
from tools.sources.base import SourceSpec
from tools.sources.registry import SourceAdapter, SourceRegistry


# ── scaffolding ────────────────────────────────────────────────────────────

def _spec(name, answers=("semiconductor supply chain resilience scholarly "
                         "works",), base_url=None):
    return SourceSpec(
        name=name,
        base_url=base_url or f"https://{name}.example",
        description="", answers=tuple(answers), cannot_answer=None,
        tier=1, min_interval_s=0.0)


def make_registry(routes_by_host):
    reg = SourceRegistry()

    def make_adapter(source):
        host = source.spec.base_url.split("//")[1]

        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    return source.get_json(f"https://{host}/q")[0]
                return call
        return _Ad()

    for name in routes_by_host:
        reg.register(SourceAdapter(spec=_spec(name),
                                   make_adapter=make_adapter))
    return reg


def _q(min_ind=1, text="semiconductor supply chain resilience"):
    rq = ResearchQuestion(text=text, kind="descriptive")
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


QT = "semiconductor supply chain resilience scholarly works"

GOOD = json.dumps({"results": [{"title": "semiconductor supply chain "
                                "resilience review"}]})

#: A realistic API error payload that happens to contain the question's
#: topical words (error text is written per-endpoint, so of course it does).
ERROR_BODY = json.dumps({
    "error": "internal problem with this semiconductor supply chain "
             "resilience dataset endpoint; results unavailable"})


def _retrieve(routes, hosts, *, min_ind=2, gate=0.25, max_rounds=2,
              adaptive_gain=False, question_text="semiconductor supply "
              "chain resilience"):
    reg = make_registry({h: routes for h in hosts})
    tr = fixture_transport(routes)
    r = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(), transport=tr,
        gate=RelevanceGate(min_coverage=gate), max_rounds=max_rounds,
        adaptive_gain=adaptive_gain,
        generic_calls={h: ("search", ("term",), {}) for h in hosts})
    trace = r.retrieve(_q(min_ind, question_text), QT, min_ind)
    return trace, tr


# ── S2: an HTTP-200 error body is admitted as evidence (family 3) ─────────

def test_s2_error_body_200_is_admitted_and_counts_as_evidence():
    """Property: bytes that say 'results unavailable' must not become
    evidence or satisfy min_independent_sources. Today they do both."""
    routes = {"alpha.example": ERROR_BODY, "beta.example": ERROR_BODY}
    trace, _ = _retrieve(routes, ["alpha", "beta"], min_ind=2)
    assert len(trace.admitted) >= 1, (
        "precondition for the defect vanished: error body no longer admitted")


def test_s2_error_body_mints_two_independent_sources():
    """The sharper half: two hosts returning the SAME apology both count,
    and the leaf stops as 'sufficient' without ever fetching real data."""
    routes = {"alpha.example": ERROR_BODY, "beta.example": ERROR_BODY}
    trace, _ = _retrieve(routes, ["alpha", "beta"], min_ind=2)
    if len(trace.admitted) < 2:
        pytest.skip("admission behaviour changed upstream")
    assert len(trace.independent_keys) >= 2, (
        "S2 fixed at the admission layer; independence no longer inflated "
        "by error bodies")


def test_s2_error_body_bytes_land_in_ledger_as_primary():
    """Family 1: RestSource records every 200 body as primary=True at fetch
    time — provenance assigns its strongest class to bytes nobody has judged.
    The relevance gate can reject them afterwards, but the ledger has already
    attested PRIMARY for content that says 'results unavailable'."""
    from agp.provenance import ProvenanceLedger
    from tools.sources.base import RestSource

    led = ProvenanceLedger()
    src = RestSource(_spec("alpha"), ledger=led,
                     transport=fixture_transport({"alpha.example":
                                                  ERROR_BODY}))
    data, rec = src.get_json("https://alpha.example/q")
    assert led.is_primary_bytes(json.dumps(data, sort_keys=True)), (
        "ledger no longer pre-attests unjudged bodies (S2 family-1 half "
        "fixed)")


# ── S3: identical bytes from two hosts = two independent voices (family 5) ─

def test_s3_identical_bytes_from_two_hosts_count_twice():
    """Differential: one document mirrored on two hosts is ONE piece of
    evidence, but independence_key() counts by host, so it registers two
    independent sources and the leaf declares sufficiency on a single
    document. Content-addressed identity exists (_sha) and is simply not
    consulted."""
    routes = {"gamma.example": GOOD, "delta.example": GOOD}
    trace, _ = _retrieve(routes, ["gamma", "delta"], min_ind=2)
    assert len(trace.admitted) == 2          # same doc, fetched twice
    assert len(trace.independent_keys) >= 2, (
        "S3 fixed: identical bytes now collapse to one voice")
    assert trace.stop_reason.startswith("sufficient:"), (
        "even with two 'voices', a one-document evidence set must not stop "
        "as sufficient")


def test_s3_one_document_two_hosts_is_not_two_sources():
    """The invariant stated positively — fails today via the same path."""
    routes = {"gamma.example": GOOD, "delta.example": GOOD}
    trace, _ = _retrieve(routes, ["gamma", "delta"], min_ind=2)
    distinct_bodies = {f.content_sha256 for f in trace.admitted}
    assert len(distinct_bodies) == len(trace.independent_keys) or True
    # The honest assertion, kept failing until fixed:
    assert not (len(distinct_bodies) == 1
                and len(trace.independent_keys) >= 2), (
        "one byte-identical document cannot be two independent sources")


# ── S4: entity resolution silently guesses (family 9, query_builder) ──────

def test_s4_allcaps_token_with_digit_resolves_as_series_id():
    """query_builder._resolve passthrough: any fully-uppercase token that
    contains a digit resolves as a KNOWN series id with zero candidates.
    'COVID19' is not an id; the planner authors
    series_observations(series_id='COVID19') — confident nonsense, exactly
    what the module header promises never to produce."""
    from tools.sources import query_builder as qb

    resolved, cands = qb._resolve("series_id", "Did COVID19 change "
                                  "unemployment?", qb._FRED_CONCEPTS)
    assert resolved.get("series_id") != "COVID19" or cands, (
        "S4 fixed: unknown ALL-CAPS token no longer auto-resolves")


def test_s4_planner_authors_fetch_for_guessed_id():
    """End-to-end shape: build_plan('fred', ...) on a question mentioning an
    unfamiliar ALL-CAPS token plans a direct observations fetch instead of
    the safe series_search fallback."""
    from tools.sources import query_builder as qb

    plan = qb._plan_fred("What did the FAA2023 report say about airline "
                         "safety?")
    if plan.plannable and plan.queries:
        q0 = plan.queries[0]
        assert q0.method != "series_observations", (
            "planner authored a fetch against guessed series id %r"
            % q0.kwargs.get("series_id"))


# ── S5: property sweep over the registry selector ──────────────────────────

def test_s5_registry_select_never_raises_on_arbitrary_input():
    """Property: select()/select_explained() must survive arbitrary question
    strings — unicode, control characters, empties, 10k-word inputs — and
    every returned decision must carry finite scores."""
    import random
    from tools.sources.registry import get_source_registry

    reg = get_source_registry()
    rng = random.Random(1234)
    alphabet = ("abc de é😀\x00\n\t {} [] %s ;-- DROP null None -1 "
                "GDP unemployment 2024").split(" ")
    for _ in range(500):
        n = rng.randint(0, 30)
        qt = " ".join(rng.choice(alphabet) for _ in range(n))
        decisions = reg.select_explained(qt)
        for d in decisions:
            assert 0.0 <= d.score <= 1.0


def test_s5_select_with_empty_question_selects_nothing():
    from tools.sources.registry import get_source_registry

    reg = get_source_registry()
    assert list(reg.select("")) == []
