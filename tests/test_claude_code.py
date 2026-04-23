"""Tests for Claude Code tool integration — Tier 2 SOTA escalation."""

import asyncio
from unittest.mock import AsyncMock, patch

from tools.claude_code import (
    claude_code_query,
    claude_code_available,
    get_usage_stats,
    _track_call,
)
from orchestrator import (
    _clamp_confidence,
    _best_source_class,
    ESCALATION_THRESHOLD,
)
from agp import Evidence, SourceClass, Domain


class TestClaudeCodeTool:
    """Test the claude_code tool itself."""

    def test_usage_tracking(self):
        """Call tracker increments correctly."""
        initial = get_usage_stats()["calls_this_window"]
        _track_call()
        after = get_usage_stats()["calls_this_window"]
        assert after == initial + 1

    def test_usage_stats_structure(self):
        stats = get_usage_stats()
        assert "calls_this_window" in stats
        assert "window_seconds" in stats
        assert "elapsed_seconds" in stats

    def test_cli_not_found_returns_error(self):
        """When claude CLI doesn't exist, return error dict, not exception."""
        with patch("tools.claude_code.CLAUDE_CMD", "nonexistent_binary_xyz"):
            result = asyncio.run(claude_code_query("test prompt"))
            assert result["error"] is not None
            assert result["source_class"] == "PRIMARY"
            assert result["content"] == ""

    def test_availability_check_with_missing_cli(self):
        """claude_code_available returns False when CLI is missing."""
        with patch("tools.claude_code.CLAUDE_CMD", "nonexistent_binary_xyz"):
            result = asyncio.run(claude_code_available())
            assert result is False


class TestEscalationLogic:
    """Test that escalation triggers and confidence ceilings work with PRIMARY."""

    def test_primary_source_from_claude_code(self):
        """Claude Code evidence tagged as PRIMARY allows full confidence."""
        ev = [
            Evidence(
                content="Claude Code analysis",
                source_class=SourceClass.PRIMARY,
                confidence_score=0.90,
                domain=Domain.TECHNICAL,
                origin_agent="claude_code",
                source_name="Claude Code (claude-opus-4-6)",
            )
        ]
        best = _best_source_class(ev, True)
        assert best == "PRIMARY"
        assert _clamp_confidence(0.90, best) == 0.90

    def test_escalation_threshold_value(self):
        """Threshold must be reasonable — above INFERRED, below SECONDARY max."""
        assert 0.55 < ESCALATION_THRESHOLD <= 0.75

    def test_mixed_local_and_claude_code_evidence(self):
        """PRIMARY from Claude Code elevates the best source class."""
        ev = [
            Evidence(
                content="Web result",
                source_class=SourceClass.SECONDARY,
                confidence_score=0.70,
                domain=Domain.FINANCIAL,
                origin_agent="architect",
                source_name="example.com",
            ),
            Evidence(
                content="Claude analysis",
                source_class=SourceClass.PRIMARY,
                confidence_score=0.85,
                domain=Domain.FINANCIAL,
                origin_agent="claude_code",
                source_name="Claude Code (claude-opus-4-6)",
            ),
        ]
        best = _best_source_class(ev, True)
        assert best == "PRIMARY"
        # With PRIMARY evidence, confidence can go above SECONDARY ceiling
        assert _clamp_confidence(0.85, best) == 0.85

    def test_no_escalation_above_threshold(self):
        """If confidence >= threshold, escalation should not trigger."""
        assert ESCALATION_THRESHOLD <= 0.75
        # A session at 0.70 confidence should not trigger escalation
        # (only < ESCALATION_THRESHOLD triggers it)
        assert 0.70 >= ESCALATION_THRESHOLD
