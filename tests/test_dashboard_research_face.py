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


def test_trading_panels_absent_from_html():
    for panel_id in ("panel-hyps", "panel-orders", "panel-portfolio"):
        assert f'id="{panel_id}"' not in HTML, (
            f"{panel_id} must be deleted from index.html, not merely hidden"
        )


def test_no_live_hypotheses_face_or_poll():
    combined = HTML + JS
    assert "LIVE hypotheses" not in combined
    assert "api/hypotheses/live" not in JS


def test_app_js_does_not_fetch_money_endpoints():
    assert 'jsonFetch(API.orders)' not in JS
    assert 'jsonFetch(API.portfolio)' not in JS
    assert "API.hyps" not in JS
    assert "API.orders" not in JS
    assert "API.portfolio" not in JS
    # No ?trading=1 backdoor reintroducing trading markup.
    assert "TRADING_MODE" not in JS
    assert "applyTradingMode" not in JS


def test_no_executor_enable_reference():
    assert "/executor/enable" not in JS
    assert "/executor/enable" not in HTML


def test_research_panels_still_present():
    for panel_id in ("panel-state", "panel-ingestion", "panel-alerts"):
        assert f'id="{panel_id}"' in HTML, f"{panel_id} panel missing from index.html"
