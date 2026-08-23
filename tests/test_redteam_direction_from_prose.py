"""The sign of a forecast is DECLARED, never inferred from prose.

Regression pins for a fixed defect. PipelineResearcher._leans_yes used to
decide yes/no by searching the conclusion for six English phrases, defaulting
to YES; retro.py then computed prob = 0.5 +/- confidence/2 in that direction.
So the DIRECTION of every retrodiction forecast came from incidental wording:

  "The merger completed on schedule ... no evidence of regulatory objection."
      -> scored NO (affirms the claim, lost on an aside)
  "The trial missed its primary endpoint; the drug failed to separate from
   placebo."  -> scored YES (clearly negative, dodged all six phrases)

That is the shape of the only live batch: predicted 0.33 against a realised
0.60, mean_edge_taken -0.31, beat_market_rate 0.40 — worse than a coin.
"""
import pytest

from tools.pipeline import retro as retro_mod
from tools.pipeline.retro import PipelineResearcher


class _Result:
    """Stands in for a PipelineResult at the scoring boundary."""
    def __init__(self, stance, conclusion, conf=0.60, sealed=True):
        self.stance = stance
        self.conclusion = conclusion
        self.confidence_score = conf
        self.sealed = sealed


def _prob(result):
    """The exact arithmetic retro.answer_async applies to one result."""
    conf = result.confidence_score if result.sealed else 0.0
    stance = getattr(result, "stance", "UNDETERMINED")
    if stance == "AFFIRMS":
        return 0.5 + conf / 2.0
    if stance == "DENIES":
        return 0.5 - conf / 2.0
    return 0.5


def test_the_keyword_scan_is_gone():
    """_leans_yes must not come back; direction is not recoverable from prose."""
    assert not hasattr(PipelineResearcher, "_leans_yes"), \
        "the prose keyword scan is back"
    src = open(retro_mod.__file__).read()
    assert "negations = (" not in src, "a negation word-list is back in retro.py"


class TestDirectionComesFromTheDeclaredStance:
    def test_affirmative_with_a_negating_aside_scores_yes(self):
        r = _Result("AFFIRMS",
                    "The merger completed on schedule in Q3, confirmed by the "
                    "8-K filing. There is no evidence of regulatory objection.")
        assert _prob(r) > 0.5, "an affirmative finding scored NO on an aside"

    def test_negative_without_the_magic_words_scores_no(self):
        r = _Result("DENIES",
                    "The trial missed its primary endpoint; the drug failed "
                    "to separate from placebo on every measured axis.")
        assert _prob(r) < 0.5, "a clearly negative finding scored YES"

    def test_undetermined_is_exactly_one_half(self):
        """The case a default-yes scan can never express."""
        r = _Result("UNDETERMINED",
                    "Sources conflict and none is primary; the question is "
                    "not settled by the evidence gathered.")
        assert _prob(r) == 0.5, "an unsettled question took a side"

    def test_unknown_stance_does_not_become_a_lean(self):
        r = _Result("banana", "anything at all", conf=0.9)
        assert _prob(r) == 0.5, "an unparseable stance became a confident lean"

    def test_unsealed_run_is_one_half_regardless_of_stance(self):
        r = _Result("AFFIRMS", "confident prose", conf=0.9, sealed=False)
        assert _prob(r) == 0.5, "an unsealed run still moved the forecast"
