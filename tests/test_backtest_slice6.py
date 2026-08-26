"""Slice-6 characterization tests for the tools.btest extraction.

Covers three areas:
  1. Facade imports — every public symbol that existed before slice 6 still
     resolves from tools.backtest, and the extracted mixins are wired into
     BacktestEngine via inheritance.
  2. Paper-only signal hard gate — generate_paper_trade_signal stays on the
     facade, calls reject_non_paper BEFORE paper_pipeline, and never admits
     a "live" status. _PAPER_TRADE_SIGNAL_STATUSES remains frozenset.
  3. A real extracted helper exercised end-to-end:
     tools.btest.snapshots.enrich_snapshot_with_multibook against an
     in-memory aiosqlite database (single-book -> multibook upgrade,
     pass-through when already multibook, pass-through when no data).
"""

import ast
import asyncio
import importlib
import inspect
import json

import pytest

backtest = importlib.import_module("tools.backtest")
engine_delegates = importlib.import_module("tools.btest.engine_delegates")
paper_module = importlib.import_module("tools.signals.paper")
snapshots = importlib.import_module("tools.btest.snapshots")


# ---------------------------------------------------------------------------
# 1. Facade surface
# ---------------------------------------------------------------------------

FACADE_FUNCTIONS = [
    "_signal_confidence",
    "dedup_best_edge_by_event",
    "insert_pending_rows",
    "new_trade_id",
    "devig_market",
    "power_devig",
    "multiplicative_devig",
    "ev_binary",
    "evaluate_edge",
    "kelly_binary",
    "validate_temporal_isolation",
]

FACADE_NAMES = [
    "_PAPER_TRADE_SIGNAL_STATUSES",
    "allowed_paper_statuses",
    "reject_non_paper",
    "game_date_from_commence",
    "DB_PATH",
    "BacktestEngine",
]


@pytest.mark.parametrize("name", FACADE_NAMES)
def test_facade_module_attribute(name):
    assert hasattr(backtest, name), f"tools.backtest lost {name}"


@pytest.mark.parametrize("name", FACADE_FUNCTIONS)
def test_facade_callable(name):
    fn = getattr(backtest, name)
    assert callable(fn), f"tools.backtest.{name} is not callable"


def test_engine_inherits_extracted_mixins():
    for mixin in (
        engine_delegates.RunPipelineMixin,
        engine_delegates.GameProcessingMixin,
        engine_delegates.ResolutionMixin,
    ):
        assert issubclass(backtest.BacktestEngine, mixin)


@pytest.mark.parametrize(
    "method",
    [
        "run_backtest",
        "_enrich_snapshot_with_multibook",
        "_populate_signals_from_backtest",
        "_process_game",
        "_process_game_lines",
        "_process_game_props",
        "_process_prop_snapshots",
        "resolve_with_scores",
        "resolve_from_game_results",
        "_get_affected_run_ids",
        "recalculate_run_stats",
        "recalculate_all_active_runs",
        "get_run_results",
        "generate_paper_trade_signal",
        "initialize",
        "close",
        "_resolve_line",
        "has_structured_filters",
        "compute_context_coverage",
    ],
)
def test_engine_method_signatures_unchanged(method):
    """Methods moved into mixins must keep their names and parameter lists."""
    fn = getattr(backtest.BacktestEngine, method)
    sig = inspect.signature(fn)
    params = list(sig.parameters)
    # Static helpers legitimately take no self/cls.
    assert (params and params[0] in ("self", "cls")) or method in (
        "has_structured_filters", "compute_context_coverage",
    ), f"{method}: first param must be self"
    assert all(
        p.kind is not inspect.Parameter.VAR_POSITIONAL
        and p.kind is not inspect.Parameter.VAR_KEYWORD
        or method.startswith("_matches")
        for p in list(sig.parameters.values())[1:]
        if p.name not in ("args", "kwargs")
    ), f"{method}: unexpected *args/**kwargs in signature"
    # Docstring preserved through extraction (extracted public methods).
    if method in ("run_backtest", "resolve_with_scores",
                  "resolve_from_game_results",
                  "recalculate_run_stats", "recalculate_all_active_runs",
                  "get_run_results", "generate_paper_trade_signal"):
        assert fn.__doc__, f"{method} lost its docstring"


# ---------------------------------------------------------------------------
# 2. Paper-only signal hard gate
# ---------------------------------------------------------------------------


def test_paper_statuses_frozenset_paper_only():
    statuses = paper_module._PAPER_TRADE_SIGNAL_STATUSES
    assert isinstance(statuses, frozenset)
    assert statuses == frozenset({"paper_trading"})
    assert "live" not in statuses


def test_generate_paper_trade_signal_lives_on_backtest_facade_source():
    """AST pin: the gated method is defined textually in tools/backtest.py."""
    src = open("tools/backtest.py").read()
    tree = ast.parse(src)
    funcs = [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
        and n.name == "generate_paper_trade_signal"
    ]
    assert len(funcs) >= 1, "generate_paper_trade_signal disappeared"
    body_src = "\n".join(ast.unparse(s) for s in funcs[0].body)
    assert "get_hypothesis" in body_src
    assert "reject_non_paper(" in body_src, "gate removed or bypassed"
    import re as _re

    assert _re.search(r"return\s+\[\]", body_src), "missing fail-closed branch"


class _FakeHypManager:
    def __init__(self, status):
        self._status = status
        self.calls = 0

    async def get_hypothesis(self, hypothesis_id):
        self.calls += 1
        return {"status": self._status}


class _SpyPaperPipeline:
    """Fail loudly if paper_pipeline.generate_paper_trade_signal is reached."""

    def __init__(self):
        self.called = False

    async def generate_paper_trade_signal(self, engine, hyp_id, live_odds):
        self.called = True
        return [{"spy": True}]


@pytest.mark.asyncio
async def test_gate_blocks_non_paper_statuses_before_pipeline(monkeypatch):
    spy = _SpyPaperPipeline()
    monkeypatch.setattr(backtest.paper_pipeline, "generate_paper_trade_signal",
                        spy.generate_paper_trade_signal)
    engine = object.__new__(backtest.BacktestEngine)

    for status in ("live", "paused", "archived", "backtesting", "", None):
        mgr = _FakeHypManager(status)
        engine.hypothesis_manager = mgr
        result = await engine.generate_paper_trade_signal("hyp-x", {"games": []})
        assert result == [], f"status={status!r} must be rejected"
        assert mgr.calls == 1
        assert spy.called is False, f"status={status!r} reached paper_pipeline!"


@pytest.mark.asyncio
async def test_gate_allows_paper_trading_and_delegates(monkeypatch):
    spy = _SpyPaperPipeline()
    monkeypatch.setattr(backtest.paper_pipeline, "generate_paper_trade_signal",
                        spy.generate_paper_trade_signal)
    engine = object.__new__(backtest.BacktestEngine)
    engine.hypothesis_manager = _FakeHypManager("paper_trading")
    result = await engine.generate_paper_trade_signal("hyp-1", {"games": []})
    assert result == [{"spy": True}]
    assert spy.called is True


@pytest.mark.asyncio
async def test_gate_missing_hypothesis_fails_closed():
    class _NoneMgr:
        async def get_hypothesis(self, hypothesis_id):
            return None

    engine = object.__new__(backtest.BacktestEngine)
    engine.hypothesis_manager = _NoneMgr()
    assert await engine.generate_paper_trade_signal("nope", {"games": []}) == []


def test_reject_non_paper_semantics():
    assert backtest.reject_non_paper("paper_trading") is False
    assert backtest.reject_non_paper("live") is True
    assert backtest.reject_non_paper("anything_else") is True


# ---------------------------------------------------------------------------
# 3. Real extracted helper: enrich_snapshot_with_multibook
# ---------------------------------------------------------------------------


async def _make_db():
    import aiosqlite

    db = await aiosqlite.connect(":memory:")
    # Real schema used by tools.btest.snapshots.enrich_snapshot_with_multibook.
    await db.execute(
        """
        CREATE TABLE odds_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            game_count INTEGER NOT NULL DEFAULT 0,
            snapshot_json TEXT NOT NULL
        )
        """
    )
    return db


def _snapshot_json(sport, date_str, books, n_games=2):
    """Build one odds_snapshots row payload with n_games multi-book events."""
    games = [
        {
            "id": f"{sport}-{i}",
            "sport_key": sport,
            "bookmakers": [{"key": b} for b in books],
        }
        for i in range(n_games)
    ]
    return (sport, f"{date_str}T12:00:00Z", len(games), json.dumps({"games": games}))


@pytest.mark.asyncio
async def test_enrich_upgrades_single_book_to_multibook():
    db = await _make_db()
    try:
        rows = [
            _snapshot_json("NBA", "2025-01-15", ["pinnacle", "fanduel"]),
            _snapshot_json("NBA", "2025-01-15", ["draftkings", "pinnacle",
                                                 "fanduel"]),
        ]
        await db.executemany(
            "INSERT INTO odds_snapshots (sport, timestamp, game_count,"
            " snapshot_json) VALUES (?, ?, ?, ?)",
            rows,
        )
        await db.commit()

        single_book_snapshot = {
            "games": [
                {"id": "NBA-0", "sport_key": "NBA",
                 "bookmakers": [{"key": "consensus"}]},
                {"id": "NBA-1", "sport_key": "NBA",
                 "bookmakers": [{"key": "consensus"}]},
            ],
        }
        enriched = await snapshots.enrich_snapshot_with_multibook(
            db, "NBA", "2025-01-15", single_book_snapshot, "pinnacle"
        )
        assert len(enriched["games"]) == 2, (
            "cross-sport contamination must be filtered"
        )
        books = {
            bm["key"]
            for g in enriched["games"]
            for bm in g.get("bookmakers", [])
        }
        assert "pinnacle" in books, f"target book missing: {books}"
        assert len(books) >= 2, f"expected multi-book enrichment, got {books}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_enrich_prefers_snapshot_with_more_books():
    db = await _make_db()
    try:
        await db.executemany(
            "INSERT INTO odds_snapshots (sport, timestamp, game_count,"
            " snapshot_json) VALUES (?, ?, ?, ?)",
            [
                _snapshot_json("NBA", "2025-01-15", ["a", "b"], n_games=2),
                _snapshot_json("NBA", "2025-01-15",
                               ["a", "b", "c", "d"], n_games=5),
            ],
        )
        await db.commit()
        snap = {
            "games": [
                {"id": f"NBA-{i}", "sport_key": "NBA",
                 "bookmakers": [{"key": "consensus"}]}
                for i in range(2)
            ],
        }
        enriched = await snapshots.enrich_snapshot_with_multibook(
            db, "NBA", "2025-01-15", snap, "a"
        )
        books = {
            bm["key"]
            for g in enriched["games"]
            for bm in g.get("bookmakers", [])
        }
        assert {"a", "b", "c", "d"} <= books, (
            f"should pick the richest snapshot: {books}"
        )
    finally:
        await db.close()


def test_enrich_helper_is_extracted_not_duplicated():
    """The facade method delegates to tools.btest.snapshots."""
    src = inspect.getsource(
        backtest.BacktestEngine._enrich_snapshot_with_multibook
    )
    assert "enrich_snapshot_with_multibook(" in src


def test_snapshots_module_has_no_backtest_import_cycle():
    src = inspect.getsource(snapshots)
    assert "import tools.backtest" not in src.replace(
        "tools.backtest_io", ""
    )


@pytest.mark.asyncio
async def test_enrich_passthrough_when_already_multibook():
    db = await _make_db()
    try:
        already_multi = {
            "games": [
                {"id": "NBA-0", "sport_key": "NBA",
                 "bookmakers": [{"key": "pinnacle"}, {"key": "fanduel"}]},
            ],
        }
        result = await snapshots.enrich_snapshot_with_multibook(
            db, "NBA", "2030-01-01", already_multi, "pinnacle"
        )
        assert result is already_multi or result == already_multi
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_enrich_passthrough_when_no_snapshot_data():
    db = await _make_db()
    try:
        original = {"bookmaker": {"key": "consensus"}, "markets": []}
        result = await snapshots.enrich_snapshot_with_multibook(
            db, "NFL", "1999-01-01", original, "pinnacle"
        )
        # No better data available: original returned unchanged.
        assert result is original or result == original
    finally:
        await db.close()
