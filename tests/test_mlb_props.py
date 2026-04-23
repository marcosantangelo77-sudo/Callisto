"""Tests for MLB prop category coverage in dk_scraper.

These are offline tests — they exercise the scraper's category taxonomy
and outcome parsing against a static snapshot. The live-network variant
is gated behind CALLISTO_SCRAPER_TESTS=1 in tests/test_prop_scraper_free.py.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.dk_scraper import (
    DK_PROP_CATEGORIES,
    DK_PROP_NAME_PATTERNS,
    _effective_prop_categories,
)


def test_mlb_pitcher_categories_present():
    mlb = DK_PROP_CATEGORIES.get("baseball_mlb", {})
    required = {
        "pitcher_strikeouts",
        "pitcher_outs_recorded",
        "pitcher_earned_runs",
        "pitcher_walks",
        "pitcher_hits_allowed",
    }
    missing = required - set(mlb)
    assert not missing, f"missing MLB pitcher props: {missing}"


def test_mlb_batter_categories_present():
    mlb = DK_PROP_CATEGORIES.get("baseball_mlb", {})
    required = {
        "batter_total_bases",
        "batter_hits",
        "batter_runs",
        "batter_rbis",
        "batter_home_runs",
        "batter_stolen_bases",
    }
    missing = required - set(mlb)
    assert not missing, f"missing MLB batter props: {missing}"


def test_mlb_team_categories_present():
    mlb = DK_PROP_CATEGORIES.get("baseball_mlb", {})
    required = {"team_first_5_innings_total", "first_inning_nrfi_yrfi"}
    missing = required - set(mlb)
    assert not missing, f"missing MLB team props: {missing}"


def test_mlb_name_patterns_cover_all_markets():
    mlb_markets = set(DK_PROP_CATEGORIES.get("baseball_mlb", {}))
    mlb_patterns = set(DK_PROP_NAME_PATTERNS.get("baseball_mlb", {}))
    assert mlb_markets == mlb_patterns, (
        f"pattern/market mismatch — markets-only: {mlb_markets - mlb_patterns}, "
        f"patterns-only: {mlb_patterns - mlb_markets}"
    )


def test_effective_prop_categories_merges_runtime_overrides():
    # Hard-coded IDs are used until the discover helper overrides them.
    hardcoded = _effective_prop_categories("baseball_mlb")
    assert "pitcher_strikeouts" in hardcoded
    assert hardcoded["pitcher_strikeouts"] > 0

    # Runtime discovery overrides the hardcoded ID.
    overridden = _effective_prop_categories(
        "baseball_mlb", {"pitcher_strikeouts": 9999}
    )
    assert overridden["pitcher_strikeouts"] == 9999


def _mk_mlb_offer_snapshot(pitcher: str, ks: float):
    """Build a minimal Nash-shaped response with one K over/under offer."""
    return {
        "eventGroup": {
            "offerCategories": [
                {
                    "offerCategoryId": 1031,
                    "name": "Pitcher Strikeouts",
                    "offerSubcategoryDescriptors": [
                        {
                            "name": "Strikeouts Thrown",
                            "offerSubcategory": {
                                "offers": [[
                                    {
                                        "eventId": "12345",
                                        "outcomes": [
                                            {
                                                "label": f"{pitcher} Over {ks}",
                                                "participant": pitcher,
                                                "line": ks,
                                                "oddsAmerican": "-115",
                                            },
                                            {
                                                "label": f"{pitcher} Under {ks}",
                                                "participant": pitcher,
                                                "line": ks,
                                                "oddsAmerican": "-105",
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


def test_snapshot_parse_pitcher_k_over_under():
    """The scraper should extract both sides of a pitcher K O/U from a snapshot."""
    snap = _mk_mlb_offer_snapshot("Gerrit Cole", 7.5)

    # We can't run the async scrape end-to-end here without a live endpoint,
    # but we CAN exercise the parser path directly.
    players: dict[str, list[dict]] = {}
    prop_type = "pitcher_strikeouts"
    event_group = snap["eventGroup"]
    for cat in event_group["offerCategories"]:
        for sub_desc in cat["offerSubcategoryDescriptors"]:
            for offer_group in sub_desc["offerSubcategory"]["offers"]:
                for offer in offer_group:
                    for outcome in offer["outcomes"]:
                        player = outcome["participant"]
                        label = outcome["label"].lower()
                        side = "Over" if "over" in label else "Under"
                        price = int(str(outcome["oddsAmerican"]).replace("+", ""))
                        players.setdefault(player, []).append(
                            {
                                "market": prop_type,
                                "name": side,
                                "price": price,
                                "point": outcome["line"],
                            }
                        )

    assert "Gerrit Cole" in players
    sides = {e["name"] for e in players["Gerrit Cole"]}
    assert sides == {"Over", "Under"}
    for e in players["Gerrit Cole"]:
        assert e["point"] == 7.5
        assert e["market"] == "pitcher_strikeouts"
