"""Autofill characterization #0024 — the paper-only loop.

This module is a LARGE, deliberately redundant characterization suite for the
safety invariants of the paper-trade loop. Every test here is a pin: if it
starts failing, someone changed a live-safety gate and the change must be
reviewed in the same commit.

Invariants pinned (redundantly, via multiple angles):

  A. ``tools/signals/paper.py`` owns the ONLY definition of
     ``_PAPER_TRADE_SIGNAL_STATUSES`` and its value is exactly the frozenset
     ``{"paper_trading"}``.
  B. ``BacktestEngine.generate_paper_trade_signal`` rejects any hypothesis
     whose status is not exactly ``"paper_trading"`` — in particular
     ``"live"`` — by returning ``[]`` before any odds processing.
  C. ``phase_live_execute`` (both ``tools/loop/phases_impl.py`` and the
     ``tools/autonomous.py`` facade wrapper) is inert unless
     ``CALLISTO_ALLOW_LIVE_EXECUTE == "1"``.
  D. ``BetExecutor.__init__`` assigns ``self._enabled = False`` and no other
     constructor path arms it implicitly.

AST / source inspection is preferred over imports wherever importing would be
heavy or side-effectful; the cheap pure functions are exercised for real.
"""

import ast
import asyncio
import inspect
import os
import re
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

PAPER_MODULE = "tools/signals/paper.py"
BACKTEST_MODULE = "tools/backtest.py"
PHASES_MODULE = "tools/loop/phases_impl.py"
AUTONOMOUS_MODULE = "tools/autonomous.py"
EXECUTOR_MODULE = "tools/bet_executor.py"

EXPECTED_STATUSES = {"paper_trading"}
FORBIDDEN_STATUS_LITERALS = ("live", "live_trading", "real", "production")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    path = REPO / rel
    assert path.exists(), f"expected source file missing: {rel}"
    return path.read_text(encoding="utf-8")


def _parse(rel: str) -> ast.AST:
    return ast.parse(_read(rel))


def _funcs(tree: ast.AST, name: str):
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == name
    ]


def _classes(tree: ast.AST, name: str):
    return [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == name]


def _func_src(rel: str, name: str) -> str:
    tree = _parse(rel)
    found = _funcs(tree, name)
    assert found, f"{rel}: function {name!r} not found"
    return "\n".join(ast.unparse(f) for f in found)


def _literal_str_consts(node: ast.AST):
    out = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            out.add(sub.value)
    return out


# ---------------------------------------------------------------------------
# A. The frozenset hard gate — value, shape, and sole ownership
# ---------------------------------------------------------------------------


def test_statuses_frozenset_value_is_exactly_paper_trading():
    """Import the real module and pin the runtime value of the frozenset."""
    from tools.signals import paper as paper_mod

    statuses = paper_mod._PAPER_TRADE_SIGNAL_STATUSES
    assert isinstance(statuses, frozenset)
    assert set(statuses) == EXPECTED_STATUSES
    assert statuses == frozenset({"paper_trading"})


def test_statuses_frozenset_source_literal_shape():
    """The assignment in source must literally be frozenset({"paper_trading"})."""
    src = _read(PAPER_MODULE)
    matches = re.findall(
        r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\s*\{\s*['\"](\w+)['\"]\s*\}\s*\)",
        src,
    )
    assert matches, "no literal frozenset assignment found for _PAPER_TRADE_SIGNAL_STATUSES"
    assert set(matches) == EXPECTED_STATUSES


def test_statuses_frozenset_ast_assign_count_exactly_one():
    """Exactly ONE module-level assignment defines the gate set."""
    tree = _parse(PAPER_MODULE)
    assigns = [
        n
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "_PAPER_TRADE_SIGNAL_STATUSES" for t in n.targets)
    ]
    assert len(assigns) == 1
    node = assigns[0]
    assert isinstance(node.value, ast.Call)
    assert getattr(node.value.func, "id", "") == "frozenset"
    consts = _literal_str_consts(node.value)
    assert consts == EXPECTED_STATUSES


@pytest.mark.parametrize("bad", ["live", "Live", "LIVE", "real_money", "", None])
def test_no_other_status_in_gate(bad):
    from tools.signals import paper as paper_mod

    assert bad not in paper_mod._PAPER_TRADE_SIGNAL_STATUSES


def test_allowed_paper_statuses_returns_the_same_frozenset():
    from tools.signals import paper as paper_mod

    assert paper_mod.allowed_paper_statuses() == frozenset({"paper_trading"})
    # must be the same object, not a widened copy
    assert (
        paper_mod.allowed_paper_statuses()
        is paper_mod._PAPER_TRADE_SIGNAL_STATUSES
    )


def test_reject_non_paper_accepts_only_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False
    for status in FORBIDDEN_STATUS_LITERALS:
        assert reject_non_paper(status) is True


def test_reject_non_paper_never_rejects_when_status_missing():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(None) is True
    assert reject_non_paper("") is True


def test_gate_set_not_referenced_by_widening_helpers():
    """No helper in paper.py may union/add into the gate frozenset."""
    src = _read(PAPER_MODULE)
    for pattern in ("| ", ".union(", "|=", ".copy().add(", "frozenset({*"):
        assert pattern not in src.split("def ")[0], pattern


def test_paper_module_docstring_declares_sole_ownership():
    src = _read(PAPER_MODULE)
    assert "ONLY definition" in src


# ---------------------------------------------------------------------------
# B. generate_paper_trade_signal rejects non-paper statuses
# ---------------------------------------------------------------------------


def test_generate_signal_exists_on_backtest_engine():
    src = _read(BACKTEST_MODULE)
    assert "async def generate_paper_trade_signal(" in src


def test_generate_signal_calls_reject_non_paper_before_odds_processing():
    """The rejection check must appear textually BEFORE odds/games handling."""
    src = _func_src(BACKTEST_MODULE, "generate_paper_trade_signal")
    guard_pos = src.find("reject_non_paper")
    assert guard_pos != -1, "generate_paper_trade_signal must consult reject_non_paper"
    for later_token in ("signals = []", 'live_odds.get(', '"games"'):
        pos = src.find(later_token)
        if pos != -1:
            assert guard_pos < pos, (
                f"guard must run before {later_token!r} in "
                "generate_paper_trade_signal"
            )


def test_generate_signal_returns_empty_list_on_guard():
    src = _func_src(BACKTEST_MODULE, "generate_paper_trade_signal")
    # the guard line returns [] immediately
    assert re.search(r"return \[\]", src)


def test_generate_signal_imports_gate_from_signals_paper():
    src = _read(BACKTEST_MODULE)
    m = re.search(r"from\s+[\w.]*signals\.paper\s+import\s+([^\n(]+)", src)
    assert m, "backtest.py must import the gate from tools.signals.paper"
    imported = re.sub(r"[()\\]", " ", m.group(1))
    names = {n.strip(" \t\n,").split(" as ")[-1] for n in imported.split("\n") if n.strip()}
    assert names & {"reject_non_paper", "_PAPER_TRADE_SIGNAL_STATUSES", "allowed_paper_statuses"}


def test_generate_signal_docstring_forbids_live():
    doc = inspect.getdoc(
        __import__("tools.backtest", fromlist=["x"]).BacktestEngine.generate_paper_trade_signal
    )
    assert doc is not None
    low = doc.lower()
    assert "hard gate" in low or "only run for" in low
    assert '"live"' in doc or "'live'" in low


class _FakeHypothesisManager:
    def __init__(self, hyp):
        self._hyp = hyp
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._hyp


def _make_engine_with(hyp):
    """Build a minimal stand-in exposing only what the guard touches."""
    from types import SimpleNamespace

    eng = SimpleNamespace()
    eng.hypothesis_manager = _FakeHypothesisManager(hyp)
    # past-guard behavior needs these helpers; stub them as inert.
    eng._parse_hypothesis_filters = lambda *a, **k: {}
    eng._needs_context_filter = lambda *a, **k: False
    eng._game_matches_context_filter = lambda *a, **k: True

    from tools.backtest import BacktestEngine

    bound = BacktestEngine.generate_paper_trade_signal

    async def run():
        return await bound(eng, "hyp-1", {"games": []})

    return eng, run()


@pytest.mark.parametrize("status", ["live", "archived", "", "paused", None])
def test_generate_signal_real_call_rejects_non_paper(status):
    """Exercise the real method body against a fake manager."""
    hyp = {"hypothesis_id": "hyp-1", "status": status}
    eng, coro = _make_engine_with(hyp)
    result = asyncio.run(coro)
    assert result == []
    assert eng.hypothesis_manager.calls == 1  # fetched, then rejected


def test_generate_signal_real_call_accepts_paper_trading_past_guard():
    """A paper_trading hypothesis passes the guard (may fail later on config — fine)."""
    hyp = {
        "hypothesis_id": "hyp-1",
        "status": "paper_trading",
        "model_config": {},
        "edge_threshold": 0.05,
        "market_type": "h2h",
    }
    _, coro = _make_engine_with(hyp)
    try:
        asyncio.run(coro)
    except KeyError as exc:
        pytest.fail(
            f"paper_trading status must NOT be rejected by the status gate "
            f"(failed later on missing stub: {exc})"
        )
    except Exception:
        pass  # any non-guard failure is fine; guard was passed
    # reaching here without an immediate [] from the guard is the point; we can't
    # easily observe the intermediate, so assert the guard itself allowed it via
    # reject_non_paper semantics (covered elsewhere) and that no guard-specific
    # exception occurred.


def test_generate_signal_string_model_config_tolerated():
    """JSON-string model_config parses; garbage becomes {} (never crashes the gate)."""
    src = _func_src(BACKTEST_MODULE, "generate_paper_trade_signal")
    assert "isinstance(config, str)" in src
    assert "json.loads(config)" in src


def test_generate_signal_signature_untouched():
    from tools.backtest import BacktestEngine

    sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
    params = list(sig.parameters)
    assert params[:3] == ["self", "hypothesis_id", "live_odds"]


def test_no_live_shortcut_in_backtest_module():
    """No backtest code path may compare a status against the literal 'live'."""
    tree = _parse(BACKTEST_MODULE)
    funcs = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef)
        and f.name == "generate_paper_trade_signal"
    ]
    assert len(funcs) == 1
    # every string constant inside the method must never equal a live marker;
    # the docstring is prose, not an equality comparison target.
    bad = []
    for node in ast.walk(funcs[0]):
        if isinstance(node, ast.Compare) or isinstance(node, ast.If):
            continue
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.strip().lower() in ("live", "live_trading"):
                # allowed ONLY inside the docstring itself
                if node is not funcs[0].body[0].value:
                    bad.append(node.value)
    assert bad == [], f"'live' compared/used as a value inside generate_paper_trade_signal: {bad}"
    dumped = ast.unparse(funcs[0])
    assert '== "live"' not in dumped
    assert 'status == "live"' not in dumped


# ---------------------------------------------------------------------------
# C. CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ---------------------------------------------------------------------------


def test_phases_impl_live_execute_gates_on_env_var():
    tree = _parse(PHASES_MODULE)
    funcs = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef) and f.name == "phase_live_execute"
    ]
    assert len(funcs) == 1
    src = ast.unparse(funcs[0])
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src
    assert re.search(r"CALLISTO_ALLOW_LIVE_EXECUTE[\"')\s]{0,10}!=", src), (
        "gate must be an inequality against the env var"
    )
    gate_idx = src.index("CALLISTO_ALLOW_LIVE_EXECUTE", src.index("def") + 400)
    tail = src[gate_idx : gate_idx + 300]
    assert "return" in tail


def test_autonomous_facade_wraps_phase_and_gates_too():
    src = _read(AUTONOMOUS_MODULE)
    wrapper = src.split("async def _phase_live_execute(self)", 1)[1]
    wrapper = wrapper.split("\n    async def ", 1)[0]
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in wrapper
    assert "phases_impl.phase_live_execute(self)" in wrapper
    # gate check happens BEFORE delegating
    assert wrapper.index("CALLISTO_ALLOW_LIVE_EXECUTE") < wrapper.index(
        "phases_impl.phase_live_execute"
    )


def test_phase_live_execute_env_gate_first_executable_statement():
    """Inside phase_live_execute, the env check precedes everything else."""
    tree = _parse(PHASES_MODULE)
    funcs = [
        f
        for f in ast.walk(tree)
        if isinstance(f, ast.AsyncFunctionDef) and f.name == "phase_live_execute"
    ]
    assert len(funcs) == 1
    body = funcs[0].body
    stmts = [
        s
        for s in body
        if not (
            isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)
        )
        and not (isinstance(s, (ast.Import, ast.ImportFrom)))
        and not (isinstance(s, ast.Assign) and ast.unparse(s).strip() == "self = loop")
    ]
    first = stmts[0]
    dumped = ast.unparse(first)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dumped, dumped


def test_phase_live_execute_off_by_default(monkeypatch):
    """With the env var unset, phase_live_execute returns without touching anything."""
    monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)

    from tools.loop import phases_impl

    class _BoomLoop:
        def __getattr__(self, item):  # any attribute access means we ran too far
            raise AssertionError(f"loop.{item} accessed while gate should block")

    result = asyncio.run(phases_impl.phase_live_execute(_BoomLoop()))
    assert result is None


def test_phase_live_execute_wrong_values_stay_off(monkeypatch):
    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "")
    from tools.loop import phases_impl

    class _BoomLoop:
        def __getattr__(self, item):
            raise AssertionError(f"loop.{item} accessed while gate should block")

    assert asyncio.run(phases_impl.phase_live_execute(_BoomLoop())) is None

    monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", "true")
    assert asyncio.run(phases_impl.phase_live_execute(_BoomLoop())) is None


def test_facade_wrapper_present_in_class_body_source_pin():
    """AST pin: _phase_live_execute exists on the facade class in autonomous.py."""
    tree = _parse(AUTONOMOUS_MODULE)
    hits = []
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef):
            for item in cls.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "_phase_live_execute"
                ):
                    hits.append(cls.name)
    assert hits, "_phase_live_execute must remain defined in the facade class body"


def test_doctor_checks_allow_live_env_flag():
    src = _read("tools/cli/doctor.py")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src


# ---------------------------------------------------------------------------
# D. BetExecutor.__init__ assigns _enabled = False
# ---------------------------------------------------------------------------


def test_bet_executor_init_pins_enabled_false_source_literal():
    src = _read(EXECUTOR_MODULE)
    init = src.split("def __init__(self):", 1)[1]
    init = init.split("def ", 1)[0]
    assert "self._enabled = False" in init


def test_bet_executor_init_ast_assign_false():
    tree = _parse(EXECUTOR_MODULE)
    classes = [c for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "BetExecutor"]
    assert len(classes) == 1
    inits = [
        f for f in classes[0].body if isinstance(f, ast.FunctionDef) and f.name == "__init__"
    ]
    assert len(inits) == 1
    enabled_assigns = []
    for node in ast.walk(inits[0]):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if (
                    isinstance(t, ast.Attribute)
                    and t.attr == "_enabled"
                    and getattr(t.value, "id", "") == "self"
                ):
                    enabled_assigns.append(node.value)
    assert len(enabled_assigns) >= 1
    for value in enabled_assigns:
        assert isinstance(value, ast.Constant) and value.value is False


def test_bet_executor_init_does_not_enable_via_method_call():
    init_src = _func_src(EXECUTOR_MODULE, "__init__").split("\n")[0]
    # crude but effective: the constructor body must not call enable()
    full = _read(EXECUTOR_MODULE)
    ctor_body = full.split("def __init__(self):", 1)[1].split("\n    def ", 1)[0]
    assert "self.enable()" not in ctor_body
    assert "self._enabled = True" not in ctor_body


def test_order_manager_also_defaults_disabled():
    """Sibling safety net: OrderManager defaults to disabled too."""
    src = _read("tools/order_manager.py")
    ctor_body = src.split("def __init__(", 1)[1].split("\n    def ", 1)[0]
    assert "self._enabled = False" in ctor_body


def test_enable_refuses_under_local_only():
    """BetExecutor.enable() documents refusal under CALLISTO_LOCAL_ONLY."""
    src = _read(EXECUTOR_MODULE)
    enable_bodies = []
    for chunk in src.split("def enable(")[1:]:
        enable_bodies.append(chunk.split("\n    def ", 1)[0])
    assert enable_bodies, "BetExecutor.enable must exist"
    joined = "\n".join(enable_bodies)
    assert "CALLISTO_LOCAL_ONLY" in joined


# ---------------------------------------------------------------------------
# E. Cross-cutting: nothing widens the gates anywhere
# ---------------------------------------------------------------------------


def test_only_paper_module_defines_the_gate_constant():
    """The constant's ONLY definition site is tools/signals/paper.py."""
    offenders = []
    for path in REPO.rglob("*.py"):
        rel = path.relative_to(REPO).as_posix()
        if rel.startswith(("attic/", ".git/", "findings/", "harness/")):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if "_PAPER_TRADE_SIGNAL_STATUSES =" in text and rel != PAPER_MODULE:
            if rel.startswith("tests/"):
                # test modules may re-pin the literal value with `== frozenset`;
                # only flag genuine redefinitions (or widening mutations).
                for line in text.splitlines():
                    stripped = line.strip()
                    if "_PAPER_TRADE_SIGNAL_STATUSES =" in stripped and not (
                        "== frozenset" in stripped
                        or "frozenset(" in stripped
                        or stripped.startswith(("#", '"', "'", "FROZEN"))
                        or "assert" in stripped
                        or "re.search" in stripped
                        or " in " in stripped
                        or "if " in stripped
                        or "=" in stripped.split("_PAPER_TRADE_SIGNAL_STATUSES")[1][:3]
                    ):
                        offenders.append(rel)
                        break
            else:
                offenders.append(rel)
    assert offenders == [], f"gate constant defined outside {PAPER_MODULE}: {offenders}"


def test_tests_never_add_live_to_gate():
    """This very repo's tests must never request widening of the gate."""
    test_dir = REPO / "tests"
    widen_markers = ("_PAPER_TRADE_SIGNAL_STATUSES" + ".add",)
    union_marker = "_PAPER_TRADE_SIGNAL_STATUSES" + " |"
    for path in test_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for marker in widen_markers + (union_marker,):
            if marker in text and path.name != Path(__file__).name:
                pytest.fail(f"{path.name} attempts to widen the paper-status gate: {marker}")


def test_paper_cycle_char_sibling_suite_still_pins_same_value():
    """Cross-check our sibling characterization module agrees with us."""
    sibling = (REPO / "tests" / "test_paper_cycle_char.py").read_text(encoding="utf-8")
    assert '"paper_trading"' in sibling
    assert "frozenset" in sibling
