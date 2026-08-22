"""Decomposition diversity — BUILD job.

The #1 bottleneck from the live run: five sub-questions that all want
scholarly papers produce one fetch family no matter how many adapters
exist. These tests pin three behaviours:

  1. The Architect's prompt is built from the registry's OWN vocabulary,
     so proposed question_types are matchable by selection.
  2. A post-decomposition diversity check counts DISTINCT independence
     families reachable by the program and marks single-family programs
     WEAK unless the model honestly declared single_family_ok.
  3. Honest single-family questions are reported, not fabricated into
     diversity.
"""
from __future__ import annotations

import json
import sys

import pytest

sys.path.insert(0, ".")

from tools.pipeline.decompose import (
    assess_diversity,
    build_decompose_system,
    registry_catalog,
    DiversityReport,
    assess_program_diversity,
)
from tools.pipeline.model import (DECOMPOSE_SYSTEM, DIVERSITY_MANDATE,
                                  decompose_messages)
from tools.sources.registry import SourceAdapter, SourceRegistry


# ── fixtures ────────────────────────────────────────────────────────────────

def make_spec(name, answers, tier=1, base_url=None):
    from tools.sources.base import SourceSpec
    return SourceSpec(
        name=name, description=f"{name} test adapter",
        answers=tuple(answers),
        cannot_answer=("nothing relevant",),   # non-empty per contract
        tier=tier, min_interval_s=1.0,
        base_url=base_url or f"https://{name}.example.com")


def tiny_registry() -> SourceRegistry:
    """Four sources in FOUR different independence families — enough to
    distinguish 'one kind' from 'many kinds' decompositions."""
    reg = SourceRegistry()
    reg.register(SourceAdapter(make_spec(
        "openalex", ["scholarly work search by topic"], 2), lambda s: None))
    reg.register(SourceAdapter(make_spec(
        "semanticscholar", ["paper search and lookup"], 2), lambda s: None))
    reg.register(SourceAdapter(make_spec(
        "bea", ["GDP and NIPA table series; international trade"], 1),
        lambda s: None))
    reg.register(SourceAdapter(make_spec(
        "sec_fulltext", ["filings mentioning a phrase or topic"], 1),
        lambda s: None))
    return reg


# ── 1. registry vocabulary reaches the prompt ──────────────────────────────

class TestRegistryVocabularyInPrompt:
    def test_bare_prompt_falls_back_when_no_registry_possible(self):
        from tools.pipeline import model as m
        saved = m._default_registry_or_none
        m._default_registry_or_none = lambda: None
        try:
            msgs = decompose_messages("Q")
            assert msgs[0]["content"] == DECOMPOSE_SYSTEM
        finally:
            m._default_registry_or_none = saved

    def test_live_prompt_includes_mandate_and_horizon_rule(self):
        from tools.sources.registry import get_source_registry
        sysmsg = decompose_messages("semiconductors")[0]["content"]
        assert "SOURCE KINDS" in sysmsg
        assert "horizon_days MUST be a positive integer" in sysmsg
        # the registry's own vocabulary is in there
        assert "macroeconomic time series" in sysmsg

    def test_system_prompt_contains_each_source_answers(self):
        reg = tiny_registry()
        sysmsg = build_decompose_system(reg)
        for name, clause in [
                ("openalex", "scholarly work search"),
                ("bea", "GDP and NIPA table series"),
                ("sec_fulltext", "filings mentioning a phrase")]:
            assert name in sysmsg
            assert clause in sysmsg

    def test_cannot_answer_never_appears_as_capability(self):
        # cannot_answer is honest limits; feeding it as an answer line would
        # invite the Architect to propose questions sources cannot serve.
        reg = tiny_registry()
        catalog = registry_catalog(reg)
        assert "cannot" not in catalog.lower()

    def test_diversity_mandate_present(self):
        sysmsg = build_decompose_system(tiny_registry())
        assert "SOURCE KINDS" in sysmsg
        assert "single_family_ok" in sysmsg

    def test_predictive_horizon_constraint_survives(self):
        # This constraint fixed a live-run death (undated predictions);
        # it must be present in BOTH prompt forms.
        assert "horizon_days MUST be a positive integer" in DECOMPOSE_SYSTEM
        assert "horizon_days MUST be a positive integer" in DIVERSITY_MANDATE + \
            build_decompose_system(tiny_registry())


# ── 2. diversity check counts families, not sources ────────────────────────

class TestDiversityCheck:
    def test_all_scholarly_is_one_family_and_weak(self):
        rep = assess_diversity(
            tiny_registry(),
            ["scholarly work search", "paper search and lookup"])
        assert rep.n_families == 1
        assert rep.families == ["scholarly-aggregator"]
        assert rep.weak is True
        assert "WEAK" in rep.note

    def test_spanning_kinds_counts_distinct_families(self):
        rep = assess_diversity(
            tiny_registry(),
            ["scholarly work search",
             "international trade statistics",
             "filings mentioning supply chain risks"])
        assert rep.n_families >= 3
        assert rep.weak is False

    def test_openalex_and_semanticscholar_collapse(self):
        # The exact live-run collapse: two adapters, ONE independent voice.
        rep = assess_diversity(
            tiny_registry(),
            ["scholarly work search", "paper search and lookup"])
        fams = set(rep.families)
        assert fams == {"scholarly-aggregator"}
        assert sorted(rep.family_sources["scholarly-aggregator"]) == \
            ["openalex", "semanticscholar"]

    def test_single_family_ok_reports_but_does_not_flag_weak(self):
        rep = assess_diversity(
            tiny_registry(), ["scholarly work search"],
            single_family_ok=True)
        assert rep.n_families == 1
        assert rep.weak is False          # honesty claim respected
        assert "declared honest" in rep.note

    def test_empty_question_types_is_zero_families(self):
        rep = assess_diversity(tiny_registry(), [])
        assert rep.n_families == 0
        assert rep.n_sub_questions == 0

    def test_report_serialises(self):
        rep = assess_diversity(tiny_registry(),
                               ["scholarly work search"])
        d = rep.to_dict()
        assert d["n_families"] == 1
        assert json.loads(json.dumps(d))["weak"] is True


class TestProgramLevel:
    def test_assess_program_uses_leaf_question_types(self):
        from agp.research_program import (
            EvidenceRequirement, QuestionKind, ResearchQuestion,
            ResearchProgram)
        reg = tiny_registry()
        prog = ResearchProgram(root_query="Q")
        qts = {}
        for text, qt in [("lit?", "scholarly work search"),
                         ("trade?", "international trade statistics")]:
            prog.questions.append(ResearchQuestion(
                text=text, kind=QuestionKind.DESCRIPTIVE,
                evidence_requirements=EvidenceRequirement(
                    min_source_class=__import__(
                        "agp.research_program",
                        fromlist=["SourceClassRank"]).SourceClassRank.SECONDARY,
                    min_independent_sources=1)))
            qts[prog.questions[-1].question_id] = qt
        rep = assess_program_diversity(reg, prog, qts)
        assert rep.n_sub_questions == 2
        assert rep.n_families >= 2
        # weak flag on a single-family program via the same path
        weak = assess_program_diversity(
            reg, prog, {qid: "scholarly work search" for qid in qts})
        assert weak.weak is True

    def test_live_registry_semiconductor_shapes(self):
        """Against the REAL 19-source registry, question types phrased in
        registry vocabulary must reach many distinct families."""
        try:
            from tools.sources.registry import get_source_registry
            reg = get_source_registry()
        except Exception:      # pragma: no cover - offline environments
            pytest.skip("live registry unavailable")
        diverse = assess_diversity(reg, [
            "scholarly work search by title/author/topic",
            "international trade in goods and services",
            "filings mentioning a phrase, person, or topic",
            "patent application bibliographic search",
            "market-implied probability of an economic or world event",
            "news coverage volume for a person/org/topic over time",
        ])
        assert diverse.n_families >= 5, diverse.to_dict()
        # A narrower, literature-only shape reaches strictly fewer families —
        # the check measures the decomposition as written.
        mono = assess_diversity(reg,
                                ["scholarly work search by topic"])
        assert mono.n_families < diverse.n_families
