"""Tests for the live prop reactivity and late-game over-reaction
detectors.

Covers:
  * 15% line shift within 30s triggers (simulated)
  * Smaller shifts or slower shifts do not trigger
  * NBA late-game over-reaction triggers at end of Q3 with big lead +
    over-compressed spread
  * NHL equivalent with goals
  * Throughput benchmark for the detector hot path (target <50ms per
    state processed).
"""

from __future__ import annotations

import time

from tools.live_edges import (
    nba_late_overreaction_signal,
    nhl_late_overreaction_signal,
    prop_reactivity_signal,
)


# ── Prop reactivity ──────────────────────────────────────────────────


def test_prop_shift_15pct_in_30s_fires():
    """A prop line dropping 20% in 20s LATE in the game (only 15% of
    time left) is classic over-reaction — the remaining-time justified
    ceiling is 15%, actual shift is 21% → gap 6%."""
    out = prop_reactivity_signal(
        prior_line=25.5,
        new_line=20.0,  # -21%
        window_s=20.0,
        remaining_time_frac=0.15,
        new_over_price=-110,
    )
    assert out is not None
    assert out.thesis_tag == "live_prop_reactivity"
    # Drop → OVER is the counter-party edge.
    assert out.side == "OVER"
    assert out.edge > 0


def test_prop_shift_below_threshold_does_not_fire():
    out = prop_reactivity_signal(
        prior_line=25.5,
        new_line=24.0,  # -6%
        window_s=20.0,
        remaining_time_frac=0.50,
        new_over_price=-110,
    )
    assert out is None


def test_prop_shift_outside_window_does_not_fire():
    """A slow drift (60s) even if large in magnitude isn't a reaction."""
    out = prop_reactivity_signal(
        prior_line=25.5,
        new_line=20.0,
        window_s=60.0,
        remaining_time_frac=0.50,
        new_over_price=-110,
    )
    assert out is None


def test_prop_shift_justified_by_remaining_time_does_not_fire():
    """Early in the game (80% remaining) a 20% line move IS justified by
    remaining-time EV and should NOT be flagged as over-reaction."""
    out = prop_reactivity_signal(
        prior_line=25.5,
        new_line=20.0,  # -21%
        window_s=10.0,
        remaining_time_frac=0.80,  # plenty of time left to recover
        new_over_price=-110,
    )
    assert out is None


# ── NBA late over-reaction ────────────────────────────────────────────


def test_nba_late_fires_on_overcompressed_spread():
    """Home up 18 end of Q3. Expected Q4 diff ~10.8. If live spread
    shows home -5 (implied end diff +5) that's a ~5.8 pt over-extension
    toward home → fire HOME."""
    out = nba_late_overreaction_signal(
        period=3,
        time_remaining_s=30,
        home_score=90,
        away_score=72,
        live_spread_home=-5.0,
        live_home_price=-130,
    )
    assert out is not None
    assert out.side == "HOME"
    assert out.thesis_tag == "nba_late_overreaction"


def test_nba_late_does_not_fire_without_big_lead():
    out = nba_late_overreaction_signal(
        period=3,
        time_remaining_s=30,
        home_score=90,
        away_score=82,  # only 8-pt lead
        live_spread_home=-3.0,
        live_home_price=-130,
    )
    assert out is None


def test_nba_late_does_not_fire_too_early():
    """Period 2 — too early for Q3-end over-reaction."""
    out = nba_late_overreaction_signal(
        period=2,
        time_remaining_s=30,
        home_score=60,
        away_score=42,
        live_spread_home=-5.0,
        live_home_price=-130,
    )
    assert out is None


# ── NHL late over-reaction ────────────────────────────────────────────


def test_nhl_late_fires_on_three_goal_compression():
    """Home up 4-1 end of P2. Expected end diff ≈ 1.8. Live PL -0.5 →
    implied +0.5 → overextend ≈ 1.3 goals → fires HOME."""
    out = nhl_late_overreaction_signal(
        period=2,
        time_remaining_s=60,
        home_score=4,
        away_score=1,
        live_puck_line_home=-0.5,
        live_home_price=-180,
    )
    assert out is not None
    assert out.side == "HOME"
    assert out.thesis_tag == "nhl_late_overreaction"


# ── Throughput benchmark ─────────────────────────────────────────────


def test_detector_throughput_under_50ms():
    """Process 1000 synthetic states through all three detectors; total
    budget should be comfortably under 50 * 1000 = 50s; per-state must
    be < 50ms."""
    N = 1000
    t0 = time.perf_counter()
    for i in range(N):
        prop_reactivity_signal(
            prior_line=25.5,
            new_line=20.0 + (i % 5) * 0.1,
            window_s=20.0,
            remaining_time_frac=0.40,
            new_over_price=-110,
        )
        nba_late_overreaction_signal(
            period=3,
            time_remaining_s=30,
            home_score=90 + (i % 3),
            away_score=72,
            live_spread_home=-5.0,
            live_home_price=-130,
        )
        nhl_late_overreaction_signal(
            period=2,
            time_remaining_s=60,
            home_score=4,
            away_score=1,
            live_puck_line_home=-0.5,
            live_home_price=-180,
        )
    elapsed = time.perf_counter() - t0
    per_state_ms = elapsed / N * 1000
    # The three detector calls per iteration simulate processing one
    # multi-sport live state. We want < 50ms per state.
    assert per_state_ms < 50, f"detector too slow: {per_state_ms:.2f}ms/state"
    print(f"throughput: {per_state_ms:.3f} ms per state ({N} iter, {elapsed*1000:.1f} ms total)")
