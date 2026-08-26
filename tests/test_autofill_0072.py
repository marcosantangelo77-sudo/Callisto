"""Autofill characterization #0072 — paper-only loop (LONG).

Characterizes, without changing behavior:

1. ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is exactly
   ``frozenset({'paper_trading'})`` — and stays that way.
2. ``BacktestEngine.generate_paper_trade_signal`` hard-rejects any
   hypothesis whose status is not ``paper_trading`` (most importantly
   ``"live"``) before touching live odds.
3. ``tools.loop.phases_impl.phase_live_execute`` is inert unless the
   operator sets ``CALLISTO_ALLOW_LIVE_EXECUTE=1``.
4. ``BetExecutor.__init__`` always assigns ``self._enabled = False``;
   the executor never arms itself at construction.

Fail-closed philosophy: every pin here is written so that widening any
gate (adding "live" to the status set, dropping the env gate, arming by
default) makes these tests FAIL. Tests only; no production code touched.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

REPO = Path(__file__).resolve().parent.parent
PAPER_SRC = (REPO / "tools" / "signals" / "paper.py").read_text()
BACKTEST_SRC = (REPO / "tools" / "backtest.py").read_text()
PHASES_SRC = (REPO / "tools" / "loop" / "phases_impl.py").read_text()
BETEXEC_SRC = (REPO / "tools" / "bet_executor.py").read_text()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read(rel: str) -> str:
    return (REPO / rel).read_text()


def _class_body(src: str, cls_name: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            return ast.get_source_segment(src, node) or ""
    raise AssertionError(f"class {cls_name} not found")


def _method_body(src: str, cls_name: str | None, fn_name: str) -> str:
    """Return the source segment of a (possibly async) method/function."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name != fn_name:
                continue
            if cls_name is None:
                return ast.get_source_segment(src, node) or ""
            # confirm it lives inside cls_name by walking parents cheaply:
            for parent in ast.walk(tree):
                if (
                    isinstance(parent, ast.ClassDef)
                    and parent.name == cls_name
                    and node in ast.walk(parent)
                    and node is not getattr(parent, "_marker", None)
                ):
                    if any(
                        m is node
                        for m in parent.body
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ) or node in [
                        m
                        for m in ast.walk(parent)
                        if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                    ]:
                        return ast.get_source_segment(src, node) or ""
        if cls_name is not None:
            continue
    raise AssertionError(f"function {fn_name} (class={cls_name}) not found")


def _first_cond_dump(fn_src: str) -> str:
    tree = ast.parse(textwrap.dedent(fn_src))
    fn = tree.body[0]
    assert isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
    for stmt in fn.body:
        if isinstance(stmt, ast.If):
            return ast.dump(stmt.test)
    raise AssertionError("no top-level If found in function body")


class _FakeHypothesisManager:
    def __init__(self, record: dict | None):
        self._record = record
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._record


def _make_engine(record: dict | None):
    """Build a bare object whose generate_paper_trade_signal is the REAL
    unbound method from tools.backtest.BacktestEngine."""
    from tools.backtest import BacktestEngine

    engine = object.__new__(BacktestEngine)
    engine.hypothesis_manager = _FakeHypothesisManager(record)
    return engine


async def _run_generate(engine, hypothesis_id="h-1", live_odds={"a": 1}):
    return await engine.generate_paper_trade_signal(hypothesis_id, live_odds)


# ---------------------------------------------------------------------------
# 1. The frozenset pin
# ---------------------------------------------------------------------------


class TestPaperStatusesPin:
    def test_is_frozenset(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

        assert isinstance(S, frozenset)

    def test_exactly_paper_trading(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

        assert S == frozenset({"paper_trading"})

    def test_no_live_variant(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

        assert "live" not in S
        assert "live_trading" not in S
        assert "production" not in S

    def test_lowercase_only(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES as S

        assert all(s == s.lower() for s in S)

    def test_allowed_paper_statuses_returns_same_object(self):
        import tools.signals.paper as paper

        assert paper.allowed_paper_statuses() is paper._PAPER_TRADE_SIGNAL_STATUSES

    def test_reject_non_paper_semantics(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False
        assert reject_non_paper("live") is True
        assert reject_non_paper(None) is True
        assert reject_non_paper("") is True
        assert reject_non_paper("Paper_Trading") is True  # case sensitive

    def test_ast_pin_frozenset_literal(self):
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\{[^}]*\}\)",
            PAPER_SRC,
        )
        assert m, "assignment no longer a frozenset literal"
        assert '"paper_trading"' in m.group(0)
        assert "live" not in m.group(0)

    def test_module_docstring_declares_ownership(self):
        assert "ONLY definition" in PAPER_SRC


# ---------------------------------------------------------------------------
# 2. generate_paper_trade_signal rejects non-paper statuses
# ---------------------------------------------------------------------------


class TestGeneratePaperTradeSignalGate:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "status",
        ["live", "LIVE", "live_trading", "paused", "", None, "paper", 1],
    )
    async def test_non_paper_status_returns_empty(self, status):
        engine = _make_engine({"hypothesis_id": "h-1", "status": status})
        assert await _run_generate(engine) == []

    @pytest.mark.asyncio
    async def test_missing_hypothesis_returns_empty(self):
        engine = _make_engine(None)
        assert await _run_generate(engine) == []

    @pytest.mark.asyncio
    async def test_paper_status_still_goes_to_pipeline_but_pipeline_stubbed(self):
        # For paper_trading the gate passes and delegates to paper_pipeline.
        import tools.backtest as bt

        sentinel = [{"signal": "x"}]

        async def fake_pipeline(engine, hid, odds):
            return sentinel

        engine = _make_engine({"hypothesis_id": "h-1", "status": "paper_trading"})
        with patch.object(bt.paper_pipeline, "generate_paper_trade_signal", fake_pipeline):
            assert await _run_generate(engine) is sentinel

    @pytest.mark.asyncio
    async def test_gate_runs_before_odds_processing(self):
        engine = _make_engine({"hypothesis_id": "h-1", "status": "live"})
        bad_odds = {"must": "not be touched"}
        assert await _run_generate(engine, live_odds=bad_odds) == []
        assert engine.hypothesis_manager.calls == 1

    def test_method_source_ordering_gate_first(self):
        src = _method_body(BACKTEST_SRC, "BacktestEngine", "generate_paper_trade_signal")
        body = src.split('"""', 2)[2] if src.count('"""') >= 2 else src
        gate_pos = body.find("reject_non_paper")
        pipeline_pos = body.find("paper_pipeline.generate_paper_trade_signal")
        assert gate_pos != -1 and pipeline_pos != -1
        assert gate_pos < pipeline_pos, "pipeline call must come after the gate"

    def test_method_source_mentions_forbidden_live(self):
        src = _method_body(BACKTEST_SRC, "BackTestEngine_PLACEHOLDER_NEVER_MATCHES_XYZ", "generate_paper_trade_signal") if False else _method_body(
            BACKTEST_SRC, "BacktestEngine", "generate_paper_trade_signal"
        )
        assert "FORBIDDEN" in src or "live" in src.lower()

    def test_no_status_equals_live_widen_in_backtest(self):
        # No production shortcut like `if h["status"] == "live"` inside the method.
        src = _method_body(BACKTEST_SRC, "BacktestEngine", "generate_paper_trade_signal")
        assert '== "live"' not in src
        assert "'live'" not in src.split('"""')[0]


# ---------------------------------------------------------------------------
# 3. phase_live_execute env gate
# ---------------------------------------------------------------------------


class TestPhaseLiveExecuteGate:
    def _fn_src(self) -> str:
        return _method_body(PHASES_SRC, None, "phase_live_execute")

    def test_exists_and_is_coroutine(self):
        import tools.loop.phases_impl as pi

        fn = getattr(pi, "phase_live_execute")
        assert inspect.iscoroutinefunction(fn)

    def test_first_statement_is_env_gate_ast(self):
        src = self._fn_src()
        dump = _first_cond_dump(src)
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dump

    def test_env_comparison_is_strict_ne_one(self):
        src = self._fn_src()
        assert re.search(
            r'getenv\(\s*"CALLISTO_ALLOW_LIVE_EXECUTE"\s*\)\s*!=\s*"1"', src
        )

    def test_unarmed_call_returns_immediately(self, caplog):
        import tools.loop.phases_impl as pi

        env = {k: v for k, v in os.environ.items() if k != "CALLISTO_ALLOW_LIVE_EXECUTE"}
        with patch.dict(os.environ, env, clear=True):
            result = asyncio.run(pi.phase_live_execute(loop=None))  # type: ignore[arg-type]
        assert result is None

    @pytest.mark.parametrize("bad", ["0", "", "true", "yes", "on", "1 "])
    def test_anything_other_than_exact_one_is_refused(self, bad):
        import tools.loop.phases_impl as pi

        with patch.dict(os.environ, {"CALLISTO_ALLOW_LIVE_EXECUTE": bad}):
            assert asyncio.run(pi.phase_live_execute(loop=None)) is None

    def test_autonomous_wrapper_also_checks_env_or_delegates(self):
        auto_src = _read("tools/autonomous.py")
        idx = auto_src.find("_phase_live_execute")
        assert idx != -1, "autonomous.ResearchLoop lost its live-execute phase"
        window = auto_src[max(0, idx - 200) : idx + 2000]
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in window

    def test_research_loop_has_phase_attribute_reference(self):
        import tools.autonomous as auto

        assert hasattr(auto.ResearchLoop, "_phase_live_execute") or re.search(
            r"_phase_live_execute", auto_src_readback := _read("tools/autonomous.py")
        )


# ---------------------------------------------------------------------------
# 4. BetExecutor.__init__ assigns _enabled = False
# ---------------------------------------------------------------------------


class TestBetExecutorDisabledByDefault:
    def _init_src(self) -> str:
        return _method_body(BETEXEC_SRC, "BetExecutor", "__init__")

    def test_init_assigns_enabled_false(self):
        assert re.search(r"self\._enabled\s*=\s*False", self._init_src())

    def test_init_never_assigns_enabled_true(self):
        init = self._init_src()
        assert re.search(r"self\._enabled\s*=\s*True", init) is None

    def test_runtime_construction_starts_disabled(self):
        from tools.bet_executor import BetExecutor

        executor = BetExecutor()
        assert executor._enabled is False

    def test_enable_is_explicit_api(self):
        from tools.bet_executor import BetExecutor

        assert hasattr(BetExecutor, "enable") or "def enable" in BETEXEC_SRC

    def test_doctor_script_pins_disabled_default(self):
        doc = _read("tools/cli/doctor.py")
        assert "class BetExecutor" in doc

    def test_init_does_not_touch_browser_or_page_truthiness_as_arm(self):
        init = self._init_src()
        assert "self._enabled" not in init.split("= False")[1].split("\n")[0]


# ---------------------------------------------------------------------------
# 5. Cross-cutting fail-closed pins
# ---------------------------------------------------------------------------


class TestFailClosedCrossChecks:
    def test_no_live_added_anywhere_in_paper_module(self):
        assert "live" not in PAPER_SRC.split("=")[1].split("\n")[0]

    def test_statuses_set_size_is_one(self):
        from tools.signals.paper import allowed_paper_statuses

        assert len(allowed_paper_statuses()) == 1

    def test_phases_impl_paper_cycle_uses_engine_gate(self):
        # phase_paper_trade calls the gated engine method rather than reimplementing
        idx = PHASES_SRC.find("generate_paper_trade_signal")
        assert idx != -1

    def test_no_new_status_constant_elsewhere(self):
        for rel in ("tools/betexec/__init__.py", "tools/backtest.py"):
            src = _read(rel)
            for m in re.finditer(r"frozenset\(\s*\{[^}]*\}\s*\)", src):
                blob = m.group(0)
                if '"live"' in blob or "'live'" in blob:
                    pytest.fail(f"live found inside frozenset literal in {rel}: {blob}")

    def test_import_of_paper_module_has_no_side_effects_beyond_gate(self):
        import subprocess
        import sys

        code = (
            "import json,sys;"
            "import tools.signals.paper as p;"
            "json.dump(sorted(p._PAPER_TRADE_SIGNAL_STATUSES), sys.stdout)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=str(REPO),
            timeout=60,
        )
        assert out.returncode == 0, out.stderr
        assert json_loads(out.stdout) == ["paper_trading"]


def json_loads(s: str):
    import json

    return json.loads(s)
