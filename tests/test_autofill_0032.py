"""Autofill characterization #0032 — paper-only loop (LONG).

Characterization pins for the paper-only safety loop of Callisto:

1. ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is exactly
   ``frozenset({'paper_trading'})`` — no "live", no future statuses.
2. ``BacktestEngine.generate_paper_trade_signal`` rejects any hypothesis
   whose status is not exactly ``paper_trading`` (especially ``"live"``),
   returning ``[]`` BEFORE any odds processing or DB writes.
3. The autonomous-loop ``_phase_live_execute`` phase (facade in
   ``tools/autonomous.py`` AND implementation ``phase_live_execute`` in
   ``tools/loop/phases_impl.py``) is hard-gated on
   ``CALLISTO_ALLOW_LIVE_EXECUTE=1`` as its first executable statement.
4. ``BetExecutor.__init__`` assigns ``self._enabled = False`` — the
   executor never arms itself; ``enable()`` must be called explicitly.

These are characterization tests: they pin CURRENT behavior so that any
drift toward arming live betting fails loudly. If a pin is false, fail
closed — never widen the paper gate, never add "live" to
``_PAPER_TRADE_SIGNAL_STATUSES``, never remove an env gate.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

PAPER_MODULE = REPO_ROOT / "tools" / "signals" / "paper.py"
BACKTEST_MODULE = REPO_ROOT / "tools" / "backtest.py"
AUTONOMOUS_MODULE = REPO_ROOT / "tools" / "autonomous.py"
PHASES_IMPL_MODULE = REPO_ROOT / "tools" / "loop" / "phases_impl.py"
BET_EXECUTOR_MODULE = REPO_ROOT / "tools" / "bet_executor.py"


# ---------------------------------------------------------------------------
# Section 1: _PAPER_TRADE_SIGNAL_STATUSES pin
# ---------------------------------------------------------------------------


class TestPaperTradeSignalStatuses:
    """The ONLY definition of which statuses may generate paper signals."""

    def test_is_frozenset(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)

    def test_exactly_paper_trading(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_never_member(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES
        assert "LIVE" not in {s.upper() for s in _PAPER_TRADE_SIGNAL_STATUSES} - {
            s.upper() for s in _PAPER_TRADE_SIGNAL_STATUSES if s != "live"
        } | ({s.upper() for s in _PAPER_TRADE_SIGNAL_STATUSES})

    def test_no_status_contains_live_substring(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        for status in _PAPER_TRADE_SIGNAL_STATUSES:
            assert "live" not in status.lower()

    def test_allowed_paper_statuses_returns_same_set(self):
        from tools.signals.paper import (
            _PAPER_TRADE_SIGNAL_STATUSES,
            allowed_paper_statuses,
        )

        result = allowed_paper_statuses()
        assert isinstance(result, frozenset)
        assert result == _PAPER_TRADE_SIGNAL_STATUSES
        # Must be the same underlying frozenset (single source of truth).
        assert result is _PAPER_TRADE_SIGNAL_STATUSES

    def test_reject_non_paper_rejects_live(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("live") is True

    def test_reject_non_paper_accepts_only_paper_trading(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False

    @pytest.mark.parametrize(
        "bad_status",
        [
            "live",
            "Live",
            "LIVE",
            "live_trading",
            "real_money",
            "production",
            "",
            None,
            0,
            True,
        ],
    )
    def test_reject_non_paper_parametrized(self, bad_status):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper(bad_status) is True

    def test_unhashable_status_raises_not_allowed(self):
        """Unhashable junk must NEVER be treated as 'allowed'.

        Actual behavior: `unhashable in frozenset` raises TypeError, so an
        unhashable status cannot slip through the gate as permitted.
        (Sets are hashable and are rejected: not members of the frozenset.)
        """
        from tools.signals.paper import reject_non_paper

        for bad in (["paper_trading"], ["live"]):
            with pytest.raises(TypeError):
                reject_non_paper(bad)
        for rejected in (("live",), ("paper_trading",)):
            assert reject_non_paper(rejected) is True  # hashable ≠ member string

    def test_source_literal_frozenset_pin(self):
        """AST pin: the module source defines the set literally."""
        src = PAPER_MODULE.read_text()
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src

    def test_module_has_single_assignment_of_gate_constant(self):
        """AST pin: only ONE assignment site for the gate constant."""
        tree = ast.parse(PAPER_MODULE.read_text())
        assignments = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and (
                        target.id == "_PAPER_TRADE_SIGNAL_STATUSES"
                    ):
                        assignments.append(node.lineno)
        assert len(assignments) == 1

    def test_backtest_imports_gate_from_paper_module(self):
        """The gate constant must come from tools.signals.paper, not be redefined."""
        src = BACKTEST_MODULE.read_text()
        assert "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES" in src
        # No shadowing re-assignment inside backtest.py.
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    assert not (
                        isinstance(t, ast.Name)
                        and t.id == "_PAPER_TRADE_SIGNAL_STATUSES"
                    ), "backtest.py must NOT redefine the gate constant"

    def test_gate_constant_immutable_against_mutation_attempts(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        with pytest.raises((AttributeError, TypeError)):
            _PAPER_TRADE_SIGNAL_STATUSES.add("live")
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


# ---------------------------------------------------------------------------
# Section 2: generate_paper_trade_signal rejects non-paper statuses
# ---------------------------------------------------------------------------


def _extract_function_src(path: Path, name: str) -> str:
    """Return the full source text of a top-level/class-level function."""
    src = path.read_text()
    start = src.index(f"async def {name}")
    lines = src[start:].splitlines(keepends=True)
    out = [lines[0]]
    for line in lines[1:]:
        if line.strip() and not line.startswith((" ", "\t")):
            break
        out.append(line)
    return "".join(out)


class FakeHypothesisManager:
    def __init__(self, hypothesis):
        self._hypothesis = hypothesis
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._hypothesis


class RecordingSelf:
    """Minimal stand-in exposing only what the gate touches."""

    def __init__(self, hypothesis):
        self.hypothesis_manager = FakeHypothesisManager(hypothesis)
        self.unexpected_calls = []

    def __getattr__(self, name):
        self.unexpected_calls.append(name)
        raise AttributeError(
            f"gate must return [] before touching attribute {name!r}"
        )


def _make_hypothesis(status: str) -> dict:
    return {
        "id": "hyp-0032",
        "name": "char-test",
        "sport": "basketball_nba",
        "status": status,
        "edge_threshold": 0.02,
        "model_config": {"target_book": "draftkings"},
        "thesis": "",
    }


class TestGeneratePaperTradeSignalGate:
    """Behavioral characterization of the paper-signal entry gate."""

    def _call(self, status):
        from tools.backtest import BacktestEngine

        fake = RecordingSelf(_make_hypothesis(status))
        coro = BacktestEngine.generate_paper_trade_signal(
            fake, "hyp-0032", {"games": []}
        )
        result = asyncio.run(coro)
        return result, fake

    def test_missing_hypothesis_returns_empty_list(self):
        from tools.backtest import BacktestEngine

        fake = RecordingSelf(None)
        result = asyncio.run(
            BacktestEngine.generate_paper_trade_signal(fake, "nope", {"games": []})
        )
        assert result == []
        assert fake.hypothesis_manager.calls == 1

    @pytest.mark.parametrize("status", ["live", "draft", "paused", "", "retired"])
    def test_non_paper_status_returns_empty_before_odds_processing(self, status):
        result, fake = self._call(status)
        assert result == []
        assert fake.hypothesis_manager.calls == 1
        # Gate fired before ANY other attribute was touched.
        assert fake.unexpected_calls == []

    def test_live_status_specifically_rejected(self):
        result, _ = self._call("live")
        assert result == []

    def test_docstring_forbids_live(self):
        fn_src = _extract_function_src(BACKTEST_MODULE, "generate_paper_trade_signal")
        assert "live" in fn_src.lower()
        doc = getattr(
            __import__("tools.backtest", fromlist=["BacktestEngine"]).BacktestEngine,
            "generate_paper_trade_signal",
        ).__doc__
        assert doc is not None and "FORBIDDEN" in doc

    def test_gate_check_precedes_config_parsing(self):
        """Source-order pin: reject_non_paper runs before model_config parsing."""
        fn_src = _extract_function_src(BACKTEST_MODULE, "generate_paper_trade_signal")
        idx_gate = fn_src.index('reject_non_paper(h["status"])')
        idx_config = fn_src.index('config = h["model_config"]')
        assert idx_gate < idx_config

    def test_return_empty_immediately_on_gate(self):
        fn_src = _extract_function_src(BACKTEST_MODULE, "generate_paper_trade_signal")
        m = re.search(r'reject_non_paper\(h\["status"\]\):\n(\s+)return \[\]', fn_src)
        assert m is not None, "gate branch must `return []` immediately"

    def test_method_signature_takes_hypothesis_id_and_live_odds(self):
        from tools.backtest import BacktestEngine

        sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
        params = list(sig.parameters)
        assert params[:3] == ["self", "hypothesis_id", "live_odds"]

    def test_method_is_coroutine_function(self):
        from tools.backtest import BacktestEngine

        assert inspect.iscoroutinefunction(
            BacktestEngine.generate_paper_trade_signal
        )


# ---------------------------------------------------------------------------
# Section 3: CALLISTO_ALLOW_LIVE_EXECUTE gates phase_live_execute
# ---------------------------------------------------------------------------


class TestLiveExecuteEnvGate:
    """Both facade and implementation check the env var FIRST."""

    FACADE_SRC = AUTONOMOUS_MODULE.read_text()
    IMPL_SRC = PHASES_IMPL_MODULE.read_text()

    def test_facade_defines_phase_live_execute(self):
        assert "async def _phase_live_execute" in self.FACADE_SRC

    def test_impl_defines_phase_live_execute(self):
        assert "async def phase_live_execute" in self.IMPL_SRC

    @pytest.mark.parametrize(
        ("src", "label"),
        [(FACADE_SRC, "facade"), (IMPL_SRC, "phases_impl")],
    )
    def test_env_gate_present_in_source(self, src, label):
        assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE")' in src, label
        assert '!= "1"' in src, label

    def test_facade_gate_is_first_executable_statement(self):
        fn = _extract_function_src(AUTONOMOUS_MODULE, "_phase_live_execute")
        body_idx = fn.index("import os as _os")
        gate_idx = fn.index('_os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"')
        skip_idx = fn.index('return await phases_impl.phase_live_execute')
        assert body_idx < gate_idx < skip_idx

    def test_impl_gate_precedes_any_execution_logic(self):
        fn = _extract_function_src(PHASES_IMPL_MODULE, "phase_live_execute")
        gate_idx = fn.index('_os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"')
        first_return_after_gate = fn.index(
            'logger.info("live_execute skipped', gate_idx
        )
        order_manager_idx = fn.index("use_order_manager", gate_idx)
        assert first_return_after_gate < order_manager_idx

    def test_facade_delegates_to_phases_impl(self):
        fn = _extract_function_src(AUTONOMOUS_MODULE, "_phase_live_execute")
        assert "phases_impl.phase_live_execute(self)" in fn

    def _run_facade_phase(self, env_value):
        import tools.autonomous as autonomous_mod

        calls = {}

        class DummyPhasesImpl:
            @staticmethod
            async def phase_live_execute(loop):
                calls["impl"] = True
                return "IMPL-RAN"

        original = autonomous_mod.phases_impl
        autonomous_mod.phases_impl = DummyPhasesImpl()
        monkey_result = os.environ.pop("CALLISTO_ALLOW_LIVE_EXECUTE", None) or None
        try:
            if env_value is not None:
                os.environ["CALLISTO_ALLOW_LIVE_EXECUTE"] = env_value

            class Loop:
                pass

            result = asyncio.run(
                autonomous_mod.ResearchLoop._phase_live_execute(Loop())
            )
            return result, calls
        finally:
            autonomous_mod.phases_impl = original
            os.environ.pop("CALLISTO_ALLOW_LIVE_EXECUTE", None)
            if monkey_result is not None:
                os.environ["CALLISTO_ALLOW_LIVE_EXECUTE"] = monkey_result

    def test_facade_skips_without_env_var(self):
        result, calls = self._run_facade_phase(None)
        assert result is None
        assert calls == {}

    def test_facade_skips_with_wrong_value(self):
        for value in ("0", "true", "yes", "on", "1 ", " 1", "01"):
            result, calls = self._run_facade_phase(value)
            assert result is None, f"value={value!r} must NOT arm live execute"
            assert calls == {}, f"value={value!r} must NOT reach impl"

    def test_facade_runs_impl_only_with_exact_1(self):
        result, calls = self._run_facade_phase("1")
        assert result == "IMPL-RAN"
        assert calls.get("impl") is True


class TestPhaseLiveExecuteImplGate:
    """Direct behavioral tests on phases_impl.phase_live_execute's gate."""

    def test_impl_returns_none_without_env(self, monkeypatch):
        from tools.loop import phases_impl

        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)

        class Loop:
            pass

        assert asyncio.run(phases_impl.phase_live_execute(Loop())) is None

    @pytest.mark.parametrize("value", ["0", "TRUE", "yes", "1.0"])
    def test_impl_requires_exact_string_one(self, monkeypatch, value):
        from tools.loop import phases_impl

        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", value)

        class Loop:
            pass

        assert asyncio.run(phases_impl.phase_live_execute(Loop())) is None

    def test_impl_gate_before_order_manager_logic(self):
        fn = _extract_function_src(PHASES_IMPL_MODULE, "phase_live_execute")
        gate = fn.index('if _os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1":')
        om = fn.index("use_order_manager = ")
        assert gate < om

    def test_impl_imports_betexecutor_lazily_after_gate(self):
        fn = _extract_function_src(PHASES_IMPL_MODULE, "phase_live_execute")
        gate = fn.index('if _os.getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1":')
        betexec = fn.index("from tools.bet_executor import BetExecutor", gate)
        assert betexec > gate

    def test_killswitch_comment_present(self):
        fn = _extract_function_src(PHASES_IMPL_MODULE, "phase_live_execute")
        assert "ONLY" in fn  # "the ONLY arming switch"


# ---------------------------------------------------------------------------
# Section 4: BetExecutor.__init__ assigns _enabled = False
# ---------------------------------------------------------------------------


class TestBetExecutorDefaultDisabled:
    """The executor never arms itself at construction."""

    def _fresh_executor(self):
        from tools.bet_executor import BetExecutor

        return BetExecutor()

    def test_init_sets_enabled_false(self):
        executor = self._fresh_executor()
        assert executor._enabled is False

    def test_init_enabled_is_bool_false_not_truthy(self):
        executor = self._fresh_executor()
        assert bool(executor._enabled) is False

    def test_init_leaves_browser_and_page_unset(self):
        executor = self._fresh_executor()
        assert executor._browser is None
        assert executor._context is None
        assert executor._page is None

    def test_ast_pin_enabled_false_in_init(self):
        """AST pin: `self._enabled = False` appears in __init__ body."""
        tree = ast.parse(BET_EXECUTOR_MODULE.read_text())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BetExecutor":
                for item in node.body:
                    if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                        for stmt in ast.walk(item):
                            if (
                                isinstance(stmt, ast.Assign)
                                and len(stmt.targets) == 1
                                and isinstance(stmt.targets[0], ast.Attribute)
                                and stmt.targets[0].attr == "_enabled"
                                and isinstance(stmt.value, ast.Constant)
                                and stmt.value.value is False
                            ):
                                found.append(stmt.lineno)
        assert found, "BetExecutor.__init__ must assign self._enabled = False"
        assert len(found) == 1

    def test_enable_refuses_under_local_only(self, monkeypatch):
        from tools.bet_executor import BetExecutor

        monkeypatch.setenv("CALLISTO_LOCAL_ONLY", "1")
        executor = BetExecutor()
        assert executor.enable() is False
        assert executor._enabled is False

    def test_no_automatic_arm_during_construction(self):
        """Constructing twice keeps both instances disabled (no global state)."""
        e1 = self._fresh_executor()
        e2 = self._fresh_executor()
        assert e1._enabled is False and e2._enabled is False
        assert e1 is not e2

    def test_safety_comment_in_source(self):
        src = BET_EXECUTOR_MODULE.read_text()
        init_start = src.index("class BetExecutor:")
        init_body = src[init_start:init_start + 4000]
        assert "default-disabled" in init_body.lower()


# ---------------------------------------------------------------------------
# Section 5: cross-cutting fail-closed invariants
# ---------------------------------------------------------------------------


class TestFailClosedInvariants:
    """Whole-loop invariants tying the three gates together."""

    def test_paper_module_mentions_hard_gate(self):
        src = PAPER_MODULE.read_text().upper()
        assert "HARD GATE" in src

    def test_backtest_docstring_says_does_not_place_bets(self):
        doc = (
            __import__("tools.backtest", fromlist=["BacktestEngine"])
            .BacktestEngine.generate_paper_trade_signal.__doc__
        )
        assert "Does NOT place bets" in doc

    def test_callisto_startup_warns_about_live_flag(self):
        src = (REPO_ROOT / "callisto.py").read_text()
        assert "CALLISTO_ALLOW_LIVE_EXECUTE=1" in src

    def test_doctor_reports_live_flag_state(self):
        src = (REPO_ROOT / "tools" / "cli" / "doctor.py").read_text()
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src

    def test_status_cli_lists_live_flag(self):
        src = (REPO_ROOT / "tools" / "cli" / "status.py").read_text()
        assert '"CALLISTO_ALLOW_LIVE_EXECUTE"' in src

    def test_no_widening_of_paper_statuses_anywhere_in_tools(self):
        """No other module may add statuses to the allowed set."""
        forbidden = re.compile(
            r'_PAPER_TRADE_SIGNAL_STATUSES\s*(\|=|\.add\(|\.update\()', re.M
        )
        for py in (REPO_ROOT / "tools").rglob("*.py"):
            if forbidden.search(py.read_text(errors="replace")):
                pytest.fail(f"{py} widens the paper-status gate")

    def test_generate_paper_trade_signal_name_unique_definition(self):
        src = BACKTEST_MODULE.read_text()
        assert len(re.findall(r"async def generate_paper_trade_signal\b", src)) == 1

    def test_paper_gate_module_docstring_claims_ownership(self):
        src = PAPER_MODULE.read_text()
        assert "ONLY definition" in src
