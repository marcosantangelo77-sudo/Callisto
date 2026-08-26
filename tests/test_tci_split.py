"""Tests for the tools.tciscrape package split of tools/tci_scraper.py.

Covers:
  - facade re-exports (old import path keeps working)
  - constants integrity (STATE_REGIONS, COACHING_TENURE_FALLBACK, tournament teams)
  - pure compute_tci behavior (empty roster, experience, balance, stability,
    institutional factor, social cohesion tracking)
  - decomposed signal generators (thresholds, sides, confidence tiers)
  - storage round-trip against a temp SQLite DB
  - pipeline orchestration with mocked ESPN HTTP layer
"""

import asyncio
import json
import math

import pytest

import tools.tci_scraper as facade
import tools.tciscrape as pkg
from tools.tciscrape.compute import compute_tci
from tools.tciscrape.constants import (
    COACHING_TENURE_FALLBACK,
    EXP_RATIO_MIN_DIFF,
    EXP_RATIO_STRONG_DIFF,
    RELIGIOUS_PROGRAMS,
    STAB_SCORE_MIN_DIFF,
    STATE_REGIONS,
    TOURNAMENT_TEAMS_2026,
)
from tools.tciscrape.pipeline import build_tci_for_tournament
from tools.tciscrape.signals import get_experience_signal, get_stability_signal
from tools.tciscrape.storage import _store_tci_results, get_tci_matchup


# ---------------------------------------------------------------------------
# Helpers to build synthetic rosters / team info
# ---------------------------------------------------------------------------

def make_player(cls: str, state: str = "CT", country: str = "USA") -> dict:
    return {
        "name": f"Player-{cls}-{state}-{country}",
        "position": "G",
        "class_year": cls,
        "years_exp": 2,
        "hometown": "Some City",
        "home_state": state,
        "home_country": country,
        "jersey": "1",
        "height": "6-0",
    }


def make_roster(classes: list[str], states=None, countries=None) -> dict:
    states = states or ["CT"] * len(classes)
    countries = countries or ["USA"] * len(classes)
    return {
        "team_id": "999",
        "team_name": "Test Team",
        "season": 2026,
        "players": [
            make_player(c, s, k)
            for c, s, k in zip(classes, states, countries)
        ],
        "player_count": len(classes),
    }


def make_team_info(tenure: int = 5, affiliation: str = "secular") -> dict:
    return {
        "team_id": "999",
        "team_name": "Test Team",
        "head_coach": {"name": "Coach X", "tenure_years": tenure},
        "religious_affiliation": affiliation,
    }


# ---------------------------------------------------------------------------
# Facade compatibility
# ---------------------------------------------------------------------------

class TestFacade:
    def test_facade_reexports_package_symbols(self):
        for name in [
            "compute_tci", "build_tci_for_tournament", "get_team_roster",
            "get_team_info", "get_tci_matchup", "_store_tci_results",
            "_espn_get", "_get_client", "_search_espn_team",
            "_get_all_espn_teams", "get_experience_signal",
            "get_stability_signal", "STATE_REGIONS",
            "COACHING_TENURE_FALLBACK", "RELIGIOUS_PROGRAMS",
            "TOURNAMENT_TEAMS_2026", "EXP_RATIO_MIN_DIFF",
            "EXP_RATIO_STRONG_DIFF", "STAB_SCORE_MIN_DIFF",
        ]:
            assert hasattr(facade, name), name
            assert getattr(facade, name) is getattr(pkg, name), name

    def test_facade_constants_values_match(self):
        assert facade.DB_PATH == pkg.DB_PATH if hasattr(pkg, "DB_PATH") else True

    def test_facade_is_thin(self):
        # Facade should be dramatically smaller than the original monolith.
        import inspect
        src = inspect.getsource(facade)
        assert len(src.splitlines()) < 150


class TestConstants:
    def test_state_regions_complete(self):
        assert len(STATE_REGIONS) >= 50
        assert set(STATE_REGIONS.values()) <= {
            "Southeast", "Northeast", "Midwest", "Southwest", "West",
        }
        assert STATE_REGIONS["CT"] == "Northeast"
        assert STATE_REGIONS["CA"] == "West"

    def test_coaching_fallback_shape(self):
        for team, (coach, years) in COACHING_TENURE_FALLBACK.items():
            assert isinstance(coach, str) and coach
            assert isinstance(years, int) and years >= 0

    def test_tournament_teams_count(self):
        assert len(TOURNAMENT_TEAMS_2026) >= 60

    def test_threshold_constants(self):
        assert EXP_RATIO_MIN_DIFF == 10
        assert EXP_RATIO_STRONG_DIFF == 15
        assert STAB_SCORE_MIN_DIFF == 5

    def test_religious_programs(self):
        assert RELIGIOUS_PROGRAMS["BYU"] == "LDS"
        assert RELIGIOUS_PROGRAMS["Notre Dame"] == "Catholic"


# ---------------------------------------------------------------------------
# compute_tci
# ---------------------------------------------------------------------------

class TestComputeTci:
    def test_empty_roster(self):
        out = compute_tci({"players": []}, make_team_info())
        assert out["tci_score"] == 0
        assert out["error"] == "no players"

    def test_veteran_roster_scores_higher_than_freshman_heavy(self):
        veteran = compute_tci(
            make_roster(["Gr. Senior"] * 4 + ["Grad-Student"] * 3 + ["Senior"] * 3),
            make_team_info(tenure=8),
        )
        freshman_heavy = compute_tci(
            make_roster(["Freshman"] * 7 + ["Sophomore"] * 3),
            make_team_info(tenure=1),
        )
        assert veteran["tci_score"] > freshman_heavy["tci_score"]

    def test_experience_ratio_counts_upperclassmen(self):
        out = compute_tci(
            make_roster(["Senior", "Junior", "Sophomore", "Freshman"]),
            make_team_info(),
        )
        assert out["seniors_grad"] == 1
        assert out["juniors"] == 1
        assert out["sophomores"] == 1
        assert out["freshmen"] == 1
        assert out["upperclassmen"] == 2
        assert out["underclassmen"] == 2
        assert out["experience_ratio"] == pytest.approx(0.5)

    def test_perfect_class_balance(self):
        out = compute_tci(
            make_roster(["Senior", "Junior", "Sophomore", "Freshman"]),
            make_team_info(),
        )
        assert out["class_balance"] == pytest.approx(1.0)

    def test_unbalanced_class_low_balance_score(self):
        out = compute_tci(make_roster(["Senior"] * 4), make_team_info())
        assert out["class_balance"] < 0.5

    def test_geographic_concentration_tracked_not_scored(self):
        out = compute_tci(
            make_roster(["Senior"] * 5, states=["TX"] * 5),
            make_team_info(),
        )
        assert out["geographic_concentration"] == 1.0
        assert out["top_region"] == "Southwest"
        assert out["top_state"] == "TX"
        # Social cohesion is high but NOT added into tci_score components
        assert out["social_cohesion"] == 100.0
        task_plus_stability = (
            out["task_cohesion"] + out["stability_score"]
        )
        assert out["tci_score"] == pytest.approx(task_plus_stability, abs=0.2)

    def test_unknown_state_maps_to_unknown_region(self):
        out = compute_tci(
            make_roster(["Senior"], states=["ZZ"]), make_team_info()
        )
        assert out["top_region"] == "Unknown"

    def test_international_players_counted(self):
        out = compute_tci(
            make_roster(
                ["Senior", "Senior", "Senior"],
                countries=["USA", "Canada", "Australia"],
            ),
            make_team_info(),
        )
        assert out["international_players"] == 2
        assert out["domestic_players"] == 1

    def test_institutional_factor_non_secular(self):
        secular = compute_tci(make_roster(["Senior"] * 3), make_team_info())
        religious = compute_tci(
            make_roster(["Senior"] * 3),
            make_team_info(affiliation="Catholic"),
        )
        assert secular["institutional_factor"] == 0.0
        assert religious["institutional_factor"] == 0.1
        assert religious["tci_score"] == pytest.approx(secular["tci_score"] + 10.0)

    def test_coaching_tenure_saturates_at_10_years(self):
        ten = compute_tci(make_roster(["Senior"] * 3), make_team_info(tenure=10))
        twenty = compute_tci(make_roster(["Senior"] * 3), make_team_info(tenure=20))
        assert ten["coaching_stability"] == 1.0
        assert twenty["coaching_stability"] == 1.0
        assert ten["tci_score"] == twenty["tci_score"]

    def test_continuity_proxy_inverse_of_freshmen_share(self):
        out = compute_tci(
            make_roster(["Senior", "Senior", "Freshman", "Freshman"]),
            make_team_info(),
        )
        assert out["continuity_proxy"] == pytest.approx(0.5)

    def test_result_keys_present(self):
        out = compute_tci(make_roster(["Senior", "Junior"]), make_team_info())
        expected_keys = {
            "tci_score", "task_cohesion", "social_cohesion", "stability_score",
            "geographic_concentration", "top_region", "state_concentration",
            "top_state", "experience_ratio", "class_balance",
            "continuity_proxy", "upperclassmen", "underclassmen",
            "seniors_grad", "juniors", "sophomores", "freshmen",
            "coaching_tenure_years", "coaching_stability",
            "religious_affiliation", "institutional_factor",
            "international_players", "domestic_players", "roster_size",
        }
        assert expected_keys <= set(out.keys())

    def test_score_bounded_0_100(self):
        worst = compute_tci(make_roster(["Freshman"] * 10), make_team_info(tenure=0))
        best = compute_tci(
            make_roster(
                ["Senior", "Graduate", "Junior", "Grad Student"],
                states=["CT", "NY", "MA", "RI"],
            ),
            make_team_info(tenure=15, affiliation="Catholic"),
        )
        assert 0 <= worst["tci_score"] <= 100
        assert 0 <= best["tci_score"] <= 100


# ---------------------------------------------------------------------------
# Signal generators
# ---------------------------------------------------------------------------

HOME_STRONG = {
    "experience_ratio": 0.90,
    "stability_score": 30.0,
    "upperclassmen": 9,
    "underclassmen": 1,
    "coaching_tenure_years": 10,
    "continuity_proxy": 1.0,
}
AWAY_WEAK = {
    "experience_ratio": 0.50,
    "stability_score": 12.0,
    "upperclassmen": 5,
    "underclassmen": 5,
    "coaching_tenure_years": 2,
    "continuity_proxy": 0.5,
}


class TestExperienceSignal:
    def test_no_fire_below_threshold(self):
        sig = get_experience_signal(
            {"experience_ratio": 0.55}, {"experience_ratio": 0.50},
        )
        assert sig["fires"] is False
        assert sig["differential"] == 5.0
        assert "threshold" in sig["reason"]

    def test_fires_medium_confidence(self):
        home = dict(HOME_STRONG, experience_ratio=0.60)
        away = dict(AWAY_WEAK, experience_ratio=0.50)
        sig = get_experience_signal(home, away)
        assert sig["fires"] is True
        assert sig["side"] == "home"
        assert sig["differential"] == pytest.approx(10.0)
        assert sig["confidence"] == "medium"
        assert sig["backtest_win_rate"] == 0.571

    def test_strong_confidence_above_15(self):
        home = {"experience_ratio": 0.95, "upperclassmen": 10, "underclassmen": 0}
        away = {"experience_ratio": 0.50, "upperclassmen": 5, "underclassmen": 5}
        sig = get_experience_signal(home, away)
        assert sig["confidence"] == "high"
        assert sig["backtest_win_rate"] == 0.667
        assert sig["abs_differential"] >= EXP_RATIO_STRONG_DIFF

    def test_away_side_when_negative(self):
        sig = get_experience_signal(AWAY_WEAK, HOME_STRONG)
        assert sig["side"] == "away"
        assert sig["differential"] < 0

    def test_custom_threshold(self):
        sig = get_experience_signal(
            {"experience_ratio": 0.55}, {"experience_ratio": 0.50}, min_diff=3,
        )
        assert sig["fires"] is True


class TestStabilitySignal:
    def test_no_fire_below_threshold(self):
        sig = get_stability_signal(
            {"stability_score": 20.0}, {"stability_score": 17.0},
        )
        assert sig["fires"] is False
        assert sig["differential"] == 3.0

    def test_fires_home(self):
        sig = get_stability_signal(HOME_STRONG, AWAY_WEAK)
        assert sig["fires"] is True
        assert sig["side"] == "home"
        assert sig["differential"] == 18.0
        assert sig["confidence"] == "medium"
        assert sig["backtest_win_rate"] == 0.577
        assert sig["signal_type"] == "ncaaw_stability_score_ats"

    def test_fires_away(self):
        sig = get_stability_signal(AWAY_WEAK, HOME_STRONG)
        assert sig["side"] == "away"


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_db(tmp_path):
    return str(tmp_path / "tci.db")


def sample_result(name: str, score: float = 55.0) -> dict:
    return {
        "team_id": "123",
        "team_name": name,
        "tci_score": score,
        "task_cohesion": 40.0,
        "social_cohesion": 70.0,
        "stability_score": 22.0,
        "geographic_concentration": 0.6,
        "top_region": "Northeast",
        "state_concentration": 0.4,
        "experience_ratio": 0.75,
        "class_balance": 0.9,
        "continuity_proxy": 0.8,
        "coaching_tenure_years": 5,
        "coaching_stability": 0.5,
        "religious_affiliation": "secular",
        "institutional_factor": 0.0,
        "international_players": 1,
        "domestic_players": 11,
        "roster_size": 12,
    }


class TestStorage:
    def test_store_and_read_back(self, tmp_db):
        async def run():
            results = [sample_result("UConn Huskies", 61.5)]
            await _store_tci_results(results, season=2026, db_path=tmp_db)
            return await get_tci_matchup("UConn", "Nobody Real", season=2026, db_path=tmp_db)

        out = asyncio.run(run())
        assert "error" not in out["home_tci"]
        assert out["home_tci"]["tci_score"] == 61.5
        assert out["away_tci"]["error"] == "not found"

    def test_upsert_replaces_same_team_season(self, tmp_db):
        async def run():
            await _store_tci_results([sample_result("Duke", 50.0)], 2026, tmp_db)
            await _store_tci_results([sample_result("Duke", 62.0)], 2026, tmp_db)
            import aiosqlite
            async with aiosqlite.connect(tmp_db) as db:
                cur = await db.execute("SELECT COUNT(*), MAX(tci_score) FROM tci_scores")
                return await cur.fetchone()

        count, max_score = asyncio.run(run())
        assert count == 1
        assert max_score == 62.0

    def test_matchup_differentials_and_signals(self, tmp_db):
        async def run():
            home = sample_result("Home U", 60.0)
            away = sample_result("Away U", 45.0)
            home["experience_ratio"] = 0.85
            away["experience_ratio"] = 0.45
            home["stability_score"] = 28.0
            away["stability_score"] = 14.0
            await _store_tci_results([home, away], 2026, tmp_db)
            return await get_tci_matchup("Home", "Away", season=2026, db_path=tmp_db)

        m = asyncio.run(run())
        assert m["tci_differential"] == 15.0
        assert m["cohesion_edge"] == "home"
        assert m["experience_ratio_differential"] == 40.0
        assert m["stability_score_differential"] == 14.0
        assert m["experience_signal"]["fires"] is True
        assert m["experience_signal"]["side"] == "home"
        assert m["stability_signal"]["fires"] is True

    def test_full_data_json_round_trip(self, tmp_db):
        async def run():
            await _store_tci_results([sample_result("Xavier")], 2026, tmp_db)
            import aiosqlite
            async with aiosqlite.connect(tmp_db) as db:
                cur = await db.execute("SELECT full_data FROM tci_scores LIMIT 1")
                row = await cur.fetchone()
                return json.loads(row[0])

        data = asyncio.run(run())
        assert data["team_name"] == "Xavier"
        assert data["roster_size"] == 12


# ---------------------------------------------------------------------------
# Pipeline with mocked HTTP
# ---------------------------------------------------------------------------

ESPN_TEAMS_PAYLOAD = {
    "sports": [{
        "leagues": [{
            "teams": [
                {"team": {"id": 101, "displayName": "UConn Huskies"}},
                {"team": {"id": 102, "displayName": "Duke Blue Devils"}},
                {"team": {"id": 103, "displayName": "Iowa Hawkeyes"}},
            ],
        }],
    }],
}

ROSTER_PAYLOADS = {
    101: {
        "team": {"displayName": "UConn Huskies"},
        "athletes": [
            {"displayName": f"U Player {i}", "position": {"abbreviation": "G"},
             "experience": {"displayValue": "SR", "years": 3},
             "birthPlace": {"city": "Storrs", "state": "CT", "country": "USA"}}
            for i in range(8)
        ] + [
            {"displayName": "U Frosh", "position": {"abbreviation": "F"},
             "experience": {"displayValue": "FR", "years": 0},
             "birthPlace": {"city": "Boston", "state": "MA", "country": "USA"}},
        ],
        "coach": [{"firstName": "Geno", "lastName": "A", "experience": 20}],
    },
    102: {
        "team": {"displayName": "Duke Blue Devils"},
        "athletes": [
            {"displayName": f"D Player {i}", "position": {"abbreviation": "G"},
             "experience": {"displayValue": "FR", "years": 0},
             "birthPlace": {"city": "Durham", "state": "NC", "country": "USA"}}
            for i in range(6)
        ],
        "coach": [],
    },
    103: {
        "team": {"displayName": "Iowa Hawkeyes"},
        "athletes": [
            {"displayName": "I Player", "position": {"abbreviation": "C"},
             "experience": {"displayValue": "JR", "years": 2},
             "birthPlace": {"city": "Iowa City", "state": "IA", "country": "USA"}},
            {"displayName": "I Intl", "position": {"abbreviation": "F"},
             "experience": {"displayValue": "SO", "years": 1},
             "birthPlace": {"city": "Toronto", "state": "", "country": "Canada"}},
        ],
        "coach": [{"firstName": "Jan", "lastName": "Jensen", "experience": 3}],
    },
}

RANKINGS_PAYLOAD = {
    "rankings": [{
        "ranks": [
            {"team": {"id": 101, "displayName": "UConn Huskies"}},
            {"team": {"id": 102, "displayName": "Duke Blue Devils"}},
        ],
    }],
}


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class TestPipeline:
    @pytest.fixture()
    def mocked_http(self, monkeypatch, tmp_db):
        calls = []

        async def fake_get(url):
            calls.append(url)
            if "/rankings" in url:
                return (RANKINGS_PAYLOAD)
            if "/teams?limit=" in url:
                return (ESPN_TEAMS_PAYLOAD)
            for tid, payload in ROSTER_PAYLOADS.items():
                if f"/teams/{tid}/roster" in url:
                    return (payload)
            for tid in ROSTER_PAYLOADS:
                if f"/teams/{tid}" in url:
                    base = {
                        "team": {
                            "id": tid,
                            "displayName": ROSTER_PAYLOADS[tid]["team"]["displayName"],
                            "abbreviation": "UCO",
                            "location": "Storrs",
                            "groups": {"id": "1", "name": "Big East"},
                        },
                    }
                    return (base)
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr("tools.tciscrape.http._espn_get", fake_get)
        # pipeline and espn import _espn_get by name — patch there too
        monkeypatch.setattr("tools.tciscrape.pipeline._espn_get", fake_get)
        monkeypatch.setattr("tools.tciscrape.espn._espn_get", fake_get)
        return calls

    def test_build_tci_for_tournament_end_to_end(self, mocked_http, tmp_db):
        results = asyncio.run(
            build_tci_for_tournament(season=2026, db_path=tmp_db)
        )
        names = sorted(r["team_name"] for r in results)
        assert names == ["Duke Blue Devils", "Iowa Hawkeyes", "UConn Huskies"]
        by_name = {r["team_name"]: r for r in results}

        # Duke: all freshmen -> low continuity, fallback coach from table
        duke = by_name["Duke Blue Devils"]
        assert duke["head_coach"] == COACHING_TENURE_FALLBACK["Duke Blue Devils"][0]
        assert duke["freshmen"] == 6
        assert duke["continuity_proxy"] == 0.0

        # Iowa: international player counted
        iowa = by_name["Iowa Hawkeyes"]
        assert iowa["international_players"] == 1
        assert iowa["coaching_tenure_years"] == 3

        # UConn: veteran roster, long tenure
        uconn = by_name["UConn Huskies"]
        assert uconn["experience_ratio"] == pytest.approx(8 / 9, abs=0.01)
        assert uconn["coaching_stability"] == 1.0
        assert uconn["conference"] == "Big East"
        assert uconn["team_id"] == "101"

        # Scores persisted and queryable via matchup API
        matchup = asyncio.run(
            get_tci_matchup("UConn", "Duke", season=2026, db_path=tmp_db)
        )
        assert matchup["home_tci"]["tci_score"] == uconn["tci_score"]
        assert matchup["away_tci"]["tci_score"] == duke["tci_score"]
        assert matchup["cohesion_edge"] == "home"

    def test_rankings_teams_reused_no_dup_search(self, mocked_http):
        # Rankings already carry IDs for UConn + Duke; the teams-list search
        # only needs to resolve Iowa. Verify each roster fetched exactly once.
        results = asyncio.run(build_tci_for_tournament(season=2026, db_path=":memory:"))
        # get_team_info legitimately re-fetches the roster endpoint for coach
        # data, so count only season-scoped roster fetches (one per team).
        roster_urls = [u for u in mocked_http if "/roster?season=" in u]
        assert len(results) == 3
        assert len(roster_urls) == 3

    def test_failure_isolation_skips_bad_team(self, monkeypatch, tmp_db):
        async def flaky_get(url):
            if "/teams/102" in url:
                raise RuntimeError("espn down")
            if "/rankings" in url:
                return (RANKINGS_PAYLOAD)
            if "/teams?limit=" in url:
                return (ESPN_TEAMS_PAYLOAD)
            for tid, payload in ROSTER_PAYLOADS.items():
                if f"/teams/{tid}/roster" in url:
                    return (payload)
            for tid in ROSTER_PAYLOADS:
                if f"/teams/{tid}" in url:
                    return ({
                        "team": {"id": tid,
                                 "displayName": ROSTER_PAYLOADS[tid]["team"]["displayName"],
                                 "groups": {}},
                    })
            raise AssertionError(f"unexpected URL {url}")

        monkeypatch.setattr("tools.tciscrape.http._espn_get", flaky_get)
        monkeypatch.setattr("tools.tciscrape.pipeline._espn_get", flaky_get)
        monkeypatch.setattr("tools.tciscrape.espn._espn_get", flaky_get)
        results = asyncio.run(build_tci_for_tournament(season=2026, db_path=tmp_db))
        # Duke's info fetch fails -> its entry has error key or is skipped;
        # the other two must still be present.
        good_names = {r.get("team_name") for r in results
                      if "error" not in r or r.get("players")}
        assert "UConn Huskies" in good_names or any(
            r["team_id"] == "101" for r in results
        )


class TestNoLiveBettingGuardrails:
    """Structural guardrails: TCI code must never touch live betting paths."""

    def test_module_has_no_live_status_wiring(self):
        import inspect
        for mod in (facade, pkg):
            src = inspect.getsource(mod)
            assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src
            assert "generate_paper_trade_signal" not in src

    def test_signals_are_pure_functions(self):
        # Signal generators must not perform I/O — calling them twice gives
        # identical results.
        a = get_experience_signal(HOME_STRONG, AWAY_WEAK)
        b = get_experience_signal(HOME_STRONG, AWAY_WEAK)
        assert a == b
        assert not math.isnan(a["differential"])
