"""R5 build — tools/reference_class.py: reference-class-first base rates.

Hand-derived reference vectors:
  - Wilson 95% interval for 8/10 successes, z=1.96:
    denom = 1 + z^2/n = 1 + 3.8416/10 = 1.38416
    centre = (0.8 + 3.8416/20)/1.38416 = (0.8+0.19208)/1.38416 = 0.71677...
    half = 1.96*sqrt(0.8*0.2/10 + z^2/(4n^2))/denom
         = 1.96*sqrt(0.016 + 3.8416/400)/1.38416
         = 1.96*sqrt(0.025604)/1.38416 = 1.96*0.160012/1.38416 = 0.22658...
    -> [0.4902, 0.9434] approximately.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.reference_class import (
    BUILTIN_CLASSES,
    ReferenceClass,
    adjust_from_reference,
    classify_claim,
    empirical_base_rate,
    record_outcome,
    reference_class_first,
    wilson_interval,
)


@pytest.fixture()
def refclass_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_REFCLASS_DB", str(tmp_path / "rc.json"))
    return tmp_path / "rc.json"


class TestWilsonInterval:
    def test_hand_derived_8_of_10(self):
        lo, hi = wilson_interval(0.8, 10)
        assert lo == pytest.approx(0.4902, abs=2e-3)
        assert hi == pytest.approx(0.9434, abs=2e-3)

    def test_degenerate_cases(self):
        assert wilson_interval(0.5, 0) == (0.0, 1.0)
        lo, hi = wilson_interval(1.0, 50)
        assert lo > 0.9 and hi == pytest.approx(1.0)  # never claims certainty below n=inf


class TestClassification:
    def test_biotech_claim_matches_trial_class(self):
        hit = classify_claim("Will the Phase 3 trial for drug XYZ meet its "
                             "primary endpoint and win FDA approval?")
        assert hit is not None
        key, score = hit
        assert key == "clinical_trial_phase3"
        assert score >= 0.5

    def test_supply_chain_claim(self):
        hit = classify_claim("A shortage in shipping capacity causes a supply "
                             "chain disruption for automakers in Q4")
        assert hit is not None and hit[0] == "supply_chain_disruption"

    def test_unmatchable_claim_returns_none(self):
        # refusing to name a class beats guessing one
        assert classify_claim("The sky is pleasant today") is None

    def test_domain_general_keywords_not_subject_specific(self):
        # same class fires regardless of which company or molecule
        for text in ("Phase III trial of compound Q",
                     "phase 3 readout expected for their lead molecule"):
            hit = classify_claim(text)
            assert hit is not None and hit[0] == "clinical_trial_phase3"


class TestReferenceClassFirst:
    def test_literature_prior_when_no_empirical_history(self, refclass_db):
        rc = reference_class_first("Will the merger deal close after the "
                                   "takeover announcement?")
        assert rc is not None and not rc.empirical
        assert rc.base_rate == BUILTIN_CLASSES[rc.class_key]["base_rate"]
        assert "SPECULATIVE" in rc.note

    def test_empirical_overrides_literature(self, refclass_db):
        for outcome in (1, 1, 1, 0, 1, 1):   # n=6 >= min_n=5
            record_outcome("ma_deal_completion", bool(outcome))
        rc = reference_class_first("Will the announced merger deal close after "
                                   "the takeover announcement?")
        assert rc.n == 6
        assert rc.base_rate == pytest.approx(5 / 6)
        assert "Wilson" in rc.note

    def test_small_samples_do_not_override(self, refclass_db):
        record_outcome("ma_deal_completion", True)
        rc = reference_class_first("Will the announced merger deal close after "
                                   "the takeover announcement?")
        assert rc is not None and not rc.empirical


class TestAdjustFromReference:
    def test_inside_view_shift_is_logged(self, refclass_db):
        rc = reference_class_first("phase 3 trial success")
        out = adjust_from_reference(rc, evidence_shift=+0.15)
        assert out["starting_rate"] == rc.base_rate
        assert out["adjusted_prob"] == pytest.approx(rc.base_rate + 0.15)
        assert out["empirical_start"] is False

    def test_shift_bounded_and_clamped(self, refclass_db):
        rc = ReferenceClass(class_key="x", description="x", base_rate=0.95,
                            source="s", n=0, empirical=False, match_score=1.0)
        with pytest.raises(ValueError):
            adjust_from_reference(rc, 0.95)
        out = adjust_from_reference(rc, 0.5)
        assert out["adjusted_prob"] <= 0.99
