"""WAVE 5 / I3 — cross-source synthesis.

Fixtures only (no-socket guard active). Five jobs:

  JOB 1  triangulation: evidence grouped by CLAIM; corroboration counted
         in independence units (retrieval.independence_key reused)
  JOB 2  contradiction as a first-class output: sides, sources, settle-it
  JOB 3  confidence from agreement structure, never above the source-class
         ceiling — property-based invariants
  JOB 4  structured extraction table with provenance per cell
  JOB 5  honest nulls: literature-null distinguishable from retrieval failure
"""
from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from tools.pipeline.engine import FetchResult  # noqa: E402
from tools.pipeline.retrieval import (  # noqa: E402
    RejectedItem,
    RetrievalTrace,
    independence_key,
)
from tools.pipeline.synthesis import (  # noqa: E402
    NULL_LITERATURE,
    NULL_RETRIEVAL,
    EvidenceItem,
    claim_key,
    classify_null,
    confidence_from_agreement,
    detect_contradictions,
    extract_values,
    extraction_table,
    synthesize,
    triangulate,
)
from agp.thresholds import MAX_CONFIDENCE_BY_SOURCE  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────────


def _fetch(name, body="{}", sha="a" * 64, url="https://x/1"):
    return FetchResult(source_name=name, url=url, content_sha256=sha,
                       body=body, parsed={}, question_id="q1")


def item(claim, source, base="https://src.example.org", cls="SECONDARY",
         values=(), units=(), stance="", sha=None):
    return EvidenceItem(
        claim=claim, source_name=source, base_url=base, source_class=cls,
        content_sha256=sha or (source + claim)[:64].ljust(64, "0"),
        values=tuple(values), value_units=tuple(units), stance=stance)


def trace(rounds=None, rejected=None, stop="done"):
    return RetrievalTrace(question_id="q1", rounds=rounds or [],
                          rejected=rejected or [], stop_reason=stop)


# ── JOB 1: triangulation ───────────────────────────────────────────────────


class TestTriangulation:
    def test_groups_by_claim_not_document(self):
        groups = triangulate([
            item("foundry concentration is rising", "openalex"),
            item("tariffs raised chip prices", "federalregister"),
            item("foundry concentration is rising", "gdelt"),
        ])
        assert len(groups) == 2
        by_nitems = sorted(len(g.items) for g in groups)
        assert by_nitems == [1, 2]

    def test_independence_reuses_retrieval_key(self):
        # openalex + semantic_scholar are one family -> ONE voice.
        a = item("c", "openalex", base="https://openalex.org")
        b = item("c", "semantic_scholar", base="https://semanticscholar.org")
        assert a.indep_key == independence_key("openalex", "https://openalex.org")
        assert b.indep_key == a.indep_key, (
            "semantic_scholar must collapse into the openalex family — "
            "naming drift must not manufacture independence")
        g = triangulate([a, b])[0]
        assert g.independent_sources == 1
        assert len(g.items) == 2

    def test_three_independent_sources_agree(self):
        g = triangulate([
            item("resilience improved since 2021", "openalex"),
            item("resilience improved since 2021", "gdelt",
                 base="https://gdeltproject.org"),
            item("resilience improved since 2021", "federalregister",
                 base="https://federalregister.gov"),
        ])[0]
        assert g.independent_sources == 3


# ── JOB 2: contradiction ───────────────────────────────────────────────────


class TestContradiction:
    def test_numeric_conflict_between_independent_sources(self):
        g = triangulate([
            item("chip imports were 40 billion", "src_a",
                 base="https://a.org", values=(40e9,), units=("billion",)),
            item("chip imports were 55 billion", "src_b",
                 base="https://b.org", values=(55e9,), units=("billion",)),
        ])[0]
        cons = detect_contradictions(g)
        assert len(cons) == 1
        c = cons[0]
        assert c.kind == "numeric"
        assert {s["value"] for s in c.sides} == {40e9, 55e9}
        assert all(s["sources"] and s["indep_keys"] for s in c.sides)
        assert "PRIMARY" in c.what_would_settle_it or \
            "authoritative" in c.what_would_settle_it

    def test_same_publisher_twice_is_not_a_conflict(self):
        # Same number twice from one publisher: no voices in conflict; and
        # two DIFFERENT numbers from ONE publisher collapse to one voice.
        g = triangulate([
            item("imports 40 billion", "wire_copy_1",
                 base="https://wire.example", values=(40e9,)),
            item("imports 40 billion", "wire_copy_2",
                 base="https://wire.example", values=(40e9,)),
        ])[0]
        assert detect_contradictions(g) == []

    def test_stance_conflict(self):
        g = triangulate([
            item("export controls help resilience", "a",
                 base="https://a.org", stance="supports"),
            item("export controls help resilience", "b",
                 base="https://b.org", stance="refutes"),
        ])[0]
        cons = detect_contradictions(g)
        assert len(cons) == 1 and cons[0].kind == "stance"
        assert {s["stance"] for s in cons[0].sides} == {"supports", "refutes"}

    def test_close_numbers_within_tolerance_do_not_contradict(self):
        g = triangulate([
            item("capacity 100 million", "a", base="https://a.org",
                 values=(100e6,)),
            item("capacity 105 million", "b", base="https://b.org",
                 values=(105e6,)),
        ])[0]
        assert detect_contradictions(g) == []


# ── JOB 3: confidence from agreement structure (property-based) ────────────


_ceiling_st = st.sampled_from(list(MAX_CONFIDENCE_BY_SOURCE))
_n_items_st = st.integers(min_value=1, max_value=12)


class TestConfidenceInvariants:
    @settings(max_examples=300, deadline=None)
    @given(cls=_ceiling_st, n=_n_items_st)
    def test_never_above_source_class_ceiling(self, cls, n):
        items = [item("claim x", f"s{i}", base="https://same.example", cls=cls)
                 for i in range(n)]
        score, _ = confidence_from_agreement(triangulate(items)[0])
        assert score <= MAX_CONFIDENCE_BY_SOURCE[cls] + 1e-9

    @settings(max_examples=300, deadline=None)
    @given(n=_n_items_st)
    def test_volume_from_one_publisher_is_worth_one_source(self, n):
        items = [item("claim x", f"s{i}", base="https://one.example")
                 for i in range(n)]
        one = item("claim x", "solo", base="https://one.example")
        s_many, _ = confidence_from_agreement(triangulate(items)[0])
        s_one, _ = confidence_from_agreement(triangulate([one])[0])
        assert s_many == s_one

    @settings(max_examples=200, deadline=None)
    @given(k=st.integers(min_value=1, max_value=4))
    def test_more_independent_voices_never_lower(self, k):
        scores = []
        for n in range(1, k + 1):
            items = [item("claim x", f"src{i}", base=f"https://s{i}.example")
                     for i in range(n)]
            scores.append(confidence_from_agreement(triangulate(items)[0])[0])
        assert scores == sorted(scores)

    @settings(max_examples=200, deadline=None)
    @given(cls=_ceiling_st, n=st.integers(min_value=1, max_value=8),
           conflict=st.booleans())
    def test_contradiction_lowers_and_caps(self, cls, n, conflict):
        items = [item("claim x", f"src{i}", base=f"https://s{i}.example",
                      cls=cls) for i in range(n)]
        if conflict and n >= 2:
            items[0] = item("claim x", "src0", base="https://s0.example",
                            cls=cls, values=(10.0,))
            items[1] = item("claim x", "src1", base="https://s1.example",
                            cls=cls, values=(90.0,))
        g = triangulate(items)[0]
        plain, _ = confidence_from_agreement(g)
        cons = detect_contradictions(g)
        score, reasons = confidence_from_agreement(g, cons)
        assert score <= plain + 1e-9
        if conflict and n >= 2:
            assert len(cons) >= 1
            assert score < plain or plain <= 0.54
            assert score <= 0.54
            assert any("SPECULATIVE" in r for r in reasons)
        else:
            assert score == plain

    def test_corroboration_raises_only_within_provenance(self):
        # Two independent SECONDARY voices must NOT exceed the SECONDARY
        # ceiling even though three would be 'stronger'.
        items = [item("c", f"s{i}", base=f"https://{i}.org", cls="SECONDARY")
                 for i in range(5)]
        score, reasons = confidence_from_agreement(triangulate(items)[0])
        assert score == pytest.approx(MAX_CONFIDENCE_BY_SOURCE["SECONDARY"])
        joined = " ".join(reasons)
        assert "0.75" in joined and "ceiling" in joined


# ── JOB 4: extraction table ────────────────────────────────────────────────


class TestExtraction:
    def test_extract_values_normalises_units(self):
        vals = extract_values("output rose 12% to 8.5 billion units; "
                              "yield 65 nm")
        assert vals == pytest.approx([0.12, 8.5e9, 65])

    def test_table_has_provenance_per_cell(self):
        it = item("capex 20 billion", "edgar_src", base="https://sec.example",
                  values=(20e9,), units=("billion",), sha="f" * 64)
        rows = extraction_table([it])
        assert len(rows) == 1
        row = rows[0].to_dict()
        assert row["value"] == 20e9
        assert row["provenance"]["sha256"] == "f" * 64
        assert row["independence"] == it.indep_key
        assert row["source_class"] == "SECONDARY"

    def test_report_carries_table_and_survives_roundtrip(self):
        rep = synthesize("q", [
            item("price 30 billion", "a", base="https://a.org",
                 values=(30e9,), units=("bn",)),
            item("other claim", "b", base="https://b.org"),
        ])
        d = rep.to_dict()
        json.dumps(d)  # serialisable
        assert any(r["value"] == 30e9 for r in d["table"])


# ── JOB 5: honest nulls ───────────────────────────────────────────────────


class TestHonestNulls:
    def test_gate_rejections_mean_literature_null(self):
        t = trace(
            rounds=[{"round": 1, "query": "q", "admitted": 0, "sources": [
                {"name": "openalex", "rejected": "covers 0% ..."}]}],
            rejected=[RejectedItem(source_name="openalex", url="u",
                                   reason="covers 0%", relevance_score=0.0)])
        v = classify_null(t)
        assert v.status == NULL_LITERATURE
        assert v.is_honest_null
        assert "rejected at the relevance gate" in v.explanation

    def test_source_errors_mean_retrieval_failure(self):
        t = trace(rounds=[{"round": 1, "query": "q", "admitted": 0,
                           "sources": [
                               {"name": "fred",
                                "error": "HTTP 403"}]},
                          ], stop="terminator: stagnant")
        v = classify_null(t)
        assert v.status == NULL_RETRIEVAL
        assert not v.is_honest_null
        assert "RETRIEVAL FAILURE" in v.explanation
        assert "403" in v.explanation

    def test_no_route_means_retrieval_failure(self):
        t = trace(rounds=[{"round": 1, "query": "q", "admitted": 0,
                           "sources": [
                               {"name": "treasury",
                                "skipped": "no generic route"}]}],
                   stop="selected sources lack generic fetch routes")
        v = classify_null(t)
        assert v.status == NULL_RETRIEVAL
        assert "treasury" in v.explanation

    def test_nothing_attempted_is_retrieval_failure(self):
        v = classify_null(trace(rounds=[], stop=""))
        assert v.status == NULL_RETRIEVAL
        assert "no fetch was attempted" in v.explanation

    def test_synthesize_reports_nulls_per_leaf(self):
        empty = trace(rounds=[], stop="nothing")
        rep = synthesize("root q", [], null_traces={"leaf1": empty})
        d = rep.to_dict()
        assert d["nulls"]["leaf1"]["status"] == NULL_RETRIEVAL
        assert any("leaf1" in n for n in rep.notes)


# ── end-to-end over the report ─────────────────────────────────────────────


class TestSynthesizeEndToEnd:
    def test_full_report_shape(self):
        rep = synthesize("semiconductor resilience", [
            item("foundry capacity concentrated", "openalex",
                 base="https://openalex.org", cls="SECONDARY",
                 values=(0.9,), units=("",)),
            item("foundry capacity concentrated", "gdelt",
                 base="https://gdeltproject.org", cls="SIGNAL"),
            item("tariff cost passed through", "federalregister",
                 base="https://federalregister.gov", values=(15e9,),
                 units=("billion",)),
            item("tariff cost passed through", "congress_src",
                 base="https://congress.example", values=(41e9,),
                 units=("billion",)),
        ])
        d = rep.to_dict()
        assert d["n_claims"] == 2
        assert d["max_independent_agreement"] == 2
        assert len(d["contradictions"]) == 1
        # contradiction caps overall confidence at SPECULATIVE band
        assert rep.confidence <= 0.54
        # the agreeing claim keeps its structural score
        agree = next(g for g in d["groups"]
                     if g["n_items"] == 2 and g["independent_sources"] == 2)
        assert agree["confidence"] > 0

    def test_domain_general_shapes_identical(self):
        """Scholarly, market, materials questions produce identical structure."""
        def build():
            return synthesize("X", [
                item("metric m is high", "p1", base="https://p1.org",
                     values=(5.0,)),
                item("metric m is high", "p2", base="https://p2.org",
                     values=(50.0,)),
            ]).to_dict()
        a, b = build(), build()
        assert a == b
        assert len(a["contradictions"]) == 1

    def test_deterministic_group_ordering(self):
        items = [item(f"zebra claim {i}", f"s{i}", base=f"https://{i}.org")
                 for i in range(3)] + \
                [item("alpha claim", "s9", base="https://9.org")]
        keys = [g.claim for g in triangulate(items)]
        assert keys == sorted(keys, key=claim_key)


def test_claim_key_ignores_stopwords():
    assert claim_key("The yield of chips is high") == \
        claim_key("yield chips high")


def test_extract_values_empty():
    assert extract_values("") == ()
    assert extract_values("no numbers here") == ()
