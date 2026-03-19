"""Tests for confidence calibration — the system must not lie to itself."""

from orchestrator import _clamp_confidence, MAX_CONFIDENCE_NO_TOOL


class TestConfidenceCalibration:
    def test_no_tools_caps_at_probable(self):
        """Without real sources, confidence cannot exceed PROBABLE ceiling."""
        assert _clamp_confidence(0.95, has_real_sources=False) == MAX_CONFIDENCE_NO_TOOL
        assert _clamp_confidence(0.80, has_real_sources=False) == MAX_CONFIDENCE_NO_TOOL
        assert _clamp_confidence(0.55, has_real_sources=False) == 0.55

    def test_with_tools_uncapped(self):
        """With real sources, full confidence range available."""
        assert _clamp_confidence(0.95, has_real_sources=True) == 0.95
        assert _clamp_confidence(0.55, has_real_sources=True) == 0.55

    def test_clamp_bounds(self):
        assert _clamp_confidence(-0.5, has_real_sources=True) == 0.0
        assert _clamp_confidence(1.5, has_real_sources=True) == 1.0

    def test_no_tool_ceiling_value(self):
        """Ceiling must be at or below PROBABLE threshold (0.55)."""
        assert MAX_CONFIDENCE_NO_TOOL <= 0.55
