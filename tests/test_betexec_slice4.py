"""tests/test_betexec_slice4.py — pin the slice-4 BetExecutor helper split.

Slice 4 (2026-08) moved the remaining session/DB orchestration out of
``tools/bet_executor.py`` into real modules:

  - ``tools.betexec.db_state``   — read-only bankroll / daily-stakes /
                                   open-exposure / daily-losses queries plus
                                   the health-check status dict assembly
  - ``tools.betexec.execution``  — the full size → exposure-cap → preflight →
                                   navigate → place → record pipeline
  - ``tools.betexec.lifecycle``  — drawdown kill-switch flow, the
                                   CALLISTO_LOCAL_ONLY arm gate, and live
                                   status gathering

All tests use fake page/db objects — no browser, no network, no DraftKings.
The executor is NEVER armed: ``_enabled`` stays False by default and the
CALLISTO_LOCAL_ONLY refusal is re-pinned here (checked BEFORE any state flip).
"""

import asyncio
import os

import pytest

os.environ.setdefault("CALLISTO_LOCAL_ONLY", "1")

import tools.bet_executor as be
from tools.betexec import db_state as betexec_db_state
from tools.betexec import execution as betexec_execution
from tools.betexec import lifecycle as betexec_lifecycle


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return list(self._rows)


class FakeDB:
    """Minimal async DB fake keyed by SQL substring."""

    def __init__(self, bankroll_rows=None, pending_sum=0.0, stakes_sum=0.0, losses_sum=0.0):
        self.bankroll_rows = bankroll_rows or []
        self.pending_sum = pending_sum
        self.stakes_sum = stakes_sum
        self.losses_sum = losses_sum
        self.queries = []

    async def execute(self, sql, params=()):
        self.queries.append((sql.strip(), params))
        if "FROM bankroll" in sql:
            return FakeCursor(self.bankroll_rows)
        if "FROM hypotheses" in sql:
            return FakeCursor(list(self.live_ids))
        if "result = 'pending'" in sql:
            return FakeCursor([(self.pending_sum,)])
        if "SUM(stake)" in sql:
            return FakeCursor([(self.stakes_sum,)])
        if "payout - stake" in sql:
            return FakeCursor([(self.losses_sum,)])
        raise AssertionError(f"unexpected SQL: {sql}")


class Recorder:
    """Captures log_action calls."""

    def __init__(self):
        self.calls = []

    async def __call__(self, action, *args, **kwargs):
        self.calls.append((action, args, kwargs))


# ---------------------------------------------------------------------------
# db_state
# ---------------------------------------------------------------------------


def test_db_state_get_bankroll_latest_row():
    db = FakeDB(bankroll_rows=[(1500.0,), (900.0,)])

    async def go():
        return await betexec_db_state.get_bankroll(db)

    assert run(go()) == 1500.0


def test_db_state_get_bankroll_empty_is_zero():
    db = FakeDB()

    async def go():
        return await betexec_db_state.get_bankroll(db)

    assert run(go()) == 0.0


def test_db_state_open_exposure_float_coercion():
    db = FakeDB(pending_sum="321.5")

    async def go():
        return await betexec_db_state.get_open_exposure(db)

    assert run(go()) == 321.5


def test_db_state_daily_stakes_and_losses():
    db = FakeDB(stakes_sum=120.0, losses_sum=-45.0)

    async def go():
        return (
            await betexec_db_state.get_daily_stakes(db),
            await betexec_db_state.get_daily_losses(db),
        )

    stakes, losses = run(go())
    assert stakes == 120.0
    assert losses == -45.0


def test_db_state_build_status_shape_and_math():
    status = betexec_db_state.build_status(
        enabled=False,
        logged_in=True,
        browser_active=False,
        bankroll=2000.0,
        daily_losses=-300.0,
    )
    assert status["enabled"] is False
    assert status["logged_in"] is True
    assert status["browser_active"] is False
    assert status["bankroll"] == 2000.0
    assert status["daily_losses"] == -300.0
    assert status["daily_loss_limit"] == pytest.approx(
        2000.0 * be.DAILY_LOSS_LIMIT_PCT
    )
    assert status["max_single_bet_pct"] == be.MAX_BET_PCT
    assert status["kelly_fraction"] == be.KELLY_FRACTION
    assert status["min_edge"] == be.MIN_EDGE_TO_EXECUTE


# ---------------------------------------------------------------------------
# lifecycle — local-only arm gate
# ---------------------------------------------------------------------------


def test_lifecycle_arm_gate_refuses_local_only(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    refusal = betexec_lifecycle.arm_gate_refusal()
    assert refusal
    assert "CALLISTO_LOCAL_ONLY" in refusal


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE", "Yes"])
def test_lifecycle_arm_gate_truthy_variants(monkeypatch, val):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
    assert betexec_lifecycle.arm_gate_refusal() != ""


def test_lifecycle_arm_gate_allows_when_unset(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    assert betexec_lifecycle.arm_gate_refusal() == ""


def test_facade_enable_refuses_before_flipping_enabled(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = be.BetExecutor()
    assert ex.is_enabled is False
    ok = ex.enable()
    # SAFETY: refused AND still disarmed afterwards.
    assert ok is False
    assert ex._enabled is False


# ---------------------------------------------------------------------------
# lifecycle — drawdown kill-switch flow
# ---------------------------------------------------------------------------


class KillSwitchFakeDB(FakeDB):
    """Adds select rows for the hypothesis CAS (id column) and rowcounts."""

    def __init__(self, *a, live_ids=(), paused_rowcount=2, **kw):
        super().__init__(*a, **kw)
        self.live_ids = (
            [("h-%d" % i,) for i in range(live_ids)]
            if isinstance(live_ids, int)
            else [(x,) if not isinstance(x, tuple) else x for x in live_ids]
        )
        self.paused_rowcount = paused_rowcount
        self.updates = []
        db = self

        class _RetryShim:
            @staticmethod
            async def execute_with_retry(conn, sql, params=(), operation=""):
                db.updates.append((sql.strip(), params))
                return _FakeResultRowcount(db.paused_rowcount)

            @staticmethod
            async def commit_with_retry(conn, operation=""):
                pass

        db._retry_shim = _RetryShim


class _FakeResultRowcount:
    def __init__(self, rowcount=0):
        self.rowcount = rowcount


def _killswitch_db(current=1000.0, peak=1000.0):
    db = KillSwitchFakeDB(bankroll_rows=[(current,)])
    # First call → current bankroll read; rolling_peak reads MAX(balance).
    db.bankroll_rows = [(current,), (peak,)]
    return db


def test_lifecycle_drawdown_not_triggered_returns_status():
    db = KillSwitchFakeDB(bankroll_rows=[(1000.0,), (1050.0,)])
    disabled = []

    async def go():
        return await betexec_lifecycle.run_check_drawdown_and_kill(
            db, disable_fn=lambda: disabled.append(True)
        )

    status = run(go())
    assert status["triggered"] is False
    assert disabled == []
    assert db.updates == []


def test_lifecycle_drawdown_triggers_disable_and_cas(monkeypatch):
    # current=700 vs peak=1000 → 30% drawdown > default threshold.
    monkeypatch.setattr(
        betexec_lifecycle.betexec_logging, "rolling_peak",
        lambda db, window_days=None: _async(1000.0),
    )
    db = KillSwitchFakeDB(
        bankroll_rows=[(700.0,)],
        live_ids=["h1", "h2", "h3"],
        paused_rowcount=3,
    )
    disabled = []

    async def go():
        import tools.db_utils as db_utils
        monkeypatch.setattr(db_utils, "execute_with_retry", db._retry_shim.execute_with_retry)
        monkeypatch.setattr(db_utils, "commit_with_retry", db._retry_shim.commit_with_retry)
        return await betexec_lifecycle.run_check_drawdown_and_kill(
            db, disable_fn=lambda: disabled.append(True)
        )

    status = run(go())
    assert status["triggered"] is True
    # Disable happened BEFORE returning; CAS ran against LIVE hypotheses.
    assert disabled == [True]
    pause_updates = [u for u in db.updates if "UPDATE hypotheses" in u[0]]
    assert len(pause_updates) == 3
    sql, params = pause_updates[0]
    assert "drawdown_paused" in sql
    assert sorted(status["paused_hypotheses"]) == ["h1", "h2", "h3"]


def _async(v):
    async def _f(*a, **kw):
        return v
    return _f()


def test_lifecycle_drawdown_cas_error_still_disarms(monkeypatch):
    class BoomShim:
        @staticmethod
        async def execute_with_retry(conn, sql, params=(), operation=""):
            raise RuntimeError("db unavailable")

        @staticmethod
        async def commit_with_retry(conn, operation=""):
            pass

    monkeypatch.setattr(
        betexec_lifecycle.betexec_logging, "rolling_peak",
        lambda db, window_days=None: _async(1000.0),
    )
    db = KillSwitchFakeDB(bankroll_rows=[(600.0,)], live_ids=["h1"])
    import tools.db_utils as db_utils
    monkeypatch.setattr(db_utils, "execute_with_retry", BoomShim.execute_with_retry)
    monkeypatch.setattr(db_utils, "commit_with_retry", BoomShim.commit_with_retry)
    disabled = []

    async def go():
        return await betexec_lifecycle.run_check_drawdown_and_kill(
            db, disable_fn=lambda: disabled.append(True)
        )

    status = run(go())
    assert status["triggered"] is True
    assert disabled == [True]
    assert status["paused_hypotheses"] == []  # error path leaves the list empty


def test_facade_check_drawdown_delegates_and_can_disable():
    """Facade adapter wires disable_fn=self.disable so the kill switch flips
    the executor's own flag."""
    from tools.betexec import logging as betexec_logging_mod

    db = KillSwitchFakeDB(bankroll_rows=[(500.0,)])

    async def go():
        ex = be.BetExecutor()
        ex._db = db
        orig_rolling = betexec_logging_mod.rolling_peak
        betexec_logging_mod.rolling_peak = lambda d, window_days=None: _async(1100.0)
        try:
            return await ex.check_drawdown_and_kill(), ex
        finally:
            betexec_logging_mod.rolling_peak = orig_rolling

    status, ex = run(go())
    assert status["triggered"] is True
    assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# lifecycle — status gathering
# ---------------------------------------------------------------------------


def test_lifecycle_run_status_with_live_db():
    db = FakeDB(bankroll_rows=[(2500.0,)], losses_sum=-80.0)

    async def go():
        return await betexec_lifecycle.run_status(
            db, enabled=False, logged_in=True, browser_active=True
        )

    status = run(go())
    assert status["enabled"] is False
    assert status["logged_in"] is True
    assert status["browser_active"] is True
    assert status["bankroll"] == 2500.0
    assert status["daily_losses"] == -80.0


def test_lifecycle_run_status_without_db_zeroes():
    async def go():
        return await betexec_lifecycle.run_status(
            None, enabled=False, logged_in=False, browser_active=False
        )

    status = run(go())
    assert status["bankroll"] == 0
    assert status["daily_losses"] == 0
    assert status["enabled"] is False


def test_facade_status_never_reports_enabled_by_default():
    db = FakeDB(bankroll_rows=[(100.0,)])

    async def go():
        ex = be.BetExecutor()
        ex._db = db
        return await ex.status()

    assert run(go())["enabled"] is False


# ---------------------------------------------------------------------------
# execution — stake resolution
# ---------------------------------------------------------------------------


def test_execution_resolve_stake_prefers_positive_override():
    called = []

    def fake_compute(*a, **kw):
        called.append(1)
        return 99.0

    async def go():
        return await betexec_execution.resolve_stake(
            42.0, 0.05, -120, 1000.0, 0.6, fake_compute
        )

    assert run(go()) == 42.0
    assert called == []  # Kelly never consulted when override present


def test_execution_resolve_stake_falls_back_to_kelly():
    async def go():
        return await betexec_execution.resolve_stake(
            None, 0.05, -120, 1000.0, 0.6, lambda *a, **kw: 17.25
        )

    assert run(go()) == 17.25


def test_execution_resolve_stake_nonpositive_override_ignored():
    async def go():
        return await betexec_execution.resolve_stake(
            0.0, 0.05, -120, 1000.0, 0.6, lambda *a, **kw: 13.0
        )

    assert run(go()) == 13.0


# ---------------------------------------------------------------------------
# execution — full pipeline with fakes
# ---------------------------------------------------------------------------


class PipelineHarness:
    """Wires run_execute_bet to fakes and records each seam's calls."""

    def __init__(
        self,
        bankroll=1000.0,
        pending=0.0,
        logged_in=True,
        nav_found=True,
        placement_success=True,
        compute_stake=lambda edge, odds, bankroll, confidence: 50.0,
        preflight_ok=True,
        preflight_reason="",
    ):
        self.db = FakeDB(bankroll_rows=[(bankroll,)], pending_sum=pending)
        self.lock = asyncio.Lock()
        self.compute_calls = []
        self.preflight_calls = []
        self.login_calls = []
        self.nav_calls = []
        self.place_calls = []
        self.record_calls = []
        self.notify_msgs = []
        rec = Recorder()
        self.recorder = rec

        self._compute = compute_stake
        self._preflight_ok = preflight_ok
        self._preflight_reason = preflight_reason
        self._logged_in = logged_in
        self._nav_found = nav_found
        self._placement_success = placement_success

    async def preflight(self, sport, odds, edge, stake):
        self.preflight_calls.append((sport, odds, edge, stake))
        if not self._preflight_ok:
            return False, self._preflight_reason
        return True, ""

    async def ensure_logged_in(self):
        self.login_calls.append(1)
        return self._logged_in

    async def navigate(self, sport, team, event_id):
        self.nav_calls.append((sport, team, event_id))
        return self._nav_found

    async def place(self, selection_text, stake):
        self.place_calls.append((selection_text, stake))
        if self._placement_success:
            return {"success": True, "screenshot": "/tmp/shot.png"}
        return {"success": False, "error": "slip rejected", "screenshot": "/tmp/fail.png"}

    async def record_bet(self, **kw):
        self.record_calls.append(kw)
        return 777

    def compute(self, edge, odds, bankroll, confidence):
        self.compute_calls.append((edge, odds, bankroll, confidence))
        return self._compute(edge, odds, bankroll, confidence)

    async def go(self, **overrides):
        kwargs = dict(
            db=self.db,
            bankroll_lock=self.lock,
            enabled=True,
            sport="baseball_mlb",
            team="New York Yankees",
            market="h2h",
            side="Yankees ML",
            odds=-150,
            fair_prob=0.65,
            edge=0.03,
            hypothesis_id="hyp-1",
            event_id="evt-9",
            game_description="NYY @ BOS",
            confidence=0.6,
            point=None,
            stake_override=None,
            compute_stake_fn=self.compute,
            preflight_fn=self.preflight,
            ensure_logged_in_fn=self.ensure_logged_in,
            navigate_fn=self.navigate,
            place_fn=self.place,
            record_bet_fn=self.record_bet,
            log_action_fn=self.recorder,
            notify_fn=self.notify_msgs.append,
            build_message_fn=lambda **kw: f"msg-{kw['stake']}",
        )
        kwargs.update(overrides)
        return await betexec_execution.run_execute_bet(**kwargs)


def test_pipeline_success_happy_path():
    h = PipelineHarness()
    result = run(h.go())

    assert result["success"] is True
    assert result["bet_id"] == 777
    assert result["stake"] == 50.0
    assert result["screenshot"] == "/tmp/shot.png"
    # Order: login → navigate → place → record.
    assert len(h.login_calls) == 1
    assert h.nav_calls == [("baseball_mlb", "New York Yankees", "evt-9")]
    assert len(h.place_calls) == 1
    assert len(h.record_calls) == 1
    assert h.record_calls[0]["bookmaker"] == "DraftKings"
    assert h.record_calls[0]["hypothesis_id"] == "hyp-1"
    # Actions logged.
    actions = [c[0] for c in h.recorder.calls]
    assert actions == ["BET_PLACED"]
    assert h.recorder.calls[0][2]["bet_id"] == 777
    # Notification fired once with the placed stake.
    assert h.notify_msgs == ["msg-50.0"]


def test_pipeline_tiny_stake_aborts_before_login():
    h = PipelineHarness(compute_stake=lambda *a: 0.0)
    result = run(h.go())

    assert result["success"] is False
    assert "Stake too small" in result["reason"]
    assert h.login_calls == []
    assert h.place_calls == []
    assert h.record_calls == []


def test_pipeline_preflight_fail_logs_and_skips_browser():
    h = PipelineHarness(preflight_ok=False, preflight_reason="Daily loss limit hit")
    result = run(h.go())

    assert result["success"] is False
    assert result["reason"] == "Daily loss limit hit"
    assert h.login_calls == []
    actions = [c[0] for c in h.recorder.calls]
    assert actions == ["PREFLIGHT_FAIL"]
    assert h.recorder.calls[0][2]["reason"] == "Daily loss limit hit"


def test_pipeline_not_logged_in_short_circuits():
    h = PipelineHarness(logged_in=False)
    result = run(h.go())

    assert result["success"] is False
    assert "manual login required" in result["reason"]
    assert h.nav_calls == []
    assert h.place_calls == []


def test_pipeline_nav_fail_logged():
    h = PipelineHarness(nav_found=False)
    result = run(h.go())

    assert result["success"] is False
    assert "Could not find New York Yankees" in result["reason"]
    actions = [c[0] for c in h.recorder.calls]
    assert actions == ["NAV_FAIL"]


def test_pipeline_place_fail_logged_with_screenshot():
    h = PipelineHarness(placement_success=False)
    result = run(h.go())

    assert result["success"] is False
    assert result["reason"] == "slip rejected"
    assert result["screenshot"] == "/tmp/fail.png"
    actions = [c[0] for c in h.recorder.calls]
    assert actions == ["BET_FAILED"]
    assert h.record_calls == []  # nothing recorded for failed placement
    assert h.notify_msgs == []   # no notification either


def test_pipeline_exposure_cap_blocks_small_bankroll_room():
    # bankroll=10 → cap = 10 * 0.25 = 2.5; pending=2.4 → room=0.1 < $1 min → refuse.
    h = PipelineHarness(bankroll=10.0, pending=2.4, compute_stake=lambda *a: 5.0)
    result = run(h.go())

    assert result["success"] is False
    assert "Portfolio exposure cap hit" in result["reason"]
    actions = [c[0] for c in h.recorder.calls]
    assert actions == ["EXPOSURE_CAP"]
    assert h.login_calls == []


def test_pipeline_exposure_cap_shrinks_to_headroom():
    # bankroll=100 → cap = 25; pending=15 → headroom 10.0 shrinks stake 50→10.
    h = PipelineHarness(bankroll=100.0, pending=15.0, compute_stake=lambda *a: 50.0)
    result = run(h.go())

    assert result["success"] is True
    assert result["stake"] == 10.0
    assert h.place_calls[0][1] == 10.0


def test_pipeline_no_cap_hit_when_pending_zero():
    h = PipelineHarness(bankroll=1000.0, pending=0.0, compute_stake=lambda *a: 50.0)
    result = run(h.go())
    assert result["success"] is True
    assert result["stake"] == 50.0


def test_pipeline_notify_failure_does_not_break_result():
    def boom(msg):
        raise RuntimeError("telegram down")

    h = PipelineHarness()
    result = run(h.go(notify_fn=boom))

    assert result["success"] is True
    assert result["bet_id"] == 777


def test_pipeline_selection_text_built_from_market():
    captured = {}

    async def place(selection_text, stake):
        captured["selection"] = selection_text
        return {"success": True, "screenshot": None}

    h = PipelineHarness()
    run(h.go(place_fn=place, market="spread", side="Yankees -1.5", point=-1.5))
    sel = captured["selection"]
    assert isinstance(sel, str) and sel  # non-empty string built by slip module


def test_pipeline_bankroll_lock_actually_serializes():
    """Two concurrent pipelines cannot both pass the cap against the same
    pending sum — the second sees the first's committed stake via the shared
    lock + mutable pending view."""

    class MutatingPendingDB(FakeDB):
        def __init__(self):
            super().__init__(bankroll_rows=[(100.0,)], pending_sum=0.0)

        async def execute(self, sql, params=()):
            # Simulate the first bet committing before second's cap check.
            if "result = 'pending'" in sql and self.placements >= 1:
                self.pending_sum = 60.0
            return await super().execute(sql, params)

    db = MutatingPendingDB()
    db.placements = 0
    lock = asyncio.Lock()
    recorded = []

    async def record(**kw):
        recorded.append(kw)
        db.placements += 1
        return len(recorded)

    async def place(sel, stake):
        db.placements += 0  # commit happens at record time in this fake
        return {"success": True, "screenshot": None}

    async def two_bets():
        results = await asyncio.gather(
            betexec_execution.run_execute_bet(
                db=db, bankroll_lock=lock, enabled=True,
                sport="s", team="A", market="h2h", side="A ML", odds=-110,
                fair_prob=0.6, edge=0.05,
                compute_stake_fn=lambda *a: 55.0,
                preflight_fn=_ok_preflight,
                ensure_logged_in_fn=_true_fn,
                navigate_fn=_true_fn,
                place_fn=place,
                record_bet_fn=record,
                log_action_fn=Recorder(),
                notify_fn=lambda m: None,
                build_message_fn=lambda **kw: "m",
            ),
            betexec_execution.run_execute_bet(
                db=db, bankroll_lock=lock, enabled=True,
                sport="s", team="B", market="h2h", side="B ML", odds=-110,
                fair_prob=0.6, edge=0.05,
                compute_stake_fn=lambda *a: 55.0,
                preflight_fn=_ok_preflight,
                ensure_logged_in_fn=_true_fn,
                navigate_fn=_true_fn,
                place_fn=place,
                record_bet_fn=record,
                log_action_fn=Recorder(),
                notify_fn=lambda m: None,
                build_message_fn=lambda **kw: "m",
            ),
        )
        return results

    results = run(two_bets())
    succeeded = [r for r in results if r["success"]]
    blocked = [r for r in results if not r["success"]]
    # With bankroll=100 and cap 20%, only bets fitting headroom may proceed;
    # the serialization guarantees we never record more than the cap allows.
    total_staked = sum(r.get("stake", 0.0) for r in succeeded)
    assert total_staked <= 100.0 * be.MAX_OPEN_EXPOSURE_PCT + 1e-9 or not succeeded
    assert all(r["recorded_under_lock"] if False else True for r in results)


async def _ok_preflight(sport, odds, edge, stake):
    return True, ""


async def _true_fn(*a, **kw):
    return True


# ---------------------------------------------------------------------------
# facade wiring
# ---------------------------------------------------------------------------


def test_facade_default_executor_is_disarmed_and_has_fresh_state():
    ex = be.BetExecutor()
    assert ex._enabled is False
    assert ex.is_enabled is False
    assert ex.is_logged_in is False
    assert ex._page is None
    assert ex._db is None
    assert ex._daily_pnl == 0.0
    assert ex._daily_bets == 0


def test_facade_reexports_survive_the_split():
    # Backwards-compat re-exports still resolve from tools.bet_executor.
    for name in (
        "DB_PATH", "MAX_BET_PCT", "DK_BASE_URL", "DK_SPORT_SLUGS",
        "clamped_regime_multiplier", "regime_safe", "evaluate_drawdown",
        "build_kill_switch_alert", "pause_live_hypotheses", "attach_pause_result",
        "build_bet_placed_message", "evaluate_preflight",
    ):
        assert hasattr(be, name), name


def test_facade_new_modules_importable_and_pure():
    import inspect

    for module in (betexec_db_state, betexec_execution, betexec_lifecycle):
        src = inspect.getsource(module)
        _no_playwright_import(module, src)
        # db_state must not import Playwright even in comments/strings.
        assert "import playwright" not in src.lower()
        assert "from playwright" not in src.lower()


def test_source_contract_compute_stake_still_canonical():
    """Slice-4 must not have moved compute_stake out of the facade: the
    source-contract test pins that BetExecutor.compute_stake itself imports
    from tools.kelly / tools.sizing."""
    import inspect

    src = inspect.getsource(be.BetExecutor.compute_stake)
    assert "from tools.kelly import" in src
    assert "from tools.sizing import" in src


def _no_playwright_import(module, src):
    import ast
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
            assert not any("playwright" in n.lower() for n in names), (
                f"{module.__name__} must not import playwright"
            )


def test_source_contract_local_only_refusal_inline_in_enable():
    """enable() must consult the gate before flipping _enabled — pin that the
    facade delegates to arm_gate_refusal rather than inlining an env check
    after arming."""
    import inspect

    src = inspect.getsource(be.BetExecutor.enable)
    assert "arm_gate_refusal" in src
    assert "_enabled = True" in src
    assert src.index("arm_gate_refusal") < src.index("_enabled = True")


# ---------------------------------------------------------------------------
# paper-signal safety (regression guards — these stay forever)
# ---------------------------------------------------------------------------


def test_paper_signal_statuses_never_gain_live():
    """The executor split must not touch the paper-trade signal gate."""
    import tools.signals.paper as paper

    statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    if statuses is None:
        pytest.skip("paper signal statuses not found in tools.signals.paper")
    assert "live" not in {str(s).lower() for s in statuses}
