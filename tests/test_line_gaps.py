"""Tests for line gap analysis — discontinuity exploitation."""

import pytest
from tools.line_gaps import scan_line_gaps, scan_prop_gaps, _implied_to_american


class TestImpliedToAmerican:
    def test_favorite(self):
        # 60% implied → -150
        odds = _implied_to_american(0.6)
        assert odds == -150

    def test_underdog(self):
        # 40% implied → +150
        odds = _implied_to_american(0.4)
        assert odds == 150

    def test_even(self):
        # 50% → -100 or +100
        odds = _implied_to_american(0.5)
        assert odds == -100

    def test_edge_cases(self):
        assert _implied_to_american(0.0) == 0
        assert _implied_to_american(1.0) == 0


class TestScanLineGaps:
    def _make_bookmaker(self, title, outcomes, market_key="alternate_spreads"):
        return {
            "title": title,
            "markets": [{
                "key": market_key,
                "outcomes": outcomes,
            }],
        }

    def test_detects_gap(self):
        """Book A offers -3.5 and -5.5 but skips -4.5 → gap detected."""
        book_a = self._make_bookmaker("BookA", [
            {"name": "TeamX", "point": -3.5, "price": -110},
            {"name": "TeamX", "point": -5.5, "price": -130},
        ])
        gaps = scan_line_gaps([book_a])
        assert len(gaps) >= 1
        # Sorted ascending: -5.5, -3.5. Step 0.5. Missing: -5.0, -4.5, -4.0
        gap_points = [g["gap_point"] for g in gaps]
        assert -4.5 in gap_points

    def test_no_gap_uniform(self):
        """Uniform 0.5 intervals → no gaps."""
        book = self._make_bookmaker("BookA", [
            {"name": "TeamX", "point": -3.5, "price": -110},
            {"name": "TeamX", "point": -4.0, "price": -115},
            {"name": "TeamX", "point": -4.5, "price": -120},
        ])
        gaps = scan_line_gaps([book])
        assert len(gaps) == 0

    def test_cross_book_reference(self):
        """Book B offers the gap point that Book A skips."""
        book_a = self._make_bookmaker("BookA", [
            {"name": "TeamX", "point": -3.5, "price": -110},
            {"name": "TeamX", "point": -5.5, "price": -130},
        ])
        book_b = self._make_bookmaker("BookB", [
            {"name": "TeamX", "point": -3.5, "price": -112},
            {"name": "TeamX", "point": -4.5, "price": -118},
            {"name": "TeamX", "point": -5.5, "price": -128},
        ])
        gaps = scan_line_gaps([book_a, book_b])
        # Should find the gap from BookA
        book_a_gaps = [g for g in gaps if g["bookmaker_with_gap"] == "BookA"]
        assert len(book_a_gaps) >= 1
        # The gap at -4.5 should have cross-reference to BookB
        gap_45 = [g for g in book_a_gaps if g["gap_point"] == -4.5]
        assert len(gap_45) == 1
        assert len(gap_45[0]["other_books_with_line"]) >= 1
        assert gap_45[0]["other_books_with_line"][0]["bookmaker"] == "BookB"

    def test_interpolated_value(self):
        """Interpolated implied probability is between the brackets."""
        book = self._make_bookmaker("BookA", [
            {"name": "TeamX", "point": 10, "price": -110},
            {"name": "TeamX", "point": 14, "price": -150},
        ])
        gaps = scan_line_gaps([book])
        assert len(gaps) > 0
        for g in gaps:
            low_impl = g["bracket_low"]["implied"]
            high_impl = g["bracket_high"]["implied"]
            interp = g["interpolated_implied"]
            # Interpolated should be between or equal to brackets
            assert min(low_impl, high_impl) <= interp <= max(low_impl, high_impl)

    def test_team_filter(self):
        """Team filter only returns gaps for matching team."""
        book = self._make_bookmaker("BookA", [
            {"name": "Lakers", "point": 5, "price": -110},
            {"name": "Lakers", "point": 8, "price": -130},
            {"name": "Celtics", "point": 5, "price": -110},
            {"name": "Celtics", "point": 8, "price": -130},
        ])
        gaps = scan_line_gaps([book], team_filter="Lakers")
        for g in gaps:
            assert "lakers" in g["team"].lower()

    def test_empty_input(self):
        assert scan_line_gaps([]) == []

    def test_exploitable_flag(self):
        """Exploitable gap: interpolated is much higher than other book's actual."""
        book_a = self._make_bookmaker("BookA", [
            {"name": "TeamX", "point": 10, "price": -200},  # ~66.7% implied
            {"name": "TeamX", "point": 14, "price": -300},  # ~75% implied
        ])
        book_b = self._make_bookmaker("BookB", [
            {"name": "TeamX", "point": 12, "price": -110},  # ~52.4% implied (way under interpolated ~70%)
        ])
        gaps = scan_line_gaps([book_a, book_b])
        exploitable = [g for g in gaps if g.get("exploitable")]
        assert len(exploitable) >= 1


class TestScanPropGaps:
    def test_basic_prop_gap(self):
        """Player props with gap in one book's offerings."""
        player_props = {
            "players": {
                "Player A": [
                    {"bookmaker": "BookA", "market": "player_points", "name": "Over", "point": 15, "price": -110},
                    {"bookmaker": "BookA", "market": "player_points", "name": "Over", "point": 18, "price": -130},
                    {"bookmaker": "BookB", "market": "player_points", "name": "Over", "point": 15, "price": -108},
                    {"bookmaker": "BookB", "market": "player_points", "name": "Over", "point": 16, "price": -115},
                    {"bookmaker": "BookB", "market": "player_points", "name": "Over", "point": 17, "price": -125},
                    {"bookmaker": "BookB", "market": "player_points", "name": "Over", "point": 18, "price": -132},
                ],
            },
        }
        gaps = scan_prop_gaps(player_props)
        assert len(gaps) >= 1
        assert gaps[0]["player"] == "Player A"
        assert gaps[0]["bookmaker_with_gap"] == "BookA"

    def test_no_gap(self):
        """Consecutive points → no gap."""
        player_props = {
            "players": {
                "Player A": [
                    {"bookmaker": "BookA", "market": "player_points", "name": "Over", "point": 15, "price": -110},
                    {"bookmaker": "BookA", "market": "player_points", "name": "Over", "point": 16, "price": -120},
                ],
            },
        }
        gaps = scan_prop_gaps(player_props)
        assert len(gaps) == 0

    def test_empty_input(self):
        assert scan_prop_gaps({}) == []
        assert scan_prop_gaps({"players": {}}) == []
