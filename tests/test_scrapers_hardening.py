"""Hardening tests for sportsbook scrapers.

Simulates 503, 429 with Retry-After, malformed JSON, empty events, and
connection timeouts for each scraper. Verifies:
  - retries on 5xx succeed,
  - 429 honours Retry-After without crashing,
  - malformed/empty responses return a structured payload rather than raise,
  - timeouts do not leak an exception to the caller,
  - the shared scraper_utils health registry records success/error.

All tests monkey-patch the low-level HTTP layer so we never hit the wire.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Optional

import httpx
import pytest

from tools import (
    action_network_scraper as an,
    betmgm_scraper as mgm,
    dk_scraper as dk,
    fanatics_scraper as fan,
    fanduel_scraper as fd,
    scraper_utils as su,
)
from tools.scraper_utils import (
    FatalStatusError,
    RetryableStatusError,
    all_health,
    classify_status,
    compute_backoff,
    health,
    pick_user_agent,
    retry_async,
    retry_sync,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeHeaders(dict):
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class FakeResponse:
    """Duck-typed stand-in for httpx.Response used by scrapers."""

    def __init__(self, status_code: int = 200, json_body: Any = None,
                 text: str = "", headers: Optional[dict] = None,
                 raise_json: bool = False):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text
        self.headers = _FakeHeaders(headers or {})
        self._raise_json = raise_json

    def json(self) -> Any:
        if self._raise_json:
            raise ValueError("malformed JSON")
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=self  # type: ignore[arg-type]
            )


def _fast_backoff(monkeypatch):
    """Patch scraper_utils.compute_backoff to return ~0 so tests run fast."""
    monkeypatch.setattr(su, "compute_backoff", lambda *a, **k: 0.001)


# ---------------------------------------------------------------------------
# scraper_utils tests
# ---------------------------------------------------------------------------

def test_classify_status_2xx_ok():
    assert classify_status(200) is None
    assert classify_status(301) is None


def test_classify_status_429_with_retry_after():
    with pytest.raises(RetryableStatusError) as excinfo:
        classify_status(429, retry_after_header="2")
    assert excinfo.value.status_code == 429
    assert excinfo.value.retry_after == pytest.approx(2.0, rel=0.01)


def test_classify_status_5xx_retry():
    with pytest.raises(RetryableStatusError) as excinfo:
        classify_status(503)
    assert excinfo.value.status_code == 503


def test_classify_status_4xx_fatal():
    with pytest.raises(FatalStatusError) as excinfo:
        classify_status(404)
    assert excinfo.value.status_code == 404


def test_pick_user_agent_is_a_browser_string():
    ua = pick_user_agent()
    assert isinstance(ua, str) and len(ua) > 20
    assert "Mozilla" in ua


def test_compute_backoff_grows_and_is_capped():
    a1 = compute_backoff(1, base=1.0, cap=10.0)
    a5 = compute_backoff(5, base=1.0, cap=10.0)
    assert 0.0 < a1 < a5 <= 10.0


def test_retry_async_retries_on_5xx_then_succeeds(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableStatusError(503)
        return "ok"

    out = asyncio.run(retry_async(op, scraper="t", max_attempts=4))
    assert out == "ok"
    assert calls["n"] == 3


def test_retry_async_honours_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RetryableStatusError(429, retry_after=2.5)
        return "ok"

    out = asyncio.run(retry_async(op, scraper="t", max_attempts=3, max_delay=10.0))
    assert out == "ok"
    # First sleep is the retry-after value (clamped between base and max_delay)
    assert slept and slept[0] >= 0.5 and slept[0] <= 10.0
    assert slept[0] == pytest.approx(2.5, rel=0.01)


def test_retry_async_fatal_4xx_does_not_retry(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise FatalStatusError(404)

    with pytest.raises(FatalStatusError):
        asyncio.run(retry_async(op, scraper="t", max_attempts=5))
    assert calls["n"] == 1


def test_retry_async_retries_on_timeout(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectTimeout("boom")
        return "ok"

    out = asyncio.run(retry_async(op, scraper="t", max_attempts=3))
    assert out == "ok"
    assert calls["n"] == 2


def test_retry_sync_retries_on_5xx(monkeypatch):
    monkeypatch.setattr(su, "compute_backoff", lambda *a, **k: 0.0)
    monkeypatch.setattr(su.time, "sleep", lambda *_: None)
    calls = {"n": 0}

    def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RetryableStatusError(502)
        return "ok"

    assert retry_sync(op, scraper="t", max_attempts=4) == "ok"
    assert calls["n"] == 3


def test_health_registry_records_success_and_error():
    name = "unit_test_tracker"
    su.mark_success(name)
    snap = health(name)
    assert snap["healthy"] is True
    assert snap["success_count"] >= 1
    assert snap["last_successful_pull"] is not None

    su.mark_error(name, "simulated")
    snap2 = health(name)
    assert snap2["last_error"] == "simulated"
    assert snap2["consecutive_errors"] >= 1


def test_all_health_lists_registered():
    su.register_scraper("alpha_test")
    su.mark_success("alpha_test")
    report = all_health()
    names = [s["name"] for s in report["scrapers"]]
    assert "alpha_test" in names


# ---------------------------------------------------------------------------
# FanDuel scraper tests
# ---------------------------------------------------------------------------

def _fd_ok_payload() -> dict:
    return {
        "attachments": {
            "events": {
                "1": {"eventId": 1, "name": "Miami Heat @ Boston Celtics", "openDate": "2026-04-22T23:10:00Z"},
            },
            "markets": {
                "m1": {
                    "eventId": 1,
                    "marketType": "MONEY_LINE",
                    "runners": [
                        {"runnerName": "Boston Celtics", "winRunnerOdds": {"americanDisplayOdds": {"americanOdds": -250}}},
                        {"runnerName": "Miami Heat", "winRunnerOdds": {"americanDisplayOdds": {"americanOdds": 210}}},
                    ],
                },
            },
        }
    }


def test_fd_503_then_success(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse(503, {})
            return FakeResponse(200, _fd_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert out["source"] == "fanduel_scraper"
    assert out["game_count"] == 1
    assert calls["n"] >= 2
    snap = health("fanduel_scraper")
    assert snap["last_successful_pull"] is not None


def test_fd_429_with_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(429, {}, headers={"Retry-After": "1"})
            return FakeResponse(200, _fd_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert out["game_count"] == 1
    assert any(s == pytest.approx(1.0, rel=0.01) for s in slept)


def test_fd_malformed_json_returns_error(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            return FakeResponse(200, raise_json=True)
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert "error" in out
    assert out["games"] == []


def test_fd_empty_events_returns_zero_games(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            return FakeResponse(200, {"attachments": {"events": {}, "markets": {}}})
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert out["game_count"] == 0
    assert out["games"] == []


def test_fd_connection_timeout_returns_error(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            raise httpx.ConnectTimeout("timeout")
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert "error" in out
    assert out["games"] == []


def test_fd_persistent_500_returns_error(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            return FakeResponse(500, {})
        async def aclose(self): pass

    monkeypatch.setattr(fd, "_get_client", lambda: _C())
    monkeypatch.setattr(fd, "_last_request_time", 0.0)

    out = asyncio.run(fd.scrape_fd_odds("basketball_nba"))
    assert "error" in out
    assert out["games"] == []


# ---------------------------------------------------------------------------
# BetMGM scraper tests
# ---------------------------------------------------------------------------

def _mgm_ok_payload() -> dict:
    return {
        "fixtures": [
            {
                "id": 42,
                "startDate": "2026-04-23T00:00:00Z",
                "participants": [
                    {"name": "Lakers", "properties": {"type": "home"}},
                    {"name": "Clippers", "properties": {"type": "away"}},
                ],
                "games": [
                    {
                        "name": "Moneyline",
                        "results": [
                            {"name": "Lakers", "americanOdds": -110},
                            {"name": "Clippers", "americanOdds": -110},
                        ],
                    },
                ],
            }
        ]
    }


def test_mgm_503_then_success(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse(503, {})
            return FakeResponse(200, _mgm_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(mgm, "_get_client", lambda: _C())
    monkeypatch.setattr(mgm, "_last_request_time", 0.0)

    out = asyncio.run(mgm.scrape_betmgm_odds("basketball_nba"))
    assert out["source"] == "betmgm_scraper"
    assert out["game_count"] == 1


def test_mgm_429_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(429, {}, headers={"Retry-After": "1"})
            return FakeResponse(200, _mgm_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(mgm, "_get_client", lambda: _C())
    monkeypatch.setattr(mgm, "_last_request_time", 0.0)

    out = asyncio.run(mgm.scrape_betmgm_odds("basketball_nba"))
    assert out["game_count"] == 1


def test_mgm_malformed_json(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            return FakeResponse(200, raise_json=True)
        async def aclose(self): pass

    monkeypatch.setattr(mgm, "_get_client", lambda: _C())
    monkeypatch.setattr(mgm, "_last_request_time", 0.0)

    out = asyncio.run(mgm.scrape_betmgm_odds("basketball_nba"))
    assert "error" in out
    assert out["games"] == []


def test_mgm_empty_fixtures(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            return FakeResponse(200, {"fixtures": []})
        async def aclose(self): pass

    monkeypatch.setattr(mgm, "_get_client", lambda: _C())
    monkeypatch.setattr(mgm, "_last_request_time", 0.0)

    out = asyncio.run(mgm.scrape_betmgm_odds("basketball_nba"))
    assert out["game_count"] == 0
    assert out["games"] == []


def test_mgm_timeout_returns_error(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, params=None):
            raise httpx.ConnectTimeout("timeout")
        async def aclose(self): pass

    monkeypatch.setattr(mgm, "_get_client", lambda: _C())
    monkeypatch.setattr(mgm, "_last_request_time", 0.0)

    out = asyncio.run(mgm.scrape_betmgm_odds("basketball_nba"))
    assert "error" in out


# ---------------------------------------------------------------------------
# Action Network scraper tests (httpx path -- curl_cffi may not be installed)
# ---------------------------------------------------------------------------

def _an_ok_payload() -> dict:
    return {
        "games": [
            {
                "id": 777,
                "start_time": "2026-04-22T23:10:00",
                "teams": [
                    {"full_name": "Boston Celtics", "display_name": "Celtics", "is_home": True},
                    {"full_name": "Miami Heat", "display_name": "Heat", "is_away": True},
                ],
                "odds": [
                    {"book_id": 15, "ml_home": -250, "ml_away": 210},
                ],
            }
        ]
    }


def test_an_503_then_success(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(an, "_HAS_CURL_CFFI", False)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse(503, {})
            return FakeResponse(200, _an_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(an, "_get_client", lambda: _C())
    monkeypatch.setattr(an, "_last_request_time", 0.0)

    out = asyncio.run(an.scrape_action_network("basketball_nba"))
    assert out["source"] == "action_network"
    assert out["game_count"] == 1


def test_an_429_retry_after(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(an, "_HAS_CURL_CFFI", False)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] == 1:
                return FakeResponse(429, {}, headers={"Retry-After": "1"})
            return FakeResponse(200, _an_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(an, "_get_client", lambda: _C())
    monkeypatch.setattr(an, "_last_request_time", 0.0)

    out = asyncio.run(an.scrape_action_network("basketball_nba"))
    assert out["game_count"] == 1


def test_an_empty_games(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(an, "_HAS_CURL_CFFI", False)

    class _C:
        is_closed = False
        async def get(self, url):
            return FakeResponse(200, {"games": []})
        async def aclose(self): pass

    monkeypatch.setattr(an, "_get_client", lambda: _C())
    monkeypatch.setattr(an, "_last_request_time", 0.0)

    out = asyncio.run(an.scrape_action_network("basketball_nba"))
    assert out["game_count"] == 0
    assert out["games"] == []


def test_an_timeout(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(an, "_HAS_CURL_CFFI", False)

    class _C:
        is_closed = False
        async def get(self, url):
            raise httpx.ConnectTimeout("timeout")
        async def aclose(self): pass

    monkeypatch.setattr(an, "_get_client", lambda: _C())
    monkeypatch.setattr(an, "_last_request_time", 0.0)

    out = asyncio.run(an.scrape_action_network("basketball_nba"))
    assert "error" in out
    assert out["game_count"] == 0


# ---------------------------------------------------------------------------
# DraftKings scraper tests (httpx legacy path -- curl_cffi may not be installed)
# ---------------------------------------------------------------------------

def _dk_ok_payload() -> dict:
    return {
        "eventGroup": {
            "events": [
                {"eventId": 100, "name": "Miami Heat @ Boston Celtics", "startDate": "2026-04-22T23:10:00Z"},
            ],
            "offerCategories": [
                {
                    "name": "Game Lines",
                    "offerSubcategoryDescriptors": [
                        {
                            "name": "Moneyline",
                            "offerSubcategory": {
                                "offers": [
                                    [
                                        {
                                            "eventId": 100,
                                            "label": "Moneyline",
                                            "outcomes": [
                                                {"label": "Boston Celtics", "oddsAmerican": "-250"},
                                                {"label": "Miami Heat", "oddsAmerican": "+210"},
                                            ],
                                        }
                                    ]
                                ]
                            },
                        }
                    ],
                }
            ],
        }
    }


def test_dk_legacy_503_then_success(monkeypatch):
    _fast_backoff(monkeypatch)
    # Force legacy path
    monkeypatch.setattr(dk, "_HAS_CURL_CFFI", False)

    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse(503, {})
            return FakeResponse(200, _dk_ok_payload())
        async def aclose(self): pass

    monkeypatch.setattr(dk, "_get_client", lambda: _C())
    monkeypatch.setattr(dk, "_last_request_time", 0.0)

    out = asyncio.run(dk.scrape_dk_odds("basketball_nba"))
    assert out.get("source") == "dk_scraper"
    assert out.get("game_count", 0) >= 1


def test_dk_empty_eventgroup(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(dk, "_HAS_CURL_CFFI", False)

    class _C:
        is_closed = False
        async def get(self, url):
            return FakeResponse(200, {"eventGroup": {"events": [], "offerCategories": []}})
        async def aclose(self): pass

    monkeypatch.setattr(dk, "_get_client", lambda: _C())
    monkeypatch.setattr(dk, "_last_request_time", 0.0)

    out = asyncio.run(dk.scrape_dk_odds("basketball_nba"))
    assert out.get("game_count", 0) == 0
    assert out.get("games", []) == []


def test_dk_legacy_404_no_retry(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(dk, "_HAS_CURL_CFFI", False)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url):
            calls["n"] += 1
            return FakeResponse(404, {})
        async def aclose(self): pass

    monkeypatch.setattr(dk, "_get_client", lambda: _C())
    monkeypatch.setattr(dk, "_last_request_time", 0.0)

    out = asyncio.run(dk.scrape_dk_odds("basketball_nba"))
    assert "error" in out
    assert calls["n"] == 1  # no retry on fatal 4xx


def test_dk_connection_timeout(monkeypatch):
    _fast_backoff(monkeypatch)
    monkeypatch.setattr(dk, "_HAS_CURL_CFFI", False)

    class _C:
        is_closed = False
        async def get(self, url):
            raise httpx.ConnectTimeout("timeout")
        async def aclose(self): pass

    monkeypatch.setattr(dk, "_get_client", lambda: _C())
    monkeypatch.setattr(dk, "_last_request_time", 0.0)

    out = asyncio.run(dk.scrape_dk_odds("basketball_nba"))
    assert "error" in out


# ---------------------------------------------------------------------------
# Fanatics scraper tests (keeps its 403/429 => rate_limited sentinel contract)
# ---------------------------------------------------------------------------

def test_fanatics_503_then_success(monkeypatch):
    _fast_backoff(monkeypatch)
    calls = {"n": 0}

    class _C:
        is_closed = False
        async def get(self, url, headers=None):
            calls["n"] += 1
            if calls["n"] < 2:
                return FakeResponse(503, {})
            return FakeResponse(200, {"events": []})
        async def aclose(self): pass

    monkeypatch.setattr(fan, "_get_client", lambda: _C())
    monkeypatch.setattr(fan, "_last_request_time", 0.0)

    out = asyncio.run(fan._fetch_and_parse("basketball_nba"))
    assert out["source"] == "fanatics_scraper"
    assert out.get("game_count", 0) == 0


def test_fanatics_429_returns_rate_limited(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, headers=None):
            return FakeResponse(429, {}, headers={"Retry-After": "1"})
        async def aclose(self): pass

    monkeypatch.setattr(fan, "_get_client", lambda: _C())
    monkeypatch.setattr(fan, "_last_request_time", 0.0)

    out = asyncio.run(fan._fetch_and_parse("basketball_nba"))
    assert out.get("status") == "rate_limited"
    assert out["games"] == []


def test_fanatics_timeout(monkeypatch):
    _fast_backoff(monkeypatch)

    class _C:
        is_closed = False
        async def get(self, url, headers=None):
            raise httpx.ConnectTimeout("timeout")
        async def aclose(self): pass

    monkeypatch.setattr(fan, "_get_client", lambda: _C())
    monkeypatch.setattr(fan, "_last_request_time", 0.0)

    out = asyncio.run(fan._fetch_and_parse("basketball_nba"))
    assert "error" in out
    assert out["games"] == []
