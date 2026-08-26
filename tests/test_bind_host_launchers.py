"""Launchers must bind the API to loopback unless CALLISTO_BIND_HOST overrides."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
START_BAT = PROJECT_ROOT / "start.bat"
OVERNIGHT = PROJECT_ROOT / "scripts" / "overnight_setup.py"


def test_start_bat_no_wildcard_bind():
    text = START_BAT.read_text(encoding="utf-8", errors="replace")
    assert "0.0.0.0" not in text
    assert "CALLISTO_BIND_HOST" in text
    assert "127.0.0.1" in text
    assert "--host %CALLISTO_BIND_HOST%" in text


def test_overnight_setup_no_wildcard_bind():
    src = OVERNIGHT.read_text(encoding="utf-8")
    assert "0.0.0.0" not in src
    assert "CALLISTO_BIND_HOST" in src
    assert 'os.environ.get("CALLISTO_BIND_HOST", "127.0.0.1")' in src


def test_overnight_setup_host_expression_evaluates_to_loopback():
    import ast

    tree = ast.parse(OVERNIGHT.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "environ"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "CALLISTO_BIND_HOST"
        ):
            found.append(ast.literal_eval(node.args[1]))
    assert found and all(h == "127.0.0.1" for h in found)
