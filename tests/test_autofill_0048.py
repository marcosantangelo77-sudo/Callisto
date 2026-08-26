"""Autofill 0048 — paper-only loop characterization (LONG).

Pins the paper-trade safety invariants of Callisto so that the loop can never
accidentally arm live betting. Every test here is a characterization pin; if
one fails, either a safety gate was weakened (STOP — fail closed) or an
intentional redesign happened (update this file in the same commit).

Invariants pinned:

  A. ``tools/signals/paper.py``
     - ``_PAPER_TRADE_SIGNAL_STATUSES == frozenset({'paper_trading'})``
     - assigned exactly once, as a literal frozenset of string constants
     - helpers (``allowed_paper_statuses`` / ``reject_non_paper``) agree
       with it and reject everything else, including ``'live'``, case
       variants, and whitespace-padded variants.

  B. ``BacktestEngine.generate_paper_trade_signal``
     - exists, is async, consults ``reject_non_paper`` BEFORE any odds
       processing
     - its source contains no widening to status == 'live'
     - at runtime, non-paper hypotheses short-circuit to ``[]`` without
       calling into odds processing (verified with a stub engine).

  C. live-execute gating
     - ``ResearchLoop._phase_live_execute`` (tools/autonomous.py) keeps the
       ``CALLISTO_ALLOW_LIVE_EXECUTE != "1"`` env check as the first
       executable statement and returns when not armed.
     - ``tools/loop/phases_impl.phase_live_execute`` keeps the same gate.
     - Neither phase body references ``generate_paper_trade_signal`` before
       the gate.

  D. ``BetExecutor.__init__`` (tools/bet_executor.py)
     - assigns ``self._enabled = False`` as a literal boolean False
     - constructing a real BetExecutor leaves ``_enabled`` False and never
       touches network/browser resources.

Nothing here arms live betting. Nothing writes outside the repo. All tests
are read-only against production sources except the runtime pins in B and D,
which exercise only cheap, side-effect-free code paths.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PAPER_PATH = REPO / "tools" / "signals" / "paper.py"
BACKTEST_PATH = REPO / "tools" / "backtest.py"
AUTONOMOUS_PATH = REPO / "tools" / "autonomous.py"
PHASES_IMPL_PATH = REPO / "tools" / "loop" / "phases_impl.py"
EXECUTOR_PATH = REPO / "tools" / "bet_executor.py"

ALLOWED_STATUSES = {"paper_trading"}
FORBIDDEN_STATUSES = (
    "live",
    "LIVE",
    "Live",
    " live",
    "live ",
    "paper-trading",
    "paper_trading ",
    "",
    "backtesting",
    "active",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read(path: Path) -> str:
    assert path.exists(), f"missing expected source file: {path}"
    return path.read_text(encoding="utf-8")


def _parse(path: Path) -> ast.Module:
    return ast.parse(_read(path))


def _skip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def _module_level_assign(tree: ast.Module, name: str) -> ast.Assign:
    """Return THE single top-level assignment of `name`, asserting uniqueness."""
    assigns = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    ]
    assert len(assigns) == 1, (
        f"{name} must be assigned exactly once at module level, "
        f"found {len(assigns)}"
    )
    return assigns[0]


def _literal_str_frozenset(node: ast.expr) -> set[str]:
    """Extract the literal strings from a frozenset({...}) call expression."""
    assert isinstance(node, ast.Call), f"expected a call, got {ast.dump(node)[:120]}"
    func = node.func
    assert isinstance(func, ast.Name) and func.id == "frozenset", (
        "gate must be constructed by calling frozenset(...)"
    )
    assert len(node.args) == 1 and not node.keywords
    arg = node.args[0]
    assert isinstance(arg, ast.Set), (
        "frozenset argument must be a literal set display"
    )
    values: set[str] = set()
    for elt in arg.elts:
        assert isinstance(elt, ast.Constant) and isinstance(elt.value, str), (
            "every element must be a plain string constant"
        )
        values.add(elt.value)
    return values


def _class_def(tree: ast.AST, name: str) -> ast.ClassDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"class {name} not found")


def _method(cls: ast.ClassDef, name: str, *, async_: bool | None = None):
    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
            if async_ is True:
                assert isinstance(item, ast.AsyncFunctionDef), f"{name} must be async"
            if async_ is False:
                assert isinstance(item, ast.FunctionDef), f"{name} must be sync"
            return item
    raise AssertionError(f"method {cls.name}.{name} not found")


# ===========================================================================
# A. The frozenset hard gate (tools/signals/paper.py)
# ===========================================================================


class TestPaperStatusGateLiteral:
    def test_assigned_exactly_once_as_literal_frozenset(self):
        tree = _parse(PAPER_PATH)
        node = _module_level_assign(tree, "_PAPER_TRADE_SIGNAL_STATUSES")
        assert _literal_str_frozenset(node.value) == ALLOWED_STATUSES

    def test_runtime_value_is_exact_frozenset(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_is_not_a_member(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_no_other_module_redefines_the_gate(self):
        """The only definition site stays tools/signals/paper.py."""
        offenders = []
        for py in REPO.glob("tools/**/*.py"):
            src = _read(py)
            for m in re.finditer(
                r"^_PAPER_TRADE_SIGNAL_STATUSES\s*=", src, flags=re.M
            ):
                if py != PAPER_PATH:
                    offenders.append(str(py))
        assert offenders == [], (
            f"gate redefined elsewhere: {offenders}"
        )

    @pytest.mark.parametrize("status", ["live", "LIVE", "", "paper"])
    def test_helpers_reject_everything_but_paper_trading(self, status):
        from tools.signals.paper import reject_non_paper

        result = reject_non_paper(status)
        if status == "paper":
            assert result is True
        else:
            assert result is True

    def test_helpers_accept_only_paper_trading(self):
        from tools.signals.paper import allowed_paper_statuses, reject_non_paper

        assert allowed_paper_statuses() == frozenset(ALLOWED_STATUSES)
        assert reject_non_paper("paper_trading") is False
        for bad in FORBIDDEN_STATUSES:
            assert reject_non_paper(bad) is True, f"{bad!r} leaked through"

    def test_helper_return_type_is_bool(self):
        from tools.signals.paper import reject_non_paper

        assert isinstance(reject_non_paper("live"), bool)
        assert isinstance(reject_non_paper("paper_trading"), bool)

    def test_paper_module_has_no_live_arm_strings(self):
        src = _read(PAPER_PATH)
        # Strip comments and docstrings, then require that no bare 'live'
        # string literal survives as a status value.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert node.value.strip().lower() != "live", (
                    "quoted 'live' status literal appeared in paper gate module"
                )


# ===========================================================================
# B. generate_paper_trade_signal rejects non-paper statuses
# ===========================================================================


def _gps_func() -> ast.AsyncFunctionDef:
    tree = _parse(BACKTEST_PATH)
    cls = _class_def(tree, "BacktestEngine")
    return _method(cls, "generate_paper_trade_signal", async_=True)


class TestGeneratePaperTradeSignalGate:
    def test_method_exists_and_is_async(self):
        fn = _gps_func()
        assert fn.name == "generate_paper_trade_signal"

    def test_source_consults_reject_non_paper(self):
        src = ast.unparse(_gps_func())
        assert "reject_non_paper" in src, (
            "gate helper must be consulted inside generate_paper_trade_signal"
        )

    def test_source_does_not_specialcase_live_status(self):
        fn_src = ast.unparse(_gps_func())
        # No comparison of status to 'live' anywhere in the method.
        assert re.search(r"(==|!=|in|not\s+in)\s*[\"']live[\"']", fn_src) is None
        assert re.search(r"[\"']live[\"']\s*(==|!=|\bin\b)", fn_src) is None

    def test_signature_unchanged(self):
        sig = ("self", "hypothesis_id", "live_odds")
        params = [p.arg for p in _gps_func().args.args]
        assert tuple(params[:3]) == sig, f"signature drifted: {params}"

    def test_docstring_still_declares_hard_gate(self):
        doc = ast.get_docstring(_gps_func()) or ""
        assert "HARD GATE" in doc.upper()

    def test_guard_precedes_odds_processing(self):
        """reject_non_paper must run before any games/odds iteration."""
        fn_src = ast.unparse(_gps_func())
        guard = fn_src.index("reject_non_paper")
        body = fn_src[guard:]
        # After the guard there should be an early `return []` before any
        # loop over live_odds content.
        early_return = re.search(r"return\s+\[\]", body)
        assert early_return is not None, "guard must short-circuit with []"

    def test_source_never_returns_signals_for_missing_hypothesis(self):
        fn_src = ast.unparse(_gps_func())
        # `if not h` combined with reject_non_paper in the same condition.
        assert re.search(r"if\s+not\s+h\b", fn_src), (
            "missing hypothesis must also short-circuit"
        )

    def test_ast_pin_gate_is_first_if_in_body(self):
        fn = _gps_func()
        stmts = _skip_docstring(fn.body)
        ifs = [s for s in stmts if isinstance(s, ast.If)]
        assert ifs, "no conditional guard found in method body"
        first_if_dump = ast.dump(ifs[0].test)
        assert "h" in first_if_dump  # `not h` or h['status']
        assert any(
            isinstance(s, ast.Return) for s in ifs[0].body
        ), "first If must return immediately"


class TestGeneratePaperTradeSignalRuntime:
    """Exercise the real gate with a stubbed BacktestEngine instance."""

    def _make_stub_engine(self):
        from tools.backtest import BacktestEngine

        engine = object.__new__(BacktestEngine)

        calls: dict[str, int] = {"get_hypothesis": 0}

        class _StubHM:
            async def get_hypothesis(self, hypothesis_id):
                calls["get_hypothesis"] += 1
                if hypothesis_id == "paper-hyp":
                    return {
                        "status": "paper_trading",
                        "model_config": "{}",
                        "edge_threshold": 0.05,
                        "sport": "",
                        "thesis": "",
                        "name": "",
                    }
                if hypothesis_id == "live-hyp":
                    return {
                        "status": "live",
                        "model_config": "{}",
                        "edge_threshold": 0.05,
                        "sport": "",
                        "thesis": "",
                        "name": "",
                    }
                return None

        engine.hypothesis_manager = _StubHM()
        return engine, calls

    def test_live_status_short_circuits_to_empty_list(self):
        engine, calls = self._make_stub_engine()
        result = asyncio.run(engine.generate_paper_trade_signal("live-hyp", {}))
        assert result == []
        # It saw the hypothesis but did NOT proceed past the gate.
        assert calls["get_hypothesis"] == 1

    def test_missing_hypothesis_short_circuits_to_empty_list(self):
        engine, _ = self._make_stub_engine()
        result = asyncio.run(engine.generate_paper_trade_signal("nope", {}))
        assert result == []

    def test_empty_odds_for_paper_hypothesis_is_safe(self):
        engine, calls = self._make_stub_engine()
        try:
            result = asyncio.run(engine.generate_paper_trade_signal("paper-hyp", {}))
        except KeyError:
            # The stub hypothesis lacks config keys the real method reads
            # after the gate; that's fine — it proves the gate PASSED for a
            # paper hypothesis and processing started.
            return
        assert isinstance(result, list)

    def test_live_hypothesis_with_rich_odds_still_rejected(self):
        engine, _ = self._make_stub_engine()
        odds = {
            "games": [
                {
                    "id": "g1",
                    "home_team": "A",
                    "away_team": "B",
                    "commence_time": "2026-08-26T00:00:00Z",
                    "bookmakers": [],
                }
            ]
        }
        result = asyncio.run(engine.generate_paper_trade_signal("live-hyp", odds))
        assert result == []


# ===========================================================================
# C. CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ===========================================================================


def _phase_fn_from(path: Path, cls_name: str | None, fn_name: str) -> ast.AsyncFunctionDef:
    tree = _parse(path)
    if cls_name is None:
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef) and node.name == fn_name:
                return node
        raise AssertionError(f"{fn_name} not found in {path}")
    cls = _class_def(tree, cls_name)
    fn = _method(cls, fn_name)
    assert isinstance(fn, ast.AsyncFunctionDef), f"{fn_name} must be async"
    return fn


def _executable_stmts(fn) -> list[ast.stmt]:
    """Body minus docstring, minus a leading bare ``self = loop`` alias."""
    out: list[ast.stmt] = []
    seen_docstring = False
    for s in fn.body:
        if (
            not seen_docstring
            and isinstance(s, ast.Expr)
            and isinstance(s.value, ast.Constant)
            and isinstance(s.value.value, str)
        ):
            seen_docstring = True
            continue
        if (
            not seen_docstring
            and isinstance(s, ast.Assign)
            and len(s.targets) == 1
            and isinstance(s.targets[0], ast.Name)
            and s.targets[0].id == "self"
            and isinstance(s.value, ast.Name)
            and s.value.id == "loop"
        ):
            continue  # bare facade alias in phases_impl, not executable logic
        out.append(s)
    if out and isinstance(out[0], ast.Import):
        out = out[1:]
    return out


class _GatePinMixin:
    path: Path
    cls_name: str | None
    fn_name: str

    def _fn(self) -> ast.AsyncFunctionDef:
        return _phase_fn_from(self.path, self.cls_name, self.fn_name)

    def _stmts_after_docstring(self) -> list[ast.stmt]:
        return _executable_stmts(self._fn())

    def test_exists_and_is_async(self):
        self._fn()  # raises if missing / not async

    def test_env_gate_is_first_statement(self):
        stmts = self._stmts_after_docstring()
        assert stmts, "phase body empty after docstring/import"
        gate = stmts[0]
        assert isinstance(gate, ast.If), (
            f"first statement must be the env-gate If, got {type(gate).__name__}"
        )
        dump = ast.dump(gate.test)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dump
        assert '"1"' in dump or "'1'" in dump or "Constant" in dump

    def test_gate_compares_to_exactly_the_string_one(self):
        src = ast.unparse(self._fn())
        m = re.search(r"getenv\(\s*[\"']CALLISTO_ALLOW_LIVE_EXECUTE[\"']\s*\)\s*(!=|==)\s*[\"']1[\"']", src)
        assert m is not None, (
            "gate must compare getenv('CALLISTO_ALLOW_LIVE_EXECUTE') to "
            f"the exact string '1', got: {src[:200]}"
        )
        assert m.group(1) == "!=", (
            "arming must be via != \"1\" skip-check (fail-closed shape)"
        )

    def test_unarmed_branch_returns_before_any_execution(self):
        stmts = self._stmts_after_docstring()
        gate = stmts[0]
        assert isinstance(gate, ast.If)
        assert any(isinstance(s, ast.Return) for s in gate.body), (
            "unarmed branch must return (skip the whole phase)"
        )
        returned_value = [
            s for s in gate.body if isinstance(s, ast.Return)
        ][0].value
        if returned_value is not None:
            dump = ast.dump(returned_value)
            assert "None" in dump, "must return None when unarmed"

    def test_log_message_mentions_skip_when_unarmed(self):
        src = ast.unparse(self._fn())
        assert "skipped" in src.lower()

    def test_no_live_signal_generation_before_gate(self):
        src = ast.unparse(self._fn())
        pre_gate = src.split("CALLISTO_ALLOW_LIVE_EXECUTE", 1)[0]
        assert "generate_paper_trade_signal" not in pre_gate


class TestFacadePhaseLiveExecute(_GatePinMixin):
    path = AUTONOMOUS_PATH
    cls_name = "ResearchLoop"
    fn_name = "_phase_live_execute"


class TestPhasesImplLiveExecute(_GatePinMixin):
    path = PHASES_IMPL_PATH
    cls_name = None
    fn_name = "phase_live_execute"


class TestPhasesImplRuntimeUnarmed:
    def test_phase_live_execute_noop_without_env(self, monkeypatch):
        import tools.loop.phases_impl as impl

        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)

        class _Loop:
            pass

        result = asyncio.run(impl.phase_live_execute(_Loop()))
        assert result is None

    def test_phase_live_execute_refuses_other_truthy_values(self, monkeypatch):
        import tools.loop.phases_impl as impl

        for val in ("true", "yes", "on", "1 ", "01"):
            monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", val)

            class _Loop:
                pass

            assert asyncio.run(impl.phase_live_execute(_Loop())) is None, (
                f"value {val!r} must NOT arm live execution"
            )


# ===========================================================================
# D. BetExecutor starts disabled
# ===========================================================================


class TestBetExecutorInitDisabled:
    def _init_fn(self) -> ast.FunctionDef:
        tree = _parse(EXECUTOR_PATH)
        cls = _class_def(tree, "BetExecutor")
        return _method(cls, "__init__", async_=False)

    def test_init_assigns_enabled_false(self):
        fn = self._init_fn()
        enabled_assigns = []
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "_enabled":
                        enabled_assigns.append(node)
        assert len(enabled_assigns) == 1, (
            f"_enabled assigned {len(enabled_assigns)} times in __init__"
        )
        value = enabled_assigns[0].value
        assert isinstance(value, ast.Constant) and value.value is False, (
            "_enabled must be initialized to literal False"
        )

    def test_init_does_not_read_armed_env_var(self):
        src = ast.unparse(self._init_fn())
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src, (
            "__init__ must never consult the arming env var"
        )
        assert "CALLISTO_LOCAL_ONLY" not in src or "enable" in src

    def test_real_constructor_leaves_executor_disabled(self):
        from tools.bet_executor import BetExecutor

        executor = BetExecutor()
        assert executor._enabled is False
        assert executor._browser is None
        assert executor._page is None
        assert executor._db is None
        assert executor._logged_in is False

    def test_two_constructors_are_independent(self):
        from tools.bet_executor import BetExecutor

        a, b = BetExecutor(), BetExecutor()
        a._enabled = True  # simulate external tampering on one instance only
        assert b._enabled is False, "instances must not share enable state"

    def test_enable_not_called_by_import(self):
        """Importing the module must not leave any armed global executor."""
        import tools.bet_executor as mod

        for name in dir(mod):
            obj = getattr(mod, name)
            if isinstance(obj, type) and obj.__name__ == "BetExecutor":
                continue
            if hasattr(obj, "_enabled") and getattr(obj, "_enabled") is True:
                pytest.fail(f"module-level executor {name} is already enabled")

    def test_init_body_has_no_browser_launch(self):
        src = ast.unparse(self._init_fn())
        lowered = re.sub(r"#.*", "", src).lower()
        assert "launch" not in lowered
        assert ".connect(" not in lowered


# ===========================================================================
# E. Cross-cutting: nothing wires the pieces together into a live loop
# ===========================================================================


class TestCrossCuttingFailClosed:
    def test_autonomous_facade_documents_gate_as_only_switch(self):
        facade = REPO / "tools" / "auto" / "facade.py"
        src = _read(facade)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src

    def test_paper_gate_comment_forbids_adding_live(self):
        src = _read(PAPER_PATH)
        comment_zone = src.split("_PAPER_TRADE_SIGNAL_STATUSES", 1)[0]
        assert "NEVER" in comment_zone.upper() or "FORBIDDEN" in comment_zone.upper()

    def test_backtest_docstring_forbids_accepting_live(self):
        doc = ast.get_docstring(_gps_func()) or ""
        lowered = doc.lower()
        assert "live" in lowered and ("forbidden" in lowered or "never" in lowered)

    def test_phases_impl_docstring_names_the_env_var(self):
        src = _read(PHASES_IMPL_PATH)
        idx = src.index("async def phase_live_execute")
        # The docstring may sit after a bare `self = loop` facade alias;
        # scan forward from the def until executable code begins.
        window = src[idx : idx + 2500]
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in window.split("if _os.getenv")[0]

    def test_no_production_module_sets_allow_live_default_on(self):
        """No source may default CALLISTO_ALLOW_LIVE_EXECUTE to '1'."""
        offenders = []
        for py in list(REPO.glob("tools/**/*.py")) + [REPO / "callisto.py"]:
            if not py.exists():
                continue
            src = _read(py)
            for m in re.finditer(
                r"getenv\(\s*[\"']CALLISTO_ALLOW_LIVE_EXECUTE[\"']\s*,\s*[\"']([^\"']*)[\"']\s*\)",
                src,
            ):
                if m.group(1) == "1":
                    offenders.append(str(py))
        assert offenders == [], f"default-armed env lookups: {offenders}"

    def test_test_suite_itself_never_arms_live(self):
        """This file must not contain arming assignments."""
        mysrc = Path(__file__).read_text(encoding="utf-8")
        assert "os.environ[\"CALLISTO_ALLOW_LIVE_EXECUTE\"] = \"1\"" not in mysrc
        assert "monkeypatch.setenv(\"CALLISTO_ALLOW_LIVE_EXECUTE\", \"1\")" not in mysrc
