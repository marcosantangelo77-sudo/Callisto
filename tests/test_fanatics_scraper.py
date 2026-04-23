"""Tests for the Fanatics game-level odds scraper.

Mocks the HTTP layer (monkeypatches _rate_limited_get) so we never hit
the live Fanatics endpoints. Verifies:
  * Clean JSON payload → normalized {sport, games, ...} shape.
  * 403 / 429 → error sentinel carrying status='rate_limited'.
  * 5xx → error sentinel, empty games.
  * Moneyline / spread / total classification and odds parsing.
  * Session cookie honoured when credential is set (authed header sent).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from tools import fanatics_scraper as fs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sample_event_payload() -> dict:
    """One NBA event with moneyline, spread, total on Fanatics-shaped JSON."""
    return {
        "events": [
            {
                "id": "evt-9001",
                "name": "Miami Heat @ Boston Celtics",
                "startTime": "2026-04-22T23:10:00Z",
                "participants": [
                    {"type": "home", "name": "Boston Celtics"},
                    {"type": "away", "name": "Miami Heat"},
                ],
                "markets": [
                    {
                        "marketType": "Moneyline",
                        "name": "Moneyline",
                        "selections": [
                            {"name": "Boston Celtics", "americanPrice": -250},
                            {"name": "Miami Heat", "americanPrice": 210},
                        ],
                    },
                    {
                        "marketType": "Point Spread",
                        "name": "Spread",
                        "selections": [
                            {"name": "Boston Celtics", "line": -6.5, "americanPrice": -110},
                            {"name": "Miami Heat", "line": 6.5, "americanPrice": -110},
                        ],
                    },
                    {
                        "marketType": "Total",
                        "name": "Total Points",
                        "selections": [
                            {"name": "Over 218.5", "line": 218.5, "americanPrice": -108},
                            {"name": "Under 218.5", "line": 218.5, "americanPrice": -112},
                        ],
                    },
                ],
            }
        ]
    }


class _FakeResponse:
    def __init__(self, status_code: int, json_body=None):
        self.status_code = status_code
        self._body = json_body or {}

    def json(self):
        return self._body


def _install_fake_get(monkeypatch, fake):
    """Swap _rate_limited_get for a controlled async fake."""
    async def _fake_get(url: str):
        return fake(url)
    monkeypatch.setattr(fs, "_rate_limited_get", _fake_get)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_fetch_fanatics_odds_happy_path(monkeypatch):
    payload = _sample_event_payload()

    def fake(url):
        assert "league=nba" in url
        return _FakeResponse(200, payload)

    _install_fake_get(monkeypatch, fake)

    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["source"] == "fanatics_scraper"
    assert result["game_count"] == 1
    game = result["games"][0]
    assert game["home_team"] == "Boston Celtics"
    assert game["away_team"] == "Miami Heat"
    bms = game["bookmakers"]
    assert len(bms) == 1
    assert bms[0]["key"] == "fanatics"
    mkts = {m["key"]: m for m in bms[0]["markets"]}
    assert {"h2h", "spreads", "totals"}.issubset(mkts.keys())

    h2h_outcomes = mkts["h2h"]["outcomes"]
    names = {o["name"]: o["price"] for o in h2h_outcomes}
    assert names["Boston Celtics"] == -250
    assert names["Miami Heat"] == 210

    spread_outcomes = mkts["spreads"]["outcomes"]
    bos = next(o for o in spread_outcomes if o["name"] == "Boston Celtics")
    assert bos["point"] == -6.5
    assert bos["price"] == -110

    totals_outcomes = mkts["totals"]["outcomes"]
    over = next(o for o in totals_outcomes if o["name"] == "Over")
    under = next(o for o in totals_outcomes if o["name"] == "Under")
    assert over["point"] == 218.5
    assert under["price"] == -112


def test_unsupported_sport_returns_error(monkeypatch):
    # No HTTP should be made for golf — but set up a fake just in case
    _install_fake_get(monkeypatch, lambda url: _FakeResponse(200, {"events": []}))
    result = asyncio.run(fs.fetch_fanatics_odds("golf_pga"))
    assert result.get("error")
    assert result["games"] == []


# ---------------------------------------------------------------------------
# Rate limit / forbidden / server errors
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [403, 429])
def test_rate_limited_response(monkeypatch, status):
    _install_fake_get(monkeypatch, lambda url: _FakeResponse(status, {}))
    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["games"] == []
    assert result.get("status") == "rate_limited"
    assert "rate limit" in result["error"].lower()


def test_5xx_cascades_and_returns_error(monkeypatch):
    # First endpoint 500, second also 500 — final result is an error sentinel.
    _install_fake_get(monkeypatch, lambda url: _FakeResponse(503, {}))
    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["games"] == []
    assert "error" in result


def test_json_decode_failure(monkeypatch):
    class _BrokenResponse(_FakeResponse):
        def json(self):
            raise ValueError("bad json")

    _install_fake_get(monkeypatch, lambda url: _BrokenResponse(200, {}))
    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["games"] == []
    assert "error" in result


def test_first_endpoint_fails_second_succeeds(monkeypatch):
    calls = {"n": 0}
    payload = _sample_event_payload()

    def fake(url):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeResponse(404, {})
        return _FakeResponse(200, payload)

    _install_fake_get(monkeypatch, fake)
    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["game_count"] == 1
    assert calls["n"] >= 2


# ---------------------------------------------------------------------------
# Odds parsing primitives
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("-110", -110),
        ("+150", 150),
        ("−120", -120),   # Unicode minus
        ("–105", -105),   # En-dash minus
        (-110, -110),
        (2.0, 100),            # Decimal 2.0 -> even money
        (1.5, -200),
        (None, None),
        ("", None),
        ("not-a-number", None),
    ],
)
def test_parse_american_odds_primitives(raw, expected):
    assert fs._parse_american_odds(raw) == expected


@pytest.mark.parametrize(
    "market,expected",
    [
        ({"marketType": "Moneyline"}, "h2h"),
        ({"name": "Game Winner"}, "h2h"),
        ({"marketType": "Point Spread"}, "spreads"),
        ({"name": "Run Line"}, "spreads"),
        ({"marketType": "Total"}, "totals"),
        ({"name": "Over/Under"}, "totals"),
        ({"name": "Something Obscure"}, None),
    ],
)
def test_classify_market(market, expected):
    assert fs._classify_market(market) == expected


# ---------------------------------------------------------------------------
# Session-cookie plumbing
# ---------------------------------------------------------------------------

def test_auth_cookie_absent_by_default(monkeypatch):
    # Ensure no env leaks in.
    import os
    monkeypatch.delenv("CALLISTO_FANATICS_SESSION_COOKIE", raising=False)
    assert fs._auth_cookie_header() is None


def test_auth_cookie_used_when_set(monkeypatch):
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "sess=zzz")
    hdr = fs._auth_cookie_header()
    assert hdr == "sess=zzz"


def test_auth_cookie_bare_token_wrapped(monkeypatch):
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "justatoken")
    hdr = fs._auth_cookie_header()
    assert hdr.startswith("fanatics_session=")


def test_cookie_sent_on_authed_requests(monkeypatch):
    """When the session cookie is set, the scraper attaches a Cookie
    header to its outbound request."""
    monkeypatch.setenv("CALLISTO_FANATICS_SESSION_COOKIE", "longcookievalue_authed")

    captured = {}

    class FakeClient:
        is_closed = False

        async def get(self, url, headers=None):
            captured["url"] = url
            captured["headers"] = dict(headers or {})
            return _FakeResponse(200, _sample_event_payload())

        async def aclose(self):
            pass

    monkeypatch.setattr(fs, "_client", FakeClient())
    # Exercise the real _rate_limited_get (which builds and attaches Cookie).
    result = asyncio.run(fs.fetch_fanatics_odds("basketball_nba"))
    assert result["game_count"] == 1
    assert "Cookie" in captured["headers"]
    assert "longcookievalue_authed" in captured["headers"]["Cookie"]


# ---------------------------------------------------------------------------
# Event extraction tolerance
# ---------------------------------------------------------------------------

def test_extract_events_various_shapes():
    assert fs._extract_events([{"id": 1}]) == [{"id": 1}]
    assert fs._extract_events({"events": [{"id": 2}]}) == [{"id": 2}]
    assert fs._extract_events({"items": [{"id": 3}]}) == [{"id": 3}]
    assert fs._extract_events({"markets": []}) == [{"markets": []}]  # Single-event
    assert fs._extract_events({"nothing_here": 1}) == []
    assert fs._extract_events(None) == []


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
