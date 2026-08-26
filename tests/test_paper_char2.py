"""More characterization tests: paper-only loop invariants (char wave 2).

Companion to ``tests/test_paper_cycle_char.py``. This module adds a second,
independent wave of safety pins over the same invariants, written from a
different angle so that a single refactor is unlikely to slip past both:

  A. ``_PAPER_TRADE_SIGNAL_STATUSES`` is a frozen set containing exactly
     ``{"paper_trading"}`` — never ``"live"``, never anything else.
  B. The gate helpers (``allowed_paper_statuses`` / ``reject_non_paper``)
     fail closed for every non-paper status string we can enumerate.
  C. ``BacktestEngine.generate_paper_trade_signal`` returns ``[]`` for every
     non-paper status and never touches odds processing for them.
  D. ``_phase_live_execute`` (both the ResearchLoop wrapper and the
     ``tools.loop.phases_impl`` implementation) is a no-op unless
     ``CALLISTO_ALLOW_LIVE_EXECUTE == "1"`` — exactly that string.
  E. ``BetExecutor`` starts with ``_enabled = False``, ``enable()`` refuses
     under ``CALLISTO_LOCAL_ONLY``, and ``disable()``/``shutdown()`` always
     land back at disabled.
  F. Source-level greps: no module in the paper path may add ``"live"`` to
     the paper frozenset or widen the gate.

Everything here is characterization: if an invariant changes intentionally on
master, update the pin in the same commit.
"""

import ast
import asyncio
import inspect
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PAPER_MODULE = REPO / "tools" / "signals" / "paper.py"
BACKTEST_MODULE = REPO / "tools" / "backtest.py"
AUTONOMOUS_MODULE = REPO / "tools" / "autonomous.py"
PHASES_IMPL_MODULE = REPO / "tools" / "loop" / "phases_impl.py"
EXECUTOR_MODULE = REPO / "tools" / "bet_executor.py"

FORBIDDEN_STATUSES = (
    "live",
    "LIVE",
    "Live",
    " live",
    "live ",
    "paper",
    "paper_trading ",
    "pending",
    "active",
    "armed",
    "prod",
    "production",
    "real_money",
    "",
    None,
)

# Statuses that plausibly exist in the hypothesis lifecycle today. None of
# them may pass the paper gate except paper_trading itself.
KNOWN_STATUSES = (
    "proposed",
    "backtesting",
    "paper_trading",
    "promoted",
    "retired",
    "drawdown_paused",
    "failed",
    "live",
)


def _read(rel: Path) -> str:
    assert rel.exists(), f"expected file missing: {rel}"
    return rel.read_text(encoding="utf-8")


def _parse(rel: Path) -> ast.AST:
    return ast.parse(_read(rel))


def _find_func(tree: ast.AST, name: str):
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == name
    ]


# ---------------------------------------------------------------------------
# A. The frozenset itself
# ---------------------------------------------------------------------------


def test_frozenset_type_and_membership():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    assert isinstance(S, frozenset), "gate must be immutable"
    assert S == {"paper_trading"}
    assert "live" not in S


def test_frozenset_len_is_one():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    assert len(S) == 1, f"gate widened: {sorted(S)!r}"


@pytest.mark.parametrize("status", [s for s in FORBIDDEN_STATUSES if s is not None])
def test_no_forbidden_status_in_frozenset(status):
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

    assert status not in S


def test_module_source_declares_frozenset_literal_with_paper_trading_only():
    src = _read(PAPER_MODULE)
    # The literal assignment must be a single-element frozenset/set of exactly
    # one string literal, 'paper_trading'.
    tree = _parse(PAPER_MODULE)
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES"
            for t in node.targets
        ):
            call = node.value
            assert isinstance(call, ast.Call), "gate must be built from a call"
            fname = getattr(call.func, "id", getattr(call.func, "attr", ""))
            assert fname in ("frozenset", "set"), f"unexpected constructor {fname}"
            assert len(call.args) == 1
            arg = call.args[0]
            if isinstance(arg, ast.Set):
                elems = arg.elts
            else:
                elems = arg.elts  # frozenset({...})
            assert len(elems) == 1, "gate must contain exactly one element"
            assert isinstance(elems[0], ast.Constant)
            assert elems[0].value == "paper_trading"
            found.append(node)
    assert found, "_PAPER_TRADE_SIGNAL_STATUSES assignment not found"


def test_paper_module_never_mentions_armed_live_word_as_status():
    """The word 'live' may appear in comments/docstrings but never as a set
    element or comparison target inside tools/signals/paper.py."""
    tree = _parse(PAPER_MODULE)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.strip().lower() != "live", (
                "'live' appears as a string constant in the paper gate module"
            )


# ---------------------------------------------------------------------------
# B. Gate helpers fail closed
# ---------------------------------------------------------------------------


def test_allowed_paper_returns_equal_copy():
    from tools.signals.paper import allowed_paper_statuses

    got = allowed_paper_statuses()
    assert isinstance(got, frozenset)
    assert got == {"paper_trading"}
    # Mutating the returned value must not affect the internal gate.
    with pytest.raises(AttributeError):
        got.add("live")


@pytest.mark.parametrize("status", FORBIDDEN_STATUSES)
def test_reject_non_paper_rejects_everything_else(status):
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(status) is True


def test_reject_non_paper_accepts_only_exact_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False
    # Near-misses must still be rejected.
    for near in ("Paper_trading", "PAPER_TRADING", "paper-trading", "paper trading"):
        assert reject_non_paper(near) is True, f"near-miss accepted: {near!r}"


@pytest.mark.parametrize(
    "status",
    [s for s in KNOWN_STATUSES if s != "paper_trading"],
)
def test_known_non_paper_statuses_are_rejected(status):
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(status) is True


def test_helpers_do_not_read_env_or_config():
    """The gate helpers must be pure — no os.getenv / config lookups."""
    src = _read(PAPER_MODULE)
    for func in ("allowed_paper_statuses", "reject_non_paper"):
        # crude but effective: extract each function's block
        start = src.index(f"def {func}")
        nxt = src.find("\ndef ", start + 1)
        body = src[start : nxt if nxt != -1 else len(src)]
        assert "getenv" not in body, f"{func} must not consult the environment"
        assert "config" not in body.lower() or "docstring" in body.lower()


# ---------------------------------------------------------------------------
# C. generate_paper_trade_signal hard gate (runtime)
# ---------------------------------------------------------------------------


class _FakeHypManager:
    def __init__(self, hyp):
        self._hyp = hyp
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._hyp


class _FakeEngine:
    """Minimal stand-in carrying only what the gate touches before failing."""

    def __init__(self, hyp):
        self.hypothesis_manager = _FakeHypManager(hyp)
        self._parse_hypothesis_filters_calls = 0

    def _parse_hypothesis_filters(self, *a, **k):
        self._parse_hypothesis_filters_calls += 1
        return {}

    generate_paper_trade_signal = BacktestEngineRef = None


async def _run_gate(hyp, live_odds):
    from tools.backtest import BacktestEngine

    engine = object.__new__(BacktestEngine)
    engine.hypothesis_manager = _FakeHypManager(hyp)
    engine._parse_hypothesis_filters_calls = 0

    def _no_filters(*a, **k):
        engine._parse_hypothesis_filters_calls += 1
        return {}

    engine._parse_hypothesis_filters = _no_filters
    out = await BacktestEngine.generate_paper_trade_signal(engine, "h1", live_odds)
    return out, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [s for s in FORBIDDEN_STATUSES if s])
async def test_generate_paper_trade_signal_empty_for_status(status):
    hyp = {
        "status": status,
        "model_config": {},
        "edge_threshold": 0.03,
        "thesis": "",
        "name": "",
        "sport": "",
    }
    out, engine = await _run_gate(hyp, {"games": [{"id": "g1"}]})
    assert out == []
    assert engine.hypothesis_manager.calls == 1
    # Fail-closed: filter parsing (odds processing) must never run.
    assert engine._parse_hypothesis_filters_calls == 0


@pytest.mark.asyncio
async def test_generate_paper_trade_signal_empty_when_hyp_none():
    out, engine = await _run_gate(None, {"games": []})
    assert out == []
    assert engine._parse_hypothesis_filters_calls == 0


@pytest.mark.asyncio
async def test_generate_paper_trade_signal_live_status_never_processes():
    """The explicit 'live' case: even a fully-formed live hypothesis gets []."""
    hyp = {
        "status": "live",
        "model_config": {"target_book": "draftkings"},
        "edge_threshold": 0.05,
        "thesis": "strong edge",
        "name": "live hyp",
        "sport": "basketball_nba",
    }
    live_odds = {"games": [{"id": "g1", "home_team": "A", "away_team": "B"}]}
    out, engine = await _run_gate(hyp, live_odds)
    assert out == []
    assert engine._parse_hypothesis_filters_calls == 0


def test_generate_paper_trade_signal_source_pins_fail_closed_order():
    """In the method source, reject_non_paper/get_hypothesis must appear
    before any odds-processing identifier."""
    src = _read(BACKTEST_MODULE)
    idx_method = src.index("async def generate_paper_trade_signal")
    nxt = src.find("\n    async def ", idx_method + 1)
    body = src[idx_method : nxt if nxt != -1 else len(src)]
    first_gate = min(
        (body.find(pat) for pat in ("reject_non_paper", "not h") if body.find(pat) != -1)
    )
    assert first_gate != -1
    for later_pat in ("_parse_hypothesis_filters", "devig", "signals.append"):
        pos = body.find(later_pat)
        if pos != -1:
            assert pos > first_gate, (
                f"{later_pat!r} appears before the fail-closed gate in "
                "generate_paper_trade_signal"
            )
    # Use AST so the docstring is excluded exactly: mentioning "live" in
    # the safety docstring is fine; a code-level comparison is not.
    tree = _parse(BACKTEST_MODULE)
    methods = _find_func(tree, "generate_paper_trade_signal")
    assert len(methods) == 1
    m = methods[0]
    body_nodes = list(m.body)
    if (
        body_nodes
        and isinstance(body_nodes[0], ast.Expr)
        and isinstance(body_nodes[0].value, ast.Constant)
        and isinstance(body_nodes[0].value.value, str)
    ):
        body_nodes = body_nodes[1:]
    code = ast.unparse(ast.Module(body=body_nodes, type_ignores=[]))
    assert '"live"' not in code and "'live'" not in code, (
        "generate_paper_trade_signal must not special-case 'live'"
    )


def test_backtest_imports_reject_non_paper_from_signals_paper():
    src = _read(BACKTEST_MODULE)
    assert "from tools.signals.paper import" in src or (
        "from tools.signals import" in src and "reject_non_paper" in src
    ), "tools/backtest.py should import the paper gate"


def test_no_other_tools_module_defines_its_own_paper_status_set():
    """Only tools/signals/paper.py may define the gate constant. Other modules
    may import it (backtest.py) or mention it in docstrings, but no second
    assignment is allowed."""
    tree = _parse(PAPER_MODULE)
    assert any(
        isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES" for t in n.targets)
        for n in ast.walk(tree)
    ), "paper.py must define the gate"

    offenders = []
    for py in (REPO / "tools").rglob("*.py"):
        if py == PAPER_MODULE:
            continue
        try:
            text = py.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "_PAPER_TRADE_SIGNAL_STATUSES =" in text or "_PAPER_TRADE_SIGNAL_STATUSES=" in text:
            offenders.append(str(py.relative_to(REPO)))
        else:
            try:
                pt = ast.parse(text)
            except SyntaxError:
                continue
            for node in ast.walk(pt):
                if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES"
                    for t in node.targets
                ):
                    offenders.append(str(py.relative_to(REPO)))
    assert not offenders, f"gate assigned outside paper.py: {offenders}"


# ---------------------------------------------------------------------------
# D. _phase_live_execute stays off by default
# ---------------------------------------------------------------------------


def test_phase_live_execute_wrapper_checks_env_string_exactly():
    src = _read(AUTONOMOUS_MODULE)
    idx = src.index("async def _phase_live_execute")
    nxt = src.find("\n    async def ", idx + 1)
    body = src[idx : nxt if nxt != -1 else len(src)]
    assert 'os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE")' in body or (
        '_os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE")' in body
    )
    assert '!= "1"' in body, "wrapper must compare strictly against '1'"


def test_phases_impl_phase_live_execute_checks_env_before_listing():
    src = _read(PHASES_IMPL_MODULE)
    idx = src.index("def phase_live_execute")
    nxt = src.find("\ndef ", idx + 1)
    body = src[idx : nxt if nxt != -1 else len(src)]
    env_pos = body.find("CALLISTO_ALLOW_LIVE_EXECUTE")
    assert env_pos != -1, "impl missing env check"
    # Nothing that looks like hypothesis listing may precede the env check.
    for early in ("list_hypotheses", "get_hypotheses", "fetch", "db.execute"):
        pos = body.find(early)
        if pos != -1:
            assert pos > env_pos, f"{early} runs before the arming gate"


def _fresh_loop_phase_runner(monkeypatch, env_value):
    """Build a tiny object exposing only _phase_live_execute via the real
    ResearchLoop method, without importing heavy state."""
    import tools.autonomous as auto_mod

    loop = object.__new__(auto_mod.ResearchLoop)
    called = {"impl": False}

    async def fake_impl(self):
        called["impl"] = True

    monkeypatch.setattr(
        auto_mod.phases_impl, "phase_live_execute", fake_impl, raising=True
    )
    if env_value is None:
        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
    else:
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", env_value)
    return loop, called


@pytest.mark.asyncio
async def test_phase_live_execute_noop_without_env(monkeypatch):
    loop, called = _fresh_loop_phase_runner(monkeypatch, None)
    await loop._phase_live_execute()
    assert called["impl"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("val", ["0", "", "true", "yes", "1 ", " 1", "on"])
async def test_phase_live_execute_refuses_non_exact_one(monkeypatch, val):
    loop, called = _fresh_loop_phase_runner(monkeypatch, val)
    await loop._phase_live_execute()
    assert called["impl"] is False, f"value {val!r} must NOT arm live execute"


@pytest.mark.asyncio
async def test_phase_live_execute_delegates_only_on_exact_one(monkeypatch):
    loop, called = _fresh_loop_phase_runner(monkeypatch, "1")
    await loop._phase_live_execute()
    assert called["impl"] is True


# ---------------------------------------------------------------------------
# E. BetExecutor starts disabled and stays safe
# ---------------------------------------------------------------------------


def _make_executor():
    from tools.bet_executor import BetExecutor

    ex = object.__new__(BetExecutor)
    ex._enabled = True  # sabotage: pretend someone force-enabled it
    ex._logged_in = False
    ex._browser = None
    ex._context = None
    ex._page = None
    ex._db = None
    return ex


def test_bet_executor_class_has_enabled_false_in_init_source():
    src = _read(EXECUTOR_MODULE)
    tree = _parse(EXECUTOR_MODULE)
    classes = [
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BetExecutor"
    ]
    assert len(classes) == 1
    inits = [n for n in classes[0].body if isinstance(n, ast.FunctionDef) and n.name == "__init__"]
    assert len(inits) == 1
    init_src = ast.get_source_segment(src, inits[0]) or ast.unparse(inits[0])
    assert "self._enabled = False" in init_src


def test_bet_executor_default_construction_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "")
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    assert ex.is_enabled is False
    assert ex._enabled is False


def test_enable_refuses_local_only_truthy(monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


@pytest.mark.parametrize("val", ["true", "YES", "yes", "True"])
def test_enable_refuses_all_truthy_local_only_spellings(val, monkeypatch):
    monkeypatch.setenv("CALLISTO_LOCAL_ONLY", val)
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    assert ex.enable() is False
    assert ex.is_enabled is False


def test_disable_always_lands_disabled_even_after_sabotage():
    ex = _make_executor()
    assert ex._enabled is True
    ex.disable()
    assert ex.is_enabled is False


def test_disable_idempotent():
    from tools.bet_executor import BetExecutor

    ex = BetExecutor()
    ex.disable()
    ex.disable()
    ex.disable()
    assert ex.is_enabled is False


@pytest.mark.asyncio
async def test_shutdown_resets_enabled_flag():
    ex = _make_executor()
    await ex.shutdown()
    assert ex.is_enabled is False
    assert ex._logged_in is False
    assert ex._page is None


def test_enable_refuses_local_only_via_ast_pin():
    """Source pin: enable() must reference CALLISTO_LOCAL_ONLY before setting
    _enabled = True."""
    src = _read(EXECUTOR_MODULE)
    idx = src.index("def enable(")
    nxt = src.find("\n    def ", idx + 1)
    body = src[idx : nxt if nxt != -1 else len(src)]
    local_only_pos = body.find("CALLISTO_LOCAL_ONLY")
    true_pos = body.find("self._enabled = True")
    assert local_only_pos != -1, "enable() lost the LOCAL_ONLY guard"
    assert true_pos != -1
    assert local_only_pos < true_pos, "guard must precede arming"


def test_place_bet_path_guards_on_enabled():
    """Some enabled-check must exist guarding bet placement."""
    src = _read(EXECUTOR_MODULE)
    assert "_enabled" in src
    # Find the guard usage outside __init__/enable/disable/shutdown/status.
    tree = _parse(EXECUTOR_MODULE)
    guard_methods = []
    classes = [
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "BetExecutor"
    ]
    for method in classes[0].body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if method.name in ("__init__", "enable", "disable", "shutdown", "is_enabled", "status"):
            continue
        seg = ast.unparse(method)
        if "_enabled" in seg:
            guard_methods.append(method.name)
    assert guard_methods, "no method besides lifecycle checks the enabled flag"
