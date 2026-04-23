"""Tests for lookahead-free historical snapshot picker.

Validates:
  1. Given a synthetic /odds/movements stream with T-240, T-120, T-60, T-30,
     closing entries and lead_minutes=60, the picker returns the T-60 price
     (or the closest prior) — NOT closing.
  2. When no pre-commence movement exists, the fallback path fires, tags
     snapshot_quality='closing_fallback', and logs a warning.
  3. The CALLISTO_BACKTEST_LEAD_MINUTES env override is honored (including
     the lead=0 closing-mode branch kept for A/B regression).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tools.odds_api_io import (
    _extract_movement_snapshots,
    _pick_pre_commence_entry,
    _parse_iso,
    _snapshot_to_market_outcomes,
    get_historical_snapshot,
)


# ---------------------------------------------------------------------------
# Fixture: synthetic /odds/movements stream — 2026-04-22 MLB game.
# Commence = 2026-04-22T23:00:00Z.
# ---------------------------------------------------------------------------

COMMENCE = datetime(2026, 4, 22, 23, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_before: int) -> str:
    return (COMMENCE - timedelta(minutes=minutes_before)).isoformat()


def _ts_after(minutes_after: int) -> str:
    return (COMMENCE + timedelta(minutes=minutes_after)).isoformat()


# The prices at T-240, T-120, T-60, T-30 (post-gate), closing.
# They MUST differ so we can tell which one got picked.
ML_STREAM = {
    "movements": [
        {"time": _ts(240), "odds": {"home": "2.20", "away": "1.70"}},
        {"time": _ts(120), "odds": {"home": "2.10", "away": "1.75"}},
        {"time": _ts(60),  "odds": {"home": "2.00", "away": "1.80"}},
        {"time": _ts(30),  "odds": {"home": "1.85", "away": "1.95"}},   # past gate
        {"time": _ts(0),   "odds": {"home": "1.70", "away": "2.15"}},   # closing
    ],
    "home": "Yankees",
    "away": "Red Sox",
}


SPREAD_STREAM = {
    "movements": [
        {"time": _ts(240), "odds": {"hdp": -1.5, "home": "2.40", "away": "1.60"}},
        {"time": _ts(60),  "odds": {"hdp": -1.5, "home": "2.30", "away": "1.65"}},
        {"time": _ts(0),   "odds": {"hdp": -1.5, "home": "2.10", "away": "1.75"}},  # closing
    ],
}


TOTALS_STREAM_EMPTY = {"movements": []}  # triggers fallback


def test_parse_iso_accepts_z_and_offset():
    dt = _parse_iso("2026-04-22T18:00:00Z")
    assert dt == datetime(2026, 4, 22, 18, 0, 0, tzinfo=timezone.utc)
    dt2 = _parse_iso("2026-04-22T18:00:00+00:00")
    assert dt2 == dt
    assert _parse_iso("") is None
    assert _parse_iso("garbage") is None


def test_extract_snapshots_sorts_ascending_and_drops_unparseable():
    raw = {
        "movements": [
            {"time": _ts(0),   "odds": {"home": 1.7}},
            {"time": _ts(240), "odds": {"home": 2.2}},
            {"time": "nope",   "odds": {"home": 3.3}},  # dropped
        ]
    }
    out = _extract_movement_snapshots(raw)
    assert len(out) == 2
    # Ascending by time
    assert out[0]["time"] < out[1]["time"]


def test_pick_pre_commence_returns_t_minus_60_not_closing():
    """Core correctness test: lead=60 must pick T-60 (or closest prior)."""
    entries = _extract_movement_snapshots(ML_STREAM)
    pick = _pick_pre_commence_entry(entries, COMMENCE, lead_minutes=60)
    assert pick is not None, "should have a pre-commence pick"
    # T-60 exactly matches cutoff; picker returns LATEST <= cutoff -> T-60.
    assert pick["time"] == COMMENCE - timedelta(minutes=60)
    # Verify we did NOT return the T-30 or closing prices.
    assert pick["raw"]["odds"]["home"] == "2.00"


def test_pick_pre_commence_aggressive_lead_skips_t60():
    """lead=120 should return the T-120 price, not T-60 or closing."""
    entries = _extract_movement_snapshots(ML_STREAM)
    pick = _pick_pre_commence_entry(entries, COMMENCE, lead_minutes=120)
    assert pick is not None
    assert pick["time"] == COMMENCE - timedelta(minutes=120)
    assert pick["raw"]["odds"]["home"] == "2.10"


def test_pick_pre_commence_fallback_when_no_pre_gate_data():
    """If every movement is post-commence, picker returns None -> fallback."""
    stream_only_post = {
        "movements": [
            {"time": _ts_after(5),  "odds": {"home": 1.9}},
            {"time": _ts_after(30), "odds": {"home": 1.7}},
        ]
    }
    entries = _extract_movement_snapshots(stream_only_post)
    pick = _pick_pre_commence_entry(entries, COMMENCE, lead_minutes=60)
    assert pick is None


def test_snapshot_to_market_outcomes_ml():
    entry = {"odds": {"home": "2.00", "away": "1.80"}}
    out = _snapshot_to_market_outcomes(entry, "ML", "Yankees", "Red Sox")
    assert out is not None
    assert out["key"] == "h2h"
    outcomes = {o["name"]: o["price"] for o in out["outcomes"]}
    # 2.00 decimal = +100 American; 1.80 = -125.
    assert outcomes["Yankees"] == 100
    assert outcomes["Red Sox"] == -125


def test_snapshot_to_market_outcomes_spread_point_mirror():
    entry = {"odds": {"hdp": -1.5, "home": "2.30", "away": "1.65"}}
    out = _snapshot_to_market_outcomes(entry, "Spread", "Yankees", "Red Sox")
    assert out is not None
    assert out["key"] == "spreads"
    pts = {o["name"]: o["point"] for o in out["outcomes"]}
    assert pts["Yankees"] == -1.5
    assert pts["Red Sox"] == 1.5


def test_snapshot_to_market_outcomes_totals():
    entry = {"odds": {"hdp": 8.5, "over": "1.91", "under": "1.95"}}
    out = _snapshot_to_market_outcomes(entry, "Totals", "Yankees", "Red Sox")
    assert out is not None
    assert out["key"] == "totals"
    names = sorted(o["name"] for o in out["outcomes"])
    assert names == ["Over", "Under"]


# ---------------------------------------------------------------------------
# Integration: get_historical_snapshot end-to-end with mocked movements/
# historical endpoints.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_historical_snapshot_returns_pre_commence():
    """Full E2E: mocked movements yields pre_commence tag + correct price."""

    async def fake_movements(event_id, bookmaker, market, **_):
        if market == "ML":
            return ML_STREAM
        if market == "Spread":
            return SPREAD_STREAM
        return TOTALS_STREAM_EMPTY

    async def fake_historical_odds(event_id, bookmakers=""):
        # Fallback payload — includes Totals since movements was empty.
        return {
            "id": event_id,
            "home": "Yankees",
            "away": "Red Sox",
            "date": COMMENCE.isoformat(),
            "bookmakers": {
                "DraftKings": [
                    {"name": "Totals", "updatedAt": "", "odds": [
                        {"hdp": 8.5, "over": "1.91", "under": "1.95"}
                    ]},
                ],
            },
        }

    with patch("tools.odds_api_io.get_odds_movements", side_effect=fake_movements), \
         patch("tools.odds_api_io.get_historical_odds", side_effect=fake_historical_odds):
        # Restrict the bookmakers list so we issue 3 mocked calls, not 15*3.
        res = await get_historical_snapshot(
            event_id="evt-1",
            commence_time=COMMENCE.isoformat(),
            minutes_before_commence=60,
            bookmakers="DraftKings",
        )

    assert not res.get("error"), f"error in result: {res}"
    assert res["lead_minutes"] == 60
    # One book, which used pre_commence for ML + Spread (Totals fell back).
    assert len(res["bookmakers"]) == 1
    dk = res["bookmakers"][0]
    # Partial fallback (Totals fell back to closing) but ML+Spread were
    # pre_commence, so the BOOK is marked pre_commence.
    assert dk["snapshot_quality"] == "pre_commence"
    # Verify ML outcome matches T-60 price (2.00 = +100 American).
    ml_outcomes = [m for m in dk["markets"] if m["key"] == "h2h"]
    assert ml_outcomes, "h2h market missing"
    prices = {o["name"]: o["price"] for o in ml_outcomes[0]["outcomes"]}
    assert prices["Yankees"] == 100


@pytest.mark.asyncio
async def test_get_historical_snapshot_fallback_tag_and_warning(caplog):
    """When every (book, market) has no pre-commence data, the book gets
    tagged snapshot_quality='closing_fallback' AND a warning is logged."""

    async def fake_movements_empty(event_id, bookmaker, market, **_):
        return {"movements": []}

    async def fake_historical_odds(event_id, bookmakers=""):
        return {
            "id": event_id,
            "home": "Yankees",
            "away": "Red Sox",
            "date": COMMENCE.isoformat(),
            "bookmakers": {
                "DraftKings": [
                    {"name": "ML", "updatedAt": "", "odds": [
                        {"home": "1.70", "away": "2.15"}  # closing price
                    ]},
                ],
            },
        }

    with patch("tools.odds_api_io.get_odds_movements", side_effect=fake_movements_empty), \
         patch("tools.odds_api_io.get_historical_odds", side_effect=fake_historical_odds):
        import logging
        with caplog.at_level(logging.WARNING, logger="callisto.odds_api_io"):
            res = await get_historical_snapshot(
                event_id="evt-2",
                commence_time=COMMENCE.isoformat(),
                minutes_before_commence=60,
                bookmakers="DraftKings",
            )

    assert not res.get("error"), f"error: {res}"
    assert res["snapshot_quality_mix"]["closing_fallback"] == 1
    assert res["snapshot_quality_mix"]["pre_commence"] == 0
    dk = res["bookmakers"][0]
    assert dk["snapshot_quality"] == "closing_fallback"
    # Closing price should appear in the ML outcomes.
    ml = [m for m in dk["markets"] if m["key"] == "h2h"][0]
    prices = {o["name"]: o["price"] for o in ml["outcomes"]}
    # 1.70 dec = -143 American
    assert prices["Yankees"] == -143
    # And we should have logged the fallback warning.
    assert any("closing_fallback" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_lead_minutes_env_override_zero_returns_closing_mode(monkeypatch):
    """lead_minutes=0 (env override) must route to closing-mode branch and
    skip the movements fan-out entirely."""

    called_movements = {"count": 0}

    async def fake_movements(*args, **kwargs):
        called_movements["count"] += 1
        return {"movements": []}

    async def fake_historical_odds(event_id, bookmakers=""):
        return {
            "id": event_id,
            "home": "Yankees",
            "away": "Red Sox",
            "date": COMMENCE.isoformat(),
            "bookmakers": {
                "DraftKings": [
                    {"name": "ML", "updatedAt": "", "odds": [
                        {"home": "1.70", "away": "2.15"}
                    ]},
                ],
            },
        }

    monkeypatch.setenv("CALLISTO_BACKTEST_LEAD_MINUTES", "0")
    with patch("tools.odds_api_io.get_odds_movements", side_effect=fake_movements), \
         patch("tools.odds_api_io.get_historical_odds", side_effect=fake_historical_odds):
        res = await get_historical_snapshot(
            event_id="evt-3",
            commence_time=COMMENCE.isoformat(),
            minutes_before_commence=60,  # ignored in favor of env
            bookmakers="DraftKings",
        )

    assert called_movements["count"] == 0, "closing-mode must skip movements"
    assert res["lead_minutes"] == 0
    mix = res["snapshot_quality_mix"]
    assert mix["closing_mode"] == 1
    assert mix["pre_commence"] == 0
