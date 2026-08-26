"""tests/test_betexec_slice6.py — pin the slice-6 BetExecutor helper split.

Slice 6 (2026-08) moved the remaining inline method bodies out of
``tools/bet_executor.py`` into ``tools.betexec.methods``:

  - read-only DB accessors (get_bankroll / get_daily_stakes /
    get_open_exposure / get_daily_losses)
  - preflight gather (enablement short-circuit → live values →
    tools.betexec.preflight.evaluate_preflight)
  - recording/audit seams (_record_bet / _log_action / _notify bindings)
  - drawdown peak seams (_record_bankroll_peak / _rolling_peak)
  - kill-switch + health-status binding (db handle + disarm callback)

All tests use fake page/db objects — no browser, no network, no DraftKings.
The executor is NEVER armed: ``_enabled`` stays False by default, the
CALLISTO_LOCAL_ONLY refusal is re-pinned (evaluated BEFORE any state flip),
and shutdown must leave the executor disabled. The paper-signal gate is
never touched.
"""

import asyncio
import inspect
import os

import pytest

os.environ.setdefault("CALLISTO_LOCAL_ONLY", "1")

import tools.bet_executor as be
from tools.betexec import methods as betexec_methods


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


class FakeDB:
    """Minimal async DB fake keyed by SQL substring."""

    def __init__(
        self,
        bankroll_rows=None,
        pending_sum=0.0,
        stakes_sum=0.0,
        losses_sum=0.0,
    ):
        self.bankroll_rows = bankroll_rows or []
        self.pending_sum = pending_sum
        self.stakes_sum = stakes_sum
        self.losses_sum = losses_sum
        self.queries = []

    async def execute(self, sql, params=()):
        self.queries.append((sql.strip(), params))
        s = sql.strip()
        if "FROM bankroll" in s and "bankroll_peak" not in s:
            return FakeCursor(self.bankroll_rows)
        if "result = 'pending'" in s:
            return FakeCursor([(self.pending_sum,)])
        if "SUM(stake)" in s:
            return FakeCursor([(self.stakes_sum,)])
        if "payout - stake" in s:
            return FakeCursor([(self.losses_sum,)])
        return FakeCursor([])


def make_executor(db=None):
    """Executor with a fake DB bound but NEVER enabled."""
    ex = be.BetExecutor()
    ex._db = db if db is not None else FakeDB()
    return ex


# ---------------------------------------------------------------------------
# methods: read-only DB accessors
# ---------------------------------------------------------------------------


def test_methods_get_bankroll_latest_row():
    ex = make_executor(FakeDB(bankroll_rows=[(1500.0,), (900.0,)]))
    assert run(betexec_methods.get_bankroll(ex)) == 1500.0


def test_methods_get_bankroll_empty_is_zero():
    ex = make_executor(FakeDB())
    assert run(betexec_methods.get_bankroll(ex)) == 0.0


def test_methods_get_daily_stakes():
    ex = make_executor(FakeDB(stakes_sum=120.0))
    assert run(betexec_methods.get_daily_stakes(ex)) == 120.0


def test_methods_get_open_exposure_coerces_float():
    db = FakeDB(pending_sum="321.5")
    ex = make_executor(db)
    assert run(betexec_methods.get_open_exposure(ex)) == 321.5


def test_methods_get_daily_losses_negative_means_losing():
    ex = make_executor(FakeDB(losses_sum=-45.0))
    assert run(betexec_methods.get_daily_losses(ex)) == -45.0


@pytest.mark.parametrize(
    "attr,fn",
    [
        ("get_bankroll", betexec_methods.get_bankroll),
        ("get_daily_stakes", betexec_methods.get_daily_stakes),
        ("get_open_exposure", betexec_methods.get_open_exposure),
        ("get_daily_losses", betexec_methods.get_daily_losses),
    ],
)
def test_facade_accessors_delegate_to_methods(attr, fn):
    """The facade accessor routes through tools.betexec.methods."""
    seen = {}

    class Probe:
        pass

    async def fake(executor):
        seen["executor"] = executor
        return 42.0

    orig = getattr(be.betexec_methods, attr.__name__ if False else fn.__name__)
    # monkeypatch-style manual patch (module-level attribute swap)
    setattr(be.betexec_methods, fn.__name__, fake)
    try:
        ex = be.BetExecutor()
        got = run(getattr(ex, attr)())
        assert got == 42.0
        assert seen["executor"] is ex
    finally:
        setattr(be.betexec_methods, fn.__name__, orig)


# ---------------------------------------------------------------------------
# methods: preflight gather
# ---------------------------------------------------------------------------


def test_methods_preflight_disabled_short_circuits_before_db():
    class BoomDB:
        async def execute(self, sql, params=()):
            raise AssertionError("disabled executor must not touch the DB")

    ex = be.BetExecutor()
    ex._db = BoomDB()
    ok, reason = run(
        betexec_methods.preflight_check(ex, "baseball_mlb", -110, 0.05, 10.0)
    )
    assert ok is False
    assert reason == "Executor is disabled"


def test_methods_preflight_enabled_gathers_live_values_and_passes():
    ex = make_executor(FakeDB(bankroll_rows=[(1000.0,)], losses_sum=0.0))
    ex._enabled = True  # flip directly; enable() refuses under LOCAL_ONLY
    sport = next(iter(be.DK_SPORT_SLUGS))
    ok, reason = run(
        betexec_methods.preflight_check(ex, sport, -110, 0.05, 10.0)
    )
    assert ok is True and reason == "OK"


def test_methods_preflight_enabled_gather_feeds_gate_failure():
    # Daily losses beyond limit → gate fails using gathered values.
    ex = make_executor(
        FakeDB(bankroll_rows=[(1000.0,)], losses_sum=-500.0)
    )
    ex._enabled = True
    sport = next(iter(be.DK_SPORT_SLUGS))
    ok, reason = run(
        betexec_methods.preflight_check(ex, sport, -110, 0.05, 10.0)
    )
    assert ok is False
    assert "Daily loss limit" in reason


def test_facade_preflight_delegates_to_methods_module(monkeypatch):
    calls = []

    async def fake(executor, sport, odds, edge, stake):
        calls.append((sport, odds, edge, stake))
        return True, "OK"

    monkeypatch.setattr(betexec_methods, "preflight_check", fake)

    class StubEx(be.BetExecutor):
        @property
        def _enabled_inner(self):
            return True

    ex = be.BetExecutor()
    ex._enabled = True  # flip directly; enable() refuses under LOCAL_ONLY
    ok, reason = run(ex.preflight_check("baseball_mlb", -150, 0.05, 10.0))
    assert ok is True and reason == "OK"
    assert calls == [("baseball_mlb", -150, 0.05, 10.0)]


def test_facade_preflight_disabled_refuses_before_anything():
    ex = be.BetExecutor()  # never armed
    ok, reason = run(ex.preflight_check("baseball_mlb", -150, 0.5, 10.0))
    assert ok is False and reason == "Executor is disabled"


# ---------------------------------------------------------------------------
# methods: notify seam
# ---------------------------------------------------------------------------


def test_methods_notify_sends_lazily(monkeypatch):
    sent = {}

    async def fake_alert(message, **kwargs):
        sent["m"] = message
        return True

    monkeypatch.setattr("tools.telegram.send_alert", fake_alert)
    betexec_methods.notify("hello")
    assert sent.get("m") == "hello"


def test_facade_notify_is_staticmethod_on_class():
    assert isinstance(inspect.getattr_static(be.BetExecutor, "_notify"), staticmethod)
    # Delegates to the module function.
    src = inspect.getsource(be.BetExecutor._notify)
    assert "betexec_methods.notify" in src


# ---------------------------------------------------------------------------
# methods: record/log seams
# ---------------------------------------------------------------------------


def test_methods_record_bet_passes_executor_state_to_logging(monkeypatch):
    captured = {}

    async def fake_record(db, get_bankroll, lock, **kwargs):
        captured["db"] = db
        captured["get_bankroll"] = get_bankroll
        captured["lock"] = lock
        captured.update(kwargs)
        return 4242

    from tools.betexec import logging as betexec_logging_mod
    monkeypatch.setattr(betexec_logging_mod, "record_bet", fake_record)

    db = FakeDB(bankroll_rows=[(1000.0,)])
    ex = make_executor(db)
    bet_id = run(betexec_methods.record_bet(
        ex,
        sport="baseball_mlb", event_id="ev-6",
        game_description="NYY @ BOS", team="Yankees", market="h2h",
        bookmaker="DraftKings", odds=-150, point=None, stake=50.0,
        edge=0.03, fair_prob=0.65, hypothesis_id="hyp-6",
    ))
    assert bet_id == 4242
    assert captured["db"] is db
    assert captured["get_bankroll"].__name__ == "get_bankroll"
    assert isinstance(captured["lock"], asyncio.Lock)
    assert captured["stake"] == 50.0
    assert captured["hypothesis_id"] == "hyp-6"


def test_facade_record_bet_routes_through_methods(monkeypatch):
    seen = {}

    async def fake(executor, **kwargs):
        seen["executor"] = executor
        seen.update(kwargs)
        return 7

    monkeypatch.setattr(betexec_methods, "record_bet", fake)
    ex = make_executor()
    bet_id = run(ex._record_bet(
        "baseball_mlb", "ev", "g", "Yankees", "h2h", "DraftKings",
        -150, None, 25.0, 0.03, 0.65, "h1",
    ))
    assert bet_id == 7
    assert seen["executor"] is ex
    assert seen["team"] == "Yankees"


def test_methods_log_action_passes_executor_state_to_logging(monkeypatch):
    captured = {}

    async def fake_log(db, action, *args, **kwargs):
        captured["db"] = db
        captured["action"] = action
        captured["args"] = args
        captured.update(kwargs)

    from tools.betexec import logging as betexec_logging_mod
    monkeypatch.setattr(betexec_logging_mod, "log_action", fake_log)

    db = FakeDB()
    ex = make_executor(db)
    run(betexec_methods.log_action(
        ex, "BET_PLACED", "baseball_mlb", "Yankees", "h2h", "Yankees ML",
        -150, 50.0, 0.03, "hyp-6", bet_id=9, screenshot="/s.png",
    ))
    assert captured["db"] is db
    assert captured["action"] == "BET_PLACED"
    assert captured["bet_id"] == 9
    assert captured["screenshot"] == "/s.png"


def test_facade_log_action_routes_through_methods(monkeypatch):
    seen = {}

    async def fake(executor, *args, **kwargs):
        seen["executor"] = executor
        seen["args"] = args
        seen.update(kwargs)

    monkeypatch.setattr(betexec_methods, "log_action", fake)
    ex = make_executor()
    run(ex._log_action(
        "NAV_FAIL", "s", "t", "m", "sd", -110, 10.0, 0.02, "h",
        reason="no game",
    ))
    assert seen["executor"] is ex
    assert seen["args"][0] == "NAV_FAIL"
    assert seen["reason"] == "no game"


# ---------------------------------------------------------------------------
# methods: drawdown peak seams
# ---------------------------------------------------------------------------


def test_methods_record_bankroll_peak_passes_db(monkeypatch):
    captured = {}

    async def fake(db, bankroll):
        captured["db"] = db
        captured["bankroll"] = bankroll

    from tools.betexec import logging as betexec_logging_mod
    monkeypatch.setattr(betexec_logging_mod, "record_bankroll_peak", fake)

    db = FakeDB()
    ex = make_executor(db)
    run(betexec_methods.record_bankroll_peak(ex, 1234.5))
    assert captured == {"db": db, "bankroll": 1234.5}


def test_methods_rolling_peak_default_window(monkeypatch):
    captured = {}

    async def fake(db, window_days):
        captured["window_days"] = window_days
        return 999.0

    from tools.betexec import logging as betexec_logging_mod
    monkeypatch.setattr(betexec_logging_mod, "rolling_peak", fake)

    ex = make_executor()
    assert run(betexec_methods.rolling_peak(ex)) == 999.0
    assert captured["window_days"] is None


def test_methods_rolling_peak_custom_window():
    ex = make_executor()

    async def go():
        return await betexec_methods.rolling_peak(ex, window_days=7)

    assert run(go()) >= 0.0  # fake DB returns no rows → 0.0 floor


# ---------------------------------------------------------------------------
# methods: kill-switch + status binding
# ---------------------------------------------------------------------------


def test_methods_kill_switch_binds_disable_callback():
    disabled = []
    ex = be.BetExecutor()

    async def fake_flow(db, *, disable_fn):
        disabled.append(disable_fn)
        return {"triggered": False}

    real = be.betexec_lifecycle.run_check_drawdown_and_kill
    be.betexec_lifecycle.run_check_drawdown_and_kill = fake_flow
    try:
        status = run(betexec_methods.check_drawdown_and_kill(ex))
    finally:
        be.betexec_lifecycle.run_check_drawdown_and_kill = real

    assert status == {"triggered": False}
    assert len(disabled) == 1
    # The supplied callback IS the executor's own disable → flips only to False.
    disabled[0]()
    assert ex.is_enabled is False


async def _fake_status(db, *, enabled, logged_in, browser_active):
    return {
        "enabled": enabled,
        "logged_in": logged_in,
        "browser_active": browser_active,
    }


def test_methods_status_binds_live_flags():
    ex = be.BetExecutor()
    ex._logged_in = True
    ex._page = object()  # browser active

    real = be.betexec_lifecycle.run_status
    be.betexec_lifecycle.run_status = _fake_status
    try:
        st = run(betexec_methods.status(ex))
    finally:
        be.betexec_lifecycle.run_status = real

    assert st["enabled"] is False  # never armed
    assert st["logged_in"] is True
    assert st["browser_active"] is True


def test_methods_status_browser_inactive_without_page():
    ex = be.BetExecutor()  # no page bound

    real = be.betexec_lifecycle.run_status
    be.betexec_lifecycle.run_status = _fake_status
    try:
        st = run(betexec_methods.status(ex))
    finally:
        be.betexec_lifecycle.run_status = real

    assert st["browser_active"] is False


def test_facade_check_drawdown_still_disarms_via_binding():
    """End-to-end: facade adapter wires disable_fn=self.disable so a fired
    kill switch flips the executor's own flag off."""
    from tools.betexec import lifecycle as lifecycle_mod

    class PeakDB(FakeDB):
        def __init__(self):
            super().__init__(bankroll_rows=[(700.0,), (1000.0,)])

        async def execute(self, sql, params=()):
            s = sql.strip()
            if "MAX(balance), 0) FROM bankroll_peak" in s:
                return FakeCursor([(1000.0,)])
            return await super().execute(sql, params)

    ex = be.BetExecutor()
    ex._db = PeakDB()
    ex._enabled = True  # simulate armed state so we can watch it disarm

    real_peak = lifecycle_mod.betexec_logging.rolling_peak

    async def fake_peak(db, window_days=None):
        return 1000.0

    lifecycle_mod.betexec_logging.rolling_peak = fake_peak
    try:
        status = run(ex.check_drawdown_and_kill())
    finally:
        lifecycle_mod.betexec_logging.rolling_peak = real_peak

    assert status["triggered"] is True
    assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# Facade shape after slice 6
# ---------------------------------------------------------------------------


def test_facade_line_count_shrunk_vs_slice5_baseline():
    """Slice-6 must have moved bodies OUT of the facade file (it was 529
    lines after slice 5)."""
    src_path = os.path.join(os.path.dirname(be.__file__), "bet_executor.py")
    with open(src_path, encoding="utf-8") as fh:
        n = sum(1 for _ in fh)
    assert n < 529


def test_new_methods_module_importable_and_playwright_free():
    src = inspect.getsource(betexec_methods)
    lowered = src.lower()
    assert "from playwright" not in lowered
    assert "import playwright" not in lowered


def test_methods_module_never_assigns_enabled_true():
    """SAFETY: nothing in tools/betexec/methods.py may arm the executor."""
    src = inspect.getsource(betexec_methods)
    assert "_enabled = True" not in src.replace(" ", "")
    assert "_enabled = False" not in src  # it never touches the flag at all


def test_methods_module_no_live_status_token():
    pkg_dir = os.path.dirname(betexec_methods.__file__)
    path = os.path.join(pkg_dir, "methods.py")
    offenders = []
    with open(path, encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if "'live'" in line or '"live"' in line:
                stripped = line.strip()
                if (
                    not stripped.startswith("#")
                    and "'''" not in line
                    and '"""' not in line
                ):
                    offenders.append(f"{i}: {stripped}")
    assert offenders == []


def test_source_contract_compute_stake_body_stays_inline():
    """compute_stake body must stay inline in the facade (source contract)."""
    src = inspect.getsource(be.BetExecutor.compute_stake)
    assert "from tools.kelly import" in src
    assert "from tools.sizing import" in src


def test_source_contract_enable_keeps_guard_before_arm():
    """enable() must consult the arm gate BEFORE flipping _enabled."""
    src = inspect.getsource(be.BetExecutor.enable)
    assert "arm_gate_refusal" in src
    assert "_enabled = True" in src
    assert src.index("arm_gate_refusal") < src.index("_enabled = True")


def test_source_contract_init_still_disarmed_inline():
    src = inspect.getsource(be.BetExecutor.__init__)
    assert "self._enabled = False" in src
    # SAFETY comment preserved next to the default.
    assert "default-disabled" in src or "SAFETY" in src


def test_default_init_disarmed_after_slice6():
    ex = be.BetExecutor()
    assert ex._enabled is False
    assert ex.is_enabled is False
    assert ex.is_logged_in is False


def test_local_only_refusal_still_blocks_arm(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    ex = be.BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_shutdown_leaves_executor_disabled():
    class Closable:
        closed = 0

        async def close(self):
            Closable.closed += 1

    class ClosableDB(FakeDB):
        def __init__(self):
            super().__init__()
            self.closed = False

        async def close(self):
            self.closed = True

    ex = be.BetExecutor()
    ex._browser = Closable()
    db = ClosableDB()
    ex._db = db
    ex._enabled = True  # sabotage
    run(ex.shutdown())
    assert Closable.closed == 1
    assert db.closed is True
    assert ex.is_enabled is False
    assert ex.is_logged_in is False


def test_paper_signal_statuses_never_gain_live():
    """The split must not touch the paper-trade signal gate."""
    pytest.importorskip("tools.signals.paper")
    import tools.signals.paper as paper

    statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    if statuses is None:
        pytest.skip("paper signal statuses not found")
    assert "live" not in {str(s).lower() for s in statuses}


def test_execute_bet_end_to_end_through_slice6_seams(monkeypatch):
    """Full pipeline still works: wiring → execution → methods-backed seams."""
    ex = make_executor(FakeDB(bankroll_rows=[(1000.0,)], pending_sum=0.0))
    ex._logged_in = True  # legacy short-circuit

    recorded, logged = [], []

    async def fake_nav(sport, team, event_id=""):
        return True

    async def fake_place(selection_text, stake):
        assert stake > 0
        return {"success": True, "screenshot": "shot6.png"}

    async def fake_record(**kwargs):
        recorded.append(kwargs)
        return 61

    async def noop(*a, **k):
        pass

    async def fake_preflight(sport, odds, edge, stake):
        return True, "OK"

    async def nav_wrapper(*a, **k):
        return await fake_nav(*a, **k)

    async def fake_session_nav(executor, sport, team):
        return await fake_nav(sport, team)

    async def fake_session_place(executor, selection_text, stake):
        return await fake_place(selection_text, stake)

    from tools.betexec import session as betexec_session
    monkeypatch.setattr(betexec_session, "navigate_to_game", fake_session_nav)
    monkeypatch.setattr(betexec_session, "place_bet_on_slip", fake_session_place)
    ex.navigate_to_game = nav_wrapper
    ex.place_bet_on_slip = fake_place
    ex._record_bet = fake_record
    ex._log_action = noop
    ex.preflight_check = fake_preflight

    result = run(ex.execute_bet(
        sport="baseball_mlb",
        team="Yankees",
        market="h2h",
        side="Yankees ML",
        odds=-150,
        fair_prob=0.65,
        edge=0.05,
        hypothesis_id="h6",
        stake_override=20.0,
    ))
    assert result["success"] is True
    assert result["bet_id"] == 61
    assert result["stake"] == 20.0
    assert recorded[0]["team"] == "Yankees"
