"""Tests for confidence calibration — the system must not lie to itself."""

from orchestrator import _clamp_confidence, _best_source_class, MAX_CONFIDENCE_NO_TOOL, MAX_CONFIDENCE_BY_SOURCE
from agp import Evidence, SourceClass, Domain


class TestConfidenceCalibration:
    def test_inferred_caps_at_probable(self):
        """INFERRED evidence cannot exceed PROBABLE ceiling."""
        assert _clamp_confidence(0.95, "INFERRED") == 0.55
        assert _clamp_confidence(0.80, "INFERRED") == 0.55
        assert _clamp_confidence(0.55, "INFERRED") == 0.55

    def test_secondary_caps_at_corroborated(self):
        """SECONDARY evidence cannot exceed CORROBORATED ceiling."""
        assert _clamp_confidence(0.95, "SECONDARY") == 0.75
        assert _clamp_confidence(0.75, "SECONDARY") == 0.75
        assert _clamp_confidence(0.50, "SECONDARY") == 0.50

    def test_primary_uncapped(self):
        """PRIMARY evidence allows full confidence range."""
        assert _clamp_confidence(0.95, "PRIMARY") == 0.95
        assert _clamp_confidence(1.0, "PRIMARY") == 1.0

    def test_signal_caps_at_probable(self):
        """SIGNAL evidence cannot exceed PROBABLE ceiling."""
        assert _clamp_confidence(0.80, "SIGNAL") == 0.55

    def test_clamp_bounds(self):
        assert _clamp_confidence(-0.5, "PRIMARY") == 0.0
        assert _clamp_confidence(1.5, "PRIMARY") == 1.0

    def test_no_tool_ceiling_value(self):
        """Ceiling must be at or below PROBABLE threshold (0.55)."""
        assert MAX_CONFIDENCE_NO_TOOL <= 0.55

    def test_default_is_inferred(self):
        """Default (no source class) clamps to INFERRED ceiling."""
        assert _clamp_confidence(0.95) == 0.55


class TestBestSourceClass:
    def test_empty_no_tools(self):
        assert _best_source_class([], False) == "INFERRED"

    def test_secondary_evidence(self):
        ev = [Evidence(
            content="test", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=Domain.TECHNICAL,
            origin_agent="architect", source_name="test.com",
        )]
        assert _best_source_class(ev, True) == "SECONDARY"

    def test_mixed_picks_best(self):
        ev = [
            Evidence(content="a", source_class=SourceClass.INFERRED,
                     confidence_score=0.5, domain=Domain.TECHNICAL,
                     origin_agent="architect", source_name="training"),
            Evidence(content="b", source_class=SourceClass.SECONDARY,
                     confidence_score=0.7, domain=Domain.TECHNICAL,
                     origin_agent="architect", source_name="web"),
        ]
        assert _best_source_class(ev, True) == "SECONDARY"

    def test_primary_wins(self):
        ev = [
            Evidence(content="a", source_class=SourceClass.SECONDARY,
                     confidence_score=0.7, domain=Domain.TECHNICAL,
                     origin_agent="architect", source_name="web"),
            Evidence(content="b", source_class=SourceClass.PRIMARY,
                     confidence_score=0.95, domain=Domain.TECHNICAL,
                     origin_agent="architect", source_name="direct"),
        ]
        assert _best_source_class(ev, True) == "PRIMARY"
