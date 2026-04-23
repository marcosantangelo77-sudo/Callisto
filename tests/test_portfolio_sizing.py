"""Tests for portfolio Kelly sizing with per-game / per-sport caps.

feat/portfolio-kelly-live-loop (audit 2026-04-22).
"""

import os
import pytest

# Pin caps to known values so the test doesn't drift with env overrides.
os.environ.setdefault("CALLISTO_MAX_GAME_EXPOSURE_PCT", "0.08")
os.environ.setdefault("CALLISTO_MAX_SPORT_EXPOSURE_PCT", "0.15")
os.environ.setdefault("EXECUTOR_MAX_BET_PCT", "0.05")
os.environ.setdefault("EXECUTOR_KELLY_FRACTION", "0.25")
os.environ.setdefault("EXECUTOR_MIN_BET", "1.00")

from tools.bet_executor import (  # noqa: E402
    BetExecutor,
    MAX_GAME_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
)


def _make_bets_same_event(
    n: int, event_id: str = "mlb_event_42", sport: str = "baseball_mlb"
) -> list[dict]:
    """N LIVE hyps all fire on the same MLB event."""
    return [
        {
            "edge": 0.04,
            "odds": -110,
            "confidence": 0.80,
            "event_id": event_id,
            "sport": sport,
            "market_type": "h2h",
            "hypothesis_id": f"hyp_{i}",
            "description": f"hyp_{i} signal",
            "signals_n": 150,  # force full quarter-Kelly, no dampener
        }
        for i in range(n)
    ]


def _make_bets_different_events(n: int) -> list[dict]:
    """N LIVE hyps fire on N different MLB events (uncorrelated)."""
    return [
        {
            "edge": 0.04,
            "odds": -110,
            "confidence": 0.80,
            "event_id": f"mlb_event_{i}",
            "sport": "baseball_mlb",
            "market_type": "h2h",
            "hypothesis_id": f"hyp_{i}",
            "description": f"hyp_{i} signal",
            "signals_n": 150,
        }
        for i in range(n)
    ]


def test_same_event_capped_by_game_exposure():
    """4 LIVE hyps all firing one MLB game → sum-of-stakes ≤ game cap."""
    executor = BetExecutor()
    bankroll = 10_000.0
    bets = _make_bets_same_event(4)
    # Force perfect correlation: all pairs = 1.0
    ids = [b["hypothesis_id"] for b in bets]
    corr_matrix = {}
    for i, a in enumerate(ids):
        for b in ids[i + 1:]:
            corr_matrix[(a, b)] = 1.0

    sized = executor.compute_portfolio_stakes(
        bets=bets, bankroll=bankroll, correlation_matrix=corr_matrix
    )
    total = sum(row["stake"] for row in sized)
    cap_dollars = bankroll * MAX_GAME_EXPOSURE_PCT
    assert total <= cap_dollars + 0.05, (  # 5 cent rounding tolerance
        f"Same-event sum ${total:.2f} exceeds game cap ${cap_dollars:.2f}"
    )
    # All should be on the same event
    assert all(r["event_id"] == "mlb_event_42" for r in sized if r["stake"] > 0)


def test_different_events_scales_roughly_with_n():
    """4 LIVE hyps firing 4 different games → total ≫ same-game case."""
    executor = BetExecutor()
    bankroll = 10_000.0
    same = executor.compute_portfolio_stakes(
        bets=_make_bets_same_event(4), bankroll=bankroll,
        correlation_matrix={
            (f"hyp_{i}", f"hyp_{j}"): 1.0
            for i in range(4) for j in range(i + 1, 4)
        },
    )
    diff = executor.compute_portfolio_stakes(
        bets=_make_bets_different_events(4), bankroll=bankroll,
        correlation_matrix={},  # no co-firing → rho=0
    )
    same_total = sum(r["stake"] for r in same)
    diff_total = sum(r["stake"] for r in diff)

    # Hit the 15% per-sport cap (all MLB), but diff-event should still be
    # noticeably larger than same-event (which hit the 8% per-game cap).
    assert diff_total > same_total, (
        f"Different-event total ${diff_total:.2f} should exceed "
        f"same-event total ${same_total:.2f}"
    )
    # Sport cap: all 4 are MLB → diff_total ≤ sport cap
    sport_cap = bankroll * MAX_SPORT_EXPOSURE_PCT
    assert diff_total <= sport_cap + 0.05


def test_signals_n_dampener_reduces_stakes_for_fresh_hyps():
    """Hyps with signals_n<25 get half-Kelly, not full quarter-Kelly."""
    executor = BetExecutor()
    bankroll = 10_000.0
    fresh = _make_bets_different_events(2)
    for b in fresh:
        b["signals_n"] = 10  # under LOW_N threshold
    mature = _make_bets_different_events(2)
    for b in mature:
        b["signals_n"] = 200

    s_fresh = executor.compute_portfolio_stakes(
        bets=fresh, bankroll=bankroll, correlation_matrix={}
    )
    s_mature = executor.compute_portfolio_stakes(
        bets=mature, bankroll=bankroll, correlation_matrix={}
    )
    t_fresh = sum(r["stake"] for r in s_fresh)
    t_mature = sum(r["stake"] for r in s_mature)
    # Fresh should be roughly half of mature
    assert t_fresh < t_mature * 0.75, (
        f"Fresh total ${t_fresh:.2f} should be notably smaller than "
        f"mature total ${t_mature:.2f}"
    )


def test_empty_batch_returns_empty():
    executor = BetExecutor()
    assert executor.compute_portfolio_stakes([], bankroll=10_000) == []


def test_single_bet_uses_individual_path():
    """One bet → individual Kelly, not portfolio."""
    executor = BetExecutor()
    bets = _make_bets_different_events(1)
    sized = executor.compute_portfolio_stakes(bets=bets, bankroll=10_000)
    assert len(sized) == 1
    assert "individual_kelly" in sized[0]["method"]
