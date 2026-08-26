"""Source contract: tools/dashboard.py's user-facing copy is research, not ops."""

from pathlib import Path

MODULE = Path(__file__).resolve().parent.parent / "tools" / "dashboard.py"
SOURCE = MODULE.read_text(encoding="utf-8")


def test_no_ops_dashboard_language_in_module():
    assert "ops dashboard" not in SOURCE.lower()


def test_module_positions_itself_as_research_appliance():
    lowered = SOURCE.lower()
    assert "research appliance" in lowered
    assert "loop health" in lowered


def test_live_hypotheses_route_kept():
    assert '@app.get("/api/hypotheses/live")' in SOURCE


def test_app_title_is_research_not_ops():
    assert 'title="Callisto Research Dashboard"' in SOURCE
