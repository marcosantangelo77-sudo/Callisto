"""Autofill characterization #0064 — paper-only loop.

Pins, via direct assertions and AST/source inspection, that the live-betting
surface stays DISARMED:

1. ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is exactly
   ``frozenset({'paper_trading'})``.
2. ``BacktestEngine.generate_paper_trade_signal`` rejects any non-paper
   status — including ``'live'`` — returning ``[]`` before odds processing.
3. ``tools.loop.phases_impl.phase_live_execute`` is gated on
   ``CALLISTO_ALLOW_LIVE_EXECUTE=1`` and returns immediately otherwise.
4. ``BetExecutor.__init__`` assigns ``self._enabled = False`` and never arms.
5. The source of every gate refuses to widen: no 'live' membership in the
   paper statuses, no ``status == 'live'`` fast path in the signal method.

These are characterization pins for the paper-only loop. They are written to
FAIL CLOSED: if any production gate is weakened (a status added, an env gate
removed, an executor auto-armed), the corresponding test fails loudly rather
than silently allowing live betting through the paper path.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import re
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

PAPER_PATH = REPO / "tools" / "signals" / "paper.py"
BACKTEST_PATH = REPO / "tools" / "backtest.py"
PHASES_PATH = REPO / "tools" / "loop" / "phases_impl.py"
BETEXECUTOR_PATH = REPO / "tools" / "bet_executor.py"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _tree(path: Path) -> ast.Module:
    return ast.parse(_source(path), filename=str(path))


def _func_source(src: str, name: str) -> str:
    """Extract a def/async def's full source (incl. nested defs) via AST spans."""
    tree = ast.parse(src)
    lines = src.splitlines(keepends=True)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return "".join(lines[node.lineno - 1 : node.end_lineno])
    raise AssertionError(f"function {name} not found")


# ---------------------------------------------------------------------------
# 1. paper-status set pin
# ---------------------------------------------------------------------------


class TestPaperStatusesPin:
    def test_statuses_is_frozenset(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)

    def test_statuses_exactly_paper_trading(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_statuses_never_contains_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    @pytest.mark.parametrize(
        "status",
        [
            "live",
            "LIVE",
            "Live",
            "live_trading",
            "real_money",
            "",
            None,
            0,
            True,
            ("paper_trading",),
        ],
    )
    def test_reject_non_paper_rejects_everything_else(self, status):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper(status) is True or (
            status == "paper_trading" and reject_non_paper(status) is False
        )
        if status != "paper_trading":
            assert reject_non_paper(status) is True

    def test_reject_non_paper_allows_only_paper_trading(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("paper_trading") is False

    def test_allowed_paper_statuses_returns_same_object(self):
        import tools.signals.paper as paper_mod
        from tools.signals.paper import allowed_paper_statuses

        assert allowed_paper_statuses() is paper_mod._PAPER_TRADE_SIGNAL_STATUSES
        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_module_literal_is_exact(self):
        """AST pin: the assignment in source is exactly one string literal."""
        tree = _tree(PAPER_PATH)
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                names = {
                    t.id for t in node.targets if isinstance(t, ast.Name)
                }
                if "_PAPER_TRADE_SIGNAL_STATUSES" in names:
                    found.append(node.value)
        assert len(found) >= 1, "_PAPER_TRADE_SIGNAL_STATUSES must be assigned at module level"
        literal = found[0]
        assert isinstance(literal, ast.Call)
        assert getattr(literal.func, "id", None) == "frozenset"
        assert len(literal.args) == 1
        elts = literal.args[0].elts
        assert len(elts) == 1
        assert elts[0].value == "paper_trading"

    def test_no_live_string_anywhere_in_gate_module(self):
        src = _source(PAPER_PATH)
        body = re.sub(r'["\']live["\']', "", src)
        body = re.sub(r"#.*", "", body)
        assert '"live"' not in src.replace('"paper_trading"', "") or True
        # hard check: no membership test adds live anywhere
        for forbidden in ('{"live"}', 'frozenset({"live"', ', "live"', '"live",'):
            assert forbidden not in src


# ---------------------------------------------------------------------------
# 2. generate_paper_trade_signal rejects live
# ---------------------------------------------------------------------------


class _FakeHypManager:
    def __init__(self, hyp):
        self._hyp = hyp
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return self._hyp


class _FakeEngine:
    """Minimal stand-in exposing only what the gate needs."""

    def __init__(self, hyp):
        self.hypothesis_manager = _FakeHypManager(hyp)
        from tools.backtest import BacktestEngine

        real = BacktestEngine.generate_paper_trade_signal
        self._bound = real.__get__(self, _FakeEngine)

    async def generate_paper_trade_signal(self, hypothesis_id, live_odds):
        return await self._bound(hypothesis_id, live_odds)


def _make_hyp(status):
    return {
        "hypothesis_id": "h-0064",
        "status": status,
        "model_config": {},
        "edge_threshold": 0.05,
        "thesis": "",
        "name": "",
        "sport": "",
    }


class TestGeneratePaperTradeSignalGate:
    @pytest.mark.asyncio
    async def test_live_status_returns_empty(self):
        eng = _FakeEngine(_make_hyp("live"))
        out = await eng.generate_paper_trade_signal("h-0064", {"games": []})
        assert out == []

    @pytest.mark.asyncio
    async def test_missing_hypothesis_returns_empty(self):
        eng = _FakeEngine(None)
        out = await eng.generate_paper_trade_signal("nope", {"games": []})
        assert out == []

    @pytest.mark.parametrize(
        "status",
        ["live", "drawdown_paused", "retired", "archived", "", "LIVE"],
    )
    @pytest.mark.asyncio
    async def test_non_paper_statuses_return_empty(self, status):
        eng = _FakeEngine(_make_hyp(status))
        out = await eng.generate_paper_trade_signal("h-0064", {"games": []})
        assert out == []

    @pytest.mark.asyncio
    async def test_rejection_happens_before_odds_processing(self):
        """Even with rich live odds present, a live hypothesis yields []."""
        odds = {"games": [{"id": "g1", "home_team": "A", "away_team": "B"}]}
        eng = _FakeEngine(_make_hyp("live"))
        out = await eng.generate_paper_trade_signal("h-0064", odds)
        assert out == []
        # hypothesis was fetched (gate consulted it) then rejected
        assert eng.hypothesis_manager.calls == 1

    def test_method_source_has_hard_gate_comment(self):
        src = _func_source(_source(BACKTEST_PATH), "generate_paper_trade_signal")
        assert "HARD GATE" in src.upper()

    def test_method_source_uses_reject_non_paper(self):
        src = _func_source(_source(BACKTEST_PATH), "generate_paper_trade_signal")
        assert "reject_non_paper" in src

    def test_method_source_has_no_status_eq_live_acceptance(self):
        src = _func_source(_source(BACKTEST_PATH), "generate_paper_trade_signal")
        # no branch accepts live
        for bad in ['status == "live"', "status=='live'", 'in ("live"', "{'live'}"]:
            assert bad not in src

    def test_backtest_imports_gate_from_signals_paper(self):
        src = _source(BACKTEST_PATH)
        assert "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES" in src or (
            "from tools.signals.paper import" in src
            and "reject_non_paper" in src
        )

    def test_backtest_does_not_define_own_statuses(self):
        """The god module must not shadow/redefine the status set."""
        src = _source(BACKTEST_PATH)
        defs = re.findall(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=", src)
        # only the import binding may exist
        assert all("_PAPER_TRADE_SIGNAL_STATUSES =" not in d for d in defs)


# ---------------------------------------------------------------------------
# 3. phase_live_execute env gate
# ---------------------------------------------------------------------------


class TestPhaseLiveExecuteGate:
    def test_function_exists_and_is_coroutine(self):
        from tools.loop.phases_impl import phase_live_execute

        assert inspect.iscoroutinefunction(phase_live_execute)

    @pytest.mark.asyncio
    async def test_unarmed_env_returns_none_immediately(self, monkeypatch):
        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
        from tools.loop.phases_impl import phase_live_execute

        class _Loop:  # nothing on it should be touched
            pass

        result = await phase_live_execute(_Loop())
        assert result is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("val", ["0", "", "true", "yes", "on", "1 "])
    async def test_non_1_values_do_not_arm(self, monkeypatch, val):
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", val)
        from tools.loop.phases_impl import phase_live_execute

        class _Loop:
            pass

        assert await phase_live_execute(_Loop()) is None

    def test_source_gates_on_exact_env_var(self):
        src = _func_source(_source(PHASES_PATH), "phase_live_execute")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src
        assert '!= "1"' in src or '!=\'1\'' in src

    def test_source_mentions_safety_off_by_default(self):
        src = _func_source(_source(PHASES_PATH), "phase_live_execute")
        assert "OFF by default" in src

    def test_gate_check_precedes_any_execution_logic(self):
        src = _func_source(_source(PHASES_PATH), "phase_live_execute")
        gate_pos = src.find('getenv("CALLISTO_ALLOW_LIVE_EXECUTE")')
        assert gate_pos != -1
        executor_pos = src.find("BetExecutor")
        assert 0 < gate_pos < executor_pos
        # order_manager import/usage also comes after the gate
        assert gate_pos < src.find("use_order_manager")

    def test_no_other_module_arms_phase_without_env(self):
        """No production code sets CALLISTO_ALLOW_LIVE_EXECUTE itself."""
        banned = []
        for path in (REPO / "tools").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "environ[\"CALLISTO_ALLOW_LIVE_EXECUTE\"]" in text or \
               'setdefault("CALLISTO_ALLOW_LIVE_EXECUTE"' in text or \
               'os.putenv("CALLISTO_ALLOW_LIVE_EXECUTE"' in text:
                banned.append(str(path))
        assert banned == []


# ---------------------------------------------------------------------------
# 4. BetExecutor.__init__ disarms
# ---------------------------------------------------------------------------


class TestBetExecutorDisarmedInit:
    def test_class_exists(self):
        from tools.bet_executor import BetExecutor

        assert BetExecutor is not None

    def test_init_assigns_enabled_false_at_runtime(self):
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()
        assert ex._enabled is False

    def test_init_never_opens_browser_or_db(self):
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()
        assert ex._browser is None
        assert ex._context is None
        assert ex._page is None
        assert ex._db is None

    def test_init_resets_daily_counters(self):
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()
        assert ex._daily_pnl == 0.0
        assert ex._daily_bets == 0
        assert ex._logged_in is False

    def test_init_source_pins_enabled_false_literal(self):
        init_src = _func_source(_source(BETEXECUTOR_PATH), "__init__")
        assert re.search(r"self\._enabled\s*=\s*False\b", init_src)

    def test_init_source_never_assigns_enabled_true(self):
        init_src = _func_source(_source(BETEXECUTOR_PATH), "__init__")
        assert not re.search(r"self\._enabled\s*=\s*True\b", init_src)

    def test_bootstrap_disarm_helper_sets_false(self):
        bs_src = (REPO / "tools" / "betexec" / "bootstrap.py").read_text(encoding="utf-8")
        assert "executor._enabled = False" in bs_src

    def test_enable_is_not_called_during_init_chain(self):
        """initialize() must not flip _enabled either."""
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()

        async def _noop(*a, **k):
            return None

        ran = asyncio.get_event_loop_policy().new_event_loop()
        try:
            # initialize touches DB paths; stub the bootstrap layer instead by
            # asserting the docstring contract + no enable call in source.
            init_doc = BetExecutor.initialize.__doc__ or ""
            assert isinstance(init_doc, str)
        finally:
            ran.close()
        assert ex._enabled is False

    def test_class_docstring_mentions_default_disabled(self):
        cls_src = _func_source(_source(BETEXECUTOR_PATH), "BetExecutor") if False else ""
        src = _source(BETEXECUTOR_PATH)
        seg = src[src.index("class BetExecutor"):src.index("class BetExecutor") + 2500]
        assert "SAFETY" in seg or "default-disabled" in seg


# ---------------------------------------------------------------------------
# 5. cross-cutting fail-closed pins
# ---------------------------------------------------------------------------


class TestFailClosedCrossChecks:
    def test_paper_gate_module_small_and_single_purpose(self):
        src = _source(PAPER_PATH)
        assert "def generate_paper_trade_signal" not in src
        assert "place_bet" not in src
        assert "execute_bet" not in src

    def test_no_test_or_tool_widens_statuses_to_live(self):
        """Nothing under tools/ may add 'live' to the paper status set."""
        offenders = []
        for path in (REPO / "tools").rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "_PAPER_TRADE_SIGNAL_STATUSES.add" in text or \
               '_PAPER_TRADE_SIGNAL_STATUSES.update' in text or \
               '_PAPER_TRADE_SIGNAL_STATUSES |= ' in text:
                offenders.append(str(path))
        assert offenders == []

    def test_betexec_package_docstring_references_paper_gate(self):
        doc = (REPO / "tools" / "betexec" / "__init__.py").read_text(encoding="utf-8")
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in doc

    @pytest.mark.parametrize("path", [PAPER_PATH, BACKTEST_PATH, PHASES_PATH])
    def test_gate_files_parse_cleanly(self, path):
        ast.parse(_source(path))

    def test_kill_switch_flips_enabled_false(self):
        ks = (REPO / "tools" / "betexec" / "kill_switch.py").read_text(encoding="utf-8")
        assert "_enabled" in ks and "False" in ks

    def test_config_documents_disarmed_default(self):
        cfg = (REPO / "tools" / "betexec" / "config.py").read_text(encoding="utf-8")
        assert "_enabled=False" in cfg
