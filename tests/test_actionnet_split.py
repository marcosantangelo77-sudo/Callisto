"""Tests for the tools/actionnet split of action_network_scraper.

Covers:
- package structure and facade back-compat
- team name resolution (general, sport-specific overrides, unknown)
- URL building (valid/invalid sports, date handling)
- game parsing (teams, moneyline, spreads, totals, book filtering,
  fallback ordering, missing data)
- public betting extraction and cross-book averaging
- scrape_action_network / get_public_betting orchestration with a
  mocked rate_limited_get (no network access)
"""

import asyncio

import pytest

import tools.action_network_scraper as facade
from tools.actionnet import constants as an_constants
from tools.actionnet import http as an_http
from tools.actionnet import parser as an_parser
from tools.actionnet import scraper as an_scraper
from tools.actionnet import team_names as an_team_names


# ---------------------------------------------------------------------------
# Package structure / facade back-compat
# ---------------------------------------------------------------------------

class TestFacadeBackCompat:
    def test_facade_exports_public_functions(self):
        assert facade.scrape_action_network is an_scraper.scrape_action_network
        assert facade.get_public_betting is an_scraper.get_public_betting
        assert facade.close_client is an_http.close_client

    def test_facade_exports_private_aliases(self):
        assert facade._build_url is an_parser.build_url
        assert facade._parse_game is an_parser.parse_game
        assert facade._extract_public_betting is an_parser.extract_public_betting
        assert facade._resolve_team_name is an_team_names._resolve_team_name
        assert facade._RATE_LIMIT_SECONDS == 2.0
        assert facade._SPORT_TITLES == {
            "basketball_nba": "NBA",
            "americanfootball_nfl": "NFL",
            "icehockey_nhl": "NHL",
            "basketball_ncaab": "NCAAB",
            "baseball_mlb": "MLB",
        }

    def test_mappings_shared_between_facade_and_package(self):
        assert facade.BOOK_ID_MAP is an_constants.BOOK_ID_MAP
        assert facade.LEAGUE_MAP is an_constants.LEAGUE_MAP
        assert facade.TEAM_NAME_MAP is an_team_names.TEAM_NAME_MAP

    def test_book_id_map_contents(self):
        assert len(an_constants.BOOK_ID_MAP) == 9
        assert an_constants.BOOK_ID_MAP[15] == ("draftkings", "DraftKings")
        assert an_constants.BOOK_ID_MAP[972] == ("espnbet", "ESPNBet")

    def test_league_map_contents(self):
        assert an_constants.LEAGUE_MAP["basketball_nba"] == "nba"
        assert an_constants.LEAGUE_MAP["baseball_mlb"] == "mlb"
        assert len(an_constants.LEAGUE_MAP) == 6


# ---------------------------------------------------------------------------
# Team name resolution
# ---------------------------------------------------------------------------

class TestTeamNameResolution:
    def test_general_mapping(self):
        assert an_team_names._resolve_team_name("Celtics", "basketball_nba") == "Boston Celtics"

    def test_sport_specific_override_wins(self):
        # Panthers: Carolina in NFL, Florida in NHL
        assert (
            an_team_names._resolve_team_name("Panthers", "americanfootball_nfl")
            == "Carolina Panthers"
        )
        assert (
            an_team_names._resolve_team_name("Panthers", "icehockey_nhl")
            == "Florida Panthers"
        )

    def test_kings_ambiguity(self):
        assert (
            an_team_names._resolve_team_name("Kings", "basketball_nba") == "Sacramento Kings"
        )
        assert an_team_names._resolve_team_name("Kings", "icehockey_nhl") == "Los Angeles Kings"

    def test_jets_ambiguity(self):
        assert (
            an_team_names._resolve_team_name("Jets", "americanfootball_nfl") == "New York Jets"
        )
        assert an_team_names._resolve_team_name("Jets", "icehockey_nhl") == "Winnipeg Jets"

    def test_unknown_name_returned_as_is(self):
        assert an_team_names._resolve_team_name("Mystery Team", "basketball_nba") == "Mystery Team"

    def test_alias_entries(self):
        assert an_team_names.TEAM_NAME_MAP["Blazers"] == "Portland Trail Blazers"
        assert an_team_names.TEAM_NAME_MAP["D-backs"] == "Arizona Diamondbacks"
        assert an_team_names.TEAM_NAME_MAP["A's"] == "Oakland Athletics"


# ---------------------------------------------------------------------------
# URL building
# ---------------------------------------------------------------------------

class TestBuildUrl:
    def test_basic_url(self):
        url = an_parser.build_url("basketball_nba", "20260115")
        assert url.startswith(an_constants._API_BASE + "/nba?")
        assert "period=game" in url
        assert f"bookIds={an_constants._BOOK_IDS}" in url
        assert "date=20260115" in url

    def test_unsupported_sport_raises(self):
        with pytest.raises(ValueError, match="Unsupported sport"):
            an_parser.build_url("soccer_epl", "20260115")

    def test_default_date_is_today_utc(self):
        from datetime import datetime, timezone

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        url = an_parser.build_url("basketball_nba")
        assert f"date={today}" in url


# ---------------------------------------------------------------------------
# Game parsing
# ---------------------------------------------------------------------------

def _make_game(**overrides):
    """Build a minimal valid Action Network game payload."""
    game = {
        "id": 12345,
        "start_time": "2026-01-15T19:05:00",
        "teams": [
            {"full_name": "", "display_name": "Hornets", "is_home": True},
            {"full_name": "", "display_name": "Celtics", "is_away": True},
        ],
        "odds": [
            {
                "book_id": 15,
                "ml_home": -120,
                "ml_away": +100,
                "spread_home": 1.5,
                "spread_home_line": -110,
                "spread_away": -1.5,
                "spread_away_line": -110,
                "total": 220.5,
                "over": -105,
                "under": -115,
            }
        ],
    }
    game.update(overrides)
    return game


class TestParseGame:
    def test_full_parse_all_markets(self):
        parsed = an_parser.parse_game(_make_game(), "basketball_nba")
        assert parsed is not None
        assert parsed["id"] == "action_12345"
        assert parsed["sport_key"] == "basketball_nba"
        assert parsed["sport_title"] == "NBA"
        assert parsed["home_team"] == "Charlotte Hornets"
        assert parsed["away_team"] == "Boston Celtics"
        assert parsed["commence_time"] == "2026-01-15T19:05:00Z"

        keys = [m["key"] for m in parsed["bookmakers"][0]["markets"]]
        assert keys == ["h2h", "spreads", "totals"]

        h2h = parsed["bookmakers"][0]["markets"][0]["outcomes"]
        assert h2h[0] == {"name": "Charlotte Hornets", "price": -120}
        assert h2h[1] == {"name": "Boston Celtics", "price": 100}

    def test_no_teams_returns_none(self):
        assert an_parser.parse_game({"teams": [], "odds": []}, "basketball_nba") is None

    def test_no_odds_returns_none(self):
        assert an_parser.parse_game(_make_game(odds=[]), "basketball_nba") is None

    def test_unknown_book_skipped(self):
        game = _make_game()
        game["odds"][0]["book_id"] = 99999
        assert an_parser.parse_game(game, "basketball_nba") is None

    def test_fallback_positional_teams(self):
        game = _make_game(
            teams=[
                {"full_name": "Miami Heat"},
                {"full_name": "Denver Nuggets"},
            ]
        )
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert parsed["home_team"] == "Miami Heat"
        assert parsed["away_team"] == "Denver Nuggets"

    def test_short_names_resolved_when_no_space(self):
        game = _make_game(
            teams=[
                {"display_name": "Kings", "is_home": True},
                {"display_name": "Lakers", "is_away": True},
            ]
        )
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert parsed["home_team"] == "Sacramento Kings"
        assert parsed["away_team"] == "Los Angeles Lakers"

    def test_start_time_behavior_matches_original(self):
        # Original logic: only explicit "+" offsets are left untouched;
        # naive timestamps (including negative offsets) get a Z suffix.
        game = _make_game(start_time="2026-01-15T14:05:00+00:00")
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert parsed["commence_time"].endswith("+00:00")

        game2 = _make_game(start_time="2026-01-15T14:05:00")
        parsed2 = an_parser.parse_game(game2, "basketball_nba")
        assert parsed2["commence_time"] == "2026-01-15T14:05:00Z"

    def test_missing_start_time_defaults_to_now_iso(self):
        game = _make_game(start_time="")
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert parsed["commence_time"]  # non-empty ISO string

    def test_missing_start_time_and_id_synthesizes_game_id(self):
        game = _make_game(start_time="", id="")
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert parsed["id"] == "action_Charlotte Hornets_Boston Celtics"

    def test_partial_odds_only_h2h(self):
        game = _make_game(
            odds=[{"book_id": 30, "ml_home": -150, "ml_away": 130}]
        )
        parsed = an_parser.parse_game(game, "basketball_nba")
        keys = [m["key"] for m in parsed["bookmakers"][0]["markets"]]
        assert keys == ["h2h"]

    def test_non_numeric_odds_skipped(self):
        game = _make_game(odds=[{"book_id": 15, "ml_home": "abc", "ml_away": 100}])
        parsed = an_parser.parse_game(game, "basketball_nba")
        # h2h market fails int() -> no markets -> bookmaker skipped -> None
        assert parsed is None

    def test_multiple_books_parsed(self):
        game = _make_game(
            odds=[
                {"book_id": 15, "ml_home": -120, "ml_away": 100},
                {"book_id": 30, "ml_home": -118, "ml_away": 102},
                {"book_id": 76, "total": 221, "over": -110, "under": -110},
            ]
        )
        parsed = an_parser.parse_game(game, "basketball_nba")
        assert [b["key"] for b in parsed["bookmakers"]] == ["draftkings", "fanduel", "bet365"]

    def test_totals_market_structure(self):
        game = _make_game(odds=[{"book_id": 71, "total": 6.5, "over": -110, "under": -110}])
        parsed = an_parser.parse_game(game, "icehockey_nhl")
        totals = parsed["bookmakers"][0]["markets"][0]
        assert totals["key"] == "totals"
        assert totals["outcomes"] == [
            {"name": "Over", "price": -110, "point": 6.5},
            {"name": "Under", "price": -110, "point": 6.5},
        ]


# ---------------------------------------------------------------------------
# Public betting extraction
# ---------------------------------------------------------------------------

def _make_public_game():
    return {
        "id": 777,
        "start_time": "2026-01-15T00:10:00Z",
        "teams": [
            {"full_name": "Los Angeles Lakers", "is_home": True},
            {"full_name": "Boston Celtics", "is_away": True},
        ],
        "odds": [
            {
                "book_id": 15,
                "ml_home_public": 61.0,
                "ml_away_public": 39.0,
                "spread_home_public": 55.0,
                "spread_away_public": 45.0,
            },
            {
                "book_id": 30,
                "ml_home_public": 59.0,
                "ml_away_public": 41.0,
                "total_over_public": 70.0,
                "total_under_public": 30.0,
            },
        ],
    }


class TestExtractPublicBetting:
    def test_extracts_and_averages(self):
        result = an_parser.extract_public_betting(_make_public_game(), "basketball_nba")
        assert result is not None
        assert result["game_id"] == 777
        assert result["home_team"] == "Los Angeles Lakers"
        assert result["away_team"] == "Boston Celtics"
        assert len(result["by_book"]) == 2
        avg = result["averages"]
        assert avg["ml_home_pct"] == 60.0
        assert avg["ml_away_pct"] == 40.0

    def test_spread_average_from_single_book(self):
        result = an_parser.extract_public_betting(_make_public_game(), "basketball_nba")
        assert result["averages"]["spread_home_pct"] == 55.0
        assert result["averages"]["spread_away_pct"] == 45.0
        assert result["averages"]["total_over_pct"] == 70.0

    def test_no_public_data_returns_none(self):
        game = _make_public_game()
        game["odds"] = [{"book_id": 15, "ml_home": -110}]
        assert an_parser.extract_public_betting(game, "basketball_nba") is None

    def test_unknown_book_gets_string_key(self):
        game = _make_public_game()
        game["odds"] = [
            {
                "book_id": 4242,
                "ml_home_public": 50.0,
                "ml_away_public": 50.0,
            }
        ]
        result = an_parser.extract_public_betting(game, "basketball_nba")
        assert result["by_book"][0]["book_key"] == "4242"


# ---------------------------------------------------------------------------
# Orchestration (mocked HTTP — no network access)
# ---------------------------------------------------------------------------

class TestScrapeActionNetwork:
    def test_unsupported_sport_error_payload(self):
        result = asyncio.run(an_scraper.scrape_action_network("soccer_epl"))
        assert result["error"].startswith("Unsupported sport")
        assert result["games"] == []
        assert result["source"] == "action_network"

    def test_success_payload(self, monkeypatch):
        async def fake_get(url):
            return {"games": [_make_game()]}

        monkeypatch.setattr(an_scraper, "rate_limited_get", fake_get)
        result = asyncio.run(an_scraper.scrape_action_network("basketball_nba", "20260115"))
        assert result["source"] == "action_network"
        assert result["game_count"] == 1
        assert result["games"][0]["id"] == "action_12345"
        assert result["credits"]["api_key_set"] is True

    def test_request_failure_returns_error_payload(self, monkeypatch):
        async def boom(url):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(an_scraper, "rate_limited_get", boom)
        result = asyncio.run(an_scraper.scrape_action_network("basketball_nba"))
        assert result["error"] == "connection refused"
        assert result["game_count"] == 0

    def test_filters_unparseable_games(self, monkeypatch):
        async def fake_get(url):
            return {"games": [_make_game(), {"id": 2, "teams": []}]}

        monkeypatch.setattr(an_scraper, "rate_limited_get", fake_get)
        result = asyncio.run(an_scraper.scrape_action_network("basketball_nba"))
        assert result["game_count"] == 1


class TestGetPublicBetting:
    def test_unsupported_sport_error_payload(self):
        result = asyncio.run(an_scraper.get_public_betting("soccer_epl"))
        assert result["error"].startswith("Unsupported sport")
        assert result["games"] == []

    def test_success_payload(self, monkeypatch):
        async def fake_get(url):
            return {"games": [_make_public_game()]}

        monkeypatch.setattr(an_scraper, "rate_limited_get", fake_get)
        result = asyncio.run(an_scraper.get_public_betting("basketball_nba"))
        assert result["source"] == "action_network_public_betting"
        assert result["game_count"] == 1
        assert result["games"][0]["averages"]["ml_home_pct"] == 60.0

    def test_request_failure_returns_error_payload(self, monkeypatch):
        async def boom(url):
            raise ValueError("bad json")

        monkeypatch.setattr(an_scraper, "rate_limited_get", boom)
        result = asyncio.run(an_scraper.get_public_betting("basketball_nba"))
        assert result["error"] == "bad json"
        assert result["games"] == []


# ---------------------------------------------------------------------------
# HTTP module behavior (no network)
# ---------------------------------------------------------------------------

class TestHttpModule:
    def test_rate_limit_constant(self):
        assert an_http.RATE_LIMIT_SECONDS == 2.0

    def test_close_client_resets_state(self):
        async def run():
            await an_http.close_client()
            assert an_http._client is None
            assert an_http._cffi_session is None

        asyncio.run(run())

    def test_curl_cffi_flag_is_bool(self):
        assert isinstance(an_http._HAS_CURL_CFFI, bool)

    def test_rate_limited_get_sleeps_between_calls(self, monkeypatch):
        sleeps = []

        async def fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(an_http.asyncio, "sleep", fake_sleep)
        # Pretend curl_cffi is unavailable so the httpx path runs.
        monkeypatch.setattr(an_http, "_HAS_CURL_CFFI", False)

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return {"ok": True}

        class FakeClient:
            async def get(self, url):
                return FakeResp()

        monkeypatch.setattr(
            an_http, "_get_client", lambda: FakeClient()
        )

        async def run():
            an_http._last_request_time = 0.0
            first = await an_http.rate_limited_get("http://example.test/a")
            second = await an_http.rate_limited_get("http://example.test/b")
            return first, second

        first, second = asyncio.run(run())
        assert first == {"ok": True}
        assert second == {"ok": True}
        # Second call should have been rate-limited by a sleep.
        assert any(s > 0 for s in sleeps)
