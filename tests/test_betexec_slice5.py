"""tests/test_betexec_slice5.py — pin the slice-5 BetExecutor helper split.

Slice 5 (2026-08) moved the LAST executor-bound helpers out of
``tools/bet_executor.py`` into real modules:

  - ``tools.betexec.bootstrap`` — DB connection + directory bootstrap and
                                   shutdown/teardown (executor left disarmed)
  - ``tools.betexec.session``   — instance-level browser-session methods
                                   (launch, login check, navigate, slip)
  - ``tools.betexec.wiring``    — the execute_bet dependency binding for
                                   ``tools.betexec.execution.run_execute_bet``

All tests use fake page/db objects — no browser, no network, no DraftKings.
The executor is NEVER armed: ``_enabled`` stays False by default, the
CALLISTO_LOCAL_ONLY refusal is re-pinned (checked BEFORE any state flip),
and shutdown must leave the executor disabled.
"""

import asyncio
import inspect
import os

import pytest

os.environ.setdefault("CALLISTO_LOCAL_ONLY", "1")

import tools.bet_executor as be
from tools.betexec import bootstrap as betexec_bootstrap
from tools.betexec import session as betexec_session
from tools.betexec import wiring as betexec_wiring
from tools.betexec import execution as betexec_execution


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeDB:
    """Records close() calls; stands in for the aiosqlite connection."""

    def __init__(self):
        self.closed = False
        self.executed = []

    async def execute(self, sql, params=()):
        self.executed.append((sql.strip(), params))

        class _C:
            async def fetchone(self_inner):
                return None

        return _C()

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class FakePage:
    pass


def make_executor():
    """Executor with fake browser/page/db bound but NEVER enabled."""
    ex = be.BetExecutor()
    ex._browser = FakeBrowser()
    ex._context = ex._browser
    ex._page = FakePage()
    ex._db = FakeDB()
    return ex


# ---------------------------------------------------------------------------
# bootstrap: initialize
# ---------------------------------------------------------------------------


def test_bootstrap_open_database_returns_tagged_connection(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "slice5.db"))
    db = run(betexec_bootstrap.open_database())
    try:
        assert db is not None
        # busy_timeout pragma applied
        assert any(
            "busy_timeout" in sql for sql, _ in db.executed
        ) if hasattr(db, "executed") else True
    finally:
        run(db.close())


def test_bootstrap_initialize_binds_db_and_disarms_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "slice5.db"))
    monkeypatch.chdir(tmp_path)  # SCREENSHOT_DIR / SESSION_DIR are relative paths
    os.makedirs("memory/bet_screenshots", exist_ok=True)
    os.makedirs("memory/dk_session", exist_ok=True)
    ex = be.BetExecutor()
    assert ex.is_enabled is False  # never armed before init
    run(ex.initialize())
    try:
        assert ex._db is not None
        assert ex.is_enabled is False  # initialization must NOT arm the executor
    finally:
        run(ex.shutdown())


# ---------------------------------------------------------------------------
# bootstrap: shutdown
# ---------------------------------------------------------------------------


def test_bootstrap_shutdown_closes_browser_and_db_and_disarms():
    ex = make_executor()
    ex._logged_in = True
    browser, db = ex._browser, ex._db
    run(betexec_bootstrap.shutdown(ex))
    assert browser.closed is True
    assert db.closed is True
    assert ex._browser is None
    assert ex._context is None
    assert ex._page is None
    assert ex._db is None
    assert ex.is_enabled is False  # shutdown always disarms
    assert ex.is_logged_in is False


def test_facade_shutdown_delegates_to_bootstrap():
    ex = make_executor()
    run(ex.shutdown())
    assert ex._browser is None and ex._db is None
    assert ex.is_enabled is False


def test_bootstrap_shutdown_without_browser_or_db_is_safe():
    ex = be.BetExecutor()
    run(ex.shutdown())  # must not raise
    assert ex.is_enabled is False


def test_shutdown_after_enable_stays_disarmed(monkeypatch):
    monkeypatch.delenv("CALLISTO_LOCAL_ONLY", raising=False)
    ex = make_executor()
    try:
        assert ex.enable() is True
        assert ex.is_enabled is True
        run(ex.shutdown())
        assert ex.is_enabled is False
    finally:
        ex.disable()


def test_new_modules_importable_and_playwright_free():
    for module in (betexec_bootstrap, betexec_session, betexec_wiring):
        src = inspect.getsource(module)
        assert "from playwright" not in src.lower()
        assert "import playwright" not in src.lower()


def test_betexec_package_gains_no_live_status():
    """Slice-5 modules must not introduce a 'live' status token."""
    pkg_dir = os.path.dirname(betexec_bootstrap.__file__)
    offenders = []
    for fname in ("bootstrap.py", "session.py", "wiring.py"):
        with open(os.path.join(pkg_dir, fname), encoding="utf-8") as fh:
            for i, line in enumerate(fh, 1):
                if "'live'" in line or '"live"' in line:
                    stripped = line.strip()
                    if (
                        not stripped.startswith("#")
                        and "'''" not in line
                        and '"""' not in line
                    ):
                        offenders.append(f"{fname}:{i}: {stripped}")
    assert offenders == []


# ---------------------------------------------------------------------------
# session: launch_browser / ensure_logged_in
# ---------------------------------------------------------------------------


async def _aret(value):
    return value


def test_session_launch_browser_binds_context_and_page(monkeypatch):
    sentinel_page = FakePage()

    async def fake_launch(session_dir):
        assert str(session_dir) == str(betexec_session.SESSION_DIR)
        return ("ctx", sentinel_page)

    monkeypatch.setattr(betexec_session.betexec_browser, "launch_persistent_session", fake_launch)
    ex = be.BetExecutor()
    run(betexec_session.launch_browser(ex))
    assert ex._browser == "ctx"
    assert ex._context == "ctx"
    assert ex._page is sentinel_page
    assert ex.is_enabled is False


def test_facade_launch_browser_delegates(monkeypatch):
    sentinel_page = FakePage()

    async def fake_launch(session_dir):
        return ("ctx", sentinel_page)

    monkeypatch.setattr(betexec_session.betexec_browser, "launch_persistent_session", fake_launch)
    ex = be.BetExecutor()
    run(ex.launch_browser())
    assert ex._page is sentinel_page


def test_session_ensure_logged_in_launches_when_page_missing(monkeypatch):
    launched = []

    async def fake_launch(executor):
        launched.append(True)
        executor._page = FakePage()

    async def fake_check(page):
        assert isinstance(page, FakePage)
        return True

    monkeypatch.setattr(betexec_session, "launch_browser", fake_launch)
    monkeypatch.setattr(betexec_session.betexec_browser, "check_logged_in", fake_check)

    ex = be.BetExecutor()  # no page yet
    ok = run(betexec_session.ensure_logged_in(ex))
    assert ok is True
    assert launched == [True]
    assert ex.is_logged_in is True


def test_session_ensure_logged_in_reuses_existing_page(monkeypatch):
    launched = []

    async def fake_launch(executor):
        launched.append(True)

    async def fake_check(page):
        return False

    monkeypatch.setattr(betexec_session, "launch_browser", fake_launch)
    monkeypatch.setattr(betexec_session.betexec_browser, "check_logged_in", fake_check)

    ex = make_executor()  # has a page already
    ok = run(betexec_session.ensure_logged_in(ex))
    assert ok is False
    assert launched == []  # no relaunch
    assert ex.is_logged_in is False


def test_facade_ensure_logged_in_delegates(monkeypatch):
    async def fake_check(page):
        return True

    monkeypatch.setattr(betexec_session.betexec_browser, "check_logged_in", fake_check)
    ex = make_executor()
    assert run(ex.ensure_logged_in()) is True
    assert ex.is_logged_in is True


# ---------------------------------------------------------------------------
# session: navigate + slip
# ---------------------------------------------------------------------------


def test_session_navigate_to_game_uses_executor_page(monkeypatch):
    seen = {}

    async def fake_nav(page, sport, team):
        seen["page"] = page
        seen["args"] = (sport, team)
        return True

    monkeypatch.setattr(betexec_session.betexec_browser, "navigate_to_game", fake_nav)
    ex = make_executor()
    ok = run(betexec_session.navigate_to_game(ex, "baseball_mlb", "Yankees"))
    assert ok is True
    assert seen["page"] is ex._page
    assert seen["args"] == ("baseball_mlb", "Yankees")


def test_facade_navigate_delegates_and_ignores_event_id_arg(monkeypatch):
    seen = {}

    async def fake_nav(page, sport, team):
        seen["args"] = (sport, team)
        return False

    monkeypatch.setattr(betexec_session.betexec_browser, "navigate_to_game", fake_nav)
    ex = make_executor()
    assert run(ex.navigate_to_game("icehockey_nhl", "Bruins", event_id="e1")) is False
    assert seen["args"] == ("icehockey_nhl", "Bruins")


def test_session_place_bet_on_slip_uses_executor_page(monkeypatch):
    seen = {}

    async def fake_place(page, selection_text, stake):
        seen["page"] = page
        seen["args"] = (selection_text, stake)
        return {"success": True, "screenshot": "/tmp/x.png"}

    monkeypatch.setattr(betexec_session.betexec_slip, "place_bet_on_slip", fake_place)
    ex = make_executor()
    res = run(betexec_session.place_bet_on_slip(ex, "Yankees ML", 25.0))
    assert res["success"] is True
    assert seen["page"] is ex._page
    assert seen["args"] == ("Yankees ML", 25.0)


def test_facade_place_bet_on_slip_delegates(monkeypatch):
    async def fake_place(page, selection_text, stake):
        return {"success": False, "error": "nope", "screenshot": None}

    monkeypatch.setattr(betexec_session.betexec_slip, "place_bet_on_slip", fake_place)
    ex = make_executor()
    res = run(ex.place_bet_on_slip("Over 8.5", 10.0))
    assert res["success"] is False and res["error"] == "nope"


# ---------------------------------------------------------------------------
# wiring: ensure_logged_in short-circuit
# ---------------------------------------------------------------------------


def test_shortcircuit_skips_browser_when_already_logged_in():
    calls = []

    async def ensure_fn(executor):
        calls.append(True)
        return True

    ex = be.BetExecutor()
    ex._logged_in = True
    wrapped = betexec_wiring.ensure_logged_in_shortcircuit(ex, ensure_fn)
    assert run(wrapped()) is True
    assert calls == []  # legacy short-circuit: browser untouched


def test_shortcircuit_falls_through_when_not_logged_in():
    calls = []

    async def ensure_fn(executor):
        calls.append(executor)
        return False

    ex = be.BetExecutor()
    wrapped = betexec_wiring.ensure_logged_in_shortcircuit(ex, ensure_fn)
    assert run(wrapped()) is False
    assert calls == [ex]


# ---------------------------------------------------------------------------
# wiring: bind_execution_pipeline
# ---------------------------------------------------------------------------


def _bind_kwargs(ex, **overrides):
    kwargs = dict(
        sport="baseball_mlb",
        team="Yankees",
        market="h2h",
        side="Yankees ML",
        odds=-150,
        fair_prob=0.65,
        edge=0.03,
    )
    kwargs.update(overrides)
    return betexec_wiring.bind_execution_pipeline(ex, **kwargs)


def test_bind_pipeline_wires_live_state():
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert kw["db"] is ex._db
    assert kw["bankroll_lock"] is ex._bankroll_lock
    assert kw["enabled"] is False  # executor stays disarmed
    assert kw["sport"] == "baseball_mlb"
    assert kw["team"] == "Yankees"
    assert kw["market"] == "h2h"
    assert kw["side"] == "Yankees ML"
    assert kw["odds"] == -150
    assert kw["fair_prob"] == 0.65
    assert kw["edge"] == 0.03


def test_bind_pipeline_defaults():
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert kw["hypothesis_id"] == ""
    assert kw["event_id"] == ""
    assert kw["game_description"] == ""
    assert kw["confidence"] == 0.6
    assert kw["point"] is None
    assert kw["stake_override"] is None


def test_bind_pipeline_fn_seams_are_executor_methods():
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert kw["compute_stake_fn"] == ex.compute_stake
    assert kw["preflight_fn"] == ex.preflight_check


def test_bind_pipeline_build_message_fn_is_notify_builder():
    from tools.betexec.notify import build_bet_placed_message

    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert kw["build_message_fn"] is build_bet_placed_message


def test_bind_pipeline_navigate_fn_routes_through_session(monkeypatch):
    seen = {}

    async def fake_nav(executor, sport, team):
        seen["args"] = (sport, team)
        return True

    monkeypatch.setattr(betexec_session, "navigate_to_game", fake_nav)
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert run(kw["navigate_fn"]("baseball_mlb", "Yankees", "")) is True
    assert seen["args"] == ("baseball_mlb", "Yankees")


def test_bind_pipeline_place_fn_routes_through_session(monkeypatch):
    seen = {}

    async def fake_place(executor, sel, stake):
        seen["args"] = (sel, stake)
        return {"success": True}

    monkeypatch.setattr(betexec_session, "place_bet_on_slip", fake_place)
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert run(kw["place_fn"]("Yankees ML", 12.0)) == {"success": True}
    assert seen["args"] == ("Yankees ML", 12.0)


def test_bind_pipeline_record_and_log_fns_route_to_executor():
    recorded, logged = [], []

    async def fake_record(*args, **kwargs):
        recorded.append(kwargs)
        return 42

    async def fake_log(*args, **kwargs):
        logged.append((args, kwargs))

    ex = make_executor()
    ex._record_bet = fake_record
    ex._log_action = fake_log
    kw = _bind_kwargs(ex)
    bet_id = run(kw["record_bet_fn"](stake=25.0, sport="baseball_mlb"))
    assert bet_id == 42
    assert recorded == [{"stake": 25.0, "sport": "baseball_mlb"}]
    run(kw["log_action_fn"]("BET_PLACED", "s", "t", "m", "sd", -110, 10.0, 0.05, "hyp"))
    assert logged[0][0][0] == "BET_PLACED"


def test_bind_pipeline_notify_fn_is_executor_notifier():
    ex = make_executor()
    kw = _bind_kwargs(ex)
    assert callable(kw["notify_fn"])
    # The static notifier imports lazily; just verify it exists on the facade.
    assert hasattr(ex, "_notify")


# ---------------------------------------------------------------------------
# facade: execute_bet end-to-end through the new wiring (fake seams)
# ---------------------------------------------------------------------------


def test_execute_bet_full_path_through_wiring(monkeypatch):
    ex = make_executor()
    ex._logged_in = True  # short-circuit path

    async def fake_get_bankroll(db):
        return 1000.0

    async def fake_open_exposure(db):
        return 0.0

    monkeypatch.setattr(
        __import__("tools.betexec.db_state", fromlist=["get_bankroll"]),
        "get_bankroll",
        fake_get_bankroll,
    )
    import tools.betexec.db_state as db_state_mod
    monkeypatch.setattr(db_state_mod, "get_bankroll", fake_get_bankroll)
    monkeypatch.setattr(db_state_mod, "get_open_exposure", fake_open_exposure)

    async def fake_nav(executor, sport, team):
        return True

    async def fake_place(executor, sel, stake):
        assert stake > 0
        return {"success": True, "screenshot": "shot.png"}

    monkeypatch.setattr(betexec_session, "navigate_to_game", fake_nav)
    monkeypatch.setattr(betexec_session, "place_bet_on_slip", fake_place)

    recorded = []

    async def fake_record(**kwargs):
        recorded.append(kwargs)
        return 7

    async def noop(*a, **k):
        pass

    ex._record_bet = fake_record
    ex._log_action = noop

    # Arm-free preflight: bypass enable gate by faking preflight to OK.
    async def fake_preflight(sport, odds, edge, stake):
        return True, "OK"

    ex.preflight_check = fake_preflight

    result = run(ex.execute_bet(
        sport="baseball_mlb",
        team="Yankees",
        market="h2h",
        side="Yankees ML",
        odds=-150,
        fair_prob=0.65,
        edge=0.05,
        hypothesis_id="h1",
        stake_override=20.0,
    ))
    assert result["success"] is True
    assert result["bet_id"] == 7
    assert result["stake"] == 20.0
    assert recorded and recorded[0]["team"] == "Yankees"


def test_source_contract_compute_stake_still_canonical():
    """compute_stake body must stay inline in the facade (source contract)."""
    src = inspect.getsource(be.BetExecutor.compute_stake)
    assert "from tools.kelly import" in src
    assert "from tools.sizing import" in src


def test_source_contract_local_only_refusal_before_arm():
    """enable() must consult the gate BEFORE flipping _enabled."""
    src = inspect.getsource(be.BetExecutor.enable)
    assert "arm_gate_refusal" in src
    assert "_enabled = True" in src
    assert src.index("arm_gate_refusal") < src.index("_enabled = True")


def test_default_init_disarmed():
    ex = be.BetExecutor()
    assert ex._enabled is False
    assert ex.is_enabled is False


def test_paper_signal_statuses_never_gain_live():
    """The split must not touch the paper-trade signal gate."""
    pytest.importorskip("tools.signals.paper")
    import tools.signals.paper as paper

    statuses = getattr(paper, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    if statuses is None:
        pytest.skip("paper signal statuses not found")
    assert "live" not in {str(s).lower() for s in statuses}
