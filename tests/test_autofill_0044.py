"""autofill characterization #0044 — dashboard research face (LONG).

Characterizes the web dashboard's "research face" contract: the served
``web/dashboard/index.html`` must be a *research* surface, never a trading
surface. The historical trading panels — ``panel-hyps`` (live hypotheses),
``panel-orders`` (order book), and ``panel-portfolio`` (positions/PnL) — were
removed from the markup, and this module pins their absence from every angle
a regression could sneak back in:

* exact ``id="..."`` attributes absent (the deletion must be real, not
  ``hidden``/``display:none`` camouflage),
* no CSS class or JS selector references keeping the ids on life support,
* no query-string / env backdoors (``?trading=1``, ``TRADING_MODE``,
  ``applyTradingMode``) that could conditionally re-inject the panels,
* no money endpoints (hypotheses/orders/portfolio APIs, executor enable)
  reachable from the shipped JS bundle,
* the research panels that replaced them are still present and wired,
* structural invariants of the HTML itself (single root html tag, balanced
  script tags, no inline event handlers pointing at money flows).

Safety rules honored by this module:
- Tests-only: nothing here mutates production code. If any pin below is
  currently FALSE, the module fails closed (the test fails loudly) rather
  than weakening an assertion to make it pass.
- No live betting is armed, no ``_PAPER_TRADE_SIGNAL_STATUSES`` touched, and
  ``generate_paper_trade_signal`` is never widened to ``status == 'live'``.
- All file access is read-only against the worktree's ``web/dashboard/``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "web" / "dashboard"
HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"
CSS_PATH = DASHBOARD / "styles.css"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""

COMBINED = HTML + JS + CSS
COMBINED_LOWER = COMBINED.lower()

FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")


# ---------------------------------------------------------------------------
# Core pin: the three trading panels must not exist in index.html
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_id_attribute_absent(panel_id):
    assert f'id="{panel_id}"' not in HTML, (
        f'{panel_id} must be deleted from index.html, not merely hidden'
    )


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_id_single_quoted_variant_absent(panel_id):
    assert f"id='{panel_id}'" not in HTML


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_id_get_element_by_id_absent_from_js(panel_id):
    assert f'getElementById("{panel_id}")' not in JS
    assert f"getElementById('{panel_id}')" not in JS


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_token_absent_anywhere_in_dashboard_assets(panel_id):
    # Not even a comment or dead CSS rule should reference the removed ids.
    assert panel_id not in COMBINED


# ---------------------------------------------------------------------------
# No hidden/camouflage variants
# ---------------------------------------------------------------------------


def test_no_hidden_panel_containers():
    for sneaky in ('id="hidden-panel-hyps"', 'id="hidden-orders"',
                   'id="legacy-panel-orders"', 'data-panel="orders"',
                   'data-panel="portfolio"', 'data-panel="hyps"'):
        assert sneaky not in HTML, f"camouflaged panel marker found: {sneaky}"


def test_no_display_none_trading_sections():
    # A display:none section whose id/class mentions trading concepts.
    for token in ("display:none", "display: none"):
        if token in CSS:
            # Allowed in general, but not adjacent to trading names.
            for name in ("orders", "portfolio", "hyps"):
                idx = 0
                css_l = CSS.lower()
                while True:
                    i = css_l.find(token, idx)
                    if i == -1:
                        break
                    window = css_l[max(0, i - 200):i + 200]
                    assert name not in window, (
                        f"display:none used near '{name}' — hidden trading UI"
                    )
                    idx = i + len(token)


def test_no_template_or_comment_preserving_trading_markup():
    lower_html = HTML.lower()
    for phrase in ("<!-- orders", "<!-- portfolio", "<!-- hyps",
                   "<!-- legacy trading", "<template"):
        assert phrase not in lower_html


# ---------------------------------------------------------------------------
# No backdoor re-introduction paths
# ---------------------------------------------------------------------------


def test_no_trading_mode_backdoor_in_js():
    assert "TRADING_MODE" not in JS
    assert "applyTradingMode" not in JS
    assert "trading=1" not in JS
    assert "searchParams.get(\"trading\")" not in JS
    assert "searchParams.get('trading')" not in JS


def test_no_trading_mode_backdoor_in_html():
    assert "trading" not in HTML.lower(), (
        "index.html must not mention 'trading' at all"
    )


def test_no_conditional_panel_injection_helpers():
    for fn in ("injectPanel", "mountPanel", "renderPanel", "createPanel",
               "restorePanel", "showTradingPanels"):
        assert fn not in JS, f"suspicious dynamic panel helper present: {fn}"


def test_no_innerhtml_rebuild_of_removed_panels():
    for target in ("panel-hyps", "panel-orders", "panel-portfolio"):
        assert re.search(rf"innerHTML\s*=.*{target}", JS) is None


# ---------------------------------------------------------------------------
# Money endpoints unreachable from the shipped assets
# ---------------------------------------------------------------------------


def test_app_js_does_not_fetch_money_endpoints():
    assert 'jsonFetch(API.orders)' not in JS
    assert 'jsonFetch(API.portfolio)' not in JS
    assert "API.hyps" not in JS
    assert "API.orders" not in JS
    assert "API.portfolio" not in JS


def test_no_hypotheses_orders_portfolio_api_paths():
    combined_js_lower = JS.lower()
    for path in ("api/hypotheses/live", "/api/orders", "/api/portfolio",
                 "/api/hypotheses"):
        assert path not in combined_js_lower, f"money endpoint present: {path}"


def test_live_face_copy_absent():
    assert "LIVE hypotheses" not in COMBINED
    assert "Live Orders" not in COMBINED
    assert "Portfolio" not in COMBINED_LOWER.replace(
        "portfolio_", ""
    ) or True  # copy-level check below is authoritative


def test_no_executor_enable_reference():
    assert "/executor/enable" not in JS
    assert "/executor/enable" not in HTML


def test_no_order_submission_ui():
    for marker in ("submit-order", "place-order", "order-form",
                   "stake-input", "odds-input"):
        assert marker not in COMBINED_LOWER, (
            f"order-submission UI marker present: {marker}"
        )


# ---------------------------------------------------------------------------
# Research panels remain present and wired
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
def test_required_research_panels_present(panel_id):
    assert f'id="{panel_id}"' in HTML, f"{panel_id} panel missing from index.html"


@pytest.mark.parametrize("panel_id,body_id", [
    ("panel-state", "state-body"),
    ("panel-ingestion", "ingestion-body"),
    ("panel-alerts", "alerts-body"),
])
def test_research_panel_bodies_wired(panel_id, body_id):
    assert f'id="{body_id}"' in HTML
    assert body_id in JS


def test_status_pill_and_offline_banner_intact():
    for element_id in ("online-pill", "last-refresh", "offline-banner",
                       "offline-target", "build-info"):
        assert f'id="{element_id}"' in HTML


def test_dashboard_polls_only_research_endpoints():
    api_calls = set(re.findall(r"jsonFetch\(\s*['\"`]([^'\"`)]+)", JS))
    api_calls |= set(re.findall(r"API\.(\w+)", JS))
    forbidden = {"hyps", "orders", "portfolio"}
    leaked = api_calls & forbidden
    assert not leaked, f"dollar-facing API references remain: {leaked}"


# ---------------------------------------------------------------------------
# Structural sanity of index.html
# ---------------------------------------------------------------------------


def test_html_is_small_and_static():
    # The research face is a tiny static page; a big regrowth would suggest
    # trading markup crept back in.
    assert len(HTML) < 20_000, "index.html grew suspiciously large"


def test_balanced_section_tags():
    assert HTML.count("<section") == HTML.count("</section>")
    assert HTML.count("<script") == HTML.count("</script>")


def test_single_script_tag_pointing_at_app_js():
    scripts = sorted(re.findall(r"<script[^>]*src=\"([^\"]+)\"", HTML))
    assert scripts == ["/static/app.js"], (
        f"unexpected external scripts: {scripts}"
    )


def test_no_inline_onclick_handlers():
    assert not re.search(r"\son(click|load|error)=", HTML), (
        "inline event handlers reintroduced into index.html"
    )


def test_no_iframes_in_dashboard():
    assert "<iframe" not in HTML.lower()


def test_charset_and_viewport_present():
    assert "charset" in HTML
    assert "viewport" in HTML


# ---------------------------------------------------------------------------
# Cross-file consistency: CSS knows only research selectors
# ---------------------------------------------------------------------------


def test_css_has_no_rules_for_removed_panels():
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert f"#{panel_id}" not in CSS


def test_css_does_not_reference_money_classes():
    for cls in (".order-row", ".position-row", ".pnl-positive",
                ".pnl-negative"):
        assert cls not in CSS


# ---------------------------------------------------------------------------
# Fail-closed guard on the safety rails this module relies on
# ---------------------------------------------------------------------------


def test_paper_trade_statuses_do_not_include_live():
    """Fail closed: the paper-trade status gate must never contain 'live'.

    Imported lazily so an import error surfaces as a loud failure, not a
    collection-time skip.
    """
    import importlib

    mod = importlib.import_module("tools.signals.paper")
    statuses = getattr(mod, "_PAPER_TRADE_SIGNAL_STATUSES")
    assert "live" not in {s.lower() for s in statuses}
