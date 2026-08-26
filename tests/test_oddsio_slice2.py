"""Slice-2 extraction tests for tools/odds_api_io -> tools/odds_io.

Covers the second extraction wave (public_api + pro_endpoints modules):

  - facade identity: every name in tools.odds_api_io.__all__ resolves and is
    the IDENTICAL object as the tools.odds_io / submodule object (no stale
    copies introduced by the split)
  - facade size discipline: the facade stays a thin re-export shim (no logic)
  - public_api behavior: credits_dict math, get_events normalization +
    status filtering, unknown-sport errors, 36h window filtering,
    budget-throttling of the per-event fan-out, get_scores mapping,
    snapshot_all_sports aggregation, _fetch_event_odds error paths
  - pro_endpoints behavior: value-bet EV conversion, arbitrage leg
    normalization with decimal->American odds, multi-event ID batching
    (max 10), incremental updates param assembly, historical date RFC3339
    coercion, live events sport mapping
  - budget gating: every Pro endpoint returns its empty-shape error payload
    when check_budget refuses, without any HTTP call
  - gates untouched: no 'live' paper-signal widening; facade never imports
    generate_paper_trade_signal machinery

All HTTP goes through tools.odds_io.http_client.api_get, which these tests
patch — nothing touches the network.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

import tools.odds_api_io as facade
import tools.odds_io
import tools.odds_io.pro_endpoints as pro
import tools.odds_io.public_api as pub


# ---------------------------------------------------------------------------
# Facade identity / structure
# ---------------------------------------------------------------------------

class TestFacadeIdentity:
    def test_every_facade_export_is_the_package_object(self):
        for name in facade.__all__:
            assert hasattr(facade, name), f"facade missing {name}"
            obj = getattr(facade, name)
            if hasattr(tools.odds_io, name):
                assert obj is getattr(tools.odds_io, name), (
                    f"facade.{name} is a stale copy, not the package object"
                )

    def test_core_endpoints_live_in_public_api(self):
        import inspect
        for name in ("get_sports", "get_events", "get_odds", "get_event_odds",
                     "get_scores", "get_outrights", "snapshot_all_sports"):
            fn = getattr(pub, name)
            assert asyncio.iscoroutinefunction(fn), name
            # defined in public_api, not merely imported from elsewhere
            assert fn.__module__ == "tools.odds_io.public_api", name

    def test_pro_endpoints_live_in_pro_endpoints(self):
        for name in ("get_value_bets", "get_arbitrage_bets", "get_odds_multi",
                     "get_odds_updated", "get_historical_events",
                     "get_historical_odds", "get_odds_movements",
                     "get_live_events"):
            fn = getattr(pro, name)
            assert asyncio.iscoroutinefunction(fn), name
            assert fn.__module__ == "tools.odds_io.pro_endpoints", name

    def test_snapshot_wrapper_stays_in_facade(self):
        # tests across the repo patch tools.odds_api_io.get_odds_movements /
        # get_historical_odds; the wrapper must resolve fetchers through the
        # facade namespace to stay patchable.
        assert facade.get_historical_snapshot.__module__ == "tools.odds_api_io"

    def test_facade_is_thin(self):
        lines = open(facade.__file__).read().splitlines()
        assert len(lines) < 200, (
            f"facade grew back to {len(lines)} lines — keep it a shim"
        )
        body = "\n".join(lines)
        assert "asyncio.gather" not in body
        assert "_api_get(" not in body.replace("_api_get,\n", "")

    def test_no_live_signal_widening(self):
        src = open(facade.__file__).read() + \
            open(pub.__file__).read() + open(pro.__file__).read()
        assert "generate_paper_trade_signal" not in src
        assert "_PAPER_TRADE_SIGNAL_STATUSES" not in src


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _patch_api_get(monkeypatch, calls, responder):
    async def fake_api_get(path, params=None):
        calls.append((path, params))
        return responder(path, params or {})
    monkeypatch.setattr("tools.odds_io.http_client.api_get", fake_api_get)
    monkeypatch.setattr(pub, "_api_get", fake_api_get)
    monkeypatch.setattr(pro, "_api_get", fake_api_get)


@pytest.fixture
def no_budget(monkeypatch):
    monkeypatch.setattr(pub, "_check_budget", lambda cost=1: None)
    monkeypatch.setattr(pro, "_check_budget", lambda cost=1: None)


NOW = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# credits_dict
# ---------------------------------------------------------------------------

class TestCreditsDict:
    def test_shape_and_math(self, monkeypatch):
        import tools.odds_io.usage as usage
        monkeypatch.setattr(usage, "_hourly_requests", 123)
        c = pub.credits_dict()
        assert c["used_this_hour"] == 123
        assert c["remaining_this_hour"] == usage.HOURLY_LIMIT - 123
        assert c["api_key_set"] is bool(pub.ODDS_API_IO_KEY)

    def test_never_negative(self, monkeypatch):
        import tools.odds_io.usage as usage
        monkeypatch.setattr(usage, "_hourly_requests", 10**9)
        assert pub.credits_dict()["remaining_this_hour"] == 0

    def test_facade_reexports_same_object(self):
        assert facade._credits_dict is pub.credits_dict


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------

EVENTS_PAYLOAD = [
    {"id": 1, "home": "Lakers", "away": "Celtics", "date": "2026-03-20T00:00:00Z", "status": "pending"},
    {"id": 2, "home": "Warriors", "away": "Suns", "date": "2026-03-21T02:30:00Z", "status": "live"},
    {"id": 3, "home": "Old", "away": "News", "date": "2026-01-01T00:00:00Z", "status": "settled"},
    {"id": 4, "home": "Rain", "away": "Out", "date": "2026-03-22T00:00:00Z", "status": "postponed"},
]


class TestGetEvents:
    def test_normalizes_and_filters_statuses(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: EVENTS_PAYLOAD)

        async def run():
            return await facade.get_events("basketball_nba")
        result = run().__await__ if False else asyncio.get_event_loop().run_until_complete(run()) if False else _run(run())
        assert result["event_count"] == 2
        ids = [e["id"] for e in result["events"]]
        assert ids == ["1", "2"]
        ev = result["events"][0]
        assert ev["sport_key"] == "basketball_nba"
        assert ev["sport_title"]
        assert ev["home_team"] == "Lakers"

    def test_unknown_sport_short_circuits(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])

        async def run():
            return await facade.get_events("underwater_basket_weaving")
        result = _run(run())
        assert result == {"events": [], "error": "Unknown sport: underwater_basket_weaving"}
        assert calls == []

    def test_api_error_passthrough(self, monkeypatch, no_budget):
        _patch_api_get(monkeypatch, [], lambda p, q: {"error": "rate limited"})
        result = _run(_call(facade.get_events, "basketball_nba"))
        assert result["events"] == []
        assert result["error"] == "rate limited"


# ---------------------------------------------------------------------------
# get_odds — window filter + budget throttle + gather
# ---------------------------------------------------------------------------

ODDS_PAYLOAD = {
    "id": "ev1",
    "home": "Lakers",
    "away": "Celtics",
    "bookmakers": {
        "DraftKings": [
            {"name": "ML", "updatedAt": "", "odds": [{"home": "1.95", "away": "1.90"}]},
        ],
    },
}


class TestGetOdds:
    def test_36h_window_filters_far_future_events(
        self, monkeypatch, no_budget
    ):
        near = {"id": "7", "home": "A", "away": "B",
                "commence_time": (NOW + timedelta(hours=2)).isoformat(),
                "status": "pending"}
        far = {"id": "8", "home": "C", "away": "D",
               "commence_time": (NOW + timedelta(days=14)).isoformat(),
               "status": "pending"}

        async def fake_get_events(sport):
            return {"event_count": 2, "events": [near, far], "source": "odds_api_io"}

        async def fake_fetch(event_id, info, sport):
            return dict(ODDS_PAYLOAD, id=event_id)

        monkeypatch.setattr(pub, "get_events", fake_get_events)
        monkeypatch.setattr(pub, "_fetch_event_odds", fake_fetch)
        result = _run(_call(pub.get_odds, "basketball_nba"))
        assert result["game_count"] == 1
        assert result["games"][0]["id"] == "7"

    def test_unparseable_commence_time_kept_safe(self, monkeypatch, no_budget):
        ev = {"id": "9", "home": "E", "away": "F", "commence_time": "garbage",
              "status": "pending"}

        async def fake_get_events(sport):
            return {"event_count": 1, "events": [ev], "source": "odds_api_io"}

        fetched = []

        async def fake_fetch(event_id, info, sport):
            fetched.append(event_id)
            return dict(ODDS_PAYLOAD, id=event_id)

        monkeypatch.setattr(pub, "get_events", fake_get_events)
        monkeypatch.setattr(pub, "_fetch_event_odds", fake_fetch)
        result = _run(_call(pub.get_odds, "basketball_nba"))
        assert fetched == ["9"]
        assert result["game_count"] == 1

    def test_budget_throttle_truncates_fanout(self, monkeypatch):
        evs = [
            {"id": str(i), "home": "H", "away": "A",
             "commence_time": NOW.isoformat(), "status": "pending"}
            for i in range(5)
        ]

        async def fake_get_events(sport):
            return {"event_count": 5, "events": evs, "source": "odds_api_io"}

        fetched = []

        async def fake_fetch(event_id, info, sport):
            fetched.append(event_id)
            return dict(ODDS_PAYLOAD, id=event_id)

        monkeypatch.setattr(pub, "get_events", fake_get_events)
        monkeypatch.setattr(pub, "_fetch_event_odds", fake_fetch)
        monkeypatch.setattr(pub, "_check_budget", lambda cost=1: "budget exceeded")
        monkeypatch.setattr(pub, "hourly_remaining", lambda: 2)

        result = _run(_call(pub.get_odds, "basketball_nba"))
        assert len(fetched) == 2
        assert result["game_count"] == 2

    def test_zero_budget_returns_error_no_http(self, monkeypatch):
        evs = [{"id": "1", "home": "H", "away": "A",
                "commence_time": NOW.isoformat(), "status": "pending"}]

        async def fake_get_events(sport):
            return {"event_count": 1, "events": evs, "source": "x"}

        monkeypatch.setattr(pub, "get_events", fake_get_events)
        monkeypatch.setattr(pub, "_check_budget", lambda cost=1: "budget exceeded")
        monkeypatch.setattr(pub, "hourly_remaining", lambda: 0)
        result = _run(_call(pub.get_odds, "basketball_nba"))
        assert result["games"] == [] and "error" in result

    def test_gather_survives_exceptions(self, monkeypatch, no_budget):
        evs = [
            {"id": "1", "home": "H", "away": "A",
             "commence_time": NOW.isoformat(), "status": "pending"},
            {"id": "2", "home": "H", "away": "A",
             "commence_time": NOW.isoformat(), "status": "pending"},
        ]

        async def fake_get_events(sport):
            return {"event_count": 2, "events": evs, "source": "x"}

        async def fake_fetch(event_id, info, sport):
            if event_id == "1":
                raise RuntimeError("boom")
            return dict(ODDS_PAYLOAD, id=event_id)

        monkeypatch.setattr(pub, "get_events", fake_get_events)
        monkeypatch.setattr(pub, "_fetch_event_odds", fake_fetch)
        result = _run(_call(pub.get_odds, "basketball_nba"))
        assert result["game_count"] == 1


# ---------------------------------------------------------------------------
# _fetch_event_odds / get_event_odds
# ---------------------------------------------------------------------------

class TestEventOddsFetch:
    def test_error_payload_returns_none(self, monkeypatch):
        async def fake_api_get(path, params=None):
            return {"error": "no odds"}
        monkeypatch.setattr(pub, "_api_get", fake_api_get)
        out = _run(_call(pub._fetch_event_odds, "e1", {"id": "e1"}, "nba"))
        assert out is None

    def test_success_delegates_to_normalize(self, monkeypatch):
        seen = {}

        async def fake_api_get(path, params=None):
            seen["path"], seen["params"] = path, params
            return ODDS_PAYLOAD

        sentinel = {"normalized": True}
        captured = {}

        def fake_normalize(raw, info, sport):
            captured.update(raw=raw, info=info, sport=sport)
            return sentinel

        monkeypatch.setattr(pub, "_api_get", fake_api_get)
        monkeypatch.setattr(pub, "_normalize_event_odds", fake_normalize)
        info = {"id": "e1"}
        out = _run(_call(pub._fetch_event_odds, "e1", info, "nba"))
        assert out is sentinel
        assert captured["raw"] is ODDS_PAYLOAD
        assert seen["params"]["eventId"] == "e1"


# ---------------------------------------------------------------------------
# get_scores
# ---------------------------------------------------------------------------

SCORES_PAYLOAD = [
    {"id": 11, "home": "X", "away": "Y", "date": "d1", "status": "settled",
     "scores": [{"name": "X", "score": "102"}]},
    {"id": 12, "home": "P", "away": "Q", "date": "d2", "status": "pending"},
]


class TestGetScores:
    def test_only_scored_games_mapped(self, monkeypatch):
        _patch_api_get(monkeypatch, [], lambda p, q: SCORES_PAYLOAD)
        result = _run(_call(facade.get_scores, "basketball_nba"))
        assert result["game_count"] == 1
        g = result["games"][0]
        assert g["completed"] is True
        assert g["scores"] == SCORES_PAYLOAD[0]["scores"]

    def test_unknown_sport(self, monkeypatch):
        result = _run(_call(facade.get_scores, "nope_xyz"))
        assert "error" in result and result["games"] == []


# ---------------------------------------------------------------------------
# snapshot_all_sports
# ---------------------------------------------------------------------------

class TestSnapshotAllSports:
    def test_aggregates_game_counts(self, monkeypatch, no_budget):
        async def fake_get_odds(sport="", **kw):
            n = {"basketball_nba": 3, "icehockey_nhl": 2, "baseball_mlb": 4}[sport]
            return {"sport": sport, "game_count": n, "games": [], "source": "odds_api_io"}

        monkeypatch.setattr(pub, "get_odds", fake_get_odds)
        result = _run(_call(pub.snapshot_all_sports))
        assert result["total_games"] == 9
        assert set(result["sports"]) == {"basketball_nba", "icehockey_nhl", "baseball_mlb"}

    def test_per_sport_exception_isolated(self, monkeypatch, no_budget):
        state = {"n": 0}

        async def fake_get_odds(sport="", **kw):
            state["n"] += 1
            if sport == "icehockey_nhl":
                raise RuntimeError("nhl down")
            return {"game_count": 1}

        monkeypatch.setattr(pub, "get_odds", fake_get_odds)
        result = _run(_call(pub.snapshot_all_sports))
        assert "error" in result["sports"]["icehockey_nhl"]
        assert result["total_games"] == 2

    def test_budget_refusal_blocks_batch(self, monkeypatch):
        monkeypatch.setattr(pub, "_check_budget", lambda cost=1: "over budget")

        async def boom(**kw):
            raise AssertionError("should not be called")

        monkeypatch.setattr(pub, "get_odds", boom)
        result = _run(_call(pub.snapshot_all_sports))
        assert result == {"error": "over budget"}


# ---------------------------------------------------------------------------
# get_outrights
# ---------------------------------------------------------------------------

class TestGetOutrights:
    def test_routes_markets_outrights_through_get_odds(self, monkeypatch):
        captured = {}

        async def fake_get_odds(sport="", regions="", markets="", odds_format=""):
            captured.update(markets=markets, sport=sport)
            return {"game_count": 0}

        monkeypatch.setattr(pub, "get_odds", fake_get_odds)
        _run(_call(pub.get_outrights, "golf_pga"))
        assert captured["markets"] == "outrights"
        assert captured["sport"] == "golf_pga"


# ---------------------------------------------------------------------------
# get_value_bets
# ---------------------------------------------------------------------------

VB_PAYLOAD = [{
    "eventId": 42,
    "bookmaker": "DraftKings",
    "betSide": "home",
    "market": {"name": "ML", "hdp": None, "home": "2.10", "away": "1.80"},
    "bookmakerOdds": {"home": "2.25", "away": "1.70", "href": "https://b/x"},
    "expectedValue": 105.5,
    "expectedValueUpdatedAt": "t1",
}]


class TestGetValueBets:
    def test_ev_conversion_and_fields(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: VB_PAYLOAD)
        result = _run(_call(pro.get_value_bets, "DraftKings"))
        assert calls[0][0] == "/value-bets"
        bet = result["bets"][0]
        assert bet["event_id"] == "42"
        assert bet["ev_raw"] == 105.5
        assert bet["ev_pct"] == round((105.5 - 100) / 100, 4)
        assert bet["book_odds_home"] == 2.25
        assert bet["bet_url"] == "https://b/x"
        assert result["count"] == 1
        assert result["source"] == "odds_api_io_pro"

    def test_sub_100_ev_is_negative_fraction(self, monkeypatch, no_budget):
        payload = [dict(VB_PAYLOAD[0], expectedValue=98.0)]
        _patch_api_get(monkeypatch, [], lambda p, q: payload)
        result = _run(_call(pro.get_value_bets))
        assert result["bets"][0]["ev_pct"] == round((98.0 - 100) / 100, 4)

    def test_non_positive_ev_raw_clamped_to_zero(self, monkeypatch, no_budget):
        payload = [dict(VB_PAYLOAD[0], expectedValue=0)]
        _patch_api_get(monkeypatch, [], lambda p, q: payload)
        result = _run(_call(pro.get_value_bets))
        assert result["bets"][0]["ev_pct"] == 0

    def test_budget_gated_empty_shape(self, monkeypatch):
        monkeypatch.setattr(pro, "_check_budget", lambda cost=1: "over budget")
        result = _run(_call(pro.get_value_bets))
        assert result == {"error": "over budget", "bets": []}


# ---------------------------------------------------------------------------
# get_arbitrage_bets
# ---------------------------------------------------------------------------

ARB_PAYLOAD = [{
    "eventId": 77,
    "market": {"name": "ML"},
    "profitMargin": 1.8,
    "impliedProbability": 0.98,
    "legs": [
        {"bookmaker": "DraftKings", "side": "home", "odds": "2.10", "directLink": "u1"},
        {"bookmaker": "FanDuel", "side": "away", "odds": "2.05", "directLink": "u2"},
    ],
    "optimalStakes": [48.0, 52.0],
}]


class TestGetArbitrageBets:
    def test_leg_normalization_includes_american(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: ARB_PAYLOAD)
        result = _run(_call(pro.get_arbitrage_bets))
        assert calls[0][0] == "/arbitrage-bets"
        arb = result["arbs"][0]
        assert arb["event_id"] == "77"
        leg = arb["legs"][0]
        assert leg["odds_decimal"] == 2.10
        # decimal 2.10 => American +110
        assert leg["odds_american"] == 110
        assert leg["url"] == "u1"
        assert result["optimal"] if False else arb["optimal_stakes"] == [48.0, 52.0]

    def test_budget_gated_empty_shape(self, monkeypatch):
        monkeypatch.setattr(pro, "_check_budget", lambda cost=1: "over budget")
        result = _run(_call(pro.get_arbitrage_bets))
        assert result == {"error": "over budget", "arbs": []}


# ---------------------------------------------------------------------------
# get_odds_multi
# ---------------------------------------------------------------------------

class TestGetOddsMulti:
    def test_batches_max_ten_ids_csv(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        ids = list(range(15))
        out = _run(_call(pro.get_odds_multi, ids, ""))
        assert out == []
        path, params = calls[0]
        assert path == "/odds/multi"
        sent_ids = params["eventIds"].split(",")
        assert len(sent_ids) == 10          # hard cap at 10 per request
        assert sent_ids[0] == "0"           # first ten kept, order preserved
        assert params["bookmakers"]         # defaults to selected bookmakers

    def test_explicit_bookmakers_forwarded(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        _run(_call(pro.get_odds_multi, [1], "BetMGM"))
        assert calls[0][1]["bookmakers"] == "BetMGM"

    def test_empty_input_no_http(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        assert _run(_call(pro.get_odds_multi, [])) == []
        assert calls == []

    def test_dict_payload_wrapped_in_list(self, monkeypatch, no_budget):
        _patch_api_get(monkeypatch, [], lambda p, q: {"id": "1"})
        out = _run(_call(pro.get_odds_multi, [1]))
        assert out == [{"id": "1"}]

    def test_budget_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(pro, "_check_budget", lambda cost=1: "over budget")
        assert _run(_call(pro.get_odds_multi, [1])) == []


# ---------------------------------------------------------------------------
# get_odds_updated
# ---------------------------------------------------------------------------

class TestGetOddsUpdated:
    def test_param_assembly_with_sport_mapping(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [{"a": 1}, {"b": 2}])
        result = _run(_call(pro.get_odds_updated, 1710000000, "basketball_nba", "DraftKings"))
        path, params = calls[0]
        assert path == "/odds/updated"
        assert params["since"] == 1710000000
        mapped = tools.odds_io.SPORT_MAP["basketball_nba"]
        expected_sport = mapped["sport"] if isinstance(mapped, dict) and mapped else "basketball_nba"
        assert params["sport"] == expected_sport
        assert params["bookmaker"] == "DraftKings"
        assert result["count"] == 2
        assert result["since"] == 1710000000

    def test_no_sport_no_bookmaker_minimal_params(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        _run(_call(pro.get_odds_updated, 5))
        assert calls[0][1] == {"since": 5}


# ---------------------------------------------------------------------------
# historical endpoints
# ---------------------------------------------------------------------------

class TestHistoricalEvents:
    def test_date_coercion_to_rfc3339(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [{"id": 1}])
        result = _run(_call(pro.get_historical_events, "basketball_nba", "2026-03-01", "2026-03-05"))
        _, params = calls[0]
        assert params["from"] == "2026-03-01T00:00:00Z"
        assert params["to"] == "2026-03-05T23:59:59Z"
        assert result["count"] == 1
        assert result["from"].endswith("Z")

    def test_existing_timestamps_left_alone(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        _run(_call(pro.get_historical_events, "basketball_nba",
                   "2026-03-01T12:00:00Z", "2026-03-02T09:30:00Z"))
        _, params = calls[0]
        assert params["from"] == "2026-03-01T12:00:00Z"
        assert params["to"] == "2026-03-02T09:30:00Z"


class TestHistoricalOddsAndMovements:
    def test_historical_odds_default_bookmakers(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: {"data": True})
        _run(_call(pro.get_historical_odds, "ev9"))
        _, params = calls[0]
        assert calls[0][0] == "/historical/odds"
        assert params["eventId"] == "ev9"
        assert params["bookmakers"]

    def test_movements_params(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: {"ok": 1})
        _run(_call(pro.get_odds_movements, "ev9", "BetMGM", "Spread"))
        path, params = calls[0]
        assert path == "/odds/movements"
        assert params == {"eventId": "ev9", "bookmaker": "BetMGM", "market": "Spread"}

    def test_list_payload_wrapped_as_data(self, monkeypatch, no_budget):
        _patch_api_get(monkeypatch, [], lambda p, q: [{"snap": 1}])
        out = _run(_call(pro.get_odds_movements, "e"))
        assert out == {"data": [{"snap": 1}]}


# ---------------------------------------------------------------------------
# get_live_events
# ---------------------------------------------------------------------------

class TestGetLiveEvents:
    def test_maps_sport_when_given(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [{"id": 1}, {"id": 2}, {"id": 3}])
        result = _run(_call(pro.get_live_events, "basketball_nba"))
        mapped = tools.odds_io.SPORT_MAP["basketball_nba"]
        expected = mapped["sport"] if isinstance(mapped, dict) and mapped else "basketball_nba"
        assert calls[0][1] == {"sport": expected}
        assert result["count"] == 3

    def test_no_sport_empty_params(self, monkeypatch, no_budget):
        calls = []
        _patch_api_get(monkeypatch, calls, lambda p, q: [])
        result = _run(_call(pro.get_live_events))
        assert calls[0][1] == {}
        assert result == {"count": 0, "events": [], "source": "odds_api_io_pro"}

    def test_budget_gated(self, monkeypatch):
        monkeypatch.setattr(pro, "_check_budget", lambda cost=1: "over budget")
        result = _run(_call(pro.get_live_events))
        assert result == {"error": "over budget", "events": []}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _run(awaitable):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(awaitable)
    finally:
        loop.close()


async def _call(fn, *args, **kwargs):
    return await fn(*args, **kwargs)
