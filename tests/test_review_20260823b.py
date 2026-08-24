"""Review run 2026-08-23b — findings/review_2026-08-23b.md repros.

Families hunted: 1 (verification layer that never runs), 2 (fix lands in one
copy, another keeps the bug), 3 (absence treated as success), 6 (direction of
error). Each test below is a REPRODUCTION of a defect found in recent,
unreviewed work on origin/master (build/derived-analysis-loop,
build/information-gain merges) or its unmerged sibling (build/source-health).

These tests are expected to FAIL against the code they indict; each carries a
comment naming the defect. When a fix lands, flip the assertion and delete the
indict comment.
"""
from __future__ import annotations

import json
import ssl  # must precede the socket guard
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.helpers.no_socket import NoSocket

NoSocket().install()

from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.engine import fixture_transport  # noqa: E402
from tools.pipeline.retrieval import IterativeRetriever  # noqa: E402
from tools.derived_analysis import (  # noqa: E402
    Relationship,
    detect_anomalies,
    select_for_emission,
)
from tools.sources.registry import get_source_registry  # noqa: E402


def _q(text="Will Apple report quarterly results above consensus?",
       min_ind=5):
    rq = ResearchQuestion(text=text, kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


def _retriever(routes, max_rounds=3):
    return IterativeRetriever(
        registry=get_source_registry(), ledger=ProvenanceLedger(),
        transport=fixture_transport(routes), max_rounds=max_rounds)


# ════════════════════════════════════════════════════════════════════════
# R1 (family 1 — the check/instrument that never runs): refine_query output
# is recorded on trace.queries but NEVER sent in planner mode.
# build_plan() authors every query from question.text alone, so round N+1
# re-fetches with the round-1 query string. The "iterative" loop's inspect-
# and-refine half is dead in the default configuration, AND the trace lies
# about it (trace.queries shows refined strings that were never used).
def test_r1_refined_query_is_actually_sent():
    seen_queries: list[str] = []

    routes = {
        "/doc": json.dumps({"articles": [
            {"title": "Apple quarterly earnings results above Wall Street "
                      "consensus expectations in its next report",
             "url": "https://news0.example.org/a",
             "seendate": "20240110T120000"}]}),
        "/api/v2/studies": json.dumps({"studies": [
            {"protocolSection": {"identificationModule": {
                "nctId": "NCT1",
                "briefTitle": "Apple consensus expectations study"}}}]}),
    }
    r = _retriever(routes)
    real_build = __import__("tools.sources.query_builder",
                            fromlist=["build_plan"]).build_plan

    def spy_build_plan(source_name, question):
        plan = real_build_plan(source_name, question)
        if plan.plannable:
            for pq in plan.queries:
                blob = json.dumps([list(pq.args), pq.kwargs])
                seen_queries.append(blob)
        return plan

    import tools.sources.query_builder as qb
    qb.build_plan = spy_build_plan
    try:
        t = r.retrieve(_q(), "news coverage of events", min_independent=5)
    finally:
        qb.build_plan = real_build

    assert len(t.queries) >= 2, "expected multiple rounds"
    # The trace claims refinement happened...
    assert len(set(t.queries)) == len(t.queries), (
        f"trace.queries should show refinement, got {t.queries}")
    # ...and the queries actually SENT must match what the trace records.
    # DEFECT: every sent query contains only round-1 tokens; 'next' etc.
    # from later rounds' refine_query output never reaches build_plan.
    sent_joined = " ".join(seen_queries)
    for recorded in t.queries[1:]:
        for token in recorded.split():
            if token not in t.queries[0]:
                assert token in sent_joined, (
                    f"refined token {token!r} appears in trace.queries but "
                    f"was never sent to any source — refine_query output is "
                    f"dead in planner mode")


# ════════════════════════════════════════════════════════════════════════
# R2 (family 6 — direction/meaninglessness of error): commit 54e9a81 made
# magnitude = |observed - median| / sigma where sigma falls back to an
# epsilon (max(|med|*1e-6, 1e-9)) when history dispersion is zero. A
# zero-dispersion series therefore reports ABSURD magnitudes (a 2e-5 move
# = "20 robust sigmas") which then WIN select_for_emission's ranking over
# genuine anomalies from well-dispersed histories — the bound exists to
# stop noise flooding the pipeline, and this orders noise FIRST.
def test_r2_epsilon_sigma_must_not_outrank_genuine_anomaly():
    genuine = Relationship(  # ~15 robust sigmas, real dispersion baseline
        key="genuine_break", description="real business break", unit="ratio",
        compute=lambda s: {"FY1": 1.0, "FY2": 1.05, "FY3": 0.98,
                           "FY4": 1.45})
    artifact = Relationship(  # 20 EPSILON sigmas, zero-dispersion baseline
        key="epsilon_artifact", description="rounding-scale wobble",
        unit="ratio",
        compute=lambda s: {"FY1": 1.0, "FY2": 1.0, "FY3": 1.0,
                           "FY4": 1.00002})
    anomalies = detect_anomalies([genuine, artifact], {}, entity="X")
    by_key = {a.relationship_key: a for a in anomalies}
    assert set(by_key) == {"genuine_break", "epsilon_artifact"}
    # DEFECT: epsilon-sigma artifact scores HIGHER than the genuine anomaly
    assert by_key["epsilon_artifact"].magnitude <= \
        by_key["genuine_break"].magnitude * 1.001, (
        f"zero-dispersion epsilon band inflates magnitude to "
        f"{by_key['epsilon_artifact'].magnitude} vs genuine "
        f"{by_key['genuine_break'].magnitude}: emission ordering is "
        f"dominated by rounding artifacts")
    selected, _ = select_for_emission(anomalies, limit=1)
    assert selected[0].relationship_key == "genuine_break", (
        f"emitted {selected[0].relationship_key!r} — a 2e-5 wobble beat a "
        f"genuine multi-sigma break for the single question slot")


# ════════════════════════════════════════════════════════════════════════
# R3 (families 1+3): on master, tools/sources/health.py probes cftc /
# sec_fts / semantic_scholar but the registry registers those sources as
# cftc_cot / sec_fulltext / semanticscholar. _build(name) returns None ->
# the probe dies -> BROKEN regardless of live health; the REAL names have
# no probe at all and run_all() iterates PROBES keys only, so nothing ever
# cross-checks. Three of twenty sources are invisible to the health layer.
# FIXED on unmerged origin/build/source-health — this test pins master.
def test_r3_every_registered_source_has_matching_probe():
    import tools.sources.health as H
    registered = {get_source_registry().get(n).spec.name
                  for n in get_source_registry().names()
                  if get_source_registry().get(n) is not None}
    probed = set(H.PROBES)
    missing = registered - probed
    orphaned = probed - registered
    assert not missing, (
        f"registered sources with NO health probe (invisible to health "
        f"layer): {sorted(missing)}")
    assert not orphaned, (
        f"probes keyed by names matching NO registered source — they can "
        f"only ever report BROKEN via None-unpack: {sorted(orphaned)}")


# ════════════════════════════════════════════════════════════════════════
# R4 (the inert-component check): the finance DomainPlugin — carrying
# edgar_get_statements / edgar_anomalies / edgar_build_model, and the whole
# derived-analysis entry point — has register_if_available() that NOTHING
# calls. orchestrator._default_registry seeds sports + compute only, so
# dispatch("edgar_anomalies", ...) falls through to the legacy dispatcher
# and fails. The derived-analysis loop merged to master is unreachable
# through any production front door.
def test_r4_finance_plugin_dispatchable_after_orchestrator_seed():
    from tools.domain_registry import get_tool_registry
    from tools.domains.sports import build_sports_plugin
    from tools.domains.compute import register_if_available as reg_compute

    reg = get_tool_registry()
    # Reproduce exactly what orchestrator._default_registry seeds:
    reg.core_tools[:] = []
    reg.register(build_sports_plugin([], None))
    reg_compute(reg)

    owned = {s.get("function", {}).get("name")
             for p in reg.plugins() for s in p.tool_schemas}
    assert "edgar_anomalies" in owned, (
        f"finance plugin never registered by any production caller; tools "
        f"dispatchable after orchestrator seed: {sorted(owned)}")


# ════════════════════════════════════════════════════════════════════════
# R5 (family 2 — one rule, two copies, one dead): base.independence_family()
# keeps the RAW membership test (`spec_name in members`) that was the
# original defect in fix_d2-era findings, while retrieval.in_family() is the
# normalised canonical rule. base.independence_family has zero callers today
# (latent), so this test pins DIVERGENCE, not live damage: feed both rules a
# spelling variant and require agreement.
def test_r5_independence_membership_rules_agree():
    from tools.sources.base import independence_family
    from tools.pipeline.retrieval import independence_key, in_family
    from tools.sources.base import INDEPENDENCE_FAMILIES

    for variant in ("Semantic-Scholar", "semantic_scholar", "semanticscholar"):
        base_answer = independence_family(variant)
        canon = next((fam for fam, mem in INDEPENDENCE_FAMILIES.items()
                      if in_family(variant, mem)), variant)
        assert base_answer == canon, (
            f"independence membership diverges: base.independence_family("
            f"{variant!r})={base_answer!r} vs canonical normalised rule="
            f"{canon!r} — two copies of one rule, the raw one still alive")
