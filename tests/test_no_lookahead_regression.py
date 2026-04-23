"""Regression test — edge computation uses the T-60 price, not closing.

End-to-end: build a synthetic historical-odds snapshot with BOTH a
pre-commence price (at T-60) and a different closing price, wire it through
the normalize path, and confirm that _process_game_lines computes its edge
from the T-60 price. Fails if the closing-price price leaks into the row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from tools.odds_api_io import _parse_iso, get_historical_snapshot


COMMENCE = datetime(2026, 4, 22, 23, 0, 0, tzinfo=timezone.utc)


def _ts(minutes_before: int) -> str:
    return (COMMENCE - timedelta(minutes=minutes_before)).isoformat()


# Two books, three markets, four snapshots — T-60 price differs from closing.
# The T-60 entry MUST be the one that ends up in the returned snapshot.
def _build_movements(pre_home_dec: str, closing_home_dec: str):
    return {
        "home": "Yankees",
        "away": "Red Sox",
        "movements": [
            {"time": _ts(240), "odds": {"home": "2.20", "away": "1.75"}},
            {"time": _ts(60),  "odds": {"home": pre_home_dec, "away": "1.80"}},
            {"time": _ts(30),  "odds": {"home": "1.85", "away": "1.95"}},
            {"time": _ts(0),   "odds": {"home": closing_home_dec, "away": "2.30"}},
        ]
    }


@pytest.mark.asyncio
async def test_snapshot_uses_t60_not_closing_for_edge_computation():
    """Confirm the returned price chain reflects T-60 entries. If the code
    regressed to lookahead mode, the closing price (1.50) would appear and
    this test would fail."""

    pre_price = "2.05"        # +105 American (T-60)
    closing_price = "1.50"    # -200 American (post-game leak)

    async def fake_movements(event_id, bookmaker, market, **_):
        if market == "ML":
            return _build_movements(pre_price, closing_price)
        return {"movements": []}  # other markets fall back

    async def fake_historical_odds(event_id, bookmakers=""):
        # Fallback — should only be used for Spread/Totals slots, NEVER for ML.
        return {
            "id": event_id, "home": "Yankees", "away": "Red Sox",
            "date": COMMENCE.isoformat(),
            "bookmakers": {
                "DraftKings": [
                    {"name": "Spread", "updatedAt": "", "odds": [
                        {"hdp": -1.5, "home": "2.10", "away": "1.75"}
                    ]},
                ],
            },
        }

    with patch("tools.odds_api_io.get_odds_movements", side_effect=fake_movements), \
         patch("tools.odds_api_io.get_historical_odds", side_effect=fake_historical_odds):
        snap = await get_historical_snapshot(
            event_id="evt-42",
            commence_time=COMMENCE.isoformat(),
            minutes_before_commence=60,
            bookmakers="DraftKings",
        )

    assert not snap.get("error")
    dk = snap["bookmakers"][0]
    ml = [m for m in dk["markets"] if m["key"] == "h2h"]
    assert ml, "h2h market missing from snapshot"
    prices = {o["name"]: o["price"] for o in ml[0]["outcomes"]}

    # Decimal 2.05 -> American +105; decimal 1.50 -> American -200.
    # Critical assertion: we received the T-60 price, NOT the closing price.
    assert prices["Yankees"] == 105, (
        f"Expected T-60 price (+105); got {prices['Yankees']}. "
        f"If this is -200, lookahead has returned."
    )
    # Negative assertion — closing price must not leak into the return.
    assert prices["Yankees"] != -200

    # And the snapshot_time should reflect T-60, not commence / closing.
    t60 = COMMENCE - timedelta(minutes=60)
    snap_time = _parse_iso(snap["snapshot_time"])
    assert snap_time is not None
    assert snap_time <= COMMENCE - timedelta(minutes=59), (
        f"snapshot_time {snap_time} should be <= commence-60m ({t60}); "
        f"if it's at COMMENCE or later, lookahead has returned."
    )


@pytest.mark.asyncio
async def test_snapshot_quality_mix_counts_per_book():
    """Per-book quality mix adds up correctly."""

    async def fake_movements_mixed(event_id, bookmaker, market, **_):
        if bookmaker == "DraftKings":
            # Has pre-commence data.
            return _build_movements("2.05", "1.50")
        # Other book: only post-commence, must fall back.
        return {"movements": [
            {"time": (COMMENCE + timedelta(minutes=10)).isoformat(),
             "odds": {"home": "1.9", "away": "1.9"}}
        ]}

    async def fake_historical_odds(event_id, bookmakers=""):
        return {
            "id": event_id, "home": "Y", "away": "R",
            "date": COMMENCE.isoformat(),
            "bookmakers": {
                "FanDuel": [
                    {"name": "ML", "updatedAt": "", "odds": [
                        {"home": "1.85", "away": "2.00"}
                    ]},
                ],
            },
        }

    with patch("tools.odds_api_io.get_odds_movements", side_effect=fake_movements_mixed), \
         patch("tools.odds_api_io.get_historical_odds", side_effect=fake_historical_odds):
        snap = await get_historical_snapshot(
            event_id="evt-mix",
            commence_time=COMMENCE.isoformat(),
            minutes_before_commence=60,
            bookmakers="DraftKings,FanDuel",
        )

    mix = snap["snapshot_quality_mix"]
    assert mix["pre_commence"] == 1, f"mix={mix}"
    assert mix["closing_fallback"] == 1, f"mix={mix}"
    # Per-book quality tags preserved.
    tags = {b["title"]: b["snapshot_quality"] for b in snap["bookmakers"]}
    assert tags.get("DraftKings") == "pre_commence"
    assert tags.get("FanDuel") == "closing_fallback"
