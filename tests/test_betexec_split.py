"""tests/test_betexec_split.py — pin the tools.betexec split.

The split (2026-08) moved pure helpers out of tools/bet_executor.py into the
tools.betexec package. These tests pin:

  1. Facade re-exports: every public name importable from tools.bet_executor
     resolves to (or wraps) its tools.betexec counterpart.
  2. Safety invariants: BetExecutor defaults to disabled, enable() is refused
     under CALLISTO_LOCAL_ONLY, and no "live" signal status leaks anywhere in
     the new package.
  3. Helper behaviour: Kelly fraction mapping, exposure caps, drawdown
     evaluation, regime clamping.

No browser, no network; DB-touching paths are NOT exercised. The executor is
never armed.
"""

import asyncio
import importlib
import os

import pytest

# Pin caps to known values so tests don't drift with env overrides.
os.environ.setdefault("CALLISTO_MAX_GAME_EXPOSURE_PCT", "0.08")
os.environ.setdefault("CALLISTO_MAX_SPORT_EXPOSURE_PCT", "0.15")
os.environ.setdefault("EXECUTOR_MAX_BET_PCT", "0.05")
os.environ.setdefault("EXECUTOR_KELLY_FRACTION", "0.25")
os.environ.setdefault("EXECUTOR_MIN_BET", "1.00")
os.environ.setdefault("CALLISTO_REGIME_SIZING", "1")

import tools.betexec as betexec
import tools.bet_executor as be
from tools.betexec.config import (
    MAX_GAME_EXPOSURE_PCT,
    MAX_SPORT_EXPOSURE_PCT,
    REGIME_MAX_MULT,
    REGIME_MIN_MULT,
)
from tools.betexec.drawdown import build_kill_switch_alert, evaluate_drawdown
from tools.betexec.dk_constants import DK_BASE_URL, DK_SPORT_SLUGS
from tools.betexec.regime import clamped_regime_multiplier
from tools.betexec.sizing import (
    apply_exposure_caps,
    signals_n_to_kelly_fraction,
)


# ---------------------------------------------------------------------------
# 1. Facade re-exports
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "name",
    [
        "DB_PATH", "SCREENSHOT_DIR", "SESSION_DIR",
        "MAX_BET_PCT", "MAX_OPEN_EXPOSURE_PCT", "DAILY_LOSS_LIMIT_PCT",
        "MIN_EDGE_TO_EXECUTE", "KELLY_FRACTION", "MIN_BET_AMOUNT",
        "MAX_GAME_EXPOSURE_PCT", "MAX_SPORT_EXPOSURE_PCT",
        "MAX_DRAWDOWN_PCT", "DRAWDOWN_PEAK_WINDOW_DAYS",
        "_VAR_DAMPENER_LOW_N", "_VAR_DAMPENER_HIGH_N",
        "REGIME_SIZING_ENABLED", "REGIME_SAFETY_ENABLED",
        "_REGIME_MIN_MULT", "_REGIME_MAX_MULT",
        "DK_BASE_URL", "DK_SPORT_SLUGS",
        "REGIME_SAFETY_ENABLED",
        "_clamped_regime_multiplier", "_regime_safe", "BetExecutor",
    ],
)
def test_facade_reexports_names(name):
    assert hasattr(be, name), f"tools.bet_executor lost public name {name!r}"


def test_facade_constants_match_betexec():
    assert be.MAX_BET_PCT == betexec.MAX_BET_PCT
    assert be.MAX_DRAWDOWN_PCT == betexec.MAX_DRAWDOWN_PCT
    assert be.DK_BASE_URL == DK_BASE_URL
    assert be.DK_SPORT_SLUGS is DK_SPORT_SLUGS
    assert be._REGIME_MIN_MULT == REGIME_MIN_MULT
    assert be._REGIME_MAX_MULT == REGIME_MAX_MULT


# ---------------------------------------------------------------------------
# 2. Safety invariants
# ---------------------------------------------------------------------------

def test_init_default_disabled():
    ex = be.BetExecutor()
    assert ex.is_enabled is False
    assert ex.is_logged_in is False
    assert ex._page is None
    assert ex._db is None


@pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes"])
def test_local_only_refuses_enable(monkeypatch, v):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", v)
    ex = be.BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_disable_resets_enabled():
    ex = be.BetExecutor()
    monkey = pytest.MonkeyPatch()
    monkey.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    try:
        assert ex.enable() is True
        assert ex.is_enabled is True
        ex.disable()
        assert ex.is_enabled is False
    finally:
        monkey.undo()


def test_no_live_status_in_betexec_package():
    """Guard rail: the split package must never introduce a 'live' status."""
    pkg_dir = os.path.dirname(betexec.__file__)
    offenders = []
    for fname in os.listdir(pkg_dir):
        if not fname.endswith(".py"):
            continue
        with open(os.path.join(pkg_dir, fname), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if "'live'" in line or '"live"' in line:
                    # allow docstring/comment mentions only
                    stripped = line.strip()
                    if not stripped.startswith("#") and "'''" not in line and '"""' not in line:
                        offenders.append(f"{fname}:{i}: {stripped}")
    assert offenders == [], f"betexec must not reference 'live' status: {offenders}"


# ---------------------------------------------------------------------------
# 3. Helper behaviour
# ---------------------------------------------------------------------------

class TestSignalsNKellyFraction:
    def test_low_n_half_kelly(self):
        assert signals_n_to_kelly_fraction(0) == 0.125
        assert signals_n_to_kelly_fraction(25) == 0.125

    def test_high_n_full_quarter(self):
        assert signals_n_to_kelly_fraction(100) == 0.25
        assert signals_n_to_kelly_fraction(500) == 0.25

    def test_interp_midpoint(self):
        assert signals_n_to_kelly_fraction(62) == pytest.approx(0.125 + ((62 - 25) / 75) * 0.125)

    def test_staticmethod_delegates(self):
        assert be.BetExecutor._signals_n_to_kelly_fraction(150) == signals_n_to_kelly_fraction(150)


class TestExposureCaps:
    def _rows(self, stakes, event_ids, sports):
        return [
            {
                "stake": s,
                "fraction": s / 10_000.0,
                "event_id": e,
                "sport": sp,
            }
            for s, e, sp in zip(stakes, event_ids, sports)
        ]

    def test_game_cap_scales_group(self):
        bankroll = 10_000.0
        rows = self._rows([500, 500], ["evt1", "evt1"], ["mlb", "mlb"])
        cap = bankroll * MAX_GAME_EXPOSURE_PCT  # 800
        out = apply_exposure_caps(rows, bankroll)
        total = sum(r["stake"] for r in out)
        assert total <= cap + 0.02
        assert all(abs(r["game_cap_scale"] - cap / 1000) < 5e-4 for r in out)

    def test_sport_cap_scales_across_events(self):
        bankroll = 10_000.0
        rows = self._rows([600.0] * 4, ["e1", "e2", "e3", "e4"], ["nhl"] * 4)
        cap = bankroll * MAX_SPORT_EXPOSURE_PCT  # 1500
        out = apply_exposure_caps(rows, bankroll)
        total = sum(r["stake"] for r in out)
        assert total <= cap + 0.02
        scale = cap / 2400.0
        for r in out:
            assert abs(r["sport_cap_scale"] - scale) < 5e-4

    def test_under_cap_untouched(self):
        bankroll = 10_000.0
        rows = self._rows([100, 100], ["e1", "e2"], ["mlb", "nba"])
        out = apply_exposure_caps(rows, bankroll)
        assert [r["stake"] for r in out] == [100.0, 100.0]
        assert "game_cap_scale" not in out[0]
        assert "sport_cap_scale" not in out[0]

    def test_floor_zeroes_small_stakes(self):
        bankroll = 10_000.0
        rows = self._rows([0.5], ["e1"], ["mlb"])
        out = apply_exposure_caps(rows, bankroll)
        assert out[0]["stake"] == 0.0


class TestDrawdownEval:
    def test_no_peak_no_trigger(self):
        st = evaluate_drawdown(current=900.0, peak=0.0)
        assert st["triggered"] is False

    def test_at_peak_no_trigger(self):
        st = evaluate_drawdown(current=1000.0, peak=1000.0)
        assert st["triggered"] is False

    def test_shallow_dd_no_trigger(self):
        st = evaluate_drawdown(current=950.0, peak=1000.0)
        assert st["drawdown_pct"] == 0.05
        assert st["triggered"] is False

    def test_deep_dd_triggers(self):
        st = evaluate_drawdown(current=800.0, peak=1000.0)
        assert st["drawdown_pct"] == 0.2
        assert st["triggered"] is True
        assert st["threshold_pct"] == pytest.approx(betexec.MAX_DRAWDOWN_PCT)

    def test_alert_body_mentions_kill_switch(self):
        msg = build_kill_switch_alert(800.0, 1000.0, 0.2, 3)
        assert "KILL SWITCH" in msg
        assert "3 LIVE" in msg


class TestRegimeClamp:
    def test_disabled_gate_returns_one(self):
        assert clamped_regime_multiplier("mlb", gates={"sizing_enabled": False}) == 1.0

    def test_error_degrades_to_one(self, monkeypatch):
        # Force the inner import/lookup to blow up → degrade to 1.0
        import builtins
        real_import = builtins.__import__

        def _boom(name, *a, **k):
            if name == "tools.market_regime":
                raise RuntimeError("regime module unavailable")
            return real_import(name, *a, **k)

        monkeypatch.setattr(builtins, "__import__", _boom)
        assert clamped_regime_multiplier("baseball_mlb") == 1.0

    def test_clamped_bounds_via_fake_module(self, monkeypatch):
        fake_vals = {"high": 3.0, "low": 0.0}
        import tools.market_regime as mr
        monkeypatch.setattr(mr, "current_regime_multiplier", lambda s: fake_vals[s])
        assert clamped_regime_multiplier("high") == REGIME_MAX_MULT
        assert clamped_regime_multiplier("low") == REGIME_MIN_MULT

    def test_facade_reads_own_flag(self, monkeypatch):
        """Monkeypatching the facade flag must disable via the facade path."""
        monkeypatch.setattr(be, "REGIME_SIZING_ENABLED", False, raising=False)
        assert be._clamped_regime_multiplier("baseball_mlb") == 1.0

    def test_facade_monkeypatched_helper_used_by_portfolio(self, monkeypatch):
        """compute_portfolio_stakes honours a patched module-level helper."""
        monkeypatch.setattr(be, "_clamped_regime_multiplier", lambda sport: 0.7)
        ex = be.BetExecutor()
        sized = ex.compute_portfolio_stakes(
            bets=[{
                "edge": 0.04, "odds": -110, "confidence": 0.8,
                "event_id": "e1", "sport": "baseball_mlb",
                "market_type": "h2h", "hypothesis_id": "h1",
                "description": "d", "signals_n": 150,
            }],
            bankroll=10_000.0,
        )
        assert sized[0]["regime_multiplier"] == 0.7


class TestFacadePortfolioDelegation:
    def test_empty_batch_empty_result(self):
        assert be.BetExecutor().compute_portfolio_stakes([], bankroll=10_000) == []

    def test_multi_batch_applies_caps_and_keys(self):
        ex = be.BetExecutor()
        bets = [
            {
                "edge": 0.04, "odds": -110, "confidence": 0.8,
                "event_id": f"e{i}", "sport": "baseball_mlb",
                "market_type": "h2h", "hypothesis_id": f"h{i}",
                "description": f"d{i}", "signals_n": 150,
            }
            for i in range(3)
        ]
        sized = ex.compute_portfolio_stakes(bets, bankroll=10_000.0, correlation_matrix={})
        assert len(sized) == 3
        for row in sized:
            assert row["method"] == "portfolio_kelly_n_adjusted"
            assert row["regime_multiplier"] >= REGIME_MIN_MULT
            assert row["kelly_base_fraction"] == 0.25
        # per-sport cap respected (rounding slack)
        assert sum(r["stake"] for r in sized) <= 10_000.0 * MAX_SPORT_EXPOSURE_PCT + 0.05


def test_reload_after_env_change_recomputes_flags(monkeypatch):
    """Reloading the facade with CALLISTO_REGIME_SIZING=0 flips the flag."""
    monkeypatch.setenv("CALLISTO_REGIME_SIZING", "0")
    fresh = importlib.reload(be)
    try:
        assert fresh.REGIME_SIZING_ENABLED is False
        m = fresh._clamped_regime_multiplier("baseball_mlb")
        assert m == 1.0
    finally:
        monkeypatch.setenv("CALLISTO_REGIME_SIZING", "1")
        importlib.reload(be)
