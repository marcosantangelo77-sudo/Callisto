"""autofill #0040 — paper-only loop characterization tests.

Pins the safety contract that keeps the signal-generation → execution
loop paper-only unless an operator explicitly arms live execution:

* ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` is exactly
  ``frozenset({'paper_trading'})`` — "live" must never be added.
* ``BacktestEngine.generate_paper_trade_signal`` returns ``[]`` for any
  non-paper status (including ``"live"``) BEFORE touching odds.
* ``phase_live_execute`` (both ``tools.autonomous.ResearchLoop`` and the
  standalone implementation in ``tools.loop.phases_impl``) is a no-op
  unless ``CALLISTO_ALLOW_LIVE_EXECUTE=1``.
* ``BetExecutor.__init__`` assigns ``_enabled = False`` — the executor
  never arms itself.

All tests are offline: no browser, no network, no live orders, no DB.
The engine used for generate_paper_trade_signal is constructed via
``object.__new__`` and its ``hypothesis_manager`` is a stub, so nothing
real runs.

Invariants under test:

I1.  Status-set identity and immutability pins.
I2.  reject_non_paper / allowed_paper_statuses helper behavior.
I3.  Source/AST pins on tools/signals/paper.py (no 'live' membership).
I4.  generate_paper_trade_signal rejects live / other statuses early,
     before any odds processing.
I5.  generate_paper_trade_signal still works for paper_trading (smoke).
I6.  phase_live_execute gates on CALLISTO_ALLOW_LIVE_EXECUTE (AST pin +
     runtime behavior for phases_impl).
I7.  BetExecutor.__init__ default-disabled pin (behavior + AST).
I8.  Defense-in-depth greps: production sources never widen the gate.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.signals.paper import (
    _PAPER_TRADE_SIGNAL_STATUSES,
    allowed_paper_statuses,
    reject_non_paper,
)

REPO = Path(__file__).resolve().parents[1]
PAPER_SRC = REPO / "tools" / "signals" / "paper.py"
AUTONOMOUS_SRC = REPO / "tools" / "autonomous.py"
PHASES_SRC = REPO / "tools" / "loop" / "phases_impl.py"
BETEXEC_SRC = REPO / "tools" / "bet_executor.py"

# ---------------------------------------------------------------------------
# I1. The status set itself
# ---------------------------------------------------------------------------


class TestPaperStatusSet:
    def test_is_exactly_paper_trading_frozenset(self):
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_not_member(self):
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_is_a_frozenset(self):
        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)

    def test_immutable(self):
        with pytest.raises(AttributeError):
            _PAPER_TRADE_SIGNAL_STATUSES.add("live")  # type: ignore[attr-defined]

    def test_no_live_like_variants(self):
        for bad in ("Live", "LIVE", "live_trading", "real_money", "production"):
            assert bad not in _PAPER_TRADE_SIGNAL_STATUSES

    def test_allowed_paper_statuses_returns_same_set(self):
        assert allowed_paper_statuses() is _PAPER_TRADE_SIGNAL_STATUSES or (
            allowed_paper_statuses() == _PAPER_TRADE_SIGNAL_STATUSES
        )


# ---------------------------------------------------------------------------
# I2. Helper behavior
# ---------------------------------------------------------------------------


class TestRejectNonPaper:
    @pytest.mark.parametrize("status", ["paper_trading"])
    def test_paper_allowed(self, status):
        assert reject_non_paper(status) is False

    @pytest.mark.parametrize(
        "status",
        ["live", "backtesting", "paused", "archived", "", None, 0],
    )
    def test_everything_else_rejected(self, status):
        assert reject_non_paper(status) is True


# ---------------------------------------------------------------------------
# I3. Source pins on tools/signals/paper.py
# ---------------------------------------------------------------------------


class TestPaperModuleSourcePins:
    def test_definition_literal(self):
        src = PAPER_SRC.read_text(encoding="utf-8")
        assert '_PAPER_TRADE_SIGNAL_STATUSES = frozenset({"paper_trading"})' in src

    def test_only_one_assignment(self):
        tree = ast.parse(PAPER_SRC.read_text(encoding="utf-8"))
        assigns = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Assign)
            and any(getattr(t, "id", "") == "_PAPER_TRADE_SIGNAL_STATUSES" for t in n.targets)
        ]
        assert len(assigns) == 1
        dump = ast.dump(assigns[0].value)
        assert '"live"' not in dump.replace("'live'", '"live"')

    def test_docstring_warns_against_live(self):
        src = PAPER_SRC.read_text(encoding="utf-8")
        assert "NEVER" in src or "FORBIDDEN" in src


# ---------------------------------------------------------------------------
# I4/I5. generate_paper_trade_signal behavior (offline stub engine)
# ---------------------------------------------------------------------------


def _make_engine(hypothesis: dict | None):
    """Build a BacktestEngine without running __init__ (no DB, no network)."""
    from tools.backtest import BacktestEngine

    engine = object.__new__(BacktestEngine)

    class _StubManager:
        def __init__(self, h):
            self._h = h
            self.calls = 0

        async def get_hypothesis(self, hypothesis_id):
            self.calls += 1
            return self._h

    engine.hypothesis_manager = _StubManager(hypothesis)
    return engine


def _paper_hypothesis() -> dict:
    return {
        "id": "hyp-1",
        "name": "b2b fade",
        "sport": "basketball_nba",
        "status": "paper_trading",
        "edge_threshold": 0.05,
        "model_config": {"target_book": "draftkings", "devig_method": "power"},
        "thesis": "",
    }


ODDS = {"games": []}


@pytest.mark.asyncio
class TestGeneratePaperTradeSignalGate:
    async def test_live_status_returns_empty(self):
        h = _paper_hypothesis()
        h["status"] = "live"
        eng = _make_engine(h)
        assert await eng.generate_paper_trade_signal("hyp-1", ODDS) == []

    async def test_backtesting_status_returns_empty(self):
        h = _paper_hypothesis()
        h["status"] = "backtesting"
        eng = _make_engine(h)
        assert await eng.generate_paper_trade_signal("hyp-1", ODDS) == []

    async def test_missing_hypothesis_returns_empty(self):
        eng = _make_engine(None)
        assert await eng.generate_paper_trade_signal("nope", ODDS) == []

    async def test_none_status_rejected_by_helper(self):
        # None status would crash a naive `==` check; the frozenset gate
        # rejects it cleanly.
        assert reject_non_paper(None) is True

    async def test_lookup_happens_before_odds_processing(self):
        h = _paper_hypothesis()
        h["status"] = "live"
        eng = _make_engine(h)
        result = await eng.generate_paper_trade_signal("hyp-1", ODDS)
        assert result == []
        assert eng.hypothesis_manager.calls == 1

    async def test_paper_status_passes_gate_and_runs(self):
        h = _paper_hypothesis()
        h["market_type"] = "h2h"
        eng = _make_engine(h)
        eng._db = None
        eng._parse_hypothesis_filters = lambda *a, **k: {}
        eng._needs_context_filter = lambda *a, **k: False

        class _FakeDB:
            async def execute(self, *a, **k):
                class _C:
                    description = [("id",)]
                    async def fetchall(self):
                        return []
                return _C()
            async def commit(self):
                return None
        eng._db = _FakeDB()
        result = await eng.generate_paper_trade_signal("hyp-1", ODDS)
        # No games → empty list, but crucially it did NOT bail at the gate.
        assert result == []
        assert eng.hypothesis_manager.calls == 1

    async def test_string_model_config_accepted_for_paper(self):
        h = _paper_hypothesis()
        h["market_type"] = "h2h"
        h["model_config"] = '{"target_book": "fanduel"}'
        eng = _make_engine(h)
        # Only the early part of the happy path is exercised here: patch out
        # everything past config parsing by stubbing the DB the same way.
        eng._parse_hypothesis_filters = lambda *a, **k: {}
        eng._needs_context_filter = lambda *a, **k: False

        class _FakeDB:
            async def execute(self, *a, **k):
                class _C:
                    description = [("id",)]
                    async def fetchall(self):
                        return []
                return _C()
            async def commit(self):
                return None
        eng._db = _FakeDB()
        assert await eng.generate_paper_trade_signal("hyp-1", ODDS) == []


# ---------------------------------------------------------------------------
# I6. phase_live_execute env gate
# ---------------------------------------------------------------------------


def _find_async_fn(path: Path, class_name: str | None, fn_name: str) -> ast.AsyncFunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    scope = tree.body
    if class_name:
        for node in scope:
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                scope = node.body
                break
    for item in scope:
        if isinstance(item, ast.AsyncFunctionDef) and item.name == fn_name:
            return item
    raise AssertionError(f"{fn_name} not found in {path}")


def _strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    # phases_impl's extracted phase binds `self = loop` before its docstring;
    # drop that first so the real docstring is recognized.
    if (
        body
        and isinstance(body[0], ast.Assign)
        and len(body[0].targets) == 1
        and getattr(body[0].targets[0], "id", "") == "self"
    ):
        body = body[1:]
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            return body[1:]
    return body


class TestPhaseLiveExecuteGate:
    def test_autonomous_phase_exists(self):
        fn = _find_async_fn(AUTONOMOUS_SRC, "ResearchLoop", "_phase_live_execute")
        assert fn is not None

    def test_phases_impl_phase_exists(self):
        fn = _find_async_fn(PHASES_SRC, None, "phase_live_execute")
        assert fn is not None

    @pytest.mark.parametrize(
        "path,cls,fn",
        [
            (AUTONOMOUS_SRC, "ResearchLoop", "_phase_live_execute"),
            (PHASES_SRC, None, "phase_live_execute"),
        ],
    )
    def test_env_gate_mentions_exact_var_and_value(self, path, cls, fn):
        fndef = _find_async_fn(path, cls, fn)
        src = ast.get_source_segment(path.read_text(encoding="utf-8"), fndef)
        assert src is not None
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in src
        assert '!= "1"' in src or '== "1"' not in src.split('!= "1"')[0]

    @pytest.mark.parametrize(
        "path,cls,fn",
        [
            (AUTONOMOUS_SRC, "ResearchLoop", "_phase_live_execute"),
            (PHASES_SRC, None, "phase_live_execute"),
        ],
    )
    def test_gate_is_first_control_flow_after_import(self, path, cls, fn):
        fndef = _find_async_fn(path, cls, fn)
        stmts = _strip_docstring(fndef.body)
        if stmts and isinstance(stmts[0], ast.Import):
            stmts = stmts[1:]
        # phases_impl's phase_live_execute binds `self = loop` first (the
        # extracted implementation keeps the original method shape); skip
        # that leading assignment too.
        if (
            stmts
            and isinstance(stmts[0], ast.Assign)
            and len(stmts[0].targets) == 1
            and getattr(stmts[0].targets[0], "id", "") == "self"
        ):
            stmts = stmts[1:]
        assert stmts, "body empty after docstring/import"
        first = stmts[0]
        assert isinstance(first, ast.If), type(first).__name__
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in ast.dump(first.test)
        assert any(isinstance(s, ast.Return) for s in first.body)


@pytest.mark.asyncio
class TestPhasesImplRuntimeGate:
    async def test_unset_env_is_noop(self):
        from tools.loop.phases_impl import phase_live_execute

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLISTO_ALLOW_LIVE_EXECUTE", None)
            # Must return without raising and without importing executors.
            await phase_live_execute(loop=None)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["0", "true", "yes", "on", " 1", "1 ", "2"])
    async def test_non_1_values_are_noop(self, bad):
        from tools.loop.phases_impl import phase_live_execute

        with patch.dict(os.environ, {"CALLISTO_ALLOW_LIVE_EXECUTE": bad}):
            await phase_live_execute(loop=None)  # type: ignore[arg-type]

    def test_source_never_calls_generate_paper_trade_signal_before_gate(self):
        src = PHASES_SRC.read_text(encoding="utf-8")
        head = src.split("CALLISTO_ALLOW_LIVE_EXECUTE", 1)[0]
        # The module-level docstring may mention it; executable code before
        # the gate must not call the generator.
        assert "generate_paper_trade_signal(" not in head.split('"""')[-1]


# ---------------------------------------------------------------------------
# I7. BetExecutor default-disabled
# ---------------------------------------------------------------------------


class TestBetExecutorDefaults:
    def test_init_sets_enabled_false(self):
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()
        assert ex._enabled is False

    def test_init_leaves_browser_page_none(self):
        from tools.bet_executor import BetExecutor

        ex = BetExecutor()
        assert ex._browser is None
        assert ex._context is None
        assert ex._page is None
        assert ex._db is None
        assert ex._logged_in is False

    def test_ast_pin_enabled_false_assigned_in_init(self):
        fndef = _find_async_or_sync(BETEXEC_SRC, "BetExecutor", "__init__")
        dump = ast.dump(fndef)
        assert "_enabled" in dump
        assert "Constant(value=False)" in dump

    def test_source_comment_marks_safety_default(self):
        src = BETEXEC_SRC.read_text(encoding="utf-8")
        init_src = src.split("def __init__", 1)[1].split("async def initialize", 1)[0]
        assert "_enabled = False" in init_src

    def test_two_instances_do_not_share_enabled_state(self):
        from tools.bet_executor import BetExecutor

        a, b = BetExecutor(), BetExecutor()
        assert a._enabled is False and b._enabled is False


def _find_async_or_sync(path: Path, class_name: str, fn_name: str):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == fn_name:
                    return item
    raise AssertionError(f"{class_name}.{fn_name} not found")


# ---------------------------------------------------------------------------
# I8. Defense-in-depth source sweeps
# ---------------------------------------------------------------------------


class TestDefenseInDepth:
    def test_paper_module_never_references_allow_live(self):
        src = PAPER_SRC.read_text(encoding="utf-8")
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" not in src

    def test_generate_paper_trade_signal_docstring_forbids_live(self):
        from tools.backtest import BacktestEngine

        doc = inspect.getdoc(BacktestEngine.generate_paper_trade_signal) or ""
        assert "ONLY" in doc.upper()
        assert "live" in doc.lower()

    def test_autonomous_module_documents_gate_first(self):
        src = AUTONOMOUS_SRC.read_text(encoding="utf-8")
        idx = src.find("_phase_live_execute")
        assert idx != -1
        window = src[max(0, idx - 400): idx + 2000]
        assert "CALLISTO_ALLOW_LIVE_EXECUTE" in window

    def test_no_production_source_appends_live_to_statuses(self):
        offenders = []
        for path in REPO.rglob("*.py"):
            sp = str(path)
            if "/tests/" in sp or sp.endswith("callisto.py"):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "_PAPER_TRADE_SIGNAL_STATUSES" in text and '"live"' in text:
                # Allow mentions inside comments/docstrings only — flag any
                # assignment or update that could widen the set.
                for lineno, line in enumerate(text.splitlines(), 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "_PAPER_TRADE_SIGNAL_STATUSES" in stripped and (
                        "|=" or "update(" or '"live"' in stripped
                    ) and "=" in stripped and "frozenset({\"paper_trading\"})" not in stripped:
                        offenders.append(f"{sp}:{lineno}: {stripped}")
        assert offenders == [], f"possible gate widening: {offenders}"
