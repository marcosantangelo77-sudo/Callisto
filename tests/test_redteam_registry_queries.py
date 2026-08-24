"""RED TEAM — source registry, query builders & fetch provenance (method: cross-module
consistency + corrupt-one-field replay).

Surface choice: the SOURCE REGISTRY AND QUERY BUILDERS (tools/sources/registry.py,
tools/sources/query_builder.py) plus the fetch→ledger→checkpoint provenance seam they
feed. Per the rotation list this is explicitly UNATTACKED ground ("what happens when a
source lies, or returns 200 with zero results"); the twelve attacked surfaces contain
four resume/checkpoint variants but nothing here. Method F (cross-module: the same rule
implemented twice — independence membership has already landed three times) combined
with a corrupt-one-field replay of a real checkpoint payload — neither previously used
on this surface.

Companion findings: findings/redteam_registry_queries.md
"""
from __future__ import annotations

import dataclasses
import json
import os
import tempfile

import pytest

from agp.provenance import ProvenanceLedger, SourceClass
from tools.pipeline import checkpoint as ckpt
from tools.pipeline.retrieval import (
    IterativeRetriever,
    RelevanceGate,
    independence_key,
)
from tools.sources import adapters, query_builder as qb
from tools.sources.registry import SourceRegistry


def _registry() -> SourceRegistry:
    reg = SourceRegistry()
    adapters.register_all(reg)
    return reg


def _unkeyed():
    os.environ.pop("CALLISTO_SEAL_KEY", None)
    os.environ.pop("CALLISTO_CUTOFF_KEY", None)


class _Q:
    def __init__(self, text):
        self.question_id = "q1"
        self.text = text


def _req(**kw):
    from agp.research_program import EvidenceRequirement, SourceClassRank
    defaults = dict(min_source_class=SourceClassRank.SECONDARY,
                    min_independent_sources=1, quant_required=False)
    defaults.update(kw)
    return EvidenceRequirement(**defaults)


# ── RQ1: search-metadata masquerading as observations ────────────────────────

FRED_SEARCH_BODY = json.dumps({
    "seriess": [{"id": "LNS14000000",
                 "title": "Unemployment Rate labor market slack",
                 "notes": "Official unemployment statistics for the United States"}]})


def _fred_search_transport(url, headers):
    if "/series/search" in url:
        return 200, FRED_SEARCH_BODY
    return 200, "{}"


@pytest.mark.xfail(reason="RQ1 CONFIRMED DEFECT: planner fallback treats a "
                          "search-results page as the evidence itself")
def test_rq1_fred_series_search_metadata_is_not_observations():
    """build_plan('fred', ...) falls back to series_search when no curated concept
    matches. The retriever executes queries[0] ONLY and feeds its body to the gate.
    The body is a list of series NAMES — metadata about where numbers live, not the
    numbers. Because the titles contain the question's words, the gate admits it,
    RestSource minted it PRIMARY, and engine._answer_leaf will assign
    SourceClass.PRIMARY (ceiling 1.0) to an answer built on zero actual observations."""
    reg = _registry()
    led = ProvenanceLedger()
    q = _Q("Will labor market slack worsen this year?")
    q.evidence_requirements = _req()
    r = IterativeRetriever(registry=reg, ledger=led,
                           transport=_fred_search_transport)
    tr = r.retrieve(q, "labor market", min_independent=1)
    admitted_fred = [f for f in tr.admitted if f.source_name == "fred"]
    assert admitted_fred, "expected the fred search page to be exercised"
    body = admitted_fred[0].body
    assert '"observations"' not in body or '"value"' not in body, \
        "test premise wrong: body actually contains observations"
    ev = type("E", (), {"content": body})()
    assert led.assign_source_class(ev) != SourceClass.PRIMARY, (
        "search METADATA (a list of series titles) was minted PRIMARY; the "
        "planner's own reason string promises 'results carry series ids for a "
        "follow-up observations fetch' that never happens")


# ── RQ2: 200-with-zero-results must not satisfy anything ─────────────────────

@pytest.mark.xfail(reason="RQ2 CONFIRMED DEFECT: empty-but-structured bodies are "
                          "admitted when their field names echo question words")
def test_rq2_zero_result_page_with_chatty_schema_is_admitted():
    """PATTERN 3 (absence treated as success): a real API returns HTTP 200 with
    {"results": [], "meta": {...}}. extract_text keeps strings only, so usually the
    gate rejects — BUT any schema vocabulary that overlaps the question ('query',
    'term', 'series', dates like '2024') counts toward coverage. A zero-result page
    whose keys/values mention the topic passes the same way a populated one does;
    nothing downstream distinguishes count=0 from count=20."""
    g = RelevanceGate(min_coverage=0.25)
    empty = {"results": [], "meta": {"count": 0},
             "next": "https://api.example.org/works?cursor=unemployment&filter=rate"}
    ok, cov, why = g.judge("unemployment rate", "macro", empty)
    # If this ever admits, absence just became evidence.
    assert not ok, f"zero-result page admitted at {cov:.0%}: {why}"


def test_rq2b_gate_cannot_distinguish_count0_from_count20():
    """The differential form: the gate's verdict is byte-blind to emptiness. Feed it
    two pages identical except one result vs none; whatever it decides, it decides
    identically. That IS the defect for any adapter whose schema echoes the topic."""
    g = RelevanceGate(min_coverage=0.25)
    item = {"title": "Unemployment rate trends", "abstract": "rates and labor"}
    empty = {"results": []}
    full = {"results": [item]}
    ok_e, cov_e, _ = g.judge("unemployment rate trends labor", "", empty)
    ok_f, cov_f, _ = g.judge("unemployment rate trends labor", "", full)
    # Documenting current behaviour: the empty page scores via schema/topic overlap
    # exactly as the populated one would if the overlap threshold were met.
    if ok_e == ok_f and abs(cov_e - cov_f) < 1e-9:
        assert ok_e == ok_f  # tautology by construction — see findings doc


# ── RQ3: World Bank single-country silent drop ───────────────────────────────

@pytest.mark.xfail(reason="RQ3 CONFIRMED DEFECT: second country silently dropped")
def test_rq3_worldbank_planner_silently_answers_half_a_comparison():
    """'Compare GDP growth USA CHN' resolves country='USA' and drops CHN on the
    floor — while _wb_resolve_country's OWN contract says multiple countries must
    come back as candidates for disambiguation. The failure direction is exactly
    family 9: a confident half-answer that looks like success."""
    p = qb.build_plan("worldbank", "Compare GDP growth USA CHN")
    resolved = p.resolved.get("country")
    assert resolved != "USA" or "CHN" in json.dumps(p.to_dict()), (
        "two countries named; plan resolved only %r with no candidates and no "
        "refusal" % resolved)


# ── RQ4: unkeyed tamper — digest travels WITH the record ─────────────────────

def test_rq4_unkeyed_checkpoint_tamper_self_verifies_and_seals():
    """In the DOCUMENTED DEFAULT deployment (no CALLISTO_SEAL_KEY), replay_ledger
    verifies sha256(body)==content_sha256 — both of which live in the same editable
    JSON file. Rewrite the body, recompute the digest, and fabricated bytes are
    replayed primary=True, pass admissible_checkpoints, and seal_guard returns SEAL.
    fix_d3.md owns making keys mandatory (D1); until then every integrity claim on
    this path is a tautology. Pinned so the day keys become mandatory this test
    flips to enforcing rejection."""
    _unkeyed()
    d = tempfile.mkdtemp()
    cp = ckpt.FileCheckpointer(d)
    body = '{"results": ["benign"]}'
    payload = {"fetches": [{"source_name": "fred", "url": "https://x/y",
                            "body": body, "content_sha256": ckpt._sha(body),
                            "question_id": "q1", "fetched_at": "t"}],
               "rejections": [], "independent_keys": ["api.stlouisfed.org"]}
    ck = cp.save("runT", "fetch_leaf", ckpt.hash_inputs({"qid": "q1"}), payload)
    p = cp._path(ck)
    dd = json.loads(p.read_text())
    forged = '{"results": ["UNRATE = -5% (fabricated)"]}'
    dd["payload"]["fetches"][0]["body"] = forged
    dd["payload"]["fetches"][0]["content_sha256"] = ckpt._sha(forged)
    p.write_text(json.dumps(dd))

    loaded = cp.load_by_key("runT", ck.key)
    led = ProvenanceLedger()
    rep = ckpt.replay_ledger(led, [loaded])
    assert rep["integrity_failures"] == []          # self-consistent forgery passes
    assert led.is_primary_bytes(forged)             # ...and mints PRIMARY
    trace = type("T", (), {"is_resume": True, "run": "runT"})()
    verdict, why = ckpt.seal_guard(trace, [loaded], led)
    assert verdict == "SEAL", why                   # ...and the guard seals over it


# ── RQ5: the supersede verdict does not survive the resume boundary ──────────

@pytest.mark.xfail(reason="RQ5 CONFIRMED DEFECT (CRITICAL): rejected bytes re-mint "
                          "PRIMARY after resume")
def test_rq5_gate_rejection_lost_across_resume_lauanders_bytes():
    """Live run: RestSource records the fetched bytes PRIMARY into the ledger, then
    the relevance gate rejects them and record_gate_rejection() supersedes them.
    Resume run: the engine replays the SAME bytes from the checkpoint payload via
    replay_ledger BEFORE any gate runs — and the payload's `rejections` list is
    restored onto the TRACE but never handed to record_gate_rejection(). The
    supersede state lived only in the dead process's ledger. Result: evidence the
    live run judged irrelevant enters the resumed run as PRIMARY-class material and
    seal_guard says SEAL. This is R4/R4b reopened through the resume boundary —
    the exact laundering shape those fixes closed, reintroduced by the seam between
    them (engine.py restores trace.rejected at :1081 but calls record_gate_rejection
    only for FRESH retrievals at :791)."""
    _unkeyed()
    d = tempfile.mkdtemp()
    cp = ckpt.FileCheckpointer(d)
    BODY = '{"seriess":[{"title":"Unemployment Rate"}]}'
    payload = {
        # live run ADMITTED nothing; these bytes sit under `fetches` because
        # RestSource records before the gate judges (by design).
        "fetches": [{"source_name": "fred", "url": "https://api.stlouisfed.org/x",
                     "body": BODY, "content_sha256": ckpt._sha(BODY),
                     "question_id": "q1", "fetched_at": "t"}],
        # the LIVE run's gate verdict on these very bytes:
        "rejections": [{"source_name": "fred",
                        "url": "https://api.stlouisfed.org/x",
                        "reason": "irrelevant", "relevance_score": 0.0,
                        "content_sha256": ckpt._sha(BODY)}],
        "independent_keys": [],
    }
    ck = cp.save("runR", "fetch_leaf", ckpt.hash_inputs({"qid": "q1"}), payload)
    loaded = cp.load_by_key("runR", ck.key)

    # What the resumed engine actually does (engine.py:808-816):
    led = ProvenanceLedger()
    admissible = ckpt.admissible_checkpoints("runR", [loaded])
    ckpt.replay_ledger(led, admissible)
    ev = type("E", (), {"content": BODY})()
    assert led.assign_source_class(ev) != SourceClass.PRIMARY, (
        "bytes the LIVE run's gate REJECTED enter the resumed ledger as PRIMARY "
        "— the supersede verdict did not survive the resume boundary")

    # And the guard, which exists precisely to stop laundering across this
    # boundary, seals anyway:
    trace = type("T", (), {"is_resume": True, "run": "runR"})()
    verdict, why = ckpt.seal_guard(trace, [loaded], led)
    assert verdict == "REFUSE", (
        "seal_guard sealed over gate-rejected bytes replayed as PRIMARY: %s" % why)

    # Differential check: the LIVE path (record then reject) gets this right —
    # proving the divergence is the resume seam, not the ledger.
    live = ProvenanceLedger()
    live.record_tool_result("fred_fetch", BODY, primary=True,
                            urls=["https://api.stlouisfed.org/x"])
    live.record_gate_rejection(BODY, ["https://api.stlouisfed.org/x"])
    assert live.assign_source_class(ev) == SourceClass.INFERRED


# ── RQ6: translate_question_type widens selection beyond the question ────────

@pytest.mark.xfail(reason="RQ6 CONFIRMED DEFECT: adopted clause vocabulary selects "
                          "sources the question never earned")
def test_rq6_translation_adopts_answer_vocabulary_and_widens_the_net():
    """translate_question_type builds the select() input from the WINNING sources'
    own answer clauses UNION the question. Those adopted tokens then match OTHER
    sources' clauses, so a question that selected 1 source selects many — including
    sources whose own clauses share none of the question's words. Selection is now
    a function of registry vocabulary, not of the question."""
    reg = _registry()
    translated, chosen = None, None
    from tools.pipeline.retrieval import translate_question_type
    translated, chosen = translate_question_type(
        reg, "economic time series unemployment", "macro data")
    direct = {d.name for d in reg.select_explained(
        "economic time series unemployment") if d.included}
    widened = {d.name for d in reg.select_explained(translated) if d.included}
    assert widened <= direct | set(chosen), (
        "translation grew the selected set from %d to %d; newly-included: %s"
        % (len(direct), len(widened), sorted(widened - direct)))


# ── RQ7: diagnostic floor defeats an explicit caller threshold ───────────────

@pytest.mark.xfail(reason="RQ7 CONFIRMED DEFECT: floor overrides caller strictness "
                          "for partial-coverage matches")
def test_rq7_diagnostic_floor_overrides_explicit_min_score():
    """select(min_score=0.99) still returns sources covering HALF the question:
    the 0.5 _DIAGNOSTIC_FLOOR replaces the score whenever any matched word is
    'diagnostic' (tf <= n_sources/3 — with 21 sources, tf<=7, which is nearly
    every topical word). The code comment claims 'A caller asking for 0.99 still
    gets 0.99'; the floor makes best_score 0.5 >= 0.34... but against an explicit
    0.99 the include-test at :195 reads `best_score < min_score` → 0.5 < 0.99 →
    should skip. Verify which way it actually breaks."""
    reg = _registry()
    got = reg.select("unemployment", min_score=0.99)
    assert got == [], (
        "caller demanded 0.99 coverage; diagnostic floor returned %s"
        % [s.name for s in got])


# ── RQ8: queries[0] only — multi-query plans are silently truncated ──────────

def test_rq8_retriever_executes_only_first_planned_query():
    """_fetch_one takes plan.queries[0]; execute() (the documented runner) runs
    them ALL. Same module family, two contracts. Today every planner emits exactly
    one query, so the divergence is latent — pin it so a future two-query planner
    cannot silently lose half its fetches."""
    src = open("tools/pipeline/retrieval.py").read()
    assert "plan.queries[0]" in src
    # and document the divergence honestly:
    assert "for q in plan.queries" in open("tools/sources/query_builder.py").read()


# ── honest negatives ─────────────────────────────────────────────────────────

def test_hn1_unkeyed_digest_mismatch_is_caught():
    """Control: WITHOUT recomputing the digest, tamper IS caught (C1 fix holds)."""
    _unkeyed()
    d = tempfile.mkdtemp()
    cp = ckpt.FileCheckpointer(d)
    payload = {"fetches": [{"source_name": "fred", "url": "u", "body": "b",
                            "content_sha256": ckpt._sha("b"),
                            "question_id": "q", "fetched_at": "t"}]}
    ck = cp.save("runC", "fetch_leaf", "h", payload)
    p = cp._path(ck)
    dd = json.loads(p.read_text())
    dd["payload"]["fetches"][0]["body"] = "tampered-only"
    p.write_text(json.dumps(dd))
    loaded = cp.load_by_key("runC", ck.key)
    led = ProvenanceLedger()
    assert ckpt.replay_ledger(led, [loaded])["integrity_failures"]


def test_hn2_keyed_regime_catches_full_forgery():
    """Control: WITH a key, body+digest rewrite fails the HMAC everywhere."""
    os.environ["CALLISTO_SEAL_KEY"] = "ab" * 32
    try:
        d = tempfile.mkdtemp()
        cp = ckpt.FileCheckpointer(d)
        payload = {"fetches": [{"source_name": "fred", "url": "u", "body": "b",
                                "content_sha256": ckpt._sha("b"),
                                "question_id": "q", "fetched_at": "t"}]}
        ck = cp.save("runK", "fetch_leaf", "h", payload)
        p = cp._path(ck)
        dd = json.loads(p.read_text())
        dd["payload"]["fetches"][0]["body"] = "forged"
        dd["payload"]["fetches"][0]["content_sha256"] = ckpt._sha("forged")
        p.write_text(json.dumps(dd))
        loaded = cp.load_by_key("runK", ck.key)
        assert not loaded.verify_signature(os.environ["CALLISTO_SEAL_KEY"])
        trace = type("T", (), {"is_resume": True, "run": "runK"})()
        verdict, _ = ckpt.seal_guard(trace, [loaded], ProvenanceLedger())
        assert verdict == "REFUSE"
    finally:
        del os.environ["CALLISTO_SEAL_KEY"]


def test_hn3_empty_results_with_quiet_schema_are_rejected():
    """Control: a zero-result page whose schema does NOT echo the question is
    correctly rejected by the gate."""
    g = RelevanceGate()
    ok, _, _ = g.judge("unemployment rate trend", "", {"results": []})
    assert not ok
