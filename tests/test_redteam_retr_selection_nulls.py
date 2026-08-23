"""RED TEAM — H5/H6: selection phrasing and honest-null conflation.

H5: can a source that cannot possibly answer be selected, or one that
    could answer be excluded, by phrasing alone?
H6: can a fetch failure read as an honest null? classify_null() is the
    only wall between "the literature does not address this" and
    "we failed to look".
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.sources.registry import SourceRegistry, SourceAdapter
from tools.pipeline.synthesis import (
    NULL_LITERATURE, NULL_RETRIEVAL, classify_null)
from tools.pipeline.retrieval import RetrievalTrace, RejectedItem


# ── fixtures ──────────────────────────────────────────────────────────────

def make_registry():
    from tools.sources.base import SourceSpec
    reg = SourceRegistry()
    def mk(name, answers, base="https://x.example", tier=1):
        spec = SourceSpec(name=name, base_url=base, description=name,
                          answers=tuple(answers), tier=tier)
        reg.register(SourceAdapter(spec, lambda source: object()))
        return spec
    # clinicaltrials-like adapter: its own clause vocabulary
    mk("clinicaltrials", ("trial design arms endpoints search results",))
    mk("fred", ("macro economic time series unemployment inflation",))
    mk("openalex", ("scholarly works papers citations topics",))
    mk("gdelt", ("news coverage media events",))
    return reg


# ── H5a: a source that cannot answer gets selected ────────────────────────


def test_diagnostic_floor_selects_a_source_that_cannot_answer():
    """The diagnostic-term rule grants ANY matched term a 0.50 floor —
    above min_score=0.34. One diagnostic word in a long question selects a
    source whose clause covers almost none of the question."""
    reg = make_registry()
    q = ("quantum error correction thresholds under correlated noise "
         "in topological qubit arrays with trial")
    sel = reg.select(q)
    names = [s.name for s in sel]
    assert "clinicaltrials" in names, (
        "expected the diagnostic word 'trial' to drag clinicaltrials in; "
        "if absent the floor changed — re-derive")
    d = {x.name: x for x in reg.select_explained(q)}
    if "clinicaltrials" in d and d["clinicaltrials"].included:
        assert d["clinicaltrials"].score >= 0.5, \
            "diagnostic floor granted inclusion regardless of coverage"


def test_prefix_matching_mints_spurious_selections():
    """_overlap matches on shared PREFIX in either direction with no
    minimum length: 'war' matches 'warehouse', 'art' matches 'artery',
    'con' matches 'control'. A question containing short tokens selects
    unrelated sources."""
    from tools.sources.registry import _overlap
    ok, score, matched = _overlap(["war"], {"warehouse", "logistics"})
    assert ok and score == 1.0, "'war' fully 'covered' by warehouse"


def test_translated_query_dilutes_the_real_question():
    """translate_question_type adopts ALL tokens of the winning adapters'
    answer clauses into the query text. Selection next round then runs on
    a string stuffed with every adapter's self-description — sources are
    selected for describing themselves well, not for answering."""
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import patch
    from tools.pipeline import retrieval as R

    class FakeEntry:
        def __init__(self, name, clauses):
            self.name = name
            self.spec = SimpleNamespace(answers=clauses)
    class FakeRegistry:
        def __init__(self):
            self.entries = [
                FakeEntry("clinicaltrials",
                          ("randomized controlled trial design arms "
                           "endpoints enrollment outcomes search",)),
                FakeEntry("openalex", ("scholarly works search citation "
                                       "topics abstracts",))]
        def select_explained(self, text):
            toks = set(text.split())
            out = []
            for e in self.entries:
                words = set(w for c in e.spec.answers for w in c.split())
                sc = len(toks & words) / max(1, len(toks))
                out.append(SimpleNamespace(name=e.name, included=sc >= 0.15,
                                           score=sc, spec=e.spec))
            return out
        def get(self, n):
            return next(e for e in self.entries if e.name == n)

    translated, chosen = R.translate_question_type(
        FakeRegistry(), "clinical trial recruitment bias", "trials")
    # The adopted terms include every clause word of BOTH winners:
    assert "endpoints" in translated or "citation" in translated
    print(f"translated query: {translated!r}")
    # The subject ('recruitment', 'bias') is now a minority of the query.
    subj = {"recruitment", "bias"}
    kept = [w for w in translated.split() if w in subj]
    assert len(kept) <= len(subj), "subject preserved"
    # Demonstrate the dilution ratio:
    ratio = len([w for w in translated.split() if w in subj]) / \
        max(1, len(translated.split()))
    assert ratio < 0.25, f"subject is only {ratio:.0%} of the query"


def test_core_query_strips_the_answer_out_of_the_question():
    """query_builder.core_query drops 'impact/affect/address/facing' AND
    'evidence/data'. A question whose TOPIC is one of those words is
    gutted: 'what evidence supports the impact of X' searches as just X's
    nouns — but worse, questions ABOUT impact measurement lose their
    discriminating content entirely."""
    from tools.sources.query_builder import core_query
    q = "what data measures how climate affected migration"
    core = core_query(q)
    assert "data" not in core and "affected" not in core
    assert core == "climate migration" or core == "measures climate migration"
    # The relation (affected) and the instrument (data/measures) are gone:
    # any document mentioning 'climate migration' passes relevance even if
    # it never discusses measurement.


# ── H6: fetch failure reading as an honest null ──────────────────────────


def _trace(rounds=None, rejected=None, stop=""):
    t = RetrievalTrace(question_id="q")
    t.rounds = rounds or []
    t.rejected = rejected or []
    t.stop_reason = stop
    return t


def test_all_sources_error_is_retrieval_failure():
    v = classify_null(_trace(
        rounds=[{"round": 1, "sources": [{"name": "fred",
                                          "error": "HTTP 503"}]}]))
    assert v.status == NULL_RETRIEVAL


def test_zero_attempts_is_retrieval_failure():
    v = classify_null(_trace(rounds=[], stop="selected sources lack "
                                          "generic fetch routes"))
    assert v.status == NULL_RETRIEVAL


def test_gate_rejections_with_no_admits_reads_as_literature_null():
    v = classify_null(_trace(
        rounds=[{"round": 1, "sources":
                 [{"name": "openalex", "rejected": "covers 10%"}]}],
        rejected=[RejectedItem("openalex", "u", "covers 10%", 0.1)]))
    assert v.status == NULL_LITERATURE


# ── THE ATTACKS ───────────────────────────────────────────────────────────


def test_one_error_source_plus_one_rejection_is_retrieval_failure():
    """VERIFIED behaviour (not the conflation I predicted): any error
    anywhere forces NULL_RETRIEVAL even when another source genuinely
    answered 'nothing relevant'. The safe direction — but note this makes
    the module's own 'mixed' branch (docstring: 'report the honest null
    but disclose errors') UNREACHABLE dead code: the first condition
    excludes errors, the second catches everything with them. See
    test_classify_null_mixed_branch_is_dead_code."""
    trace = _trace(
        rounds=[{"round": 1, "sources": [
            {"name": "fred", "error": "HTTP 503"},
            {"name": "openalex", "rejected": "covers 8%"}]}],
        rejected=[RejectedItem("openalex", "u", "covers 8%", 0.08)])
    v = classify_null(trace)
    assert v.status == NULL_RETRIEVAL


def test_classify_null_mixed_branch_is_dead_code():
    """synthesis.classify_null's final branch promises to handle 'some
    sources errored AND some returned junk' as a literature null with an
    error disclosure. Structurally unreachable: that input has errors
    non-empty, so the second branch (errors or ...) returns first. Either
    the docstring lies or the branch is dead — assert which."""
    import inspect
    from tools.pipeline import synthesis as S
    src = inspect.getsource(S.classify_null)
    # the second return happens whenever errors is non-empty:
    assert "if errors or no_route or not attempted_anything:" in src


def test_single_source_architecture_always_yields_honest_looking_nulls():
    """The second live run fetched nine times from OpenAlex ONLY. If
    OpenAlex is down or rate-limited, EVERY leaf errors -> NULL_RETRIEVAL,
    fine. But if OpenAlex returns 200 with zero hits for a slightly
    misworded query (the morning report's own MISS cases), the gate rejects
    nothing (no items at all) and rounds show admitted=False entries...
    Check what a zero-hit round looks like: sources get {'admitted': True}
    ONLY on admission; a source returning [] produces NO per-source entry
    beyond the fetch succeeding — actually it produces nothing in
    round_detail['sources'] unless rejected. So an empty-but-successful
    fetch is INVISIBLE to classify_null: reachable_attempt is False, and
    the verdict depends entirely on whether some other entry exists."""
    # Empty success: fetch returned [], gate never saw an item, no
    # rejection recorded, no admitted flag. Reproduce the exact shape the
    # retriever writes for a successful fetch of an empty list:
    #
    #   retrieval.py: after `fetched = ...`, gate judges the parsed body;
    #   extract_text([]) == "" -> hay empty -> matched empty -> coverage 0
    #   -> REJECTED with reason. So an empty list IS a rejection. Good.
    #
    # BUT: a body that fails json parse raises inside the adapter ->
    # caught -> recorded as ERROR. And a 200 body that parses to a scalar
    # (e.g. `null`, `0`, `"ok"`): extract_text returns "" for None/numbers
    # -> rejection. Still fine. The hole:
    from tools.pipeline.retrieval import extract_text, RelevanceGate
    g = RelevanceGate()
    ok, cov, why = g.judge("unemployment rate trends", "", {"results": []})
    assert not ok  # empty envelope correctly rejected...
    # ...as IRRELEVANT CONTENT, i.e. recorded as literature-null evidence:
    # "sources returned only irrelevant material". An API outage that
    # serves empty 200s (several do under load) therefore reads as
    # 'the literature does not address this'.
    v = classify_null(_trace(
        rounds=[{"round": 1, "sources":
                 [{"name": "fred", "rejected": why}]}],
        rejected=[RejectedItem("fred", "u", why, 0.0)]))
    assert v.status == NULL_LITERATURE, (
        "H6 CONFIRMED: an empty-envelope 200 (classic degraded-API "
        "behaviour) is classified as an honest literature null")


def test_skipped_planner_gap_is_not_counted_as_no_route():
    """retrieval.py planner mode records skipped sources with reason from
    build_plan — arbitrary prose like 'ambiguous macro concept;
    disambiguate before fetching'. classify_null only recognises the exact
    legacy string 'no generic route'. Planner skips therefore appear in NO
    branch of classify_null: a leaf where every source was unplannable has
    rounds=[] -> 'no fetch was attempted' -> retrieval_failure. OK. But a
    leaf with ONE plannable source that returned junk plus THREE skipped
    for ambiguity reports a clean literature null while 3/4 of the fanout
    never ran."""
    trace = _trace(
        rounds=[{"round": 1, "sources": [
            {"name": "fred", "skipped": "ambiguous macro concept; "
             "disambiguate before fetching"},
            {"name": "bls", "skipped": "BLS API v2 has no free-text search"},
            {"name": "openalex", "rejected": "covers 20%"}]}],
        rejected=[RejectedItem("openalex", "u", "covers 20%", 0.2)])
    v = classify_null(trace)
    assert v.status == NULL_LITERATURE
    assert "fred" not in v.explanation and "bls" not in v.explanation, (
        "H6 sibling CONFIRMED: planner-skipped sources (never asked) are "
        "absent from the verdict — partial coverage reads as full survey")


def test_skip_only_leaf_is_mislabeled_literature_null():
    """VERIFIED H6 CONFIRMATION: a leaf where EVERY source was skipped at
    planning (zero fetches attempted, zero errors recorded) falls through
    both branches to the final literature-null return. 'We failed to
    look' is rendered as 'the literature is silent'. The second branch's
    `not attempted_anything` clause never fires because rounds is
    non-empty (a round ran; every source in it was skipped)."""
    trace2 = _trace(rounds=[{"round": 1, "sources": [
        {"name": "c", "skipped": "ambiguous; disambiguate"}]}])
    v2 = classify_null(trace2)
    assert v2.status == NULL_LITERATURE, (
        "H6 CONFIRMED: zero-fetch leaf classified as an honest LITERATURE "
        "null")
