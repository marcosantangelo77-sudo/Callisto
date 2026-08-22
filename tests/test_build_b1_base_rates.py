"""B1 build tests — base-rate-relative thresholds.

The absolute 0.45 hit-rate floor is correct at a ~50% base rate and
mass-rejects true positives anywhere base rates are 1–5%. These tests pin:

  * tools/resolvers/base_rates.py arithmetic (floor derivation + clamps +
    never-below-chance guarantee)
  * auto-reject in hypothesis.py using the derived floor
  * review_live_hypotheses using the derived floor per hypothesis
  * sports behaviour unchanged at ~50% implied priors (legacy floor holds)
"""

from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

import tools.resolvers.base_rates as br_mod
from tools.resolvers.base_rates import (
    base_rate_relative_floor,
    expected_base_rate_from_events,
)


# ── pure arithmetic ───────────────────────────────────────────────────


def test_unknown_base_rate_falls_back_to_legacy_floor():
    assert base_rate_relative_floor(None, legacy_floor=0.45) == 0.45
    assert base_rate_relative_floor(0.0, legacy_floor=0.45) == 0.45
    assert base_rate_relative_floor(float("nan"), legacy_floor=0.45) == 0.45


def test_low_base_rate_gets_low_floor():
    # 2% base-rate domain (drug discovery): floor ≈ 2.2%, not 45%.
    f = base_rate_relative_floor(0.02, legacy_floor=0.45)
    assert f == pytest.approx(0.02 * 1.10)
    assert f < 0.05


def test_high_base_rate_caps_at_legacy_ceiling():
    assert base_rate_relative_floor(0.50, legacy_floor=0.45) == 0.45
    assert base_rate_relative_floor(0.60, legacy_floor=0.45) == 0.45


def test_floor_never_below_chance_and_within_absolute_bounds():
    for b in (0.01, 0.02, 0.05, 0.15, 0.30, 0.50, 0.80):
        f = base_rate_relative_floor(b, legacy_floor=0.45)
        assert f >= min(b, br_mod.BASE_RATE_FLOOR_ABS_MIN) - 1e-12
        assert br_mod.BASE_RATE_FLOOR_ABS_MIN <= f <= br_mod.BASE_RATE_FLOOR_ABS_MAX


def test_negative_or_invalid_lift_env_cannot_disable_the_gate(monkeypatch):
    """Rule 4: nothing may weaken a gate below chance."""
    monkeypatch.setenv("CALLISTO_MIN_BASE_RATE_LIFT", "-5.0")
    importlib.reload(br_mod)
    try:
        f = base_rate_relative_floor(0.02, legacy_floor=0.45)
        assert f >= 0.02  # still at least the base rate itself
    finally:
        monkeypatch.delenv("CALLISTO_MIN_BASE_RATE_LIFT", raising=False)
        importlib.reload(br_mod)


def test_expected_base_rate_from_events_prefers_explicit_then_market():
    rows = [{"book_implied_prob": 0.52}, {"book_implied_prob": 0.48}]
    assert expected_base_rate_from_events(rows) == pytest.approx(0.50)
    rows2 = [{"base_rate": 0.03}, {"base_rate": 0.05}]
    assert expected_base_rate_from_events(rows2) == pytest.approx(0.04)
    assert expected_base_rate_from_events([{}, {}]) is None
    assert expected_base_rate_from_events([]) is None


# ── wired into hypothesis.py ──────────────────────────────────────────

from tests.test_build_b1_clv_gate import (  # reuse the minimal fixtures
    _hyp,
    _mgr,
    _setup_db,
    _trade,
)


@pytest.mark.asyncio
async def test_auto_reject_uses_derived_floor_for_low_base_rate_claim():
    """A longshot-prop claim (~20% prior) hitting 35% over 12+ signals was
    mass-auto-rejected by the old absolute floor despite beating its prior.
    It must now survive; a 10% performer must still be rejected."""
    db = await _setup_db()
    try:
        await _hyp(db, "h-long", status="backtesting",
                   promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
        # 12 signals, prior ~0.20, actual 4/12 = 33% hits.
        for i in range(12):
            await db.execute(
                "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
                "book_odds_american, book_implied_prob, model_fair_prob, edge, "
                "ev_pct, signal_generated, actual_result, game_date, snapshot_time, "
                "model_factors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r1", f"L{i}", "h-long", 400, 0.20, 0.26, 0.06, 8.0, 1,
                 "won" if i < 4 else "lost", "2025-12-01",
                 "2025-12-01T00:00:00+00:00", "{}"),
            )
        await db.commit()
        r = await (await _mgr(db)).check_promotion_readiness("h-long")
        rejects = [c for c in r["checks"] if c.startswith("AUTO-REJECT") and "hit_rate" in c]
        assert not rejects, rejects

        # Mirror case: 3/12 = 25% vs a 30%-prior-derived floor of 33% → the
        # base-rate-relative clause fires (p-value tiers don't: p≈0.25).
        await _hyp(db, "h-dog", status="backtesting",
                   promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
        for i in range(12):
            await db.execute(
                "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
                "book_odds_american, book_implied_prob, model_fair_prob, edge, "
                "ev_pct, signal_generated, actual_result, game_date, snapshot_time, "
                "model_factors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r2", f"D{i}", "h-dog", 233, 0.30, 0.36, 0.06, 8.0, 1,
                 "won" if i < 3 else "lost", "2025-12-01",
                 "2025-12-01T00:00:00+00:00", "{}"),
            )
        await db.commit()
        r2 = await (await _mgr(db)).check_promotion_readiness("h-dog")
        rejects2 = [c for c in r2["checks"] if c.startswith("AUTO-REJECT") and "hit_rate" in c]
        assert rejects2 and "base-rate-relative" in rejects2[0]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_sports_prior_unchanged_legacy_floor_holds():
    """~50% prior claims keep the exact legacy behaviour: 40% hit → reject."""
    db = await _setup_db()
    try:
        await _hyp(db, "h-spr", status="backtesting",
                   promoted_at=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat())
        for i in range(12):
            await db.execute(
                "INSERT INTO backtest_events (run_id, event_id, hypothesis_id, "
                "book_odds_american, book_implied_prob, model_fair_prob, edge, "
                "ev_pct, signal_generated, actual_result, game_date, snapshot_time, "
                "model_factors) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("r3", f"S{i}", "h-spr", -110, 0.524, 0.56, 0.036, 5.0, 1,
                 "won" if i < 5 else "lost", "2025-12-01",
                 "2025-12-01T00:00:00+00:00", "{}"),
            )
        await db.commit()
        r = await (await _mgr(db)).check_promotion_readiness("h-spr")
        # 41% hit at n=12: should_reject must be True (either via the
        # strong-p tier or the hit-rate clause) and the derived floor for a
        # 52.4% prior must remain the legacy 0.45.
        assert r["should_reject"] is True
        from tools.resolvers.base_rates import base_rate_relative_floor as _brf
        assert _brf(0.524, legacy_floor=0.45) == 0.45
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_review_live_demote_floor_is_base_rate_relative():
    """LIVE claim with 20% priors going 3/15 (20%) must NOT be demoted by
    the hit-rate clause alone (old code: 20% < 45% → demoted)."""
    from tests.test_build_b1_clv_gate import _trade as _t  # noqa: F401
    db = await _setup_db()
    try:
        await _hyp(db, "h-live-low", status="live",
                   promoted_at=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        for i in range(15):
            gd = (datetime.now(timezone.utc) - timedelta(days=i % 7)).strftime("%Y-%m-%d")
            await db.execute(
                "INSERT INTO paper_trades (trade_id, hypothesis_id, event_id, sport, "
                "market, side, book, signal_time, signal_odds_american, "
                "signal_implied_prob, model_fair_prob, edge, ev_pct, clv_implied, "
                "actual_result, hypothetical_pnl, game_date, home_team, away_team) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"lv{i}", "h-live-low", f"E{i}", "baseball_mlb", "prop", "Over",
                 "dk", datetime.now(timezone.utc).isoformat(), 400, 0.20, 0.24,
                 0.04, 5.0, 0.21, "won" if i < 3 else "lost",
                 90.0 if i < 3 else -100.0, gd, "H", "A"),
            )
        await db.commit()
        results = await (await _mgr(db)).review_live_hypotheses(window_days=60)
        out = results[0]
        assert out["expected_base_rate"] == pytest.approx(0.20, abs=0.01)
        assert out["effective_hit_rate_floor"] < 0.30
        assert out["demoted"] is False, out["reasons"]

        # Opt-out keeps legacy semantics exactly.
        results2 = await (await _mgr(db)).review_live_hypotheses(
            window_days=60, base_rate_relative=False
        )
        assert results2[0]["demoted"] is True
    finally:
        await db.close()
