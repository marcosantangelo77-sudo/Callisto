"""tests/test_betexec_slice2.py — pin the slice-2 BetExecutor orchestration split.

Slice 2 (2026-08) moved the remaining Playwright/DB orchestration out of
``tools/bet_executor.py`` into real modules:

  - ``tools.betexec.browser``   — persistent-session launch, login check,
                                  game navigation
  - ``tools.betexec.slip``      — bet-slip interaction + selection-text
                                  mapping + screenshot path helpers
  - ``tools.betexec.logging``   — executor_log audit trail, bets-table
                                  recorder (dup guard, bankroll write under
                                  lock), bankroll-peak / rolling-peak

These tests drive all of that with fake page/db objects — no browser, no
network, no real DraftKings. The executor is NEVER armed: ``_enabled``
stays False and CALLISTO_LOCAL_ONLY refusal is re-pinned here.
"""

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("CALLISTO_LOCAL_ONLY", "1")

import tools.bet_executor as be
from tools.betexec import browser as betexec_browser
from tools.betexec import slip as betexec_slip
from tools.betexec import logging as betexec_logging


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeElement:
    def __init__(self, text="ok"):
        self.text = text
        self.clicked = 0
        self.filled = None

    async def click(self, click_count=1):
        self.clicked += 1

    async def fill(self, value):
        self.filled = value

    async def inner_text(self):
        return self.text


def _mk_dirs(tmp_path):
    (tmp_path / "shots").mkdir(parents=True, exist_ok=True)
    (tmp_path / "sess").mkdir(parents=True, exist_ok=True)


class FakePage:
    """Minimal Playwright-page stand-in driven by a scripted selector map."""

    def __init__(self, selector_map=None, fail_screenshot=False):
        # map: selector-prefix-key -> element or None
        self.selector_map = selector_map or {}
        self.gotos = []
        self.waits = 0
        self.screenshots = []
        self.fail_screenshot = fail_screenshot
        self._counter = 0

    async def goto(self, url, **kwargs):
        self.gotos.append(url)

    async def wait_for_timeout(self, ms):
        self.waits += 1

    async def query_selector(self, selector):
        for key, el in self.selector_map.items():
            if key in selector:
                return el
        return None

    async def screenshot(self, path=None):
        if self.fail_screenshot:
            raise RuntimeError("screenshot failed")
        self.screenshots.append(path)
        Path(path).write_bytes(b"png")  # touch file so str(path) round-trips


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def fetchall(self):
        return self._rows


class FakeResult:
    def __init__(self, lastrowid=0, rowcount=0):
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []


class FakeDB:
    """In-memory stand-in for aiosqlite.Connection covering our SQL surface."""

    def __init__(self):
        self.executed = []
        self.bankroll_rows: list[tuple] = [()]
        self.dup_rows = []
        self.peak_rows = [(500.0,)]
        self.insert_lastrowid = 4242

    async def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        s = sql.strip()
        if s.startswith("SELECT balance"):
            return FakeCursor(self.bankroll_rows)
        if "bet_id FROM bets" in s and "result = 'pending'" in s and "event_id" not in s:
            return FakeCursor([])
        if "SELECT bet_id FROM bets" in s:
            return FakeCursor(self.dup_rows)
        if "MAX(balance), 0) FROM bankroll_peak" in s:
            return FakeCursor(self.peak_rows)
        if "MAX(balance), 0) FROM bankroll WHERE" in s:
            return FakeCursor([(600.0,)])
        if s.startswith("INSERT INTO bets"):
            return FakeResult(lastrowid=self.insert_lastrowid)
        if s.startswith("UPDATE hypotheses"):
            return FakeResult(rowcount=1)
        return FakeCursor([])

    async def rollback(self):
        pass

    async def commit(self):
        pass

    async def close(self):
        pass


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _tmp_dirs(monkeypatch, tmp_path):
    _mk_dirs(tmp_path)
    monkeypatch.setattr(be, "SCREENSHOT_DIR", tmp_path / "shots")
    monkeypatch.setattr(be, "SESSION_DIR", tmp_path / "sess")
    monkeypatch.setattr(betexec_slip, "SCREENSHOT_DIR", tmp_path / "shots")
    yield tmp_path


# ---------------------------------------------------------------------------
# browser module
# ---------------------------------------------------------------------------

def test_launch_persistent_session_returns_context_and_page(tmp_path):
    class FakePWContext:
        def __init__(self):
            self.pages = []

        async def new_page(self):
            return FakePage()

        async def close(self):
            pass

    launched = {}

    class FakeChromium:
        async def launch_persistent_context(self, user_data_dir, **kw):
            launched["dir"] = user_data_dir
            launched.update(kw)
            return FakePWContext()

    class FakeAPW:
        def __init__(self):
            self.chromium = FakeChromium()

        def __call__(self):
            return self

        async def start(self):
            return self

    import sys, types
    import tools.betexec.browser as br

    fake_mod = types.ModuleType("playwright")
    fake_async = types.ModuleType("playwright.async_api")
    fake_async.async_playwright = FakeAPW
    fake_mod.async_api = fake_async
    monkeypatch = None
    saved = {k: sys.modules.get(k) for k in ("playwright", "playwright.async_api")}
    sys.modules["playwright"] = fake_mod
    sys.modules["playwright.async_api"] = fake_async
    try:
        ctx, page = run(br.launch_persistent_session(str(tmp_path / "sess")))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert ctx is not None and isinstance(page, FakePage)
    assert launched["dir"] == str(tmp_path / "sess")
    assert launched["args"] == betexec_browser.LAUNCH_ARGS


def test_check_logged_in_true_when_balance_selector_present():
    page = FakePage({"data-testid='user-balance'": FakeElement()})
    assert run(betexec_browser.check_logged_in(page)) is True
    assert any("sportsbook" in u or "draftkings" in u.lower() for u in page.gotos)


def test_check_logged_in_false_when_sign_in_button_visible():
    page = FakePage({"sign-in-button": FakeElement()})
    assert run(betexec_browser.check_logged_in(page)) is False


def test_check_logged_in_true_when_neither_selector_matches():
    page = FakePage({})
    assert run(betexec_browser.check_logged_in(page)) is True


def test_check_logged_in_false_on_navigation_error():
    class BoomPage(FakePage):
        async def goto(self, url, **kw):
            raise RuntimeError("net down")

    assert run(betexec_browser.check_logged_in(BoomPage())) is False


def test_game_page_url_unknown_sport_is_none():
    assert betexec_browser.game_page_url("quidditch") is None


def test_navigate_to_game_unknown_sport_short_circuits():
    page = FakePage()
    assert run(betexec_browser.navigate_to_game(page, "quidditch", "X")) is False
    assert page.gotos == []


def test_navigate_to_game_clicks_team_link():
    link = FakeElement()
    page = FakePage({"a:has-text('Yankees')": link})
    assert run(betexec_browser.navigate_to_game(page, "baseball_mlb", "Yankees")) is True
    assert link.clicked == 1


def test_navigate_to_game_false_when_team_missing():
    page = FakePage({})
    assert run(betexec_browser.navigate_to_game(page, "baseball_mlb", "Yankees")) is False


# ---------------------------------------------------------------------------
# slip module
# ---------------------------------------------------------------------------

def test_build_selection_text_markets():
    f = betexec_slip.build_selection_text
    assert f("h2h", "Yankees", "Yankees ML") == "Yankees"
    assert f("spreads", "Yankees", "-1.5", point=-1.5) == "-1.5"
    assert f("spreads", "Yankees", "+2.5", point=2.5) == "+2.5"
    assert f("totals", "Game", "Over") == "Over"
    assert f("prop", "Game", "Yes") == "Yes"
    assert f("spreads", "Yankees", "", point=None) == "Yankees"


def test_place_bet_on_slip_selection_not_found():
    page = FakePage({})
    res = run(betexec_slip.place_bet_on_slip(page, "Yankees ML", 25.0))
    assert res["success"] is False
    assert "not found" in res["error"]
    assert res["screenshot"] is None


def test_place_bet_on_slip_happy_path(tmp_path):
    stake_input = FakeElement()
    confirm = FakeElement()
    receipt = FakeElement()

    page = FakePage({
        "button:has-text('Yankees ML')": FakeElement(),
        "input[data-testid='bet-slip-stake']": stake_input,
        "place-bet-button": confirm,
        ".bet-receipt": receipt,
    })
    res = run(betexec_slip.place_bet_on_slip(page, "Yankees ML", 25.0))
    assert res["success"] is True
    assert stake_input.filled == "25.00"
    assert confirm.clicked >= 1
    assert res["confirmation"].startswith("Bet confirmed at ")
    assert res["screenshot"] and Path(res["screenshot"]).exists()


def test_place_bet_on_slip_no_confirm_button_keeps_pre_screenshot(tmp_path):
    page = FakePage({
        "button:has-text('Yankees ML')": FakeElement(),
        "input[data-testid='bet-slip-stake']": FakeElement(),
    })
    res = run(betexec_slip.place_bet_on_slip(page, "Yankees ML", 10.0))
    assert res["success"] is False
    assert res["error"] == "Could not find Place Bet button"
    assert "pre_confirm_" in res["screenshot"]


def test_place_bet_on_slip_dk_error_message(tmp_path):
    err_el = FakeElement(text="odds have changed")
    page = FakePage({
        "button:has-text('Yankees ML')": FakeElement(),
        "input[data-testid='bet-slip-stake']": FakeElement(),
        "[data-testid='place-bet-button']": FakeElement(),
        ".betslip-error": err_el,
    })
    res = run(betexec_slip.place_bet_on_slip(page, "Yankees ML", 10.0))
    assert res["success"] is False
    assert "DK error" in res["error"] and "odds have changed" in res["error"]


def test_place_bet_on_slip_exception_still_reports(tmp_path):
    class ExplodingSel(FakePage):
        async def query_selector(self, sel):
            raise RuntimeError("page crashed")

    res = run(betexec_slip.place_bet_on_slip(ExplodingSel({}), "X", 5.0))
    assert res["success"] is False
    assert res["error"] == "page crashed"


def test_place_bet_on_slip_error_screenshot_failure_swallowed():
    class DoubleBoom(FakePage):
        async def query_selector(self, sel):
            raise RuntimeError("crash A")

        async def screenshot(self, path=None):
            raise RuntimeError("crash B")

    res = run(betexec_slip.place_bet_on_slip(DoubleBoom(), "X", 5.0))
    assert res["success"] is False and res["error"] == "crash A"
    assert res["screenshot"] is None


# ---------------------------------------------------------------------------
# logging module
# ---------------------------------------------------------------------------

def test_implied_probability_clamps_and_formula():
    f = betexec_logging.implied_probability
    assert f(0.65, 0.03) == pytest.approx(0.62)
    assert f(0.02, 0.05) == 0.0          # clamped at 0
    assert f(0.99, -0.05) == 1.0         # clamped at 1


def test_build_bet_insert_params_shape():
    sql, params = betexec_logging.build_bet_insert_params(
        "2026-08-26T00:00:00+00:00", "baseball_mlb", "ev-1", "NYY @ BOS",
        "New York Yankees", "h2h", "DraftKings", -150, None,
        50.0, 0.03, 0.65, "hyp-abc", bankroll=1000.0,
    )
    assert "INSERT INTO bets" in sql
    assert "'single'" in sql and "'pending'" in sql
    # placed_at, sport, event_id, game_desc, team, market, bookmaker, odds,
    # point, implied, stake, edge, kelly, notes, tags
    assert len(params) == 15
    assert params[9] == pytest.approx(round(0.62, 6))
    assert params[12] == pytest.approx(0.05)  # 50/1000
    assert "hyp-abc" in params[13] and "auto,hypothesis:hyp-abc" == params[14]


def test_ensure_executor_log_schema_executes_create(db_tmp=None):
    db = FakeDB()
    committed = {}
    import tools.db_utils as du

    async def fake_commit(d, **kw):
        committed["yes"] = d is db

    orig = du.commit_with_retry
    du.commit_with_retry = fake_commit
    try:
        run(betexec_logging.ensure_executor_log_schema(db))
    finally:
        du.commit_with_retry = orig
    assert any("CREATE TABLE IF NOT EXISTS executor_log" in s for s, _ in db.executed)
    assert committed["yes"]


def test_log_action_inserts_audit_row(monkeypatch):
    db = FakeDB()
    inserted = {}

    import tools.db_utils as du

    async def fake_exec(d, sql, params, **kw):
        inserted["sql"] = sql
        inserted["params"] = params
        return FakeResult()

    async def fake_commit(d, **kw):
        pass

    monkeypatch.setattr(du, "execute_with_retry", fake_exec)
    monkeypatch.setattr(du, "commit_with_retry", fake_commit)
    run(betexec_logging.log_action(
        db, "BET_PLACED", "baseball_mlb", "Yankees", "h2h", "Yankees ML",
        -150, 50.0, 0.03, "hyp-1", bet_id=7, screenshot="/s.png",
    ))
    assert "INSERT INTO executor_log" in inserted["sql"]
    p = inserted["params"]
    # (timestamp, action, sport, team, market, side, odds, stake, edge,
    #  hypothesis_id, bet_id, screenshot_path, status, error)
    assert p[1] == "BET_PLACED"
    assert p[10] == 7 and p[11] == "/s.png"
    assert p[12] == "success" and p[13] is None


def test_record_bet_duplicate_guard_returns_existing_id():
    db = FakeDB()
    db.dup_rows = [(31337,)]
    calls = {"bankroll": 0}

    async def get_bankroll():
        calls["bankroll"] += 1
        return 1000.0

    bet_id = run(betexec_logging.record_bet(
        db, get_bankroll, asyncio.Lock(),
        sport="baseball_mlb", event_id="ev-1", game_description="g",
        team="Yankees", market="h2h", bookmaker="DraftKings",
        odds=-150, point=None, stake=50.0, edge=0.03, fair_prob=0.65,
        hypothesis_id="hyp-1",
    ))
    assert bet_id == 31337
    assert calls["bankroll"] == 0  # short-circuits before sizing/bankroll read


def test_record_bet_inserts_and_updates_bankroll():
    db = FakeDB()
    inserts = []

    import tools.db_utils as du
    orig_exec = du.execute_with_retry
    orig_commit = du.commit_with_retry

    async def fake_exec(d, sql, params, **kw):
        inserts.append((sql.strip(), params))
        if sql.strip().startswith("INSERT INTO bets"):
            return FakeResult(lastrowid=99)
        return FakeResult()

    async def fake_commit(d, **kw):
        pass

    du.execute_with_retry, du.commit_with_retry = fake_exec, fake_commit
    try:
        bet_id = run(betexec_logging.record_bet(
            db, _mk_bankroll(1000.0), asyncio.Lock(),
            sport="baseball_mlb", event_id="ev-2", game_description="g",
            team="Red Sox", market="spreads", bookmaker="DraftKings",
            odds=110, point=-1.5, stake=40.0, edge=0.04, fair_prob=0.60,
            hypothesis_id="hyp-2",
        ))
    finally:
        du.execute_with_retry, du.commit_with_retry = orig_exec, orig_commit

    assert bet_id == 99
    kinds = [s.split()[0] + " " + s.split()[1] for s, _ in inserts]
    assert any("INSERT INTO bets" in s for s, _ in inserts)
    bk = [p for s, p in inserts if "INSERT INTO bankroll" in s]
    assert bk and bk[0][1] == 960.0 and bk[0][2] == -40.0 and bk[0][3] == 99


def _mk_bankroll(value):
    async def get_bankroll():
        return value
    return get_bankroll


def test_record_bet_rolls_back_on_bankroll_write_failure():
    db = FakeDB()
    rolled_back = []

    import tools.db_utils as du
    orig_exec, orig_commit = du.execute_with_retry, du.commit_with_retry

    async def fake_exec(d, sql, params, **kw):
        s = sql.strip()
        if s.startswith("INSERT INTO bets"):
            return FakeResult(lastrowid=5)
        if s.startswith("INSERT INTO bankroll"):
            raise RuntimeError("disk full")
        return FakeResult()

    async def fake_commit(d, **kw):
        pass

    du.execute_with_retry, du.commit_with_retry = fake_exec, fake_commit

    class RollbackDB(FakeDB):
        async def rollback(self):
            rolled_back.append(True)

    try:
        with pytest.raises(RuntimeError, match="disk full"):
            run(betexec_logging.record_bet(
                RollbackDB(), _mk_bankroll(500.0), asyncio.Lock(),
                sport="s", event_id="e", game_description="g",
                team="T", market="h2h", bookmaker="B",
                odds=-100, point=None, stake=10.0, edge=0.02, fair_prob=0.52,
                hypothesis_id="h",
            ))
    finally:
        du.execute_with_retry, du.commit_with_retry = orig_exec, orig_commit
    assert rolled_back == [True]


def test_record_bankroll_peak_best_effort():
    db = FakeDB()

    import tools.db_utils as du
    orig_exec, orig_commit = du.execute_with_retry, du.commit_with_retry

    async def boom(*a, **k):
        raise RuntimeError("no table")

    du.execute_with_retry, du.commit_with_retry = boom, orig_commit
    try:
        run(betexec_logging.record_bankroll_peak(db, 123.0))  # must not raise
    finally:
        du.execute_with_retry, du.commit_with_retry = orig_exec, orig_commit


def test_rolling_peak_falls_back_to_bankroll_history():
    db = FakeDB()
    db.peak_rows = [(None,)]
    assert run(betexec_logging.rolling_peak(db)) == 600.0
    db.peak_rows = []
    db2 = FakeDB()
    db2.peak_rows = [(250.0,)]
    assert run(betexec_logging.rolling_peak(db2)) == 250.0
    assert run(betexec_logging.rolling_peak(db, window_days=7)) == 600.0


# ---------------------------------------------------------------------------
# Facade delegation + safety invariants
# ---------------------------------------------------------------------------

def test_facade_delegates_launch_and_login(monkeypatch):
    ex = be.BetExecutor()
    assert ex.is_enabled is False  # NEVER armed

    sentinel_page = FakePage()

    async def fake_launch(session_dir):
        return ("ctx", sentinel_page)

    monkeypatch.setattr(betexec_browser, "launch_persistent_session", fake_launch)
    run(ex.launch_browser())
    assert ex._browser == "ctx" and ex._page is sentinel_page

    monkeypatch.setattr(betexec_browser, "check_logged_in", _aret(True))
    ok = run(ex.ensure_logged_in())
    assert ok is True and ex.is_logged_in is True


def _aret(v):
    async def f(*a, **k):
        return v
    return f


def test_facade_delegates_slip_and_navigation(monkeypatch):
    captured = {}

    async def fake_nav(page, sport, team):
        captured["nav"] = (sport, team)
        return True

    async def fake_place(page, selection_text, stake):
        captured["slip"] = (selection_text, stake)
        return betexec_slip.build_result() | {"success": True}

    monkeypatch.setattr(betexec_browser, "navigate_to_game", fake_nav)
    monkeypatch.setattr(betexec_slip, "place_bet_on_slip", fake_place)

    ex = be.BetExecutor()
    ex._page = FakePage()
    assert run(ex.navigate_to_game("baseball_mlb", "Yankees")) is True
    assert captured["nav"] == ("baseball_mlb", "Yankees")
    res = run(ex.place_bet_on_slip("Yankees ML", 33.0))
    assert res["success"] and captured["slip"] == ("Yankees ML", 33.0)


def test_facade_execute_bet_uses_extracted_selection_mapping(monkeypatch):
    ex = be.BetExecutor()
    seen = {}

    async def fake_place(page, selection_text, stake):
        seen["selection"] = selection_text
        return {"success": False, "error": "stop here", "screenshot": None}

    monkeypatch.setattr(betexec_slip, "place_bet_on_slip", fake_place)
    monkeypatch.setattr(be.betexec_browser, "navigate_to_game", _aret(True))

    ex._logged_in = True
    db = FakeDB()
    db.bankroll_rows = [(1000.0,)]
    ex._db = db
    # Flip the internal flag directly (no enable() call): we must exercise the
    # pipeline without ever going through the arming path.
    ex._enabled = True
    res = run(ex.execute_bet(
        sport="baseball_mlb", team="Yankees", market="spreads", side="-1.5",
        odds=-110, fair_prob=0.6, edge=0.05, hypothesis_id="h",
        stake_override=25.0, point=-1.5,
    ))
    assert res["success"] is False
    assert seen["selection"] == "-1.5"


def test_facade_preflight_disabled_and_local_only_enable_refusal():
    ex = be.BetExecutor()
    ok, reason = run(ex.preflight_check("baseball_mlb", -150, 0.05, 10.0))
    assert ok is False and reason == "Executor is disabled"

    # CALLISTO_LOCAL_ONLY=1 set at module import → enable() must refuse.
    assert be.BetExecutor().enable() is False
    assert be.BetExecutor().is_enabled is False


def test_status_dict_shape_without_db():
    ex = be.BetExecutor()
    st = run(ex.status())
    assert st["enabled"] is False
    assert st["browser_active"] is False
    assert st["bankroll"] == 0


def test_shutdown_resets_state():
    ex = be.BetExecutor()

    class Closable:
        closed = 0

        async def close(self):
            Closable.closed += 1

    ex._browser = Closable()
    ex._db = FakeDB()
    ex._enabled = True
    run(ex.shutdown())
    assert Closable.closed == 1
    assert ex._browser is None and ex._db is None
    assert ex.is_enabled is False and ex.is_logged_in is False
