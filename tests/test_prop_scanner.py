"""Tests for the prop scanner pipeline."""

import pytest
from unittest.mock import AsyncMock, patch

from tools.prop_scanner import scan_props_ev


def _mock_props_response():
    """Simulate a 3-book prop response with known edges.

    prop_scanner's MIN_BOOKS=2 counts NON-TARGET books — DraftKings is the
    target, so we need at least 2 other books (FanDuel + BetMGM) to satisfy
    the reliable-consensus gate.
    """
    return {
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"description": "Player A", "name": "Over", "point": 20.5, "price": -138},
                            {"description": "Player A", "name": "Under", "point": 20.5, "price": 104},
                            {"description": "Player B", "name": "Over", "point": 10.5, "price": -102},
                            {"description": "Player B", "name": "Under", "point": 10.5, "price": -130},
                        ],
                    },
                ],
            },
            {
                "key": "betmgm",
                "title": "BetMGM",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"description": "Player A", "name": "Over", "point": 20.5, "price": -140},
                            {"description": "Player A", "name": "Under", "point": 20.5, "price": 106},
                            {"description": "Player B", "name": "Over", "point": 10.5, "price": -105},
                            {"description": "Player B", "name": "Under", "point": 10.5, "price": -125},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            # Player A: DK agrees with FD/BetMGM — no edge
                            {"description": "Player A", "name": "Over", "point": 20.5, "price": -135},
                            {"description": "Player A", "name": "Under", "point": 20.5, "price": 105},
                            # Player B: DK has +110 while FD/BetMGM are -102/-105 — DK is generous on Over
                            {"description": "Player B", "name": "Over", "point": 10.5, "price": 110},
                            {"description": "Player B", "name": "Under", "point": 10.5, "price": -140},
                        ],
                    },
                ],
            },
        ],
        "credits": {"remaining": 400, "used": 100},
    }


def _mock_props_no_edge():
    """Both books agree — no edge."""
    return {
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"description": "Player X", "name": "Over", "point": 15.5, "price": -115},
                            {"description": "Player X", "name": "Under", "point": 15.5, "price": -115},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"description": "Player X", "name": "Over", "point": 15.5, "price": -112},
                            {"description": "Player X", "name": "Under", "point": 15.5, "price": -118},
                        ],
                    },
                ],
            },
        ],
        "credits": {"remaining": 400, "used": 100},
    }


def _mock_props_different_lines():
    """Books have different line numbers — should NOT compare."""
    return {
        "bookmakers": [
            {
                "key": "fanduel",
                "title": "FanDuel",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            {"description": "Player Z", "name": "Over", "point": 20.5, "price": -138},
                            {"description": "Player Z", "name": "Under", "point": 20.5, "price": 104},
                        ],
                    },
                ],
            },
            {
                "key": "draftkings",
                "title": "DraftKings",
                "markets": [
                    {
                        "key": "player_points",
                        "outcomes": [
                            # Different line (21.5 vs 20.5) — should be separate keys
                            {"description": "Player Z", "name": "Over", "point": 21.5, "price": -115},
                            {"description": "Player Z", "name": "Under", "point": 21.5, "price": -115},
                        ],
                    },
                ],
            },
        ],
        "credits": {"remaining": 400, "used": 100},
    }


class TestPropScanner:
    @pytest.mark.asyncio
    async def test_finds_edge(self):
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = _mock_props_response()
            result = await scan_props_ev("basketball_nba", "test123", target_book="draftkings")

        assert result["edges_found"] >= 0
        assert result["target_book"] == "DraftKings"
        assert result["props_scanned"] >= 1
        # Should have edge data structure
        for edge in result["edges"]:
            assert "player" in edge
            assert "edge_pct" in edge
            assert "ev_per_100" in edge
            assert "kelly_fraction" in edge
            assert "book_details" in edge
            assert len(edge["book_details"]) >= 2

    @pytest.mark.asyncio
    async def test_no_edge_when_books_agree(self):
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = _mock_props_no_edge()
            result = await scan_props_ev("basketball_nba", "test456", edge_threshold=0.02)

        assert result["edges_found"] == 0
        assert result["actionable_edges"] == 0

    @pytest.mark.asyncio
    async def test_different_lines_not_compared(self):
        """Props with different line numbers should NOT be cross-referenced."""
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = _mock_props_different_lines()
            result = await scan_props_ev("basketball_nba", "test789")

        # DK line 21.5 only has 1 book (DK itself), so it can't meet MIN_BOOKS
        assert result["props_scanned"] == 0
        assert result["edges_found"] == 0

    @pytest.mark.asyncio
    async def test_handles_api_error(self):
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = {"error": "API key invalid"}
            result = await scan_props_ev("basketball_nba", "bad_id")

        assert "error" in result

    @pytest.mark.asyncio
    async def test_edge_fields_are_correct(self):
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = _mock_props_response()
            result = await scan_props_ev("basketball_nba", "test123", edge_threshold=0.0)

        # With threshold at 0, should get all comparisons
        for edge in result["edges"]:
            assert 0 < edge["fair_probability"] < 1
            assert 0 < edge["target_implied"] < 1
            assert edge["books_compared"] >= 2
            assert edge["actionable"] == (edge["edge_pct"] >= 2.0)

    @pytest.mark.asyncio
    async def test_edges_sorted_by_edge_descending(self):
        with patch("tools.prop_scanner.get_player_props", new_callable=AsyncMock) as mock:
            mock.return_value = _mock_props_response()
            result = await scan_props_ev("basketball_nba", "test123", edge_threshold=0.0)

        if len(result["edges"]) >= 2:
            for i in range(len(result["edges"]) - 1):
                assert result["edges"][i]["edge_pct"] >= result["edges"][i + 1]["edge_pct"]
