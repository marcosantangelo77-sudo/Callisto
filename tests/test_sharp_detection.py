"""Unit tests for tools.quant.sharp_detection.

Synthetic time-series construction lets us drive known microstructure
events (one steam event, one first-mover follower, one RLM) through the
detectors and assert on the exact output.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tools.quant.sharp_detection import (
    LineTick,
    SharpSignal,
    detect_first_mover,
    detect_steam_event,
    detect_reverse_line_movement,
    scan_market_movement,
)


def _tick(book: str, seconds_offset: float, prob: float, limit: float = None) -> LineTick:
    """Shorthand for tests. base_ts is module-global so multiple calls
    within the same test produce comparable timestamps."""
    base = datetime(2026, 4, 18, 20, 0, 0, tzinfo=timezone.utc)
    return LineTick(
        book=book,
        market_key="E1|h2h|Yankees",
        implied_prob=prob,
        ts=base + timedelta(seconds=seconds_offset),
        limit=limit,
    )


# ──────────────────────────────────────────────────────────────────────
# First-mover
# ──────────────────────────────────────────────────────────────────────


def test_first_mover_detects_single_follower():
    # Pinnacle moves first at t=0, DraftKings follows at t=30 in the same
    # direction. Expect one first_mover signal citing pinnacle first.
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("pinnacle", 30, 0.515),          # +0.015
        _tick("draftkings", 30, 0.500),
        _tick("draftkings", 60, 0.518),        # +0.018, same direction
    ]
    sigs = detect_first_mover(ticks, min_move=0.005)
    assert len(sigs) == 1
    assert sigs[0].kind == "first_mover"
    assert sigs[0].first_book == "pinnacle"
    assert "draftkings" in sigs[0].participating_books
    assert sigs[0].direction == +1


def test_first_mover_ignores_opposite_direction_moves():
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 30, 0.500),
        _tick("draftkings", 60, 0.485),        # moves the OTHER way
    ]
    sigs = detect_first_mover(ticks, min_move=0.005)
    assert sigs == []


def test_first_mover_requires_movement_beyond_min_move():
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("pinnacle", 30, 0.502),          # only +0.002
        _tick("draftkings", 30, 0.500),
        _tick("draftkings", 60, 0.502),        # only +0.002
    ]
    sigs = detect_first_mover(ticks, min_move=0.005)
    assert sigs == []


def test_first_mover_respects_window():
    # DraftKings move lands 1000s after Pinnacle — outside the default
    # 300s window — no signal.
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 30, 0.500),
        _tick("draftkings", 1030, 0.518),
    ]
    sigs = detect_first_mover(ticks, min_move=0.005, lookback_window_s=300)
    assert sigs == []


# ──────────────────────────────────────────────────────────────────────
# Steam detection
# ──────────────────────────────────────────────────────────────────────


def test_steam_detects_three_book_cluster():
    # Three books move within 30s in the same direction.
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("draftkings", 0, 0.500),
        _tick("fanduel", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 35, 0.520),
        _tick("fanduel", 45, 0.518),
    ]
    sigs = detect_steam_event(ticks, min_move=0.005, window_s=60, min_books=3)
    assert len(sigs) == 1
    assert sigs[0].kind == "steam"
    assert len(sigs[0].participating_books) == 3
    assert sigs[0].direction == +1


def test_steam_rejects_false_positive_with_two_books():
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("draftkings", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 40, 0.518),
    ]
    # min_books=3 — only 2 books moved.
    sigs = detect_steam_event(ticks, min_move=0.005, window_s=60, min_books=3)
    assert sigs == []


def test_steam_does_not_cross_window():
    # Three books but spread across 180s with window_s=60 — should NOT
    # fire as a single steam event.
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("draftkings", 0, 0.500),
        _tick("fanduel", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 120, 0.515),
        _tick("fanduel", 210, 0.515),
    ]
    sigs = detect_steam_event(ticks, min_move=0.005, window_s=60, min_books=3)
    assert sigs == []


def test_steam_ignores_mixed_direction_cluster():
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("draftkings", 0, 0.500),
        _tick("fanduel", 0, 0.500),
        _tick("pinnacle", 30, 0.515),           # up
        _tick("draftkings", 35, 0.485),         # down
        _tick("fanduel", 45, 0.518),            # up
    ]
    sigs = detect_steam_event(ticks, min_move=0.005, window_s=60, min_books=3)
    # Only two books (pinnacle + fanduel) moved up — below min_books=3.
    assert sigs == []


# ──────────────────────────────────────────────────────────────────────
# Reverse line movement
# ──────────────────────────────────────────────────────────────────────


def test_rlm_fires_when_line_drifts_off_public_side():
    # Public is 75% on Yankees. Line drifts OFF Yankees (implied prob
    # drops).
    ticks = [
        _tick("pinnacle", 0, 0.550),
        _tick("draftkings", 0, 0.555),
        _tick("pinnacle", 60, 0.535),
        _tick("draftkings", 60, 0.540),
    ]
    sig = detect_reverse_line_movement(ticks, public_pct_on_side=0.75, min_move=0.005)
    assert sig is not None
    assert sig.kind == "rlm"
    assert sig.direction == -1


def test_rlm_silent_when_public_not_lopsided():
    ticks = [
        _tick("pinnacle", 0, 0.550),
        _tick("pinnacle", 60, 0.535),
    ]
    assert detect_reverse_line_movement(ticks, public_pct_on_side=0.52) is None


def test_rlm_silent_when_line_agrees_with_public():
    # Public on the side, line drifts TOWARD the public side — not RLM.
    ticks = [
        _tick("pinnacle", 0, 0.550),
        _tick("pinnacle", 60, 0.570),
    ]
    assert detect_reverse_line_movement(ticks, public_pct_on_side=0.75) is None


# ──────────────────────────────────────────────────────────────────────
# Composite scan
# ──────────────────────────────────────────────────────────────────────


def test_scan_returns_limit_down_event():
    ticks = [
        _tick("draftkings", 0, 0.500, limit=1000),
        _tick("draftkings", 60, 0.505, limit=200),      # 80% drop
    ]
    sigs = scan_market_movement(ticks)
    kinds = [s.kind for s in sigs]
    assert "limit_down" in kinds


def test_scan_combines_steam_and_first_mover():
    # Build a scenario where steam fires AND first-mover fires for the
    # same chain of events.
    ticks = [
        _tick("pinnacle", 0, 0.500),
        _tick("draftkings", 0, 0.500),
        _tick("fanduel", 0, 0.500),
        _tick("pinnacle", 30, 0.515),
        _tick("draftkings", 35, 0.518),
        _tick("fanduel", 45, 0.516),
    ]
    sigs = scan_market_movement(ticks, steam_min_books=3)
    kinds = {s.kind for s in sigs}
    # Both steam and first_mover should trigger.
    assert "steam" in kinds
    assert "first_mover" in kinds


def test_scan_handles_empty_input():
    assert scan_market_movement([]) == []
