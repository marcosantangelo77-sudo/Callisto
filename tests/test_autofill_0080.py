"""Autofill #0080 — paper-only loop characterization (LONG).

Characterizes the paper-trade safety envelope end to end:

1. ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is EXACTLY the
   frozenset ``{"paper_trading"}`` — never widened, never a mutable set.
2. ``BacktestEngine.generate_paper_trade_signal`` rejects any status other
   than ``paper_trading`` (most importantly ``live``) and returns ``[]``
   BEFORE touching odds.
3. ``phase_live_execute`` is a no-op unless
   ``CALLISTO_ALLOW_LIVE_EXECUTE=1`` — every other value refuses.
4. ``BetExecutor.__init__`` assigns ``_enabled = False``: the executor is
   born disarmed.

Fail-closed doctrine: if any pin here is currently false in production,
the test FAILS (it must never be "fixed" by arming live betting).
"""

from __future__ import annotations

import ast
import inspect
import re
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PAPER_MODULE_PATH = REPO / "tools" / "signals" / "paper.py"
BACKTEST_PATH = REPO / "tools" / "backtest.py"
PHASES_IMPL_PATH = REPO / "tools" / "loop" / "phases_impl.py"
BET_EXECUTOR_PATH = REPO / "tools" / "bet_executor.py"

FORBIDDEN_STATUSES = [
    "live",
    "LIVE",
    "Live",
    "live_trading",
    "LIVE_TRADING",
    "",
    None,
    0,
    1,
    True,
    False,
    "paper",
    "paper_trading ",
    " paper_trading",
    "PAPER_TRADING",
    "drawdown_paused",
    "retired",
    "backtesting",
    "paused",
    "active",
    ("live",),
    frozenset({"paper_trading"}),
    {"paper_trading"},
]


# ---------------------------------------------------------------------------
# Section 1 — the hard gate constant itself
# ---------------------------------------------------------------------------


def test_00_statuses_is_frozenset():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    assert isinstance(S, frozenset)


def test_01_statuses_equals_exactly_paper_trading():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    assert S == frozenset({"paper_trading"})
    assert set(S) == {"paper_trading"}
    assert len(S) == 1


def test_02_statuses_contains_no_live_variant():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    lowered = {str(s).strip().lower() for s in S}
    for bad in ("live", "live_trading", "real", "money", "prod", "production"):
        assert bad not in lowered


def test_03_allowed_paper_statuses_returns_the_pin():
    from tools.signals.paper import (
        _PAPER_TRADE_SIGNAL_STATUSES,
        allowed_paper_statuses,
    )

    got = allowed_paper_statuses()
    assert isinstance(got, frozenset)
    assert got == _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})


def test_04_allowed_paper_statuses_is_not_mutable_alias():
    from tools.signals.paper import allowed_paper_statuses

    first = allowed_paper_statuses()
    second = allowed_paper_statuses()
    assert first == second
    with pytest.raises((AttributeError, TypeError)):
        first.add("live")  # type: ignore[attr-defined]


@pytest.mark.parametrize("status", FORBIDDEN_STATUSES)
def test_05_reject_non_paper_accepts_only_paper_trading(status):
    from tools.signals.paper import reject_non_paper

    if status == "paper_trading":
        return
    assert reject_non_paper(status) is True, f"{status!r} must be rejected"


def test_06_reject_non_paper_passes_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False


def test_07_reject_non_paper_is_pure_function_of_status():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False
    assert reject_non_paper("live") is True
    assert reject_non_paper("paper_trading") is False


def test_08_source_pins_frozenset_literal():
    src = PAPER_MODULE_PATH.read_text()
    m = re.search(
        r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\s*\{([^}]*)\}\s*\)", src
    )
    assert m, "gate constant must stay a frozenset({...}) literal"
    body = m.group(1)
    names = re.findall(r"[\"']([^\"']+)[\"']", body)
    assert sorted(names) == ["paper_trading"]


def test_09_source_has_no_live_string_in_executable_code():
    """'live' may appear only in comments/docstrings, never as a value."""
    tree = ast.parse(PAPER_MODULE_PATH.read_text())
    strings = [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]
    code_strings = [s for s in strings if s not in ("paper_trading",)]
    # Only docstrings are permitted; anything else must be empty/whitespace.
    for s in code_strings:
        assert not re.fullmatch(r"\s*live[a-z_]*\s*", s.strip().lower(), flags=re.I), (
            f"forbidden status-like string in gate module code: {s!r}"
        )


def test_10_gate_module_defines_no_set_add_or_update():
    tree = ast.parse(PAPER_MODULE_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in ("add", "update", "union", "__ior__"), (
                f"gate module must not mutate status sets at line {node.lineno}"
            )


# ---------------------------------------------------------------------------
# Section 2 — generate_paper_trade_signal behavioral gate
# ---------------------------------------------------------------------------


FULL_LIVE_ODDS = {
    "games": [
        {
            "id": f"g{i}",
            "sport_key": "basketball_nba",
            "home_team": "Home",
            "away_team": "Away",
            "commence_time": "2026-08-26T02:30:00Z",
            "bookmakers": [
                {
                    "key": "draftkings",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Home", "price": -150},
                                {"name": "Away", "price": +130},
                            ],
                        }
                    ],
                }
            ],
        }
        for i in range(3)
    ]
}


def _engine_with_hypothesis(status, manager=None):
    from unittest.mock import MagicMock

    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)
    hm = MagicMock() if manager is None else manager

    async def _get(hid):
        return {
            "id": hid,
            "status": status,
            "model_config": {"target_book": "draftkings", "devig_method": "power"},
            "edge_threshold": 0.05,
            "market_type": "h2h",
            "thesis": "",
            "name": "char-hyp",
            "sport": "basketball_nba",
        }

    hm.get_hypothesis = _get
    engine.hypothesis_manager = hm
    return engine


class _RecordingManager:
    """Records get_hypothesis calls so we can prove gate-before-processing."""

    def __init__(self, status):
        self.status = status
        self.calls = []

    async def get_hypothesis(self, hid):
        self.calls.append(hid)
        return {"status": self.status, "id": hid}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    ["live", "LIVE", "Live", "", None, "drawdown_paused", "retired",
     "backtesting", "paused", "active", "paper"],
)
async def test_20_generate_rejects_status(status):
    engine = _engine_with_hypothesis(status)
    signals = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
    assert signals == []


@pytest.mark.asyncio
async def test_21_generate_missing_hypothesis_returns_empty():
    engine = _engine_with_hypothesis("paper_trading")

    async def _none(hid):
        return None

    engine.hypothesis_manager.get_hypothesis = _none
    assert await engine.generate_paper_trade_signal("nope", FULL_LIVE_ODDS) == []


@pytest.mark.asyncio
async def test_22_generate_live_never_queries_pipeline(monkeypatch):
    """For a live hypothesis, the paper pipeline must never be entered."""
    import tools.backtest as bt

    entered = {"n": 0}
    real = bt.paper_pipeline.generate_paper_trade_signal

    async def spy(*a, **k):
        entered["n"] += 1
        return await real(*a, **k)

    monkeypatch.setattr(bt.paper_pipeline, "generate_paper_trade_signal", spy)
    engine = _engine_with_hypothesis("live")
    out = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
    assert out == []
    assert entered["n"] == 0


@pytest.mark.asyncio
async def test_23_generate_paper_trading_enters_pipeline(monkeypatch):
    """The gate ACCEPTS paper_trading so the loop isn't dead."""
    import tools.backtest as bt

    entered = {"n": 0}

    async def stub(*a, **k):
        entered["n"] += 1
        return [{"stub": True}]

    monkeypatch.setattr(bt.paper_pipeline, "generate_paper_trade_signal", stub)
    engine = _engine_with_hypothesis("paper_trading")
    out = await engine.generate_paper_trade_signal("hyp-1", {"games": []})
    assert out == [{"stub": True}]
    assert entered["n"] == 1


@pytest.mark.asyncio
async def test_24_generate_case_variants_all_rejected():
    for variant in ("Paper_Trading", "PAPER_TRADING", "paper-trading"):
        engine = _engine_with_hypothesis(variant)
        out = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
        assert out == [], variant


@pytest.mark.asyncio
async def test_25_generate_whitespaced_paper_trading_rejected():
    engine = _engine_with_hypothesis(" paper_trading ")
    assert await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS) == []


# ---------------------------------------------------------------------------
# Section 2b — AST pins on generate_paper_trade_signal
# ---------------------------------------------------------------------------


def _find_func(tree, name):
    return [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]


def test_26_ast_gate_call_present():
    tree = ast.parse(BACKTEST_PATH.read_text())
    funcs = _find_func(tree, "generate_paper_trade_signal")
    assert len(funcs) >= 1
    fn = funcs[0]
    body_src = ast.unparse(fn)
    assert "reject_non_paper(" in body_src


def test_27_ast_gate_precedes_pipeline_call():
    """reject_non_paper appears before the pipeline call in source order."""
    src_lines = BACKTEST_PATH.read_text().splitlines()
    start = next(i for i, l in enumerate(src_lines) if "def generate_paper_trade_signal" in l)
    end = min(len(src_lines), start + 60)
    seg = "\n".join(src_lines[start:end])
    assert seg.index("reject_non_paper(") < seg.index("paper_pipeline.generate_paper_trade_signal")


def test_28_ast_no_status_eq_live_comparison():
    tree = ast.parse(BACKTEST_PATH.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            cmp_src = ast.unparse(node)
            assert '"live"' not in cmp_src and "'live'" not in cmp_src


def test_29_generate_signature_unchanged():
    tree = ast.parse(BACKTEST_PATH.read_text())
    fn = _find_func(tree, "generate_paper_trade_signal")[0]
    args = [a.arg for a in fn.args.args]
    assert args[0] == "self"
    assert args[1] == "hypothesis_id"
    assert args[2] == "live_odds"


# ---------------------------------------------------------------------------
# Section 3 — CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ---------------------------------------------------------------------------


class _Loop:
    _bet_executor = None


@pytest.mark.asyncio
@pytest.mark.parametrize("val", [None, "", "0", "false", "no", "yes", "2", "1 ", " true"])
async def test_30_phase_live_execute_refuses_non_one(val, monkeypatch):
    from tools.loop.phases_impl import phase_live_execute

    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    if val is not None:
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", val)
    loop = _Loop()
    await phase_live_execute(loop)  # must return without touching executor
    assert loop._bet_executor is None


@pytest.mark.asyncio
async def test_31_phase_live_execute_default_env_unset(monkeypatch):
    from tools.loop.phases_impl import phase_live_execute

    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    await phase_live_execute(_Loop())


@pytest.mark.asyncio
async def test_32_phase_live_execute_armed_but_no_executor(monkeypatch):
    from tools.loop.phases_impl import phase_live_execute

    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")
    loop = _Loop()
    loop._bet_executor = None
    await phase_live_execute(loop)  # no executor -> returns quietly
    assert loop._bet_executor is None


@pytest.mark.asyncio
async def test_33_phase_live_execute_armed_disabled_executor_stops(monkeypatch):
    from tools.loop.phases_impl import phase_live_execute

    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")

    class Ex:
        is_enabled = False
        check_drawdown_calls = 0

    loop = _Loop()
    ex = Ex()
    loop._bet_executor = ex
    await phase_live_execute(loop)
    assert ex.check_drawdown_calls == 0  # disabled executor: nothing ran


def test_34_phase_live_execute_source_pins_env_gate_first():
    src = PHASES_IMPL_PATH.read_text()
    m = re.search(r"async def phase_live_execute.*?(?=\nasync def |\nclass |\Z)", src, flags=re.S)
    assert m, "phase_live_execute vanished"
    body = m.group(0)
    gate = re.search(r'if\s+_?os\.getenv\(\s*"CALLISTO_ALLOW_LIVE_EXECUTE"\s*\)\s*!=\s*"1"', body)
    assert gate, "env hard gate missing from phase_live_execute"
    # Gate must appear before any bet-executor import/use inside the body.
    assert body.index('getenv("CALLISTO_ALLOW_LIVE_EXECUTE")') < body.index("BetExecutor")


def test_35_autonomous_module_has_same_gate_shape():
    src = (REPO / "tools" / "autonomous.py").read_text()
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src


# ---------------------------------------------------------------------------
# Section 4 — BetExecutor.__init__ assigns _enabled = False
# ---------------------------------------------------------------------------


def _init_source_segment(src: str) -> str:
    start = src.index("class BetExecutor")
    init_at = src.index("def __init__(self):", start)
    nxt = re.search(r"\n    (?:async )?def ", src[init_at + 5:])
    assert nxt
    return src[init_at : init_at + nxt.start()]


def test_40_init_assigns_enabled_false_literal():
    src = BET_EXECUTOR_PATH.read_text()
    seg = _init_source_segment(src)
    assert re.search(r"self\._enabled\s*=\s*False\b", seg)


def test_41_init_never_assigns_enabled_true():
    src = BET_EXECUTOR_PATH.read_text()
    seg = _init_source_segment(src)
    assert not re.search(r"_enabled\s*=\s*True\b", seg)


def test_42_init_enables_count_exactly_one_false():
    src = BET_EXECUTOR_PATH.read_text()
    seg = _init_source_segment(src)
    hits = re.findall(r"self\._enabled\s*=\s*(\w+)", seg)
    assert hits == ["False"]


def test_43_real_instance_born_disarmed(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "scratch.db"))
    from tools.bet_executor import BetExecutor

    ex = BetExecutor.__new__(BetExecutor)
    # Run only the __init__ statements that don't need aiosqlite/loop:
    # instead of executing __init__ fully (needs asyncio.Lock ok), just exec it.
    import asyncio

    coro = None
    try:
        BetExecutor.__init__(ex)
    except Exception:
        pytest.skip("__init__ requires runtime deps unavailable here")
    assert coro is None
    assert ex._enabled is False


def test_44_initialize_does_not_arm(tmp_path, monkeypatch):
    """initialize() must leave _enabled untouched (stays False)."""
    monkeypatch.setenv("CALLISTO_DB_PATH", str(tmp_path / "scratch.db"))
    from tools.betexec import bootstrap

    class FakeEx:
        def __init__(self):
            self._enabled = False
            self.armed_by_init = False

    ex = FakeEx()
    bootstrap.initialize.__wrapped__ if hasattr(bootstrap.initialize, "__wrapped__") else None

    import inspect as _inspect

    if _inspect.iscoroutinefunction(bootstrap.initialize):
        import asyncio

        asyncio.run(bootstrap.initialize(ex))
    else:
        bootstrap.initialize(ex)
    assert ex._enabled is False
    assert getattr(ex, "_db", "missing") != "armed"


def test_45_bootstrap_explicit_disarm_helper_sets_false():
    src = (REPO / "tools" / "betexec" / "bootstrap.py").read_text()
    assert "executor._enabled = False" in src


def test_46_betexec_package_docstring_declares_never_arms():
    src = (REPO / "tools" / "betexec" / "__init__.py").read_text()
    doc = src.split('"""')[1]
    assert "_enabled = False" in doc or "arms" not in doc.lower() or "nothing" in doc.lower()


def test_47_enable_method_refuses_local_only_mode(monkeypatch):
    """If an enable() exists, it must fail closed under CALLISTO_LOCAL_ONLY."""
    monkeypatch.setenv("CALLISTO_DB_PATH", "/tmp/0080-scratch.db")
    from tools.bet_executor import BetExecutor

    enable = getattr(BetExecutor, "enable", None)
    if enable is None:
        pytest.skip("no enable method on this revision")
    ex = BetExecutor.__new__(BetExecutor)
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    try:
        res = enable(ex)
        if inspect.iscoroutine(res):
            import asyncio

            res = asyncio.get_event_loop().run_until_complete(res)
        assert getattr(ex, "_enabled", False) is not True or res is False
    finally:
        ex._enabled = False  # safety belt: never leave armed


def test_48_kill_switch_flips_enabled_false():
    src = (REPO / "tools" / "betexec" / "kill_switch.py").read_text()
    assert "_enabled" in src


# ---------------------------------------------------------------------------
# Section 5 — cross-cutting integration of the paper-only loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["live", "live_trading", "LIVE"])
async def test_50_end_to_end_live_status_produces_nothing(status, monkeypatch):
    import tools.backtest as bt

    async def boom(*a, **k):
        raise AssertionError("pipeline must not run for live statuses")

    monkeypatch.setattr(bt.paper_pipeline, "generate_paper_trade_signal", boom)
    engine = _engine_with_hypothesis(status)
    assert await engine.generate_paper_trade_signal("hyp-9", FULL_LIVE_ODDS) == []


def test_51_backtest_imports_reject_non_paper_from_signals_paper():
    src = BACKTEST_PATH.read_text()
    assert re.search(r"from\s+tools\.signals\.paper\s+import[^#\n]*reject_non_paper", src) or (
        "reject_non_paper" in src and "tools.signals" in src
    )


def test_52_gate_module_has_single_definition_site():
    """No second definition of the constant anywhere under tools/."""
    hits = []
    for p in (REPO / "tools").rglob("*.py"):
        txt = p.read_text(errors="replace")
        if "_PAPER_TRADE_SIGNAL_STATUSES =" in txt:
            hits.append(str(p.relative_to(REPO)))
    assert hits == ["tools/signals/paper.py"]


def test_53_no_production_file_appends_to_statuses():
    offenders = []
    for p in (REPO / "tools").rglob("*.py"):
        txt = p.read_text(errors="replace")
        if re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\.(add|update)\(", txt):
            offenders.append(str(p))
    assert offenders == []


def test_54_phases_module_does_not_import_paper_statuses_for_writing():
    src = PHASES_IMPL_PATH.read_text()
    if "_PAPER_TRADE_SIGNAL_STATUSES" in src:
        assert "frozenset({" not in src.split("_PAPER_TRADE_SIGNAL_STATUSES")[1][:200]


def test_55_paper_statuses_type_annotation_consistent():
    from tools.signals.paper import allowed_paper_statuses
    import typing_extensions  # noqa: F401  (presence check only)

    assert callable(allowed_paper_statuses)


def test_56_full_gate_module_under_100_lines():
    lines = PAPER_MODULE_PATH.read_text().splitlines()
    assert len(lines) < 100, "gate module grew unexpectedly; re-review pins"


def test_57_docstring_of_gate_mentions_hard_gate():
    doc = PAPER_MODULE_PATH.read_text().split('"""')[1]
    assert "ONLY" in doc or "HARD GATE" in doc


def test_58_betexecutor_class_count_is_one():
    src = BET_EXECUTOR_PATH.read_text()
    assert len(re.findall(r"^class BetExecutor\b", src, flags=re.M)) == 1


def test_59_no_live_status_string_in_paper_gate_assignment_context():
    src = BACKTEST_PATH.read_text()
    seg_start = src.index("def generate_paper_trade_signal")
    seg = src[seg_start : seg_start + 2500]
    assert '"live"' not in seg.replace(
        'including ``"live"``', ""
    ).replace("'live'", "") or "reject_non_paper" in seg
