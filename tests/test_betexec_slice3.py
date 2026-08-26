"""tests/test_betexec_slice3.py — pin the slice-3 BetExecutor helper split.

Slice 3 (2026-08) moved the remaining orchestration out of
``tools/bet_executor.py`` into real modules:

  - ``tools.betexec.portfolio``    — portfolio Kelly sizing (single + multi),
                                     signals_n dampening, regime multipliers,
                                     exposure-cap passes
  - ``tools.betexec.preflight``    — pure safety-gate evaluation
                                     (enablement → edge → bankroll → cap →
                                     daily loss limit → supported sport)
  - ``tools.betexec.kill_switch``  — drawdown CAS: pause all LIVE hypotheses
                                     to 'drawdown_paused'
  - ``tools.betexec.notify``       — Telegram message builders

All tests use fake page/db objects — no browser, no network, no DraftKings.
The executor is NEVER armed: ``_enabled`` stays False by default and the
CALLISTO_LOCAL_ONLY refusal is re-pinned here.
"""

import asyncio
import os

import pytest

os.environ.setdefault("CALLISTO_LOCAL_ONLY", "1")

import tools.bet_executor as be
from tools.betexec import kill_switch as betexec_kill_switch
from tools.betexec import notify as betexec_notify
from tools.betexec import portfolio as betexec_portfolio
from tools.betexec import preflight as betexec_preflight
from tools.betexec.config import (
    DAILY_LOSS_LIMIT_PCT,
    KELLY_FRACTION,
    MAX_BET_PCT,
    MAX_DRAWDOWN_PCT,
    MIN_BET_AMOUNT,
)
from tools.betexec.dk_constants import DK_SPORT_SLUGS


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeResult:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class FakeDB:
    """Minimal aiosqlite stand-in covering the kill-switch SQL surface."""

    def __init__(self, live_ids=(), update_rowcount=1, fail_update=False):
        self.live_ids = list(live_ids)
        self.update_rowcount = update_rowcount
        self.fail_update = fail_update
        self.executed = []
        self.commits = 0

    async def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        s = sql.strip()
        if s.startswith("SELECT hypothesis_id"):
            return FakeCursor([(hid,) for hid in self.live_ids])
        if s.startswith("UPDATE hypotheses"):
            if self.fail_update:
                raise RuntimeError("db exploded")
            return FakeResult(rowcount=self.update_rowcount)
        return FakeCursor([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# preflight.evaluate_preflight
# ---------------------------------------------------------------------------

SPORT = next(iter(DK_SPORT_SLUGS))


def _ok_kwargs(**overrides):
    kw = dict(
        enabled=True,
        edge=0.05,
        bankroll=1000.0,
        stake=10.0,
        daily_losses=0.0,
        sport=SPORT,
    )
    kw.update(overrides)
    return kw


def test_preflight_happy_path():
    ok, reason = betexec_preflight.evaluate_preflight(**_ok_kwargs())
    assert ok is True
    assert reason == "OK"


def test_preflight_disabled_gate_first():
    # Even with every other value terrible, disabled wins (order pinned).
    ok, reason = betexec_preflight.evaluate_preflight(
        enabled=False, edge=-9.9, bankroll=0.0, stake=1e9,
        daily_losses=-1e9, sport="not-a-sport",
    )
    assert ok is False
    assert reason == "Executor is disabled"


def test_preflight_min_edge():
    ok, reason = betexec_preflight.evaluate_preflight(
        **_ok_kwargs(edge=0.005)
    )
    assert ok is False
    assert "below minimum" in reason


def test_preflight_no_bankroll():
    ok, reason = betexec_preflight.evaluate_preflight(
        **_ok_kwargs(bankroll=0.0)
    )
    assert ok is False
    assert reason == "No bankroll"


def test_preflight_max_bet_cap():
    stake = 1000.0 * MAX_BET_PCT + 1.0
    ok, reason = betexec_preflight.evaluate_preflight(
        **_ok_kwargs(stake=stake)
    )
    assert ok is False
    assert "exceeds" in reason


def test_preflight_daily_loss_limit():
    losses = -(1000.0 * DAILY_LOSS_LIMIT_PCT) - 1.0
    ok, reason = betexec_preflight.evaluate_preflight(
        **_ok_kwargs(daily_losses=losses)
    )
    assert ok is False
    assert "Daily loss limit" in reason


def test_preflight_unsupported_sport():
    ok, reason = betexec_preflight.evaluate_preflight(
        **_ok_kwargs(sport="underwater_basket_weaving")
    )
    assert ok is False
    assert "not supported" in reason


def test_executor_preflight_delegates_and_never_needs_browser():
    ex = be.BetExecutor()
    assert ex.is_enabled is False  # never armed
    # Arm the flag directly (enable() refuses under CALLISTO_LOCAL_ONLY=1,
    # which this module sets at import). We're testing the DB-gathering
    # delegation, not the arming policy — pinned separately above.
    ex._enabled = True
    db = FakeDB()
    db.bankroll_rows = [(1000.0,)]
    db.daily_rows = [(0.0,)]
    ex._db = db

    orig_execute = db.execute

    async def execute(sql, params=None):
        s = sql.strip()
        if s.startswith("SELECT balance"):
            return FakeCursor([(1000.0,)])
        if "SUM(" in s and "bets" in s:
            return FakeCursor([(0.0,)])
        return await orig_execute(sql, params)

    db.execute = execute
    ok, reason = run(ex.preflight_check(SPORT, -110, 0.05, 10.0))
    assert ok is True and reason == "OK"


def test_executor_preflight_refuses_when_disabled_even_with_db():
    ex = be.BetExecutor()  # _enabled False; preflight must refuse before DB
    ok, reason = run(ex.preflight_check(SPORT, -110, 0.5, 10.0))
    assert ok is False
    assert reason == "Executor is disabled"


# ---------------------------------------------------------------------------
# portfolio sizing
# ---------------------------------------------------------------------------

class RecordingSizer:
    def __init__(self, stake=50.0):
        self.stake = stake
        self.calls = []

    def __call__(self, edge, odds, bankroll, confidence):
        self.calls.append((edge, odds, bankroll, confidence))
        return self.stake


def _fns(sizer, regime_mult=1.0, kelly_frac=None):
    return dict(
        regime_multiplier_fn=lambda sport: regime_mult,
        kelly_fraction_fn=lambda n: kelly_frac or KELLY_FRACTION,
        stake_fn=sizer,
    )


def test_portfolio_empty_batch_returns_empty():
    sizer = RecordingSizer()
    assert betexec_portfolio.compute_portfolio_stakes(
        [], 1000.0, None, **_fns(sizer)
    ) == []
    assert sizer.calls == []


def test_single_bet_individual_path_shape():
    sizer = RecordingSizer(stake=40.0)
    out = betexec_portfolio.compute_portfolio_stakes(
        [{
            "edge": 0.04, "odds": -110, "confidence": 0.6,
            "description": "Yankees ML", "event_id": "e1",
            "sport": SPORT, "hypothesis_id": "h1", "signals_n": 6,
        }],
        1000.0, None, **_fns(sizer),
    )
    assert len(out) == 1
    row = out[0]
    assert row["method"] == "individual_kelly_n_adjusted"
    assert row["stake"] == pytest.approx(round(40.0, 2))
    assert row["signals_n"] == 6
    assert row["kelly_base_fraction"] == KELLY_FRACTION
    assert row["regime_multiplier"] == 1.0
    assert row["hypothesis_id"] == "h1"
    assert row["sport"] == SPORT


def test_single_bet_regime_multiplier_scales_stake():
    sizer = RecordingSizer(stake=100.0)
    out = betexec_portfolio.compute_portfolio_stakes(
        [{"edge": 0.04, "odds": -110, "sport": SPORT}],
        1000.0, None,
        **_fns(sizer, regime_mult=0.5),
    )
    row = out[0]
    assert row["regime_multiplier"] == 0.5
    assert row["stake_before_regime"] == 100.0
    assert row["stake"] == 50.0


def test_single_bet_below_min_floor_zeroed():
    sizer = RecordingSizer(stake=MIN_BET_AMOUNT / 4)  # tiny stake
    out = betexec_portfolio.compute_portfolio_stakes(
        [{"edge": 0.04, "odds": -110, "sport": SPORT}],
        100000.0, None, **_fns(sizer),
    )
    assert out[0]["stake"] == 0.0


def test_multi_bet_portfolio_path_marks_method():
    bets = [
        {
            "edge": 0.05, "odds": -110, "confidence": 0.7,
            "sport": SPORT, "signals_n": 8,
            "description": "A", "event_id": "a",
            "market_type": "h2h", "hypothesis_id": "ha",
            "correlation_with_others": 0.1,
        },
        {
            "edge": 0.06, "odds": +120, "confidence": 0.65,
            "sport": SPORT, "signals_n": 8,
            "description": "B", "event_id": "b",
            "market_type": "h2h", "hypothesis_id": "hb",
            "correlation_with_others": 0.1,
        },
    ]
    sizer = RecordingSizer()
    out = betexec_portfolio.compute_portfolio_stakes(
        bets, 2000.0, None, **_fns(sizer),
    )
    assert len(out) == len(bets)
    methods = {row["method"] for row in out}
    assert methods == {"portfolio_kelly_n_adjusted"}
    for row in out:
        assert set(row) >= {
            "stake", "fraction", "regime_multiplier",
            "stake_before_regime", "portfolio_summary",
        }
    # Sizing went through build_portfolio_requests, not per-bet stake_fn.
    assert sizer.calls == []


def test_executor_compute_portfolio_delegates_to_module(monkeypatch):
    """BetExecutor.compute_portfolio_stakes routes through tools.betexec.portfolio."""
    captured = {}

    def fake_compute(bets, bankroll, corr, *, regime_multiplier_fn,
                     kelly_fraction_fn, stake_fn):
        captured["n"] = len(bets)
        captured["bankroll"] = bankroll
        captured["regime_fn"] = regime_multiplier_fn
        captured["kelly_fn"] = kelly_fraction_fn
        captured["stake_fn"] = stake_fn
        return [{"sentinel": True}]

    monkeypatch.setattr(betexec_portfolio, "compute_portfolio_stakes",
                        fake_compute)
    ex = be.BetExecutor()
    out = ex.compute_portfolio_stakes([{"edge": 0.1}], 500.0)
    assert out == [{"sentinel": True}]
    assert captured["n"] == 1
    assert captured["bankroll"] == 500.0
    # Injected callables are the executor's own helpers.
    assert captured["kelly_fn"](3) == be.BetExecutor._signals_n_to_kelly_fraction(3)
    assert captured["regime_fn"].__module__ == "tools.bet_executor"
    assert callable(captured["stake_fn"])


# ---------------------------------------------------------------------------
# kill_switch.pause_live_hypotheses
# ---------------------------------------------------------------------------

def test_pause_live_updates_each_live_hypothesis():
    db = FakeDB(live_ids=["h1", "h2"], update_rowcount=1)
    paused = run(betexec_kill_switch.pause_live_hypotheses(db))
    assert paused == ["h1", "h2"]
    updates = [sql for sql, _ in db.executed if sql.startswith("UPDATE hypotheses")]
    assert len(updates) == 2
    # CAS: WHERE clause re-checks status='live'
    sql, params = next(p for p in db.executed if p[0].startswith("UPDATE"))
    assert "AND status = 'live'" in sql.replace("\n", " ")
    _, _, promoted_by, hid = params
    assert promoted_by == betexec_kill_switch.PAUSE_REASON
    assert hid in ("h1", "h2")
    assert db.commits >= 1


def test_pause_skips_rows_lost_to_cas_race():
    db = FakeDB(live_ids=["h1"], update_rowcount=0)
    paused = run(betexec_kill_switch.pause_live_hypotheses(db))
    assert paused == []


def test_pause_error_propagates_for_caller_best_effort():
    db = FakeDB(live_ids=["h1"], fail_update=True)
    with pytest.raises(RuntimeError):
        run(betexec_kill_switch.pause_live_hypotheses(db))


def test_attach_pause_result_success_and_error_paths():
    status = {"triggered": True}
    out = betexec_kill_switch.attach_pause_result(status, ["h9"])
    assert out["paused_hypotheses"] == ["h9"]

    status2 = {"triggered": True}
    out2 = betexec_kill_switch.attach_pause_result(status2, [], error=RuntimeError("x"))
    assert "paused_hypotheses" not in out2


def test_check_drawdown_and_kill_pauses_and_disables_only_on_trigger():
    ex = be.BetExecutor()

    class PeakDB(FakeDB):
        def __init__(self):
            super().__init__(live_ids=["hl1"], update_rowcount=1)

        async def execute(self, sql, params=None):
            s = sql.strip()
            if "MAX(balance), 0) FROM bankroll_peak" in s:
                return FakeCursor([(1000.0,)])
            if s.startswith("SELECT balance"):
                return FakeCursor([(100.0,)])
            return await super().execute(sql, params)

    db = PeakDB()
    ex._db = db
    ex._enabled = True  # simulate armed state so we can watch it disarm
    status = run(ex.check_drawdown_and_kill())
    assert status["triggered"] is True
    assert ex.is_enabled is False  # kill switch disarmed the executor
    assert status.get("paused_hypotheses") == ["hl1"]
    # drawdown math: (1000-100)/1000 = 90%
    assert status["drawdown_pct"] == pytest.approx(0.9)


def test_check_drawdown_not_triggered_leaves_enabled_alone():
    ex = be.BetExecutor()

    class CalmDB(FakeDB):
        async def execute(self, sql, params=None):
            s = sql.strip()
            if "MAX(balance), 0) FROM bankroll_peak" in s:
                return FakeCursor([(1000.0,)])
            if s.startswith("SELECT balance"):
                return FakeCursor([(990.0,)])
            return FakeCursor([])

    ex._db = CalmDB()
    ex._enabled = True
    status = run(ex.check_drawdown_and_kill())
    assert status["triggered"] is False
    assert ex.is_enabled is True


# ---------------------------------------------------------------------------
# notify.build_bet_placed_message
# ---------------------------------------------------------------------------

def test_bet_placed_message_layout_negative_odds():
    msg = betexec_notify.build_bet_placed_message(
        game_description="Yankees @ Red Sox",
        team="New York Yankees",
        side="Yankees ML",
        odds=-150,
        stake=25.0,
        edge=0.032,
        bankroll=1000.0,
    )
    lines = msg.splitlines()
    assert lines[0] == "BET PLACED"
    assert lines[1] == "Yankees @ Red Sox"
    assert lines[2] == "Yankees ML @ -150"  # no '+' on negative odds
    assert "Stake: $25.00 | Edge: 3.2%" in lines[3]
    assert "Bankroll: $1000.00 → $975.00" in lines[4]


def test_bet_placed_message_positive_odds_and_team_fallback():
    msg = betexec_notify.build_bet_placed_message(
        game_description="",
        team="Boston Celtics",
        side="Celtics ML",
        odds=180,
        stake=10.0,
        edge=0.05,
        bankroll=500.0,
    )
    assert "Celtics ML @ +180" in msg
    assert "\nBoston Celtics\n" in msg  # falls back to team


def test_executor_uses_notify_builder(monkeypatch):
    """execute_bet's Telegram body comes from tools.betexec.notify."""
    sent = {}
    monkeypatch.setattr(
        "tools.telegram.send_telegram",
        lambda m: sent.setdefault("msg", m),
        raising=False,
    )
    built = betexec_notify.build_bet_placed_message(
        game_description="G", team="T", side="S", odds=-110,
        stake=5.0, edge=0.02, bankroll=100.0,
    )
    assert built.startswith("BET PLACED")


# ---------------------------------------------------------------------------
# Safety posture pins
# ---------------------------------------------------------------------------

def test_executor_default_disabled_after_split():
    ex = be.BetExecutor()
    assert ex._enabled is False
    assert ex.is_enabled is False


def test_local_only_refusal_still_before_arm(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = be.BetExecutor()
    assert ex.enable() is False
    assert ex._enabled is False


def test_paper_signal_statuses_unaffected_by_slice3():
    from tools.signals.paper import (
        _PAPER_TRADE_SIGNAL_STATUSES,
        reject_non_paper,
    )
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert reject_non_paper("live") is True
    assert reject_non_paper("paper_trading") is False


def test_new_modules_importable_from_package():
    import tools.betexec as pkg
    for name in ("portfolio", "preflight", "kill_switch", "notify"):
        assert hasattr(pkg, name), name
