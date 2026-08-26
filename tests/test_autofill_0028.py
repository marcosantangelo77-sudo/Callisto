"""Autofill characterization #0028 — dashboard research face (LONG).

Characterizes the structural contract of ``web/dashboard/index.html``:
the default face of the Callisto dashboard must be a *research appliance*
(loop health, data ingestion, alerts) and must NOT contain the trading /
betting panels (panel-hyps, panel-orders, panel-portfolio).

These tests read files as text — no browser, no network, no server startup.
They are pure characterization: if any assertion fails, that means someone
reintroduced live-betting markup into the dashboard HTML, which is a
fail-closed condition for this task. Nothing in this module arms or enables
live betting; it only asserts the trading panels stay deleted.

Safety posture:
- never adds "live" to _PAPER_TRADE_SIGNAL_STATUSES
- never widens generate_paper_trade_signal to status == 'live'
- production gates are observed, not weakened
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "web" / "dashboard"
HTML_PATH = DASHBOARD_DIR / "index.html"

# The three trading panels that were removed as part of the research-face work.
FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research panels that must remain present.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Other identifiers / endpoints associated with the removed trading face.
FORBIDDEN_TRADING_TOKENS = (
    "hypotheses/live",
    "api/hypotheses",
    "api/orders",
    "api/portfolio",
    "/executor/enable",
    "TRADING_MODE",
    "applyTradingMode",
    "trading=1",
)


def _read_html() -> str:
    return HTML_PATH.read_text(encoding="utf-8")


def _html_lower() -> str:
    return _read_html().lower()


# ---------------------------------------------------------------------------
# Core contract: forbidden panels absent
# ---------------------------------------------------------------------------


class TestForbiddenPanelsAbsent:
    """panel-hyps / panel-orders / panel-portfolio must not exist at all."""

    def test_no_panel_hyps_element(self):
        assert 'id="panel-hyps"' not in _read_html()

    def test_no_panel_orders_element(self):
        assert 'id="panel-orders"' not in _read_html()

    def test_no_panel_portfolio_element(self):
        assert 'id="panel-portfolio"' not in _read_html()

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_panel_id_absent_anywhere_in_markup(self, panel_id: str):
        html = _read_html()
        assert f'id="{panel_id}"' not in html, (
            f"{panel_id} must be deleted from index.html, not merely hidden"
        )

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_panel_id_not_even_referenced_as_string(self, panel_id: str):
        # No JS hooks, comments, or data attributes referencing the old ids.
        assert panel_id not in _read_html()

    def test_no_hidden_trading_sections_via_css_class(self):
        html = _read_html()
        for panel_id in FORBIDDEN_PANEL_IDS:
            pattern = re.compile(
                r"<section[^>]*id=[\"']" + re.escape(panel_id) + r"[\"']"
            )
            assert not pattern.search(html)

    def test_no_commented_out_trading_panels(self):
        # Deleted means deleted: no <!-- <section id="panel-orders"> ... --> husks.
        html = _read_html()
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in html.lower()


# ---------------------------------------------------------------------------
# Required research panels still present
# ---------------------------------------------------------------------------


class TestResearchPanelsPresent:
    @pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
    def test_panel_present_with_section_tag(self, panel_id: str):
        html = _read_html()
        pattern = re.compile(
            r"<section[^>]*id=[\"']" + re.escape(panel_id) + r"[\"']"
        )
        assert pattern.search(html), f"{panel_id} section missing from index.html"

    @pytest.mark.parametrize("panel_id,body_id", [
        ("panel-state", "state-body"),
        ("panel-ingestion", "ingestion-body"),
        ("panel-alerts", "alerts-body"),
    ])
    def test_panel_has_body_div(self, panel_id: str, body_id: str):
        html = _read_html()
        assert f'id="{body_id}"' in html, f"{panel_id} lost its body div {body_id}"

    def test_research_face_headings(self):
        html = _html_lower()
        assert "loop health" in html
        assert "data ingestion" in html
        assert "alerts" in html

    def test_exactly_three_panels_defined(self):
        html = _read_html()
        ids = re.findall(r'<section[^>]*id=["\']([^"\']+)["\']', html)
        assert sorted(ids) == sorted(REQUIRED_PANEL_IDS)


# ---------------------------------------------------------------------------
# Document shape / structure
# ---------------------------------------------------------------------------


class TestDocumentShape:
    def test_file_exists_and_nonempty(self):
        assert HTML_PATH.exists()
        assert len(_read_html().strip()) > 0

    def test_is_html_document(self):
        html = _read_html()
        assert html.lstrip().lower().startswith("<!doctype html>")

    def test_title_is_research_appliance(self):
        html = _read_html()
        m = re.search(r"<title>(.*?)</title>", html, re.DOTALL)
        assert m is not None
        assert "research" in m.group(1).lower()

    def test_no_ops_dashboard_wording(self):
        assert "ops dashboard" not in _html_lower()

    def test_read_only_footer_claim(self):
        assert "read-only" in _html_lower() or "read only" in _html_lower()

    def test_script_reference_points_at_app_js(self):
        assert '/static/app.js' in _read_html()

    def test_stylesheet_reference_present(self):
        assert "/static/styles.css" in _read_html()

    def test_charset_declared(self):
        assert 'charset="utf-8"' in _read_html().lower()

    def test_offline_banner_kept(self):
        # The DB-fallback banner is part of the research face.
        html = _read_html()
        assert 'id="offline-banner"' in html

    def test_online_pill_kept(self):
        assert 'id="online-pill"' in _read_html()

    def test_wellformed_balanced_sections(self):
        html = _read_html()
        assert html.count("<section") == html.count("</section>")
        assert html.count("<div") == html.count("</div>")


# ---------------------------------------------------------------------------
# No trading tokens / endpoints reintroduced
# ---------------------------------------------------------------------------


class TestNoTradingTokens:
    @pytest.mark.parametrize("token", FORBIDDEN_TRADING_TOKENS)
    def test_token_absent_from_html(self, token: str):
        assert token not in _read_html(), (
            f"trading token {token!r} found in index.html — research face violated"
        )

    def test_no_querystring_trading_backdoor(self):
        html = _html_lower()
        assert "trading=1" not in html
        assert "?trading" not in html

    def test_no_live_betting_wording(self):
        html = _read_html()
        assert "LIVE hypotheses" not in html
        # "not live betting" (negation in a comment) is fine; bare advocacy isn't.
        assert not re.search(r"(?<!not )live betting", html.lower())

    def test_no_executor_endpoints(self):
        html = _html_lower()
        for ep in ("/executor/enable", "executor/disable", "executor/status"):
            assert ep not in html


# ---------------------------------------------------------------------------
# Companion dashboard assets keep the same face (characterization)
# ---------------------------------------------------------------------------


class TestCompanionAssetsConsistent:
    def test_app_js_exists(self):
        assert (DASHBOARD_DIR / "app.js").exists()

    def test_app_js_does_not_poll_forbidden_panels(self):
        js = (DASHBOARD_DIR / "app.js").read_text(encoding="utf-8")
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in js

    def test_styles_css_exists(self):
        assert (DASHBOARD_DIR / "styles.css").exists()

    def test_dashboard_dir_contains_only_expected_assets(self):
        names = {p.name for p in DASHBOARD_DIR.iterdir()}
        assert names <= {"index.html", "app.js", "styles.css"}
