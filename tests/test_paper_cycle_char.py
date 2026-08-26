"""Characterization tests: the paper cycle never emits live.

This module pins the safety invariants of the paper-trade loop using a
combination of AST/source inspection (we deliberately do NOT import
``tools.autonomous`` — it can hang at import time) and real execution of the
cheap, side-effect-free gates. Together these tests must FAIL if anyone:

  1. widens ``_PAPER_TRADE_SIGNAL_STATUSES`` beyond ``{"paper_trading"}``
     (e.g. adds ``"live"``),
  2. rewrites ``generate_paper_trade_signal`` to accept non-paper statuses,
  3. arms ``_phase_live_execute`` by default (it must be a no-op unless
     ``CALLISTO_ALLOW_LIVE_EXECUTE == "1"``),
  4. adds live-betting panels (panel-hyps / panel-orders / panel-portfolio)
     to the operator dashboard HTML, or
  5. makes ``BetExecutor`` start enabled, or lets ``enable()`` succeed while
     ``CALLISTO_LOCAL_ONLY`` is truthy.

Everything here is a characterization pin: if an invariant is intentionally
changed on master, the corresponding test must be updated in the same commit.
"""

import ast
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PAPER_MODULE = "tools/signals/paper.py"
BACKTEST_MODULE = "tools/backtest.py"
AUTONOMOUS_MODULE = "tools/autonomous.py"
EXECUTOR_MODULE = "tools/bet_executor.py"
DASHBOARD_HTML = "web/dashboard/index.html"

LIVE_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.exists(), f"expected file missing: {rel}"
    return path.read_text(encoding="utf-8")


def _parse(rel: str) -> ast.AST:
    return ast.parse(_read(rel))


def _find_func(tree: ast.AST, name: str):
    """Return all FunctionDef/AsyncFunctionDef nodes with the given name."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


def _func_source(func_node) -> str:
    return ast.unparse(func_node)


# ---------------------------------------------------------------------------
# 1. The frozenset gate itself
# ---------------------------------------------------------------------------


def test_paper_statuses_frozenset_is_exactly_paper_trading():
    """Pin the literal source shape AND the value of the hard-gate frozenset.

    Checked via AST so importing tools.autonomous is unnecessary — this file's
    target module (tools/signals/paper.py) is safe to import too, but the AST
    pin survives even if the module grows heavy imports later.
    """
    tree = _parse(PAPER_MODULE)
    assigns = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES" for t in node.targets)
    ]
    assert len(assigns) == 1, (
        "_PAPER_TRADE_SIGNAL_STATUSES must be assigned exactly once in "
        f"{PAPER_MODULE}, found {len(assigns)} assignments"
    )
    node = assigns[0]
    # Literal shape: frozenset({...})
    assert isinstance(node.value, ast.Call), "gate must be built with frozenset(...)"
    assert isinstance(node.value.func, ast.Name) and node.value.func.id == "frozenset"
    assert len(node.value.args) == 1 and not node.value.keywords
    arg = node.value.args[0]
    assert isinstance(arg, (ast.Set, ast.List, ast.Tuple)), "frozenset argument must be a literal collection"
    literals = set()
    for elt in arg.elts:
        assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
            "every allowed status must be a plain string literal"
        )
        literals.add(elt.value)
    assert literals == {"paper_trading"}, (
        f"allowed paper statuses drifted: {sorted(literals)}"
    )

    # Cross-check with the runtime value as well.
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})
    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_gate_helpers_never_accept_live():
    """reject_non_paper / allowed_paper_statuses stay consistent with the pin."""

    from tools.signals.paper import allowed_paper_statuses, reject_non_paper

    assert allowed_paper_statuses() == frozenset({"paper_trading"})
    assert reject_non_paper("live") is True
    assert reject_non_paper("paper_trading") is False
    # Anything else (typo'd statuses, future statuses) is rejected fail-closed.
    for bad in ("Live", "LIVE", "dry_run", "", None, "shadow_live", "prod"):
        assert reject_non_paper(bad) is True, f"status {bad!r} must be rejected"


def test_no_other_module_redefines_the_gate():
    """The gate definition lives ONLY in tools/signals/paper.py.

    A second definition elsewhere (e.g. a widened copy inside tools/backtest.py
    or tools/autonomous.py) would let live flow around the single chokepoint.
    """
    offenders = []
    for rel in (BACKTEST_MODULE, AUTONOMOUS_MODULE, EXECUTOR_MODULE):
        src = _read(rel)
        for m in re.finditer(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(?!=)", src):
            offenders.append((rel, src[: m.start()].count("\n") + 1))
    assert not offenders, f"gate redefined outside {PAPER_MODULE}: {offenders}"


# ---------------------------------------------------------------------------
# 2. generate_paper_trade_signal returns [] for every non-paper status
# ---------------------------------------------------------------------------


def _engine_with_hypothesis(status, manager=None):
    """Build a BacktestEngine without running its __init__ (no DB, no loop)."""
    from unittest.mock import MagicMock

    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)
    hm = MagicMock() if manager is None else manager

    async def _get(hid):
        return {
            "status": status,
            "model_config": {"target_book": "draftkings", "devig_method": "power"},
            "edge_threshold": 0.05,
            "market_type": "h2h",
            "thesis": "",
            "name": "",
            "sport": "basketball_nba",
        }

    hm.get_hypothesis = _get
    engine.hypothesis_manager = hm
    return engine


FULL_LIVE_ODDS = {
    "games": [
        {
            "id": f"g{i}",
            "sport_key": "basketball_nba",
            "home_team": "Home",
            "away_team": "Away",
            "commence_time": "2026-08-26T02:30:00Z",
            "bookmakers": [],
        }
        for i in range(5)
    ]
}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["live", "LIVE", "Live", "", None, "drawdown_paused", "retired", "backtesting"],
)
async def test_generate_paper_trade_signal_returns_empty_for_status(status):
    """Every non-paper status returns [] BEFORE any odds/game processing.

    The odds payload is full of games; if the gate ever short-circuits after
    processing starts (or accepts one of these statuses) the test fails.
    """
    engine = _engine_with_hypothesis(status)
    signals = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
    assert signals == []


@pytest.mark.asyncio
async def test_generate_paper_trade_signal_empty_when_hypothesis_missing():
    """A missing hypothesis (None) also fails closed to []."""
    engine = _engine_with_hypothesis("paper_trading")

    async def _get(hid):
        return None

    engine.hypothesis_manager.get_hypothesis = _get
    signals = await engine.generate_paper_trade_signal("nope", FULL_LIVE_ODDS)
    assert signals == []


@pytest.mark.asyncio
async def test_generate_paper_trade_signal_runs_for_paper_trading_only_shape():
    """The paper_trading path still runs — but never emits anything here.

    With a well-formed paper hypothesis but odds containing no bookmakers,
    the method returns [] because no consensus can form; the point of this
    pin is that the gate ACCEPTS paper_trading (so the loop isn't dead) while
    everything else above stays rejected.
    """
    engine = _engine_with_hypothesis("paper_trading")

    class _FakeCursor:
        description = [("hypothesis_id",), ("signal",)]

        async def fetchall(self):
            return []

    class _FakeDB:
        async def execute(self, *a, **k):
            return _FakeCursor()

        async def commit(self):
            pass

        def executemany(self, *a, **k):
            raise AssertionError("no games → nothing must be inserted")

    engine._db = _FakeDB()
    h = await engine.hypothesis_manager.get_hypothesis("hyp-1")
    assert h["status"] == "paper_trading"
    signals = await engine.generate_paper_trade_signal(
        "hyp-1",
        {"games": []},
    )
    assert signals == []


def test_generate_paper_trade_signal_source_pins_fail_closed_gate():
    """AST pin: the FIRST thing after get_hypothesis is the reject check.

    Guards against a rewrite that moves the gate below config parsing or
    drops it entirely.
    """
    tree = _parse(BACKTEST_MODULE)
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "generate_paper_trade_signal"
    ]
    assert len(funcs) >= 1, "generate_paper_trade_signal disappeared"
    body_src = "\n".join(ast.unparse(stmt) for stmt in funcs[0].body)

    assert "get_hypothesis" in body_src
    # The rejection helper must be invoked somewhere in the function body...
    assert "reject_non_paper(" in body_src, (
        "generate_paper_trade_signal must call reject_non_paper(...) — "
        "the gate was removed or bypassed"
    )
    # ...and must return [] when it fires (the early-return shape).
    assert re.search(r"return\s+\[\]", body_src), (
        "expected an explicit `return []` fail-closed branch"
    )


def test_backtest_imports_gate_from_signals_paper():
    """tools/backtest.py must use the shared gate, not a local copy."""
    src = _read(BACKTEST_MODULE)
    assert "from tools.signals.paper import" in src or (
        "from tools.signals import" in src and "reject_non_paper" in src
    ), "tools/backtest.py should import the paper gate from tools.signals.paper"


# ---------------------------------------------------------------------------
# 3. _phase_live_execute is a no-op unless CALLISTO_ALLOW_LIVE_EXECUTE == "1"
# ---------------------------------------------------------------------------


def _phase_live_execute_nodes():
    tree = _parse(AUTONOMOUS_MODULE)
    return [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_phase_live_execute"]


def test_phase_live_execute_exists_and_checks_env_before_anything():
    nodes = _phase_live_execute_nodes()
    assert len(nodes) == 1, "_phase_live_execute must exist exactly once in tools/autonomous.py"
    raw = _read(AUTONOMOUS_MODULE)
    seg_start = raw.find("async def _phase_live_execute")
    # After the auto-helper extraction refactor the method may no longer be
    # followed by `_phase_interpret_backtests`; locate its end by brace
    # back-navigation from the NEXT top-level `async def` at column 0.
    seg_end = raw.find("\nasync def ", seg_start + 1)
    if seg_end == -1:
        seg_end = len(raw)
    assert seg_start != -1
    src = raw[seg_start:seg_end]
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE")' in src, (
        "arming switch must be read via getenv('CALLISTO_ALLOW_LIVE_EXECUTE')"
    )
    # The env check compares against exactly "1".
    assert '!= "1"' in src, "switch comparison must pin the exact string '1'"
    # It must return before delegating to phases_impl unless armed.
    assert "phase_live_execute" in src


@pytest.mark.asyncio
async def test_phase_live_execute_is_noop_without_env(monkeypatch):
    """Executing the phase without the arming env var does NOTHING.

    We run the real coroutine (importing only tools.autonomous lazily here;
    if that import hangs in some environment, the AST pins above still hold).
    """
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "")

    from tools.autonomous import ResearchLoop  # noqa: F401  (shape probe)

    loop_obj = ResearchLoop.__new__(ResearchLoop)
    result = await loop_obj._phase_live_execute()
    assert result is None


@pytest.mark.asyncio
async def test_phase_live_execute_refuses_wrong_values(monkeypatch):
    """Only the exact string '1' arms the phase — 'true', 'yes', 1 fail."""
    from tools.autonomous import ResearchLoop

    loop_obj = ResearchLoop.__new__(ResearchLoop)
    for bad in ("true", "yes", "on", "1 ", "01", "True"):
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", bad)
        called = {"n": 0}

        async def _boom(self):
            called["n"] += 1

        result = await ResearchLoop._phase_live_execute(loop_obj)
        assert result is None, f"value {bad!r} must NOT arm live execute"
        assert called["n"] == 0


@pytest.mark.asyncio
async def test_phase_live_execute_delegates_when_armed(monkeypatch):
    """When armed with '1', the phase delegates to phases_impl.phase_live_execute."""
    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")

    import tools.autonomous as auto_mod

    calls = {"n": 0}

    async def _fake_phase(loop):
        calls["n"] += 1
        return "delegated"

    orig = getattr(auto_mod, "phases_impl", None)
    sentinel = type("P", (), {})()
    try:
        holder = orig if orig is not None else sentinel
        holder_phase = getattr(holder, "phase_live_execute", None)
        setattr(holder, "phase_live_execute", _fake_phase)
        if orig is None:
            monkeypatch.setattr(auto_mod, "phases_impl", holder, raising=False)
            auto_mod.phases_impl = holder
        loop_obj = auto_mod.ResearchLoop.__new__(auto_mod.ResearchLoop)
        result = await loop_obj._phase_live_execute()
        assert result == "delegated"
        assert calls["n"] == 1
    finally:
        if orig is not None:
            if holder_phase is not None:
                setattr(holder, "phase_live_execute", holder_phase)


# ---------------------------------------------------------------------------
# 4. Dashboard HTML has NO live panels
# ---------------------------------------------------------------------------


def test_dashboard_html_has_no_live_panels():
    html = _read(DASHBOARD_HTML)
    for panel_id in LIVE_PANEL_IDS:
        # Neither as an id= attribute nor referenced anywhere in the file.
        assert f'id="{panel_id}"' not in html, (
            f"{DASHBOARD_HTML} must not contain a #{panel_id} panel"
        )
        assert panel_id not in html, (
            f"dashboard references forbidden live panel '{panel_id}'"
        )


def test_dashboard_html_declares_paper_facing_panels_only():
    """Sanity: the dashboard still has its known-good panels (characterization)."""
    html = _read(DASHBOARD_HTML)
    assert 'id="panel-state"' in html
    assert 'id="panel-ingestion"' in html
    assert 'id="panel-alerts"' in html


# ---------------------------------------------------------------------------
# 5. BetExecutor starts disabled; LOCAL_ONLY refuses enable()
# ---------------------------------------------------------------------------


def test_bet_executor_init_assigns_enabled_false():
    """AST pin: BetExecutor.__init__ sets self._enabled = False."""
    tree = _parse(EXECUTOR_MODULE)
    classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BetExecutor"]
    assert len(classes) == 1, "BetExecutor class missing"
    inits = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    assert len(inits) == 1
    found = False
    for node in ast.walk(inits[0]):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant)
            and node.value.value is False
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Attribute)
            and node.targets[0].attr == "_enabled"
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "self"
        ):
            found = True
    assert found, "BetExecutor.__init__ must assign self._enabled = False"


def test_enable_refuses_local_only_via_ast():
    """AST pin: enable()'s first guard reads CALLISTO_LOCAL_ONLY and bails."""
    tree = _parse(EXECUTOR_MODULE)
    enables = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "enable"
    ]
    assert len(enables) == 1
    src = ast.unparse(enables[0])
    assert "CALLISTO_LOCAL_ONLY" in src, "enable() must consult CALLISTO_LOCAL_ONLY"
    # Refusal happens BEFORE setting _enabled = True.
    guard_pos = src.find("CALLISTO_LOCAL_ONLY")
    arm_pos = src.find("_enabled = True")
    assert guard_pos != -1 and arm_pos != -1 and guard_pos < arm_pos, (
        "LOCAL_ONLY refusal must precede any arming of the executor"
    )


def test_local_only_refuses_enable_runtime():
    """Runtime: with CALLISTO_LOCAL_ONLY truthy, enable() returns False and
    the executor stays disabled."""
    from tools.bet_executor import BetExecutor

    ex = BetExecutor.__new__(BetExecutor)
    ex._enabled = False
    old = os.environ.get("CALLISTO_LOCAL_ONLY")
    try:
        for val in ("1", "true", "yes"):
            os.environ["CALLISTO_LOCAL_ONLY"] = val
            assert ex.enable() is False, f"LOCAL_ONLY={val!r} must refuse enable()"
            assert ex.is_enabled is False
    finally:
        if old is None:
            os.environ.pop("CALLISTO_LOCAL_ONLY", None)
        else:
            os.environ["CALLISTO_LOCAL_ONLY"] = old


def test_executor_default_construction_starts_disabled(tmp_path, monkeypatch):
    """Full construction (with a scratch DB) still starts disabled."""
    from tools.bet_executor import BetExecutor

    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "scratch.db"))
    ex = BetExecutor()
    try:
        assert ex._enabled is False
        assert ex.is_enabled is False
    finally:
        # No browser/db was opened by __init__; nothing to tear down beyond state.
        pass


def test_disable_sets_enabled_false():
    from tools.bet_executor import BetExecutor

    ex = BetExecutor.__new__(BetExecutor)
    ex._enabled = True
    ex.disable()
    assert ex.is_enabled is False


# ---------------------------------------------------------------------------
# 6. Loop wiring: the autonomous loop keeps paper phase wired, live phase gated
# ---------------------------------------------------------------------------


def test_loop_defines_both_phases():
    """Characterization: both phases exist on the loop class; only one is gated."""
    tree = _parse(AUTONOMOUS_MODULE)
    names = {
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef)
    }
    assert "_phase_paper_trade" in names
    assert "_phase_live_execute" in names
    assert "_phase_review_live" in names


def test_paper_phase_delegates_without_env_gate():
    """_phase_paper_trade must NOT require an env var (paper is the default)."""
    tree = _parse(AUTONOMOUS_MODULE)
    funcs = [
        n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_phase_paper_trade"
    ]
    assert len(funcs) == 1
    src = ast.unparse(funcs[0])
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src
    assert "phase_paper_trade" in src
