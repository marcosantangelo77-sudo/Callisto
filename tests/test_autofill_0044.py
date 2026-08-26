"""Autofill characterization #0044 — dashboard research face (LONG).

Pins the source contract that ``web/dashboard/index.html`` is a RESEARCH
dashboard, not a trading/LIVE-ops surface:

* The three legacy trading panels — ``panel-hyps``, ``panel-orders``,
  and ``panel-portfolio`` — must be entirely ABSENT from index.html.
  Absence is checked as raw text (attribute forms, JS string fragments,
  CSS selectors) so a "merely hidden" panel or a reintroduction via a
  template/JS injection path fails loudly.
* The research panels that replaced them must still be present.
* No LIVE-betting vocabulary may reappear in the default face.

These tests are pure characterization over the static files: no browser,
no network, no production code changes, no live betting surface armed.
A final section fail-closes on the paper-trade status gate to guarantee
this module can never be the thing that widens it.
"""

import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD_DIR = os.path.join(REPO_ROOT, "web", "dashboard")
HTML_PATH = os.path.join(DASHBOARD_DIR, "index.html")
JS_PATH = os.path.join(DASHBOARD_DIR, "app.js")
CSS_PATH = os.path.join(DASHBOARD_DIR, "styles.css")

with open(HTML_PATH, encoding="utf-8") as _f:
    HTML = _f.read()
with open(JS_PATH, encoding="utf-8") as _f:
    JS = _f.read()

FORBIDDEN_PANELS = ("panel-hyps", "panel-orders", "panel-portfolio")
REQUIRED_PANELS = ("panel-state", "panel-ingestion", "panel-alerts")


# ---------------------------------------------------------------------------
# Core contract: forbidden panels absent from index.html
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("panel_id", FORBIDDEN_PANELS)
def test_panel_id_attribute_absent_from_html(panel_id):
    assert f'id="{panel_id}"' not in HTML, (
        f'{panel_id} must be deleted from index.html, not merely hidden'
    )


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANELS)
def test_panel_token_fully_absent_from_html(panel_id):
    # Not just the attribute form — the bare token must not appear anywhere
    # in the markup (comments, data attributes, class names, scripts).
    assert panel_id not in HTML


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANELS)
def test_no_href_or_anchor_to_forbidden_panel(panel_id):
    assert f'href="#{panel_id}"' not in HTML
    assert f'scrollTo("{panel_id}"' not in HTML


def test_no_conditional_trading_block_in_html():
    # A reintroduction often hides behind a conditional/template marker.
    for needle in (
        "{% if",
        "{{#if",
        "v-if=",
        "x-show=",
        "data-trading",
        "trading=1",
        "?trading",
    ):
        assert needle not in HTML, f"conditional trading marker {needle!r} found"


def test_html_has_no_inline_script_reintroducing_panels():
    inline_scripts = re.findall(r"<script[^>]*>(.*?)</script>", HTML, re.S)
    for body in inline_scripts:
        for panel_id in FORBIDDEN_PANELS:
            assert panel_id not in body


def test_html_contains_no_hidden_style_panels():
    # Any <section> carrying display:none / hidden would be a smuggled panel.
    for m in re.finditer(r"<section\b[^>]*>", HTML):
        tag = m.group(0)
        assert "display:none" not in tag.replace(" ", "")
        assert 'style="display: none"' not in tag


def test_no_section_create_injection_of_forbidden_panels():
    # Generic section-creation could smuggle trading panels back in.
    for pattern in (
        "createElement('section')",
        'createElement("section")',
        "insertAdjacentHTML",
    ):
        assert pattern not in JS, (
            f"JS DOM-injection pattern {pattern!r} could reintroduce panels"
        )
    # innerHTML templates must never emit a forbidden panel id.
    for panel_id in FORBIDDEN_PANELS:
        assert panel_id not in JS


# ---------------------------------------------------------------------------
# Research face intact
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("panel_id", REQUIRED_PANELS)
def test_required_research_panel_present(panel_id):
    assert f'id="{panel_id}"' in HTML


def test_research_panels_are_sections_with_panel_class():
    for panel_id in REQUIRED_PANELS:
        m = re.search(rf'<section\b[^>]*id="{panel_id}"[^>]*>', HTML)
        assert m, f"{panel_id} is not a <section>"
        assert "panel" in m.group(0)


def test_offline_banner_present():
    assert 'id="offline-banner"' in HTML
    assert 'class="banner' in HTML


def test_build_info_present():
    assert 'id="build-info"' in HTML


def test_online_pill_defaults_muted():
    m = re.search(r'<span id="online-pill"[^>]*>', HTML)
    assert m, "online-pill missing"
    assert "pill-muted" in m.group(0)


# ---------------------------------------------------------------------------
# No LIVE betting face anywhere in the dashboard sources
# ---------------------------------------------------------------------------

def test_no_live_hypotheses_label():
    assert "LIVE hypotheses" not in HTML + JS


def test_app_js_never_polls_live_hypotheses_endpoint():
    assert "api/hypotheses/live" not in JS


def test_app_js_has_no_money_endpoints():
    for key in ("API.hyps", "API.orders", "API.portfolio"):
        assert key not in JS


def test_app_js_never_fetches_orders_or_portfolio():
    for call in ('jsonFetch(API.orders)', 'jsonFetch(API.portfolio)', 'jsonFetch(API.hyps)'):
        assert call not in JS


def test_no_executor_enable_reference():
    for src in (HTML, JS):
        assert "/executor/enable" not in src


def test_no_trading_mode_backdoor():
    for token in ("TRADING_MODE", "applyTradingMode", "tradingMode"):
        assert token not in JS


def test_no_arm_live_vocabulary_in_dashboard():
    combined = (HTML + "\n" + JS).lower()
    for phrase in ("arm live", "place bet", "submit order"):
        assert phrase not in combined


# ---------------------------------------------------------------------------
# Fail-closed gate pins: this module must never widen the paper-trade gate
# ---------------------------------------------------------------------------

def test_paper_trade_statuses_is_frozenset_paper_trading_only():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
    lowered = {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}
    assert "live" not in lowered
    assert _PAPER_TRADE_SIGNAL_STATUSES <= {"paper_trading"}


def test_generate_paper_trade_signal_gates_via_reject_non_paper():
    import ast

    with open(os.path.join(REPO_ROOT, "tools", "backtest.py"), encoding="utf-8") as f:
        tree = ast.parse(f.read())
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef,)) and node.name == "generate_paper_trade_signal":
            fn = node
            break
    assert fn is not None
    names = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
    assert "reject_non_paper" in names, (
        "generate_paper_trade_signal must fail closed through reject_non_paper"
    )
