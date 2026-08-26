"""autofill characterization #0016 — paper-only loop.

Characterization tests pinning the safety gates of the Callisto paper-trading
loop:

1. ``_PAPER_TRADE_SIGNAL_STATUSES`` is exactly ``frozenset({'paper_trading'})``.
2. ``generate_paper_trade_signal`` rejects any hypothesis whose status is not
   exactly ``"paper_trading"`` — including ``"live"``.
3. ``phase_live_execute`` refuses to run unless the operator explicitly sets
   ``CALLISTO_ALLOW_LIVE_EXECUTE=1``.
4. ``BetExecutor.__init__`` assigns ``_enabled = False`` (default-disabled).

These are characterization / pin tests: if any of them fail, a production
safety gate has been weakened and the change must be treated as FAIL CLOSED —
do not "fix" the test by arming live betting, do not add ``"live"`` to
``_PAPER_TRADE_SIGNAL_STATUSES``, and never widen
``generate_paper_trade_signal`` to accept ``status == 'live'``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import os
import textwrap
import types
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Imports under test
# ---------------------------------------------------------------------------

import tools.signals.paper as paper_signals
from tools.signals.paper import (
    _PAPER_TRADE_SIGNAL_STATUSES,
    allowed_paper_statuses,
    reject_non_paper,
)
from tools.backtest import BacktestEngine
from tools.loop.phases_impl import phase_live_execute
from tools.bet_executor import BetExecutor


# ===========================================================================
# 1. The paper-status hard gate constant
# ===========================================================================


class TestPaperStatusGateConstant:
    """Pin the ONLY definition of which statuses may generate paper signals."""

    def test_constant_is_a_frozenset(self):
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)

    def test_constant_is_exactly_paper_trading(self):
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_is_not_allowed(self):
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_no_other_statuses_allowed(self):
        # Exactly one member; nothing else sneaks in.
        assert len(_PAPER_TRADE_SIGNAL_STATUSES) == 1

    def test_case_variants_rejected(self):
        for bad in ("LIVE", "Live", "Paper_Trading", "PAPER_TRADING", ""):
            assert bad not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_similar_but_wrong_statuses_rejected(self):
        for bad in (
            "paper",
            "trading",
            "paper_trading ",
            " paper_trading",
            "live_trading",
            "proven",
            "active",
        ):
            assert bad not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_allowed_paper_statuses_returns_the_same_frozenset(self):
        result = allowed_paper_statuses()
        assert isinstance(result, frozenset)
        assert result == _PAPER_TRADE_SIGNAL_STATUSES

    def test_allowed_paper_statuses_returns_exact_object(self):
        # Same object identity: no copy that could drift out of sync.
        assert allowed_paper_statuses() is _PAPER_TRADE_SIGNAL_STATUSES

    def test_module_attribute_not_shadowed_by_import_time_mutation(self):
        import importlib

        mod = importlib.import_module("tools.signals.paper")
        reloaded_value = getattr(mod, "_PAPER_TRADE_SIGNAL_STATUSES")
        assert reloaded_value == frozenset({"paper_trading"})


# ===========================================================================
# 2. reject_non_paper predicate
# ===========================================================================


class TestRejectNonPaper:
    @pytest.mark.parametrize(
        "status,expected_reject",
        [
            ("paper_trading", False),
            ("live", True),
            ("LIVE", True),
            ("", True),
            (None, True),
            (0, True),
            ("paper-trading", True),  # hyphen variant must NOT pass
            ("paper_trading\n", True),
        ],
    )
    def test_predicate(self, status, expected_reject):
        assert reject_non_paper(status) is expected_reject

    def test_predicate_is_pure_function_of_constant(self):
        # reject_non_paper(s) must be equivalent to membership in the gate set.
        for status in ("paper_trading", "live", "x", None, 42):
            assert reject_non_paper(status) == (
                status not in _PAPER_TRADE_SIGNAL_STATUSES
            )

    def test_predicate_never_raises_on_arbitrary_objects(self):
        class Weird:
            def __eq__(self, other):  # noqa: D105
                raise RuntimeError("boom")

            def __hash__(self):
                return hash("Weird")

        # frozenset membership uses hashing first; must not blow up.
        assert reject_non_paper(Weird()) is True


# ===========================================================================
# 3. Source-level pins (AST): the gate lives where it must live
# ===========================================================================

PAPER_MODULE_PATH = inspect.getfile(paper_signals)


def _module_source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _parse(path: str) -> ast.AST:
    return ast.parse(_module_source(path))


class TestPaperSignalsSourcePins:
    def test_gate_defined_as_module_level_assignment(self):
        tree = _parse(PAPER_MODULE_PATH)
        targets = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        targets.append(t.id)
        assert "_PAPER_TRADE_SIGNAL_STATUSES" in targets

    def test_gate_assigned_a_frozenset_literal_in_source(self):
        tree = _parse(PAPER_MODULE_PATH)
        found = None
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "_PAPER_TRADE_SIGNAL_STATUSES":
                        found = node.value
        assert isinstance(found, ast.Call)
        assert isinstance(found.func, ast.Name)
        assert found.func.id == "frozenset"
        # Literal set argument containing only 'paper_trading'
        assert len(found.args) == 1
        arg = found.args[0]
        assert isinstance(arg, ast.Set)
        elems = [e.value for e in arg.elts if isinstance(e, ast.Constant)]
        assert elems == ["paper_trading"]

    def test_no_live_string_anywhere_in_gate_definition_line(self):
        src = _module_source(PAPER_MODULE_PATH)
        gate_lines = [
            ln
            for ln in src.splitlines()
            if "_PAPER_TRADE_SIGNAL_STATUSES =" in ln
        ]
        assert gate_lines, "gate assignment line missing"
        for ln in gate_lines:
            assert '"live"' not in ln and "'live'" not in ln

    def test_reject_non_paper_uses_membership_not_equality_chain(self):
        fn_src = textwrap.dedent(
            inspect.getsource(paper_signals.reject_non_paper)
        )
        assert "not in" in fn_src
        # Guard against a widened implementation comparing to 'live'.
        assert '"live"' not in fn_src and "'live'" not in fn_src


BACKTEST_PATH = inspect.getfile(BacktestEngine)


class TestBacktestEngineSourcePins:
    def _engine_class_node(self) -> ast.ClassDef:
        tree = _parse(BACKTEST_PATH)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "BacktestEngine":
                return node
        pytest.fail("BacktestEngine class not found")

    def _method_node(self, name: str) -> ast.FunctionDef:
        cls = self._engine_class_node()
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name:
                return item
        pytest.fail(f"{name} method not found on BacktestEngine")

    def test_generate_paper_trade_signal_exists_and_is_async(self):
        node = self._method_node("generate_paper_trade_signal")
        assert isinstance(node, ast.AsyncFunctionDef)

    def test_generate_paper_trade_signal_calls_reject_non_paper(self):
        node = self._method_node("generate_paper_trade_signal")
        call_names = []
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call):
                f = sub.func
                if isinstance(f, ast.Name):
                    call_names.append(f.id)
                elif isinstance(f, ast.Attribute):
                    call_names.append(f.attr)
        assert "reject_non_paper" in call_names

    def test_gate_check_precedes_any_odds_processing(self):
        # The reject_non_paper check must appear before the first use of
        # live_odds in the method body (early return before processing).
        node = self._method_node("generate_paper_trade_signal")
        body = node.body
        first_odds_idx = None
        gate_idx = None
        for i, stmt in enumerate(body):
            src = ast.dump(stmt)
            if gate_idx is None and "reject_non_paper" in src:
                gate_idx = i
            if first_odds_idx is None and "live_odds" in src:
                first_odds_idx = i
        assert gate_idx is not None
        if first_odds_idx is not None:
            assert gate_idx <= first_odds_idx

    def test_method_signature_takes_hypothesis_id_and_live_odds(self):
        sig = inspect.signature(BacktestEngine.generate_paper_trade_signal)
        params = list(sig.parameters)
        assert params[:3] == ["self", "hypothesis_id", "live_odds"]


# ===========================================================================
# 4. Behavioral: generate_paper_trade_signal rejects non-paper statuses
# ===========================================================================


def _make_engine_with_status(status, **extra):
    engine = object.__new__(BacktestEngine)
    fake_hyp = {
        "hypothesis_id": "hyp-1",
        "status": status,
        "model_config": {"threshold": 0.05},
        "sport": "baseball_mlb",
        "market_type": "h2h",
    }
    fake_hyp.update(extra)
    manager = types.SimpleNamespace(
        get_hypothesis=mock.AsyncMock(return_value=fake_hyp)
    )
    engine.hypothesis_manager = manager
    # Minimal async-DB stub: generate_paper_trade_signal queries
    # backtest_events after processing; with no games it must return [].
    cursor_stub = types.SimpleNamespace(
        fetchall=mock.AsyncMock(return_value=[]),
        description=[("hypothesis_id",), ("signal_generated",)],
    )
    engine._db = types.SimpleNamespace(
        execute=mock.AsyncMock(return_value=cursor_stub),
        executemany=mock.AsyncMock(),
        commit=mock.AsyncMock(),
    )
    return engine


class TestGeneratePaperTradeSignalRejectsNonPaper:
    LIVE_ODDS = {"baseball_mlb": {"home": -150, "away": +130}}

    @pytest.mark.parametrize(
        "bad_status",
        ["live", "LIVE", "retired", "", None, "paused", "archived", "pending"],
    )
    def test_bad_status_yields_empty_list(self, bad_status):
        engine = _make_engine_with_status(bad_status)
        result = asyncio.run(
            engine.generate_paper_trade_signal("hyp-1", dict(self.LIVE_ODDS))
        )
        assert result == []

    def test_missing_hypothesis_yields_empty_list(self):
        engine = object.__new__(BacktestEngine)
        engine.hypothesis_manager = types.SimpleNamespace(
            get_hypothesis=mock.AsyncMock(return_value=None)
        )
        result = asyncio.run(
            engine.generate_paper_trade_signal("nope", {"x": 1})
        )
        assert result == []

    def test_live_never_reaches_signal_computation(self):
        engine = _make_engine_with_status("live")
        with mock.patch.object(
            BacktestEngine,
            "_parse_hypothesis_filters",
            autospec=True,
        ) as spy:
            result = asyncio.run(
                engine.generate_paper_trade_signal("hyp-1", {"k": 1})
            )
            spy.assert_not_called()
        assert result == []

    def test_paper_trading_status_passes_the_gate(self):
        engine = _make_engine_with_status(
            "paper_trading", edge_threshold=0.03
        )
        with mock.patch.object(
            BacktestEngine,
            "_parse_hypothesis_filters",
            autospec=True,
            return_value={},
        ):
            result = asyncio.run(
                engine.generate_paper_trade_signal("hyp-1", {})
            )
        # Gate passed (filters parsing was reached); signals list may be
        # empty because no games were supplied, but it must be a list.
        assert isinstance(result, list)

    def test_gate_consulted_via_shared_module_function(self):
        # Swapping the shared predicate must change behavior — proves the
        # method delegates to tools.signals.paper rather than its own check.
        engine = _make_engine_with_status("live", edge_threshold=0.03)
        with mock.patch(
            "tools.backtest.reject_non_paper", side_effect=lambda s: s != "live"
        ):
            result = asyncio.run(
                engine.generate_paper_trade_signal("hyp-1", {})
            )
        assert result == []


# ===========================================================================
# 5. phase_live_execute env-var gate
# ===========================================================================


class TestPhaseLiveExecuteGate:
    def test_refuses_without_env_var(self, monkeypatch, caplog):
        monkeypatch.delenv("CALLISTO_ALLOW_LIVE_EXECUTE", raising=False)
        with caplog.at_level("INFO"):
            asyncio.run(phase_live_execute(loop=None))
        assert "CALLISTO_ALLOW_LIVE_EXECUTE!=1" in caplog.text or any(
            "live_execute skipped" in r.message for r in caplog.records
        )

    def test_refuses_with_env_var_set_to_something_else(self, monkeypatch, caplog):
        for val in ("0", "true", "yes", "2"):
            monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", val)
            caplog.clear()
            with caplog.at_level("INFO"):
                asyncio.run(phase_live_execute(loop=None))
            assert any(
                "live_execute skipped" in r.message for r in caplog.records
            ), f"value {val!r} must NOT arm live execute"

    def test_env_var_comparison_is_strict_equality_to_one(self):
        src = inspect.getsource(phase_live_execute)
        assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src

    @pytest.mark.parametrize(
        "env_val", ["0", ""],
    )
    def test_no_betexecutor_construction_when_gated_off(self, env_val, monkeypatch):
        monkeypatch.setenv("CALLISTO_ALLOW_LIVE_EXECUTE", env_val)
        with mock.patch("tools.bet_executor.BetExecutor") as spy:
            asyncio.run(phase_live_execute(loop=None))
            spy.assert_not_called()

    def test_docstring_documents_env_var_as_only_arming_switch(self):
        src = inspect.getsource(phase_live_execute)
        parts = src.split('"""')
        doc = parts[1] if len(parts) > 1 else ""
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in doc


# ===========================================================================
# 6. BetExecutor default-disabled
# ===========================================================================


class TestBetExecutorDefaultDisabled:
    def test_init_assigns_enabled_false(self):
        executor = BetExecutor.__new__(BetExecutor)
        BetExecutor.__init__(executor)
        assert executor._enabled is False

    def test_init_directly(self):
        executor = BetExecutor()
        assert executor._enabled is False

    def test_is_enabled_false_after_init(self):
        executor = BetExecutor()
        getter = getattr(executor, "is_enabled", None)
        if callable(getter) and not isinstance(getter, bool):
            assert getter() is False
        else:
            assert bool(getter) is False

    def test_source_pin_enabled_false_in_init(self):
        init_src = textwrap.dedent(inspect.getsource(BetExecutor.__init__))
        assert "self._enabled = False" in init_src
        assert "self._enabled = True" not in init_src

    def test_ast_pin_enabled_false_is_only_boolean_assignment_in_init(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(BetExecutor.__init__)))
        enabled_assignments = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Attribute) and t.attr == "_enabled":
                        value = node.value
                        if isinstance(value, ast.Constant):
                            enabled_assignments.append(value.value)
        assert enabled_assignments == [False]

    def test_enable_is_an_explicit_separate_method(self):
        assert hasattr(BetExecutor, "enable"), (
            "arming must require an explicit enable() call"
        )


# ===========================================================================
# 7. Cross-cutting: production files were not weakened
# ===========================================================================


class TestProductionGatesUntouched:
    def test_paper_module_does_not_import_or_mention_arming(self):
        src = _module_source(PAPER_MODULE_PATH)
        assert "enable()" not in src
        assert "arm" not in src.lower().replace("arming switch", "")

    def test_phases_impl_does_not_default_allow_live(self):
        phases_path = inspect.getfile(phase_live_execute)
        src = _module_source(phases_path)
        # No fallback default like getenv(..., "1")
        assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE", "1")' not in src
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src  # gate still present

    def test_backtest_docstring_forbids_live_status(self):
        doc = inspect.getdoc(BacktestEngine.generate_paper_trade_signal) or ""
        lowered = doc.lower()
        assert 'status == "live"' in lowered or "live" in lowered
        assert "forbidden" in lowered or "hard gate" in lowered
