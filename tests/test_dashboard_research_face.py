"""Source contract: the dashboard's default face is research, not LIVE betting.

Reads web/dashboard/index.html and web/dashboard/app.js as text (no browser).
"""

from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "web" / "dashboard"
HTML = (DASHBOARD / "index.html").read_text(encoding="utf-8")
JS = (DASHBOARD / "app.js").read_text(encoding="utf-8")


def test_title_and_brand_are_research_not_ops_dashboard():
    combined = (HTML + JS).lower()
    assert "ops dashboard" not in combined


def test_trading_panels_hidden_by_default_in_html():
    for panel_id in ("panel-hyps", "panel-orders", "panel-portfolio"):
        # Find the section tag carrying this id and require the hidden attr.
        import re

        m = re.search(rf"<section[^>]*id=\"{panel_id}\"[^>]*>", HTML)
        assert m, f"{panel_id} section not found in index.html"
        assert "hidden" in m.group(0), f"{panel_id} is not hidden in HTML"


def test_app_js_only_unhides_when_trading_eq_1():
    assert 'get("trading") === "1"' in JS
    assert "applyTradingMode" in JS
    # Unhide must be gated on TRADING_MODE.
    unhide_idx = JS.index("el.hidden = false")
    gate = JS[:unhide_idx]
    assert "TRADING_MODE" in gate.split("function applyTradingMode")[-1]


def test_app_js_skips_money_polls_when_hidden():
    # Each money endpoint fetch must be conditional on TRADING_MODE.
    for endpoint in ("API.hyps", "API.orders", "API.portfolio"):
        assert f"TRADING_MODE ? jsonFetch({endpoint})" in JS


def test_no_executor_enable_reference():
    assert "/executor/enable" not in JS
    assert "/executor/enable" not in HTML
