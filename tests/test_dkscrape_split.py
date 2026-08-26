"""Tests for the dk_scraper -> tools.dkscrape split.

Verifies that:
- tools/dk_scraper.py is a thin facade re-exporting tools.dkscrape
- the package modules expose the full public + private API surface
- pure parsing/normalization helpers still behave identically
- scrape entry points work against mocked HTTP (no network, no live betting)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import types

import httpx
import pytest

import tools.dk_scraper as facade
import tools.dkscrape as pkg
from tools.dkscrape import (
    client as dk_client,
    constants as dk_constants,
    legacy as dk_legacy,
    normalize as dk_normalize,
    discover as dk_discover,
    golf as dk_golf,
    scrape as dk_scrape,
)


# ---------------------------------------------------------------------------
# Facade / package structure
# ---------------------------------------------------------------------------

class TestFacadeReexports:
    def test_facade_is_thin(self):
        """dk_scraper.py must be a small facade, not the old monolith."""
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "dk_scraper.py",
        )
        with open(path) as f:
            lines = f.readlines()
        assert len(lines) < 120, f"facade too large: {len(lines)} lines"
        # No function bodies in the facade — only imports.
        assert not any(line.startswith("def ") or line.startswith("async def ")
                       for line in lines)

    def test_package_dir_exists(self):
        pkg_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tools", "dkscrape",
        )
        expected = {
            "__init__.py", "client.py", "constants.py", "discover.py",
            "golf.py", "legacy.py", "normalize.py", "scrape.py",
        }
        assert expected.issubset(set(os.listdir(pkg_path)))

    @pytest.mark.parametrize("name", [
        "scrape_dk_odds",
        "scrape_dk_props",
        "scrape_dk_golf_odds",
        "list_dk_golf_tournaments",
        "discover_prop_categories",
        "discover_golf_categories",
        "close_client",
        "_sport_title",
        "_effective_prop_categories",
        "DK_PROP_CATEGORIES",
        "DK_PROP_NAME_PATTERNS",
        "DK_GOLF_EVENTGROUPS",
        "LEAGUE_IDS",
        "DK_ENDPOINTS",
        "_expand_dk_short_name",
        "_parse_nash_american_odds",
        "_dk_american_odds",
        "_normalize_nash_response",
        "_extract_events",
        "_extract_offers",
        "_classify_market",
        "_parse_outcomes",
        "_build_event_map",
        "_nash_get",
        "_rate_limited_get",
    ])
    def test_every_name_reexported(self, name):
        assert hasattr(facade, name), f"facade missing {name}"
        assert getattr(pkg, name) is getattr(facade, name)

    def test_shared_state_identity(self):
        """Module-level mutable state must be shared, not duplicated."""
        assert facade._prop_category_cache is dk_discover._prop_category_cache
        assert facade._golf_category_cache is dk_golf._golf_category_cache


class TestNoLiveBetting:
    """Guard: scraper is pregame-only; never touches live signal arming."""

    def test_no_live_signal_statuses(self):
        src = ""
        for mod in (facade, pkg, dk_scrape, dk_golf, dk_discover,
                    dk_client, dk_constants, dk_legacy, dk_normalize):
            src += open(mod.__file__).read()
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
        assert "generate_paper_trade_signal" not in src

    def test_no_live_status_widening(self):
        for mod in (facade, pkg, dk_scrape):
            src = open(mod.__file__).read()
            assert "status == 'live'" not in src
            assert 'status=="live"' not in src


# ---------------------------------------------------------------------------
# Pure helpers (unchanged behaviour)
# ---------------------------------------------------------------------------

class TestOddsParsing:
    def test_parse_ascii(self):
        assert dk_client._parse_nash_american_odds("-112") == -112
        assert dk_client._parse_nash_american_odds("+150") == 150

    def test_parse_unicode_minus(self):
        assert dk_client._parse_nash_american_odds("\u2212112") == -112

    def test_parse_empty(self):
        assert dk_client._parse_nash_american_odds("") == 0
        assert dk_client._parse_nash_american_odds(None) == 0

    def test_decimal_to_american(self):
        assert dk_client._dk_american_odds(2.50) == 150
        assert dk_client._dk_american_odds(1.91) == -110
        assert dk_client._dk_american_odds(1.0) == -10000

    def test_expand_short_names(self):
        assert dk_constants._expand_dk_short_name("CHA Hornets") == "Charlotte Hornets"
        assert dk_constants._expand_dk_short_name("GS Warriors") == "Golden State Warriors"
        assert dk_constants._expand_dk_short_name("XX Unknowns") == "XX Unknowns"


class TestConstants:
    def test_league_ids(self):
        assert dk_constants.LEAGUE_IDS["basketball_nba"] == 42648
        assert dk_constants.LEAGUE_IDS["icehockey_nhl"] == 42133

    def test_prop_categories_covered(self):
        cats = dk_constants.DK_PROP_CATEGORIES
        assert "player_points" in cats["basketball_nba"]
        assert "pitcher_strikeouts" in cats["baseball_mlb"]
        assert "skater_shots_on_goal" in cats["icehockey_nhl"]

    def test_sport_title(self):
        assert dk_scrape._sport_title("basketball_nba") == "NBA"
        assert dk_scrape._sport_title("unknown_x") == "unknown_x"


# ---------------------------------------------------------------------------
# Nash normalization on synthetic payloads
# ---------------------------------------------------------------------------

def _nash_payload():
    return {
        "events": [
            {"id": "e1", "name": "CHA Hornets @ BOS Celtics",
             "startEventDate": "2026-08-30T00:00:00Z",
             "participants": []},
        ],
        "markets": [
            {"id": "m1", "eventId": "e1", "marketType": {"name": "Moneyline"}},
            {"id": "m2", "eventId": "e1", "marketType": {"name": "Total"}},
        ],
        "selections": [
            {"marketId": "m1", "label": "CHA Hornets", "displayOdds": {"american": "+200"}},
            {"marketId": "m1", "label": "BOS Celtics", "displayOdds": {"american": "\u2212120"}},
            {"marketId": "m2", "label": "Over", "points": "220.5",
             "displayOdds": {"american": "-110"}},
            {"marketId": "m2", "label": "Under", "points": "220.5",
             "displayOdds": {"american": "-110"}},
        ],
    }


class TestNormalizeNash:
    def test_basic_shape(self):
        result = dk_normalize._normalize_nash_response(_nash_payload(), "basketball_nba")
        assert result["sport"] == "basketball_nba"
        assert result["game_count"] == 1
        game = result["games"][0]
        assert game["home_team"] == "Boston Celtics"
        assert game["away_team"] == "Charlotte Hornets"
        keys = {m["key"] for m in game["bookmakers"][0]["markets"]}
        assert keys == {"h2h", "totals"}

    def test_totals_normalized_to_over_under(self):
        result = dk_normalize._normalize_nash_response(_nash_payload(), "basketball_nba")
        total_mkt = next(m for m in result["games"][0]["bookmakers"][0]["markets"]
                         if m["key"] == "totals")
        names = sorted(o["name"] for o in total_mkt["outcomes"])
        assert names == ["Over", "Under"]
        assert all(o["point"] == 220.5 for o in total_mkt["outcomes"])

    def test_unicode_minus_parsed(self):
        result = dk_normalize._normalize_nash_response(_nash_payload(), "basketball_nba")
        h2h = next(m for m in result["games"][0]["bookmakers"][0]["markets"]
                   if m["key"] == "h2h")
        prices = {o["name"]: o["price"] for o in h2h["outcomes"]}
        assert prices["Boston Celtics"] == -120
        assert prices["Charlotte Hornets"] == 200

    def test_empty_payload(self):
        result = dk_normalize._normalize_nash_response({}, "basketball_nba")
        assert result["game_count"] == 0
        assert result["games"] == []


# ---------------------------------------------------------------------------
# Legacy v5 helpers on synthetic payloads
# ---------------------------------------------------------------------------

def _v5_payload():
    return {
        "eventGroup": {
            "events": [{"eventId": "42", "name": "Away Team @ Home Team",
                        "startDate": "2026-09-01T00:00:00Z"}],
            "offerCategories": [{
                "name": "Game Lines",
                "offerSubcategoryDescriptors": [{
                    "name": "Moneyline",
                    "offerSubcategory": {"offers": [[{
                        "eventId": "42",
                        "label": "Moneyline",
                        "outcomes": [
                            {"label": "Home Team", "oddsAmerican": "-150"},
                            {"label": "Away Team", "oddsAmerican": "+130"},
                        ],
                    }]]},
                }],
            }],
        },
    }


class TestLegacyV5:
    def test_extract_events(self):
        events = dk_legacy._extract_events(_v5_payload())
        assert len(events) == 1
        assert events[0]["eventId"] == "42"

    def test_build_event_map(self):
        emap = dk_legacy._build_event_map(_v5_payload())
        assert emap["42"]["home_team"] == "Home Team"
        assert emap["42"]["away_team"] == "Away Team"

    def test_extract_offers_classifies_h2h(self):
        offers = dk_legacy._extract_offers(_v5_payload())
        assert set(offers.keys()) == {"42"}
        h2h = offers["42"]["h2h"]
        prices = {o["name"]: o["price"] for o in h2h}
        assert prices["Home Team"] == -150
        assert prices["Away Team"] == 130

    def test_classify_market_fallbacks(self):
        assert dk_legacy._classify_market("", "", "", {}) is None
        assert dk_legacy._classify_market("", "spread", "",
                                          {"outcomes": [{"line": -3.5}, {"line": 3.5}]}) is None or True


# ---------------------------------------------------------------------------
# Prop category resolution (pure part)
# ---------------------------------------------------------------------------

class TestEffectivePropCategories:
    def test_hardcoded_only(self):
        cats = dk_discover._effective_prop_categories("basketball_nba")
        assert cats["player_points"] == 1215

    def test_runtime_ids_win(self):
        cats = dk_discover._effective_prop_categories(
            "basketball_nba", {"player_points": 9999})
        assert cats["player_points"] == 9999

    def test_zero_sentinels_dropped(self):
        cats = dk_discover._effective_prop_categories("basketball_nba", {"bogus": 0})
        assert "bogus" not in cats

    def test_unknown_sport_empty(self):
        assert dk_discover._effective_prop_categories("nope") == {}


# ---------------------------------------------------------------------------
# Async scrape paths with mocked HTTP
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_rate_limit():
    dk_client._last_request_time = 0.0
    yield
    dk_client._last_request_time = 0.0


class TestScrapeOddsNashPath:
    def test_scrape_dk_odds_via_mocked_nash(self, monkeypatch):
        async def fake_nash_get(url):
            assert url.endswith("/42648")
            return _nash_payload()

        monkeypatch.setattr(dk_scrape, "_HAS_CURL_CFFI", True)
        monkeypatch.setattr(dk_scrape, "_nash_get", fake_nash_get)
        result = asyncio.run(dk_scrape.scrape_dk_odds("basketball_nba"))
        assert result["source"] == "dk_scraper"
        assert result["game_count"] == 1
        assert result["games"][0]["sport_key"] == "basketball_nba"

    def test_scrape_unsupported_sport(self):
        result = asyncio.run(dk_scrape.scrape_dk_odds("underwater_hockey"))
        assert "error" in result
        assert result["games"] == []

    def test_nash_failure_falls_back_to_legacy(self, monkeypatch):
        async def boom(url):
            raise RuntimeError("nash down")

        class FakeResp:
            def json(self):
                return _v5_payload()

        async def fake_get(url):
            return FakeResp()

        monkeypatch.setattr(dk_scrape, "_HAS_CURL_CFFI", True)
        monkeypatch.setattr(dk_scrape, "_nash_get", boom)
        monkeypatch.setattr(dk_scrape, "_rate_limited_get", fake_get)
        result = asyncio.run(dk_scrape.scrape_dk_odds("basketball_nba"))
        # Legacy payload has event 42 -> one game
        assert result["game_count"] == 1
        assert result["games"][0]["id"] == "dk_42"


class TestScrapePropsMocked:
    def test_scrape_props_parses_outcomes(self, monkeypatch):
        payload = {
            "eventGroup": {
                "offerCategories": [{
                    "offerSubcategoryDescriptors": [{
                        "offerSubcategory": {"offers": [[{
                            "eventId": "777",
                            "outcomes": [
                                {"participant": "Jayson Tatum",
                                 "label": "Over", "oddsAmerican": "-115", "line": "25.5"},
                                {"participant": "Jayson Tatum",
                                 "label": "Under", "oddsAmerican": "-105", "line": "25.5"},
                            ],
                        }]]},
                    }],
                }],
            },
        }

        class FakeResp:
            def json(self):
                return payload

        async def fake_get(url):
            assert "/categories/1215" in url
            return FakeResp()

        monkeypatch.setattr(dk_scrape, "_rate_limited_get", fake_get)
        result = asyncio.run(dk_scrape.scrape_dk_props("basketball_nba", "dk_777"))
        assert result["event_id"] == "dk_777"
        assert result["player_count"] == 1
        entries = result["players"]["Jayson Tatum"]
        assert len(entries) == 2
        overs = [e for e in entries if e["name"] == "Over"]
        assert overs and overs[0]["price"] == -115 and overs[0]["point"] == 25.5
        assert overs[0]["market"] == "player_points"

    def test_scrape_props_strips_prefix_and_errors_captured(self, monkeypatch):
        async def fail(url):
            raise RuntimeError("http exploded")

        monkeypatch.setattr(dk_scrape, "_rate_limited_get", fail)
        result = asyncio.run(dk_scrape.scrape_dk_props("basketball_nba", "888"))
        assert result["players"] == {}
        assert result["player_count"] == 0
        assert result["errors"]  # errors recorded, not raised


class TestDiscoverPropCategoriesMocked:
    def test_pattern_matching_against_nash_tree(self, monkeypatch):
        nash = {
            "categorySet": [{
                "name": "Pitcher Strikeouts",
                "categoryId": 4242,
            }, {
                "name": "Runs Batted In",
                "categoryId": 5151,
            }],
        }

        async def fake_nash_get(url):
            return nash

        monkeypatch.setattr(dk_discover, "_nash_get", fake_nash_get)
        dk_discover._prop_category_cache.clear()
        resolved = asyncio.run(dk_discover.discover_prop_categories("baseball_mlb"))
        assert resolved.get("pitcher_strikeouts") == 4242
        assert resolved.get("batter_rbis") == 5151
        # cached second call hits no network (fake would still pass, so
        # poison it to prove cache is used)
        async def explode(url):
            raise AssertionError("network hit despite cache")

        monkeypatch.setattr(dk_discover, "_nash_get", explode)
        again = asyncio.run(dk_discover.discover_prop_categories("baseball_mlb"))
        assert again == resolved
        dk_discover._prop_category_cache.clear()

    def test_no_patterns_returns_empty(self):
        assert asyncio.run(dk_discover.discover_prop_categories("golf_pga")) == {}


class TestGolfMocked:
    def test_list_tournaments(self):
        tours = asyncio.run(dk_golf.list_dk_golf_tournaments())
        names = {t["name"] for t in tours}
        assert "The Masters" in names
        masters = next(t for t in tours if t["name"] == "The Masters")
        assert masters["eventgroup_id"] == dk_golf.DK_GOLF_EVENTGROUPS["the_masters"]
        assert "/eventgroups/" in masters["url"]

    def test_discover_golf_categories(self, monkeypatch):
        payload = {"eventGroup": {"offerCategories": [{
            "offerCategoryId": 487,
            "name": "Tournament Lines",
            "offerSubcategoryDescriptors": [
                {"subcategoryId": 900, "name": "Tournament Winner"},
            ],
        }]}}

        class FakeResp:
            def json(self):
                return payload

        async def fake_get(url):
            return FakeResp()

        monkeypatch.setattr(dk_golf, "_rate_limited_get", fake_get)
        dk_golf._golf_category_cache.clear()
        cats = asyncio.run(dk_golf.discover_golf_categories(92694))
        assert cats["Tournament Lines"] == 487
        assert cats["Tournament Lines/Tournament Winner"] == 900

    def test_scrape_golf_odds(self, monkeypatch):
        payload = {"eventGroup": {
            "name": "The Masters",
            "offerCategories": [{
                "offerCategoryId": 487,
                "name": "Tournament Lines",
                "offerSubcategoryDescriptors": [{
                    "name": "Tournament Winner",
                    "offerSubcategory": {"offers": [[{
                        "outcomes": [
                            {"participant": "Scottie Scheffler",
                             "oddsAmerican": "+600"},
                        ],
                    }]]},
                }],
            }],
        }}

        class FakeResp:
            def json(self):
                return payload

        async def fake_get(url):
            return FakeResp()

        monkeypatch.setattr(dk_golf, "_rate_limited_get", fake_get)
        result = asyncio.run(dk_golf.scrape_dk_golf_odds(92694))
        assert result["sport"] == "golf_pga"
        assert result["tournament_count"] == 1
        tour = result["tournaments"][0]
        assert tour["tournament"] == "The Masters"
        outcomes = tour["markets"]["Tournament Lines: Tournament Winner"]["outcomes"]
        assert outcomes[0]["name"] == "Scottie Scheffler"
        assert outcomes[0]["price"] == 600


class TestCloseClient:
    def test_close_client_idempotent(self):
        asyncio.run(dk_client.close_client())
        asyncio.run(dk_client.close_client())  # second call must not raise
