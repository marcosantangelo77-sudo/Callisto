"""Pin: _phase_live_execute is a no-op unless CALLISTO_ALLOW_LIVE_EXECUTE=1.

Does NOT import tools.autonomous — that module pulls the rest of the
research loop and hangs this environment. The gate is pinned by AST so
the first executable statements of the method refuse to run unless armed.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "tools" / "autonomous.py"


def _phase_fn() -> ast.AsyncFunctionDef:
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ResearchLoop":
            for item in node.body:
                if (
                    isinstance(item, ast.AsyncFunctionDef)
                    and item.name == "_phase_live_execute"
                ):
                    return item
    raise AssertionError("ResearchLoop._phase_live_execute not found")


def _skip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        return body[1:]
    return body


def test_live_execute_gate_is_first_control_flow():
    """The env gate must run before drawdown / odds / order_manager / executor."""
    body = _skip_docstring(_phase_fn().body)
    # Allow a leading `import os as _os`.
    stmts = body
    if stmts and isinstance(stmts[0], ast.Import):
        stmts = stmts[1:]
    assert stmts, "method body empty after docstring/import"
    gate = stmts[0]
    assert isinstance(gate, ast.If), (
        f"first statement after docstring must be the env If, got {type(gate).__name__}"
    )
    dump = ast.dump(gate.test)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in dump
    # Compare against literal "1" (the only arming value).
    assert "1" in dump
    # Body of the if must return (skip the rest of the phase).
    assert any(isinstance(s, ast.Return) for s in gate.body)


def test_source_does_not_treat_other_truthy_as_armed():
    src = ast.get_source_segment(SRC.read_text(encoding="utf-8"), _phase_fn())
    assert src is not None
    assert 'getenv("CALLISTO_ALLOW_LIVE_EXECUTE") != "1"' in src
    assert "generate_paper_trade_signal" not in src.split("CALLISTO_ALLOW_LIVE_EXECUTE", 1)[0]
