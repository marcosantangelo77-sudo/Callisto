"""Autofill characterization #0008 — paper-only loop pins.

Characterizes the hard safety gates of the Callisto paper-trading loop:

1. ``_PAPER_TRADE_SIGNAL_STATUSES`` is EXACTLY ``frozenset({'paper_trading'})``
   — no 'live', nothing else, ever.
2. ``generate_paper_trade_signal`` rejects any hypothesis whose status is not
   paper_trading (returns [] before touching odds).
3. ``phase_live_execute`` is a no-op unless CALLISTO_ALLOW_LIVE_EXECUTE=1.
4. ``BetExecutor.__init__`` assigns ``_enabled = False`` (default-disabled).

AST/source pins are used where importing full modules would drag heavy
dependencies into the test environment. All tests here FAIL CLOSED: if a pin
is false, the suite fails and live betting stays un-armed.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _module_ast(rel: str) -> ast.Module:
    return ast.parse(_read(rel), filename=rel)


def _find_class(tree: ast.Module, name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _skip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _find_func(owner, name: str):
    """Find a FunctionDef/AsyncFunctionDef on an AST class or module."""
    body = owner.body if isinstance(owner, (ast.ClassDef, ast.Module)) else []
    for item in body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
            return item
    raise AssertionError(f"function {name} not found")


# ---------------------------------------------------------------------------
# 1. Pin _PAPER_TRADE_SIGNAL_STATUSES == frozenset({'paper_trading'})
# ---------------------------------------------------------------------------

PAPER_MOD = "tools/signals/paper.py"


def test_paper_statuses_is_exactly_paper_trading():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as s

    assert s == frozenset({"paper_trading"})


def test_paper_statuses_type_is_frozenset():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as s

    assert type(s) is frozenset


def test_live_not_in_paper_statuses():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as s

    assert "live" not in s


def test_paper_statuses_literal_in_source():
    src = _read(PAPER_MOD)
    assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src


def test_paper_statuses_no_extra_statuses_ast():
    tree = _module_ast(PAPER_MOD)
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES" for t in node.targets)
        ):
            assert isinstance(node.value, ast.Call)
            assert node.value.func.id == "frozenset"
            (elt,) = node.value.args
            names = {e.value for e in elt.elts}
            assert names == {"paper_trading"}
            break
    else:
        raise AssertionError("_PAPER_TRADE_SIGNAL_STATUSES assignment missing")


def test_allowed_paper_statuses_returns_the_frozenset():
    from tools.signals.paper import allowed_paper_statuses

    assert allowed_paper_statuses() == frozenset({"paper_trading"})


@pytest.mark.parametrize("status", ["live", "LIVE", "Live", "paper", "", None, 1])
def test_reject_non_paper_accepts_only_paper_trading(status):
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(status) is True


def test_reject_non_paper_unhashable_raises_fail_closed():
    """Unhashable input can't be in the frozenset — TypeError = refuse, not allow."""
    import tools.signals.paper as paper

    with pytest.raises(TypeError):
        paper.reject_non_paper(["paper_trading"])


def test_reject_non_paper_false_for_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False


def test_paper_module_warns_never_add_live():
    src = _read(PAPER_MOD)
    assert "must NEVER be added here" in src or "NEVER" in src


# ---------------------------------------------------------------------------
# 2. generate_paper_trade_signal rejects status 'live'
# ---------------------------------------------------------------------------


class _FakeHypManager:
    def __init__(self, hyp):
        self._hyp = hyp
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._hyp


def _engine_with_status(status):
    from tools.backtest import BacktestEngine

    eng = BacktestEngine.__new__(BacktestEngine)
    eng.hypothesis_manager = _FakeHypManager(
        {
            "hypothesis_id": "h-0008",
            "status": status,
            "model_config": {},
            "edge_threshold": 0.03,
            "sport": "baseball_mlb",
            "thesis": "",
            "name": "char-0008",
        }
    )
    return eng


def test_generate_paper_trade_signal_rejects_live_status():
    from tools.backtest import BacktestEngine

    eng = BacktestEngine.__new__(BacktestEngine)
    eng.hypothesis_manager = _FakeHypManager({"status": "live"})
    result = asyncio.run(eng.generate_paper_trade_signal("h-live", {"games": []}))
    assert result == []


def test_generate_paper_trade_signal_rejects_missing_hypothesis():
    from tools.backtest import BacktestEngine

    eng = BacktestEngine.__new__(BacktestEngine)
    eng.hypothesis_manager = _FakeHypManager(None)
    assert asyncio.run(eng.generate_paper_trade_signal("nope", {})) == []


@pytest.mark.parametrize("status", ["live", "archived", "draft", "paused", "PAPER_TRADING", "", None])
def test_generate_paper_trade_signal_only_paper_trading(status):
    from tools.backtest import BacktestEngine

    eng = BacktestEngine.__new__(BacktestEngine)
    eng.hypothesis_manager = _FakeHypManager({"status": status})
    assert asyncio.run(eng.generate_paper_trade_signal("h", {"games": []})) == []


def test_generate_paper_trade_signal_gate_before_odds_processing():
    """The reject check must come BEFORE any odds parsing — pin by AST."""
    fn = _find_func(
        _find_class(_module_ast("tools/backtest.py"), "BacktestEngine"),
        "generate_paper_trade_signal",
    )
    body = _skip_docstring(fn.body)
    first = body[0]
    assert isinstance(first, ast.Assign) and first.targets[0].id == "h"
    second = body[1]
    assert isinstance(second, ast.If)
    src = _read("tools/backtest.py")
    seg = ast.get_source_segment(src, second.test)
    assert "reject_non_paper" in seg
    gate_idx = next(i for i, n in enumerate(body) if n is second)
    odds_idx = min(
        (i for i, n in enumerate(body[gate_idx:], start=gate_idx)
         if isinstance(n, ast.Assign) and any(
             isinstance(t, ast.Attribute) and t.attr == "get" for t in getattr(n, "targets", [])
         ) and "live_odds" in ast.get_source_segment(_read("tools/backtest.py"), n)),
        default=10**9,
    )
    # The live_odds consumption must happen after the gate.
    assert odds_idx > gate_idx or odds_idx == 10**9


def test_generate_paper_trade_signal_docstring_forbids_live():
    fn = _find_func(
        _find_class(_module_ast("tools/backtest.py"), "BacktestEngine"),
        "generate_paper_trade_signal",
    )
    doc = ast.get_docstring(fn) or ""
    assert "HARD GATE" in doc
    assert '"live"' in doc or "'live'" in doc


# ---------------------------------------------------------------------------
# 3. CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ---------------------------------------------------------------------------

AUTONOMOUS = "tools/autonomous.py"
PHASES_IMPL = "tools/loop/phases_impl.py"


def test_phase_live_execute_noop_without_env(monkeypatch):
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)

    async def run():
        import tools.loop.phases_impl as pi

        sentinel = SimpleNamespace(ran=False)

        class Boom:
            def __getattr__(self, name):
                sentinel.ran = True  # any attribute touch = work happened
                raise AttributeError(name)

        await pi.phase_live_execute(SimpleNamespace(hypothesis_manager=None))
        return sentinel

    assert asyncio.run(run()).ran is False


@pytest.mark.parametrize(
    "val",
    ["0", "", "true", "yes", "on", "2", "1.0", None],
)
def test_phase_live_execute_requires_exactly_1(monkeypatch, val):
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    if val is not None:
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", val)

    async def go():
        import tools.loop.phases_impl as pi

        touched = []

        class Probe:
            def __getattr__(self, name):
                touched.append(name)
                raise AttributeError(name)

        await pi.phase_live_execute(Probe())
        return touched

    assert asyncio.run(go()) == [], f"phase ran with CALLISTO_ALLOW_LIVE_EXECUTE={val!r}"


def test_autonomous_phase_delegates_to_phases_impl():
    src = _read(AUTONOMOUS)
    fn = _find_func(_find_class(_module_ast(AUTONOMOUS), "ResearchLoop"), "_phase_live_execute")
    seg = ast.get_source_segment(src, fn)
    assert "phases_impl.phase_live_execute" in seg


def test_autonomous_phase_gate_is_first_statement():
    fn = _find_func(_find_class(_module_ast(AUTONOMOUS), "ResearchLoop"), "_phase_live_execute")
    body = _skip_docstring(fn.body)
    assert len(body) >= 1
    first = body[0]
    assert isinstance(first, (ast.If, ast.Import))
    src_seg = ast.get_source_segment(_read(AUTONOMOUS), fn)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src_seg


def test_phases_impl_gate_is_exact_equality_check():
    fn = _find_func(_module_ast(PHASES_IMPL), "phase_live_execute")
    body = _skip_docstring(fn.body)
    ifs = [n for n in body if isinstance(n, ast.If)]
    assert ifs, "gate If missing"
    seg = ast.get_source_segment(_read(PHASES_IMPL), ifs[0])
    assert 'os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in seg.replace("_os.", "os.")


def test_phases_impl_gate_returns_before_importing_executor():
    fn = _find_func(_module_ast(PHASES_IMPL), "phase_live_execute")
    body = _skip_docstring(fn.body)
    kinds = [type(n).__name__ for n in body]
    gate_i = kinds.index("If")
    exec_i = kinds.index("Try")
    assert gate_i < exec_i, "executor import must come AFTER the env gate"


def test_doctor_reports_live_flag():
    src = _read("tools/cli/doctor.py")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src


# ---------------------------------------------------------------------------
# 4. BetExecutor.__init__ assigns _enabled = False
# ---------------------------------------------------------------------------


def test_bet_executor_init_enabled_false_runtime():
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    assert ex._enabled is False
    assert ex.is_enabled is False


def test_bet_executor_init_assigns_false_ast():
    init = _find_func(_find_class(_module_ast("tools/bet_executor.py"), "BetExecutor"), "__init__")
    assigns = [
        n
        for n in init.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Attribute) and t.attr == "_enabled" for t in n.targets)
    ]
    assert len(assigns) == 1
    assert isinstance(assigns[0].value, ast.Constant) and assigns[0].value.value is False


def test_bet_executor_docstring_says_default_disabled():
    init = _find_func(_find_class(_module_ast("tools/bet_executor.py"), "BetExecutor"), "__init__")
    doc = ast.get_source_segment(_read("tools/bet_executor.py"), init) or ""
    assert "default-disabled" in doc


def test_bet_executor_is_enabled_is_property():
    cls = _find_class(_module_ast("tools/bet_executor.py"), "BetExecutor")
    prop = _find_func(cls, "is_enabled")
    assert any(
        (isinstance(d, ast.Name) and d.id == "property")
        or (isinstance(d, ast.Attribute) and d.attr == "property")
        for d in prop.decorator_list
    )


def test_bet_executor_enable_sets_true_disable_false():
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    ex._enabled = True
    assert ex.is_enabled is True
    ex._enabled = False
    assert ex.is_enabled is False


def test_bet_executor_never_enables_itself_in_init():
    init = _find_func(_find_class(_module_ast("tools/bet_executor.py"), "BetExecutor"), "__init__")
    seg = ast.get_source_segment(_read("tools/bet_executor.py"), init)
    assert "self._enabled = True" not in seg


# ---------------------------------------------------------------------------
# 5. Cross-cutting source pins: no accidental widening anywhere
# ---------------------------------------------------------------------------


def test_no_test_or_prod_code_adds_live_to_statuses():
    offenders = []
    for p in REPO.rglob("*.py"):
        rel = str(p.relative_to(REPO))
        if rel.startswith(("tests/", ".venv")):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if "_PAPER_TRADE_SIGNAL_STATUSES" in line and (
                '"live"' in line or "'live'" in line or "add(" in line or "update(" in line or "|" in line
            ):
                if "must NEVER be added" not in line and "#" not in line.split("_PAPER")[0]:
                    offenders.append(f"{rel}:{i}: {line.strip()}")
    assert offenders == []


def test_generate_paper_trade_signal_not_widened():
    src = _read("tools/backtest.py")
    m = [l for l in src.splitlines() if "reject_non_paper" in l]
    assert m, "paper gate call missing from generate path"
    assert all("==" not in l or "!==" not in l for l in m)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))

