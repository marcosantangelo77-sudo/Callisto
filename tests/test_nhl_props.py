"""Tests for NHL prop category coverage in dk_scraper."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dk_scraper import DK_PROP_CATEGORIES, DK_PROP_NAME_PATTERNS


def test_nhl_skater_categories_present():
    nhl = DK_PROP_CATEGORIES.get("icehockey_nhl", {})
    required = {
        "skater_shots_on_goal",
        "skater_points",
        "skater_goals",
        "skater_assists",
        "skater_hits",
        "skater_blocks",
    }
    missing = required - set(nhl)
    assert not missing, f"missing NHL skater props: {missing}"


def test_nhl_goalie_categories_present():
    nhl = DK_PROP_CATEGORIES.get("icehockey_nhl", {})
    required = {"goalie_saves", "goalie_goals_against"}
    missing = required - set(nhl)
    assert not missing, f"missing NHL goalie props: {missing}"


def test_nhl_team_categories_present():
    nhl = DK_PROP_CATEGORIES.get("icehockey_nhl", {})
    required = {"team_total_goals", "team_total_goals_first_period", "team_shots_on_goal"}
    missing = required - set(nhl)
    assert not missing, f"missing NHL team props: {missing}"


def test_nhl_name_patterns_cover_all_markets():
    nhl_markets = set(DK_PROP_CATEGORIES.get("icehockey_nhl", {}))
    nhl_patterns = set(DK_PROP_NAME_PATTERNS.get("icehockey_nhl", {}))
    assert nhl_markets == nhl_patterns, (
        f"pattern/market mismatch — markets-only: {nhl_markets - nhl_patterns}, "
        f"patterns-only: {nhl_patterns - nhl_markets}"
    )


def test_snapshot_parse_sog_over_under():
    """Parse a synthetic Nash-shaped NHL SOG offer for both sides."""
    pkg = {
        "eventGroup": {
            "offerCategories": [
                {
                    "offerCategoryId": 1510,
                    "name": "Shots on Goal",
                    "offerSubcategoryDescriptors": [
                        {
                            "name": "Player Shots on Goal",
                            "offerSubcategory": {
                                "offers": [[
                                    {
                                        "eventId": "ed-22",
                                        "outcomes": [
                                            {
                                                "label": "Connor McDavid Over 3.5",
                                                "participant": "Connor McDavid",
                                                "line": 3.5,
                                                "oddsAmerican": "-120",
                                            },
                                            {
                                                "label": "Connor McDavid Under 3.5",
                                                "participant": "Connor McDavid",
                                                "line": 3.5,
                                                "oddsAmerican": "+100",
                                            },
                                        ],
                                    }
                                ]]
                            },
                        }
                    ],
                }
            ]
        }
    }

    players: dict[str, list[dict]] = {}
    for cat in pkg["eventGroup"]["offerCategories"]:
        for sub in cat["offerSubcategoryDescriptors"]:
            for grp in sub["offerSubcategory"]["offers"]:
                for offer in grp:
                    for oc in offer["outcomes"]:
                        side = "Over" if "over" in oc["label"].lower() else "Under"
                        price = int(str(oc["oddsAmerican"]).replace("+", ""))
                        players.setdefault(oc["participant"], []).append({
                            "market": "skater_shots_on_goal",
                            "name": side,
                            "price": price,
                            "point": oc["line"],
                        })

    assert "Connor McDavid" in players
    entries = players["Connor McDavid"]
    assert {e["name"] for e in entries} == {"Over", "Under"}
    assert all(e["point"] == 3.5 for e in entries)
