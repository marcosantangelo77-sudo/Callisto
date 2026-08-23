"""The SIGN of every retrodiction forecast comes from a keyword scan.

PipelineResearcher._leans_yes decides yes/no by searching the conclusion for
six hardcoded English phrases, defaulting to YES. The probability is then
0.5 ± confidence/2 in that direction.

So a conclusion that AFFIRMS its claim scores NO whenever it happens to contain
"no evidence", "does not", or "unlikely" anywhere — phrases that appear
constantly in careful research prose, usually about a SIDE point. The forecast
direction is decided by incidental wording, not by the finding.

Observed consequence in the only live batch: predicted 0.33 against a realised
0.60, mean_edge_taken -0.31, beat_market_rate 0.40 — worse than a coin. A
systematically inverted sign produces exactly that shape.
"""
import pytest

from tools.pipeline.retro import PipelineResearcher

leans_yes = PipelineResearcher._leans_yes


@pytest.mark.xfail(strict=True, reason=(
    "OPEN DEFECT: forecast direction is a substring scan over prose. "
    "PipelineResult carries no stance field, so fixing this means synthesis "
    "must DECLARE whether it affirms or denies. Remove these markers with "
    "that change — strict=True makes them fail loudly once it lands."))
class TestDirectionIsDecidedByIncidentalWording:
    def test_affirmative_conclusion_with_an_aside_flips_to_no(self):
        """Affirms the claim; mentions a side point with 'no evidence'."""
        c = ("The merger completed on schedule in Q3, confirmed by the "
             "8-K filing and two independent trade reports. There is no "
             "evidence of regulatory objection.")
        assert leans_yes(c), \
            "an affirmative conclusion scored NO because of an aside"

    def test_hedged_affirmative_flips_on_unlikely(self):
        c = ("The company did raise guidance, though a further raise this "
             "quarter is unlikely.")
        assert leans_yes(c), "an affirmative conclusion scored NO on a hedge"

    def test_negative_conclusion_without_the_magic_words_scores_yes(self):
        """The default-YES half: a clear NO that dodges all six phrases."""
        c = ("The trial missed its primary endpoint; the drug failed to "
             "separate from placebo on every measured axis.")
        assert not leans_yes(c), \
            "a clearly negative conclusion scored YES (default-yes)"


@pytest.mark.xfail(strict=True, reason="OPEN DEFECT: see above")
def test_direction_should_not_come_from_prose_at_all():
    """The structural point: a forecast's sign must be a declared field.

    This pin fails while direction is inferred from conclusion text. It is
    documentation of a design defect, not of a typo — remove it only when the
    pipeline emits an explicit stance that the scorer reads.
    """
    import inspect
    src = inspect.getsource(PipelineResearcher.answer_async) \
        if hasattr(PipelineResearcher, "answer_async") else ""
    assert "_leans_yes" not in src, (
        "forecast direction is still inferred from prose by keyword scan; "
        "the pipeline should declare its stance explicitly")
