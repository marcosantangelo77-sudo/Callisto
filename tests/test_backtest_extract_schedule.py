"""Slice 2: game-date helper extracted to tools.signals.schedule."""

from __future__ import annotations

import ast
from pathlib import Path

BACKTEST = Path("tools/backtest.py").read_text()
SCHEDULE = Path("tools/signals/schedule.py").read_text()
INIT = Path("tools/signals/__init__.py").read_text()


def _tree(src: str) -> ast.Module:
    return ast.parse(src)


def test_nested_def_gone_from_backtest() -> None:
    """The nested helper body (canonical logic) must not live in backtest.py."""
    tree = _tree(BACKTEST)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name != "_game_date_from_commence" or len(node.body) <= 3, (
                "generate_paper_trade_signal must not re-own the date logic; "
                "only a thin wrapper is allowed"
            )
    # The docstring-bearing implementation must be gone.
    assert "Venue-local game date for this game." not in BACKTEST
    assert "from tools.game_dates import local_game_date" not in BACKTEST


def test_schedule_module_owns_helper() -> None:
    tree = _tree(SCHEDULE)
    names = [
        n.name
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "game_date_from_commence"
    ]
    assert names == ["game_date_from_commence"]
    # fail-closed fallback chain present: venue-local first, then UTC slice.
    assert "local_game_date" in SCHEDULE
    assert "ct[:10]" in SCHEDULE


def test_backtest_imports_and_calls_canonical_helper() -> None:
    assert (
        "from tools.signals.schedule import game_date_from_commence" in BACKTEST
    )
    assert "game_date_from_commence(game_obj" in BACKTEST


def test_init_exports_helper() -> None:
    assert "game_date_from_commence" in INIT


def test_fallback_behavior() -> None:
    """Direct behavior check of the extracted helper's fail-closed fallbacks."""
    import sys

    sys.path.insert(0, str(Path.cwd()))
    from tools.signals.schedule import game_date_from_commence

    today = "2026-08-26"
    # missing commence_time -> venue fallback impossible -> today
    assert game_date_from_commence({}, sport="mlb", today=today) == today
    # unparseable timestamp -> UTC [:10] slice
    g = {"commence_time": "garbage-but-long-string", "home_team": "", "sport_key": ""}
    assert game_date_from_commence(g, sport="mlb", today=today) == "garbage-bu"
    # short garbage -> today
    assert game_date_from_commence({"commence_time": "x"}, today=today) == today


def test_paper_gate_unchanged() -> None:
    src = Path("tools/signals/paper.py").read_text()
    assert 'frozenset({"paper_trading"})' in src
    gate_line = [l for l in src.splitlines() if "frozenset(" in l and "=" in l][0]
    assert '"live"' not in gate_line
