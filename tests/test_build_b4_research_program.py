"""B4 build tests — ResearchProgram object (agp/research_program.py).

Domain-generality is itself under test: nothing in these fixtures mentions
a sport; the same shapes must work for finance, biology, supply chain.
"""

from datetime import date

import pytest

from agp.research_program import (
    ArtifactRef,
    EvidenceRequirement,
    Horizon,
    ProgramStatus,
    QuestionKind,
    QuestionStatus,
    QuantileForecast,
    ResearchProgram,
    ResearchQuestion,
    SourceClassRank,
    MAX_TREE_DEPTH,
    pinball_loss,
    scale_reference,
    score_quantile_forecast,
)


def make_btc_program() -> ResearchProgram:
    """The flagship shape: 10y BTC target decomposed into dated indicators."""
    root = ResearchQuestion(
        text="BTC price target at 1, 5 and 10 years",
        kind=QuestionKind.PREDICTIVE,
        horizon=Horizon(claim_date=date(2026, 8, 22),
                        resolve_date=date(2036, 8, 22)),
        evidence_requirements=EvidenceRequirement(
            min_source_class=SourceClassRank.PRIMARY,
            min_independent_sources=3, quant_required=True),
        children=[
            ResearchQuestion(
                text="Cumulative spot-ETF net inflows exceed $X by 2027-Q2",
                kind=QuestionKind.PREDICTIVE,
                horizon=Horizon(claim_date=date(2026, 8, 22),
                                resolve_date=date(2027, 6, 30)),
                evidence_requirements=EvidenceRequirement(
                    min_source_class=SourceClassRank.PRIMARY,
                    quant_required=True)),
            ResearchQuestion(
                text="Do fiat monetary expansions historically precede "
                     "hard-asset repricing?",
                kind=QuestionKind.CAUSAL,
                evidence_requirements=EvidenceRequirement(
                    min_source_class=SourceClassRank.SECONDARY,
                    min_independent_sources=3)),
        ])
    return ResearchProgram(root_query="What's your BTC price target at "
                                      "1/5/10 years?", domain="FINANCIAL",
                           questions=[root])


class TestStructure:
    def test_tree_walk_and_leaves(self):
        p = make_btc_program()
        assert len(list(p.walk_questions())) == 3
        assert {q.text for q in p.leaves} == {
            "Cumulative spot-ETF net inflows exceed $X by 2027-Q2",
            "Do fiat monetary expansions historically precede "
            "hard-asset repricing?"}

    def test_predictive_without_horizon_is_invalid(self):
        q = ResearchQuestion(text="BTC above 100k someday",
                             kind=QuestionKind.PREDICTIVE)
        assert any("lacks a horizon" in e for e in q.validate())

    def test_horizon_must_be_forward(self):
        h = Horizon(claim_date=date(2027, 1, 1), resolve_date=date(2026, 1, 1))
        errs = h.validate()
        assert errs and "after" in errs[0]

    def test_valid_program_has_no_errors(self):
        assert make_btc_program().validate() == []

    def test_depth_limit(self):
        leaf = ResearchQuestion(text="leaf", kind=QuestionKind.DESCRIPTIVE)
        mid = ResearchQuestion(text="mid", kind=QuestionKind.DESCRIPTIVE,
                               children=[leaf])
        top = ResearchQuestion(text="top", kind=QuestionKind.DESCRIPTIVE,
                               children=[mid])
        root = ResearchQuestion(text="root", kind=QuestionKind.DESCRIPTIVE,
                                children=[top])
        errs = [e for e in root.validate() if "deeper" in e]
        assert errs

    def test_fingerprint_is_content_stable(self):
        p = make_btc_program()
        fp1, fp2 = p.fingerprint(), make_btc_program().fingerprint()
        assert fp1 != fp2          # ids differ
        assert p.fingerprint() == p.fingerprint()

    def test_domain_general_shapes(self):
        # Same structure, protein folding. Must validate identically.
        q = ResearchQuestion(
            text="AlphaFold-3 median GDT_TS on CASP15 targets > 0.8 by 2028",
            kind=QuestionKind.PREDICTIVE,
            horizon=Horizon(date(2026, 1, 1), date(2028, 12, 31)),
            evidence_requirements=EvidenceRequirement(
                min_source_class=SourceClassRank.PRIMARY,
                quant_required=True))
        prog = ResearchProgram(root_query="protein folding accuracy",
                               questions=[q])
        assert prog.validate() == []


class TestEvidenceRequirementsAsGates:
    def test_unmet_reasons(self):
        req = EvidenceRequirement(min_source_class=SourceClassRank.PRIMARY,
                                  min_independent_sources=3,
                                  quant_required=True)
        reasons = req.unmet_reasons(achieved_source_class=SourceClassRank.SECONDARY,
                                    independent_sources=2, produced_quant=False)
        assert len(reasons) == 3

    def test_met(self):
        req = EvidenceRequirement(min_source_class=SourceClassRank.SECONDARY,
                                  min_independent_sources=1)
        assert req.unmet_reasons(SourceClassRank.PRIMARY, 4, True) == []

    def test_rank_ordering(self):
        assert SourceClassRank.INFERRED.value < SourceClassRank.PRIMARY.value


class TestQuantileForecasts:
    def test_ordered_quantiles_required(self):
        f = QuantileForecast(date(2027, 6, 30), p10=90.0, p50=50.0, p90=110.0)
        assert any("ordered" in e for e in f.validate())

    def test_pinball_zero_on_exact(self):
        assert pinball_loss(0.5, 100.0, 100.0) == 0.0

    def test_pinball_asymmetry(self):
        # Over-predicting the median costs more when observed is below it.
        below = pinball_loss(0.9, predicted=100.0, observed=50.0)
        above = pinball_loss(0.9, predicted=50.0, observed=100.0)
        assert below < above

    def test_continuous_scoring_before_settlement(self):
        """The point of quantile storage: year-1 checkpoint scores NOW."""
        f2027 = QuantileForecast(date(2027, 6, 30), 60_000, 120_000, 250_000)
        loss = score_quantile_forecast(f2027, observed=95_000)
        assert 0 <= loss
        # observed below p10 -> large penalty at high levels
        bad = score_quantile_forecast(f2027, observed=5_000)
        assert bad > loss

    def test_invalid_forecast_raises(self):
        with pytest.raises(ValueError):
            score_quantile_forecast(
                QuantileForecast(date(2027, 1, 1), 10, 5, 1), observed=3)

    def test_scale_reference_mad(self):
        assert scale_reference([10, 10, 10, 10]) == 1.0   # degenerate guard
        assert scale_reference([]) == 1.0
        assert scale_reference([0, 10, 20]) == 10.0


class TestArtifactsAndStatus:
    def test_artifact_sha_enforced(self):
        with pytest.raises(ValueError):
            ArtifactRef(kind="csv", sha256="tooshort")

    def test_program_status_enum(self):
        assert ProgramStatus.SUSPENDED.value == "suspended"
        assert QuestionStatus.FALSIFIED.value == "falsified"

    def test_max_depth_constant(self):
        assert MAX_TREE_DEPTH == 3
