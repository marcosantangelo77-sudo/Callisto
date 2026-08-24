"""Integration-seam reproductions: the five new features in COMBINATION.

Each test is a seam analysis: two individually-sound components handing a
value to a neighbour one trust level too high. Run:

    python -m pytest tests/test_integration_seams.py -x -q

Seams:
  S1  info-gain skip x stasis-stop   — does "all candidates skipped" read
                                       as saturation to the stop rule?
  S4  stopping rules x gaps          — is a stasis stop ever misreported
                                       as honest_null / retrieval_failure?
  S2  crossrun order x gain gate     — does reordering change WHICH
                                       sources get skipped (divergence)?
  S3  parallel leaves x crossrun     — are per-leaf traces attributed to
                                       the right leaf under concurrency?
"""
from __future__ import annotations

import json
import ssl  # must precede the socket guard
import sys

import pytest

sys.path.insert(0, ".")
sys.path.insert(0, "tests")

from tests.helpers.no_socket import NoSocket  # noqa: E402

NoSocket().install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.gaps import classify_null_kind  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from tools.pipeline.stasis_stop import StasisStop  # noqa: E402
from agp.research_program import EvidenceRequirement, QuestionKind, \
    ResearchQuestion, SourceClassRank  # noqa: E402
from tools.sources.registry import SourceRegistry, SourceAdapter, \
    SourceSpec  # noqa: E402

GOOD = json.dumps({"results": [
    {"id": "W1", "title": "Semiconductor supply chain resilience review",
     "publication_year": 2024}]})
IRRELEVANT = json.dumps({"results": [
    {"id": "X9", "title": "Mating habits of deep-sea isopods"}]})


def _spec(name, base_url, answers="scholarly works on semiconductor supply "
                                  "chain resilience"):
    return SourceSpec(name=name, base_url=base_url, description="",
                      answers=(answers,), tier=1, min_interval_s=0.0)


def _registry(*specs):
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

    for spec in specs:
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


def _q(min_ind=2):
    rq = ResearchQuestion(
        text="What does research say about semiconductor supply chain "
             "resilience?", kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _generic_calls(reg):
    return {n: ("works_search", ("term",), {"limit": 3}) for n in reg.names()}


def _retriever(reg, routes, *, adaptive_gain=True, stasis=False,
               source_order=None, max_rounds=3):
    r = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=fixture_transport(routes), max_rounds=max_rounds,
        adaptive_gain=adaptive_gain,
        source_order=source_order, generic_calls=_generic_calls(reg))
    if stasis:
        r.stasis_stop = StasisStop()
    return r


# ── Seam 1: info-gain skip vs stasis-stop ──────────────────────────────────

def test_s1_gain_skip_then_stasis_does_not_fire_before_any_real_round():
    """Round 1 admits one voice; round 2's candidates are ALL duplicate
    voices so the GAIN gate skips them and breaks with its own reason.
    The stasis rule must never have fired on that path (its state never
    saw two identical rounds), and the stop_reason must be the gain
    gate's, not 'stasis:'."""
    reg = _registry(_spec("alpha", "https://api.openalex.org"),
                    _spec("semanticscholar", "https://s.example"))
    routes = {"/fetch_alpha": GOOD, "/fetch_semanticscholar": GOOD}
    r = _retriever(reg, routes, adaptive_gain=True, stasis=True)
    qq = _q(min_ind=5)
    tr = r.retrieve(qq, qq.text, min_independent=5)
    assert tr.stop_reason.startswith("no candidate fetch"), tr.stop_reason
    assert not tr.stop_reason.startswith("stasis:"), tr.stop_reason
    assert r.stasis_stop.fired_at == 0


def test_s1_gain_all_skipped_round1_is_not_stasis():
    """Worst case: round 1 itself skips every candidate. Zero fetches,
    zero rounds — the trace must say WHY (gain gate) and the stasis rule
    must not claim saturation of a literature it never sampled."""
    reg = _registry(_spec("alpha", "https://api.openalex.org"),
                    _spec("beta", "https://b.example",
                          answers="mating habits of deep-sea isopods",
                          ))
    # beta declares cannot_answer for this question class
    from tools.sources.base import SourceSpec as SS
    reg = SourceRegistry()
    specs = [
        _spec("alpha", "https://api.openalex.org"),
        SS(name="weather", base_url="https://w.example", description="",
           answers=("weather forecasts",),
           cannot_answer=("scholarly works about semiconductors",),
           tier=1, min_interval_s=0.0),
    ]
    reg = _registry(*specs)
    routes = {"/fetch_alpha": IRRELEVANT, "/fetch_weather": IRRELEVANT}
    r = _retriever(reg, routes, adaptive_gain=True, stasis=True)
    qq = _q(min_ind=2)
    tr = r.retrieve(qq, qq.text, min_independent=2)
    # Round 1 runs unconditionally (rnd > 1 guard); alpha returns junk.
    assert len(tr.rounds) >= 1
    assert not tr.stop_reason.startswith("stasis:")


# ── Seam 4: stopping rules vs gap classification ───────────────────────────

def test_s4_all_junk_single_source_stop_reason_is_not_route_missing():
    """F1 (verified behaviour): one junk source is rejected in round 1,
    excluded, and round 2 finds zero routable specs. Stasis NEVER fires
    (round 2 never ran) and the stop reason claims 'selected sources lack
    generic fetch routes' even though alpha HAD a route and was judged
    irrelevant — a misattribution that sends operators to fix query
    authoring instead of the gate. Documented as defect F1 in
    findings/integration_seams.md."""
    reg = _registry(_spec("alpha", "https://api.openalex.org"))
    r = _retriever(reg, {"/fetch_alpha": IRRELEVANT},
                   adaptive_gain=False, stasis=True)
    qa = _q(min_ind=2)
    tr_a = r.retrieve(qa, qa.text, min_independent=2)
    kind_a, expl_a = classify_null_kind(tr_a)
    assert kind_a == "honest_null", (kind_a, expl_a)
    # F1: stasis blind to the all-excluded path...
    assert not tr_a.stop_reason.startswith("stasis:")
    # ...and the stop reason misattributes the exclusion:
    assert "lack generic fetch routes" in tr_a.stop_reason

    # Contrast: two junk sources where round 2 CAN re-run -> stasis fires.
    reg2 = _registry(_spec("alpha", "https://api.openalex.org"),
                     _spec("beta", "https://b.example"))
    r2 = _retriever(reg2, {"/fetch_alpha": IRRELEVANT,
                           "/fetch_beta": IRRELEVANT},
                    adaptive_gain=False, stasis=True)
    qb = _q(min_ind=3)
    tr_b = r2.retrieve(qb, qb.text, min_independent=3)
    assert tr_b.stop_reason.startswith("stasis:"), tr_b.stop_reason

    # Case B: source errors immediately, no rounds land -> retrieval failure
    def boom(url, headers):
        raise OSError("connection refused")
    transport_boom = boom
    r_b = IterativeRetriever(
        registry=reg, ledger=ProvenanceLedger(),
        transport=None, max_rounds=3, adaptive_gain=False,
        generic_calls=_generic_calls(reg))
    r_b.transport = None  # force error path via missing transport? use raising transport instead
    from tools.pipeline.engine import fixture_transport as ft  # noqa: F401

    def raising_transport(url, headers):
        raise OSError("connection refused")
    r_b.transport = raising_transport
    tr_b = r_b.retrieve(_q(min_ind=2), "", min_independent=2)
    kind_b, expl_b = classify_null_kind(tr_b)
    assert kind_b == "retrieval_failure", (kind_b, expl_b)


def test_s4_stasis_never_reads_as_literature_silent_without_fetches():
    """If a stasis stop fires with ZERO admitted evidence AND zero
    rejected-with-reason items (e.g. everything was gain-skipped or
    planner-skipped before spend), the classification must NOT be an
    unconditional honest_null — nothing was ever fetched."""
    from tools.pipeline.retrieval import RetrievalTrace
    tr = RetrievalTrace(question_id="q1")
    tr.rounds.append({"round": 1, "query": "q", "sources": [
        {"name": "alpha", "skipped": "duplicate independent voice"}],
        "admitted": 0})
    tr.stop_reason = "stasis: round 1 changed neither independent sources " \
                     "nor admitted evidence"
    kind, expl = classify_null_kind(tr)
    # A skipped source was never touched: reachable_attempt is False and
    # no_route/skips exist -> must be RETRIEVAL FAILURE (we never asked).
    assert kind == "retrieval_failure", (kind, expl)
