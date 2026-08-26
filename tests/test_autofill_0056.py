"""Autofill characterization #0056 — the paper-only loop.

This module is a LARGE characterization pin over the four safety invariants
that keep Callisto's betting loop paper-only by default:

  A. ``_PAPER_TRADE_SIGNAL_STATUSES`` is exactly ``frozenset({"paper_trading"})``
     and the helper functions in :mod:`tools.signals.paper` never admit any
     other status (in particular never ``"live"``).
  B. ``BacktestEngine.generate_paper_trade_signal`` rejects every hypothesis
     whose status is not exactly ``"paper_trading"`` — it returns ``[]``
     BEFORE touching odds — and its source still contains that fail-closed
     gate as its first control flow.
  C. The research loop's live-execute phase (both the facade method and the
     implementation) is a no-op unless ``CALLISTO_ALLOW_LIVE_EXECUTE == "1"``
     is set in the environment.
  D. ``BetExecutor.__init__`` assigns ``self._enabled = False`` and the
     executor refuses to arm itself implicitly; the source keeps the
     default-disabled assignment with an explicit safety comment.

Everything here is characterization: nothing in this file arms live betting,
adds "live" to any status set, or widens any gate. If one of these pins is
intentionally changed on master, update the test in the SAME commit.

We deliberately avoid importing ``tools.autonomous`` at module import time
(it can pull heavy/hanging deps); AST/source pins are used there instead,
while cheap, side-effect-free objects (paper gate helpers, BetExecutor)
are exercised for real.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PAPER_MODULE = REPO / "tools/signals/paper.py"
BACKTEST_MODULE = REPO / "tools/backtest.py"
AUTONOMOUS_MODULE = REPO / "tools/autonomous.py"
PHASES_IMPL_MODULE = REPO / "tools/loop/phases_impl.py"
EXECUTOR_MODULE = REPO / "tools/bet_executor.py"

PAPER_STATUSES = ("paper_trading",)
FORBIDDEN_STATUSES = (
    "live",
    "Live",
    "LIVE",
    "backtesting",
    "drawdown_paused",
    "archived",
    "pending_review",
    "",
    None,
    1,
    True,
    0.0,
)


def _read(rel_path: Path) -> str:
    assert rel_path.exists(), f"expected repo file missing: {rel_path}"
    return rel_path.read_text(encoding="utf-8")


def _parse(rel_path: Path) -> ast.AST:
    return ast.parse(_read(rel_path))


def _funcs(tree: ast.AST, name: str):
    """All function nodes named *name* anywhere in the tree."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]


def _body_no_docstring(fn: ast.AST) -> list[ast.stmt]:
    body = list(getattr(fn, "body", []))
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


# ---------------------------------------------------------------------------
# A. _PAPER_TRADE_SIGNAL_STATUSES pin
# ---------------------------------------------------------------------------


class TestPaperStatusSetPin:
    def test_statuses_is_frozenset_of_paper_trading_only(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_never_in_statuses(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_statuses_type_is_immutable(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert type(_PAPER_TRADE_SIGNAL_STATUSES) is frozenset

    def test_allowed_paper_statuses_returns_same_set(self):
        from tools.signals.paper import allowed_paper_statuses

        got = allowed_paper_statuses()
        assert got == frozenset({"paper_trading"})
        assert type(got) is frozenset

    @pytest.mark.parametrize("status", FORBIDDEN_STATUSES)
    def test_reject_non_paper_rejects_everything_else(self, status):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper(status) is True

    def test_reject_non_paper_accepts_only_paper_trading(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False

    def test_source_defines_set_as_frozenset_literal(self):
        src = _read(PAPER_MODULE)
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\s*\{[^}]*\}\s*\)",
            src,
        )
        assert m, "statuses must be assigned a frozenset({...}) literal"
        literal = m.group(0)
        assert '"paper_trading"' in literal or "'paper_trading'" in literal
        assert re.search(r"\blive\b", literal) is None

    def test_source_has_exactly_one_status_definition(self):
        src = _read(PAPER_MODULE)
        defs = re.findall(r"^_PAPER_TRADE_SIGNAL_STATUSES\s*=", src, flags=re.M)
        assert len(defs) == 1, "the status set must have a single definition"

    def test_gate_module_has_hard_gate_comment(self):
        src = _read(PAPER_MODULE)
        assert "HARD GATE" in src
        assert "NEVER be added" in src or "NEVER" in src

    def test_betexec_package_documents_the_paper_gate(self):
        src = _read(REPO / "tools/betexec/__init__.py")
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in src


# ---------------------------------------------------------------------------
# B. generate_paper_trade_signal fail-closed behavior
# ---------------------------------------------------------------------------

FULL_LIVE_ODDS = {
    "games": [
        {
            "id": "evt-1",
            "home_team": "Lakers",
            "away_team": "Celtics",
            "sport": "basketball_nba",
            "commence_time": "2026-08-26T00:00:00Z",
            "bookmakers": [],
        }
    ]
}


class _FakeHypManager:
    def __init__(self, hyp):
        self._hyp = hyp

    async def get_hypothesis(self, hypothesis_id):
        return self._hyp


def _make_engine(hyp):
    from tools.backtest import BacktestEngine

    engine = BacktestEngine.__new__(BacktestEngine)
    engine.hypothesis_manager = _FakeHypManager(hyp)
    return engine


def _hyp(status="paper_trading"):
    return {
        "hypothesis_id": "hyp-1",
        "status": status,
        "model_config": {},
        "edge_threshold": 99.0,  # absurdly high → even if gated through, 0 signals
        "thesis": "",
        "name": "",
        "sport": "",
    }


class TestGeneratePaperTradeSignalGate:
    @pytest.mark.asyncio
    async def test_returns_empty_for_live_status(self):
        engine = _make_engine(_hyp("live"))
        signals = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
        assert signals == []

    @pytest.mark.parametrize("status", ["backtesting", "drawdown_paused", "", "LIVE"])
    @pytest.mark.asyncio
    async def test_returns_empty_for_any_non_paper_status(self, status):
        engine = _make_engine(_hyp(status))
        signals = await engine.generate_paper_trade_signal("hyp-1", FULL_LIVE_ODDS)
        assert signals == []

    @pytest.mark.asyncio
    async def test_missing_hypothesis_yields_empty(self):
        engine = _make_engine(None)
        signals = await engine.generate_paper_trade_signal("nope", FULL_LIVE_ODDS)
        assert signals == []

    def test_signature_takes_hypothesis_id_and_live_odds(self):
        tree = _parse(BACKTEST_MODULE)
        fns = _funcs(tree, "generate_paper_trade_signal")
        assert len(fns) == 1
        fn = fns[0]
        assert isinstance(fn, ast.AsyncFunctionDef)
        names = [a.arg for a in fn.args.args]
        assert names[:3] == ["self", "hypothesis_id", "live_odds"]

    def test_gate_is_first_control_flow_after_fetch(self):
        """The rejection must happen before ANY odds processing."""
        tree = _parse(BACKTEST_MODULE)
        fn = _funcs(tree, "generate_paper_trade_signal")[0]
        body = _body_no_docstring(fn)
        assert len(body) >= 2
        fetch = body[0]
        assert isinstance(fetch, ast.Assign), "first stmt must fetch hypothesis"
        gate = body[1]
        assert isinstance(gate, ast.If), "second stmt must be the gate `if`"
        test_src = ast.unparse(gate.test)
        assert "reject_non_paper" in test_src
        # Gate returns immediately (empty list), no else-branch processing.
        assert gate.orelse == []
        returns = [
            s for s in ast.walk(gate) if isinstance(s, ast.Return)
        ]
        assert len(returns) == 1
        assert isinstance(returns[0].value, ast.List)
        assert returns[0].value.elts == []

    def test_docstring_declares_hard_gate_against_live(self):
        tree = _parse(BACKTEST_MODULE)
        fn = _funcs(tree, "generate_paper_trade_signal")[0]
        doc = ast.get_docstring(fn) or ""
        assert "HARD GATE" in doc.upper()
        assert "live" in doc.lower()
        assert "FORBIDDEN" in doc.upper() or "ONLY" in doc.upper()

    def test_source_mentions_reject_non_paper_once_at_call_site(self):
        src = _read(BACKTEST_MODULE)
        idx = src.index("async def generate_paper_trade_signal")
        tail = src[idx:]
        end = tail.index("\n    async def ", 10) if "\n    async def " in tail[10:] else len(tail)
        fn_src = tail[:end]
        assert "reject_non_paper(h[\"status\"])" in fn_src.replace("'", '"')

    def test_import_of_gate_helper_present(self):
        src = _read(BACKTEST_MODULE)
        assert "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES" in src

    def test_backtest_does_not_widen_statuses_inline(self):
        src = _read(BACKTEST_MODULE)
        for m in re.finditer(r"frozenset\(", src):
            blob = src[m.start():m.start() + 200]
            chunk = blob.split(")", 1)[0]
            assert "live" not in chunk.lower().replace("_live_odds", ""), chunk


# ---------------------------------------------------------------------------
# C. CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ---------------------------------------------------------------------------


class TestLiveExecuteEnvGate:
    def test_facade_method_exists_and_is_async(self):
        tree = _parse(AUTONOMOUS_MODULE)
        fns = [f for c in ast.walk(tree) if isinstance(c, ast.ClassDef)
               for f in c.body if isinstance(f, ast.AsyncFunctionDef)
               and f.name == "_phase_live_execute"]
        assert len(fns) == 1

    def _facade_fn(self) -> ast.AsyncFunctionDef:
        tree = _parse(AUTONOMOUS_MODULE)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "_phase_live_execute":
                        return item
        raise AssertionError("ResearchLoop._phase_live_execute not found")

    def test_env_check_is_first_control_flow(self):
        fn = self._facade_fn()
        body = _body_no_docstring(fn)
        stmts = [s for s in body if not isinstance(s, ast.Import)]
        assert stmts, "gate body empty"
        first = stmts[0]
        assert isinstance(first, ast.If), "env gate must be the first statement"
        assert 'CALLISTO_ALLOW_LIVE_EXECUTE' in ast.unparse(first.test)

    def test_gate_compares_to_exact_string_one(self):
        src = _read(AUTONOMOUS_MODULE)
        assert '_os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src

    def test_gate_returns_without_delegating(self):
        fn = self._facade_fn()
        gate = _body_no_docstring(fn)[1]
        assert isinstance(gate, ast.If)
        assert any(isinstance(s, ast.Return) for s in ast.walk(gate))
        # The delegation to phases_impl must NOT be inside the guarded branch.
        unparse = ast.unparse(gate)
        assert "phases_impl.phase_live_execute" not in unparse

    def test_impl_phase_also_checks_env_before_anything(self):
        tree = _parse(PHASES_IMPL_MODULE)
        fns = _funcs(tree, "phase_live_execute")
        assert len(fns) == 1
        body = _body_no_docstring(fns[0])
        stmts = [s for s in body if not isinstance(s, ast.Import)]
        assert stmts
        # Allow any leading aliasing assigns / stray bare-string literals
        # before the gate; the FIRST real control flow must be the env If.
        idx = next(
            (i for i, s in enumerate(stmts) if isinstance(s, ast.If)),
            None,
        )
        assert idx is not None, "no env-gate `if` found in phase_live_execute"
        for s in stmts[:idx]:
            assert isinstance(s, (ast.Assign, ast.Expr)), (
                f"unexpected statement before env gate: {ast.dump(s)[:80]}"
            )
            if isinstance(s, ast.Assign):
                assert ast.unparse(s).startswith("self =")
        first = stmts[idx]
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in ast.unparse(first.test)

    def test_impl_skips_with_log_when_not_armed(self):
        src = _read(PHASES_IMPL_MODULE)
        seg = src[src.index("async def phase_live_execute"):]
        seg = seg[:seg.index("\nasync def ", 10)] if "\nasync def " in seg[10:] else seg
        assert "live_execute skipped" in seg

    def test_doctor_reports_the_gate_variable(self):
        src = _read(REPO / "tools/cli/doctor.py")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src

    def test_autonomous_docstrings_name_the_only_arming_switch(self):
        src = _read(AUTONOMOUS_MODULE)
        assert "ONLY arming switch" in src or "ONLY" in src
        assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in src

    def test_no_default_arming_anywhere_in_loop_code(self):
        """No code may treat e.g. 'true'/'yes'/unset as armed."""
        for mod in (AUTONOMOUS_MODULE, PHASES_IMPL_MODULE):
            src = _read(mod)
            for m in re.finditer(r'getenv\(\s*"CALLISTO_ALLOW_LIVE_EXECUTE"[^)]*\)', src):
                call = m.group(0)
                assert '!= "1"' in src[m.end():m.end() + 40] or '== "1"' in src[max(0, m.start() - 40):m.end()], call


# ---------------------------------------------------------------------------
# D. BetExecutor starts disabled
# ---------------------------------------------------------------------------


class TestBetExecutorDisabledByDefault:
    def test_init_assigns_enabled_false(self):
        from tools.bet_executor import BetExecutor

        executor = BetExecutor()
        assert executor._enabled is False
        assert type(executor._enabled) is bool

    def test_fresh_executor_has_no_browser_or_db(self):
        from tools.bet_executor import BetExecutor

        executor = BetExecutor()
        assert executor._browser is None
        assert executor._context is None
        assert executor._page is None
        assert executor._db is None

    def test_init_source_pins_disabled_assignment_with_comment(self):
        src = _read(EXECUTOR_MODULE)
        m = re.search(r"class BetExecutor\b.*?def __init__\(self\):\s*(.*?)\n        self\.initialize|class BetExecutor\b.*?def __init__\(self\):(.*?)(?=\n    (?:async )?def )", src, flags=re.S)
        assert m, "BetExecutor.__init__ not found"
        init_src = m.group(1) or m.group(2)
        assert re.search(r"self\._enabled\s*=\s*False\b", init_src), init_src
        assert "SAFETY" in init_src.upper(), "disabled assignment must carry SAFETY comment"

    def test_enable_refuses_when_local_only(self):
        from tools.bet_executor import BetExecutor

        executor = BetExecutor()
        monkey_local_only = "CALLISTO_LOCAL_ONLY"
        old = os.environ.get(monkey_local_only)
        try:
            os.environ[monkey_local_only] = "1"
            assert executor.enable() is False
            assert executor._enabled is False
        finally:
            if old is None:
                os.environ.pop(monkey_local_only, None)
            else:
                os.environ[monkey_local_only] = old

    def test_class_defined_once(self):
        src = _read(EXECUTOR_MODULE)
        assert len(re.findall(r"^class BetExecutor\b", src, flags=re.M)) == 1

    def test_kill_switch_docs_reference_flipping_enabled_false(self):
        src = _read(REPO / "tools/betexec/kill_switch.py")
        assert "_enabled" in src and "False" in src


# ---------------------------------------------------------------------------
# Cross-cutting: the loop stays paper-only end to end
# ---------------------------------------------------------------------------


class TestLoopStaysPaperOnly:
    def test_paper_module_is_single_source_of_truth(self):
        """Only tools/signals/paper.py assigns the status set."""
        offenders = []
        for py in REPO.rglob("*.py"):
            if "attic" in py.parts or ".git" in py.parts:
                continue
            try:
                src = py.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if re.search(r"^_PAPER_TRADE_SIGNAL_STATUSES\s*=", src, flags=re.M):
                rel = py.relative_to(REPO).as_posix()
                if rel != "tools/signals/paper.py":
                    offenders.append(rel)
        assert offenders == [], f"status set redefined outside gate module: {offenders}"

    def test_generate_paper_signal_not_exported_for_live_paths(self):
        src = _read(AUTONOMOUS_MODULE)
        assert "generate_paper_trade_signal" not in src.split("def get_status")[0].split("phase_live_execute")[-1]

    def test_no_test_helper_arms_live(self):
        """This characterization module itself must never arm live."""
        mysrc = Path(__file__).read_text(encoding="utf-8")
        armed = "os.environ[" + '"CALLISTO_ALLOW_LIVE_EXECUTE"' + "] = " + '"1"'
        assert armed not in mysrc

    @pytest.mark.parametrize(
        "module_rel",
        [
            "tools/signals/paper.py",
            "tools/backtest.py",
            "tools/autonomous.py",
            "tools/loop/phases_impl.py",
            "tools/bet_executor.py",
        ],
    )
    def test_safety_modules_parse_cleanly(self, module_rel):
        ast.parse(_read(REPO / module_rel))
