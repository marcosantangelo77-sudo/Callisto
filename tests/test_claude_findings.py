"""Tests for Claude deep work findings -> self-repair pipeline.

Verifies that:
1. Finding classification maps keywords to correct strategies
2. handle_claude_findings routes findings and returns results
3. Sport priority ordering works correctly
"""

import asyncio
import json
import os
import sys

import pytest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.self_repair import SelfRepairEngine


class TestFindingClassification:
    """Test that free-text findings are classified to the right strategies."""

    def test_duplicate_events_keywords(self):
        assert SelfRepairEngine._classify_finding("Multiple hypotheses tested identical event sets") == "duplicate_events"
        assert SelfRepairEngine._classify_finding("These hypotheses are using the same games") == "duplicate_events"
        assert SelfRepairEngine._classify_finding("Duplicate backtest data detected") == "duplicate_events"

    def test_side_filter_keywords(self):
        assert SelfRepairEngine._classify_finding("Side filter not applied to totals hypotheses") == "side_filter_broken"
        assert SelfRepairEngine._classify_finding("Over/under not filtered correctly") == "side_filter_broken"
        assert SelfRepairEngine._classify_finding("The side_filter is missing for 'totals under' hypothesis") == "side_filter_broken"

    def test_prioritize_sports_keywords(self):
        assert SelfRepairEngine._classify_finding("Should prioritize NBA and NFL over MLB") == "prioritize_sports"
        assert SelfRepairEngine._classify_finding("NBA over MLB — more historical data") == "prioritize_sports"
        assert SelfRepairEngine._classify_finding("Need to reorder backtest queue by sport priority") == "prioritize_sports"

    def test_low_sample_keywords(self):
        assert SelfRepairEngine._classify_finding("Low sample size for these hypotheses") == "low_sample_size"
        assert SelfRepairEngine._classify_finding("Not enough data to draw conclusions") == "low_sample_size"
        assert SelfRepairEngine._classify_finding("Too few events for statistical significance") == "low_sample_size"

    def test_promotion_threshold_keywords(self):
        assert SelfRepairEngine._classify_finding("Zero promotions after 50 cycles") == "promotion_thresholds_strict"
        assert SelfRepairEngine._classify_finding("Nothing promoted — promotion threshold may be too strict") == "promotion_thresholds_strict"
        assert SelfRepairEngine._classify_finding("0 promotions despite promising signals") == "promotion_thresholds_strict"

    def test_edge_ceiling_keywords(self):
        assert SelfRepairEngine._classify_finding("Edge ceiling at 2.5% prevents discoveries") == "edge_ceiling"
        assert SelfRepairEngine._classify_finding("Thresholds above 2% should be lowered") == "edge_ceiling"
        assert SelfRepairEngine._classify_finding("Max edge threshold too high for this market") == "edge_ceiling"

    def test_unknown_finding(self):
        assert SelfRepairEngine._classify_finding("Something completely unrelated to any pattern") == "unknown"
        assert SelfRepairEngine._classify_finding("") == "unknown"

    def test_case_insensitive(self):
        assert SelfRepairEngine._classify_finding("IDENTICAL EVENT SETS detected") == "duplicate_events"
        assert SelfRepairEngine._classify_finding("PRIORITIZE NBA over mlb") == "prioritize_sports"

    def test_first_match_wins(self):
        """When multiple patterns could match, the first pattern in the list wins."""
        # "identical" matches duplicate_events (first pattern) even if other words match later patterns
        assert SelfRepairEngine._classify_finding("identical event sets, also a filtering bug") == "duplicate_events"


class TestSportPriority:
    """Test that SPORT_PRIORITY ordering is correct."""

    def test_nba_first(self):
        from tools.autonomous import SPORT_PRIORITY
        assert SPORT_PRIORITY["basketball_nba"] < SPORT_PRIORITY["baseball_mlb"]
        assert SPORT_PRIORITY["basketball_nba"] < SPORT_PRIORITY["icehockey_nhl"]

    def test_nfl_before_mlb(self):
        from tools.autonomous import SPORT_PRIORITY
        assert SPORT_PRIORITY["americanfootball_nfl"] < SPORT_PRIORITY["baseball_mlb"]

    def test_mlb_near_bottom(self):
        from tools.autonomous import SPORT_PRIORITY
        # MLB should have lower priority (higher number) than NBA, NFL, NHL
        assert SPORT_PRIORITY["baseball_mlb"] > SPORT_PRIORITY["basketball_nba"]
        assert SPORT_PRIORITY["baseball_mlb"] > SPORT_PRIORITY["americanfootball_nfl"]
        assert SPORT_PRIORITY["baseball_mlb"] > SPORT_PRIORITY["icehockey_nhl"]

    def test_sort_by_priority(self):
        from tools.autonomous import SPORT_PRIORITY
        hypotheses = [
            {"sport": "baseball_mlb", "name": "mlb_h1"},
            {"sport": "basketball_nba", "name": "nba_h1"},
            {"sport": "icehockey_nhl", "name": "nhl_h1"},
            {"sport": "americanfootball_nfl", "name": "nfl_h1"},
        ]
        sorted_h = sorted(hypotheses, key=lambda h: SPORT_PRIORITY.get(h.get("sport", ""), 99))
        assert sorted_h[0]["sport"] == "basketball_nba"
        assert sorted_h[1]["sport"] == "americanfootball_nfl"
        assert sorted_h[2]["sport"] == "icehockey_nhl"
        assert sorted_h[3]["sport"] == "baseball_mlb"

    def test_unknown_sport_last(self):
        from tools.autonomous import SPORT_PRIORITY
        hypotheses = [
            {"sport": "unknown_sport", "name": "unknown_h1"},
            {"sport": "basketball_nba", "name": "nba_h1"},
        ]
        sorted_h = sorted(hypotheses, key=lambda h: SPORT_PRIORITY.get(h.get("sport", ""), 99))
        assert sorted_h[0]["sport"] == "basketball_nba"
        assert sorted_h[1]["sport"] == "unknown_sport"


class TestHandleClaudeFindings:
    """Test that handle_claude_findings correctly routes findings to handlers."""

    @pytest.fixture
    def engine(self):
        return SelfRepairEngine()

    def test_unknown_finding_recorded(self, engine):
        """Unknown findings should be recorded for review, not crash."""
        findings = [{"severity": "LOW", "description": "Completely novel issue never seen before"}]
        results = asyncio.run(engine.handle_claude_findings(findings))
        assert len(results) == 1
        assert results[0]["action"] == "recorded_for_review"
        assert results[0]["fixed"] is False

    def test_sport_priority_finding(self, engine):
        """Sport priority findings should confirm the priority is active."""
        findings = [{"severity": "HIGH", "description": "Prioritize NBA over MLB for backtesting"}]
        results = asyncio.run(engine.handle_claude_findings(findings))
        assert len(results) == 1
        assert results[0]["fixed"] is True
        assert results[0]["action"] == "sport_priority_confirmed"

    def test_multiple_findings(self, engine):
        """Multiple findings should each produce a result."""
        findings = [
            {"severity": "CRITICAL", "description": "Identical event sets across hypotheses"},
            {"severity": "HIGH", "description": "Prioritize NBA over MLB"},
            {"severity": "LOW", "description": "Some random observation"},
        ]
        results = asyncio.run(engine.handle_claude_findings(findings))
        assert len(results) == 3

    def test_empty_findings(self, engine):
        """Empty findings list should return empty results."""
        results = asyncio.run(engine.handle_claude_findings([]))
        assert results == []


class TestNoTelegramSendMessage:
    """Verify telegram.send_message is not used anywhere."""

    def test_no_send_message_in_autonomous(self):
        import tools.autonomous
        source = open(tools.autonomous.__file__, "r").read()
        assert "telegram.send_message" not in source

    def test_no_send_message_in_api(self):
        import api
        source = open(api.__file__, "r").read()
        assert "telegram.send_message" not in source

    def test_no_send_message_in_health(self):
        import tools.health
        source = open(tools.health.__file__, "r").read()
        assert "telegram.send_message" not in source
