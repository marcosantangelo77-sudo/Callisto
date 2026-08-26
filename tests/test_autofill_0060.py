"""autofill characterization #0060 — dashboard research face (LONG).

Characterizes the static content contract of ``web/dashboard/index.html``
(the dashboard's default "face") as a research appliance rather than an
operations / live-betting console.

Contract under characterization
-------------------------------
1. ``index.html`` must NOT contain the trading panel mount points
   ``panel-hyps``, ``panel-orders``, ``panel-portfolio`` — not as element
   ids, not in comments, not inside inline JS, not even URL-encoded or
   split across attribute boundaries that a naive reader might miss.
2. The panels must be *deleted*, not merely hidden: no
   ``style="display:none"``, no ``hidden`` class on a trading-panel
   section, no ``?trading=1`` style query-string backdoor markup.
3. The research face remains intact: ``panel-state``,
   ``panel-ingestion`` and ``panel-alerts`` are still mounted, the page
   is branded "research", and the footer advertises read-only refresh.
4. The companion ``app.js`` does not fetch money endpoints (orders,
   portfolio, hypotheses) and contains no executor-enable reference.
5. Fail-closed safety pins (read-only characterization — nothing here
   arms anything):
   - ``tools.signals.paper._PAPER_TRADE_SIGNAL_STATUSES`` stays exactly
     ``{"paper_trading"}`` — never widened to include ``"live"``.
   - ``generate_paper_trade_signal`` refuses status ``"live"``.

Safety rules honored by this module:
- Tests only READ files and import pure helpers. No browser, no network,
  no executor construction, no betting of any kind is armed.
- If any fail-closed pin were false, these tests FAIL CLOSED loudly
  (assertion error), they never adapt to a widened surface.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "web" / "dashboard"
HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")
COMBINED = HTML + JS
LOW = COMBINED.lower()

BANNED_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

MONEY_API_KEYS = ("hyps", "orders", "portfolio")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _element_ids(html: str) -> list[str]:
    """Every id="..." value appearing anywhere in the document."""
    return re.findall(r'id\s*=\s*["\']([^"\']+)["\']', html)


def _sections_with_ids(html: str) -> dict[str, str]:
    """Map section id -> full section tag text."""
    out = {}
    for m in re.finditer(r"<section\b[^>]*>", html):
        tag = m.group(0)
        idm = re.search(r'id\s*=\s*["\']([^"\']+)["\']', tag)
        if idm:
            out[idm.group(1)] = tag
    return out


def _data_urls(js: str) -> list[str]:
    return re.findall(r'["\']((?:api/|/api/)[^"\']*)["\']', js)


# ---------------------------------------------------------------------------
# 1. Banned trading panel ids are absent from index.html
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", BANNED_PANEL_IDS)
def test_banned_panel_id_absent_as_element_id(panel_id):
    assert f'id="{panel_id}"' not in HTML
    assert f"id='{panel_id}'" not in HTML


@pytest.mark.parametrize("panel_id", BANNED_PANEL_IDS)
def test_banned_panel_id_absent_entirely_from_html(panel_id):
    # Not even in comments or strings: the token itself must be gone.
    assert panel_id not in HTML


@pytest.mark.parametrize("panel_id", BANNED_PANEL_IDS)
def test_banned_panel_id_absent_from_js(panel_id):
    assert panel_id not in JS


def test_no_getElementById_targets_a_banned_panel():
    targets = re.findall(r'getElementById\(\s*["\']([^"\']+)["\']\s*\)', JS)
    for t in targets:
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id not in t


def test_html_has_no_hidden_or_display_none_sections():
    for sid, tag in _sections_with_ids(HTML).items():
        assert "display:none" not in tag.replace(" ", ""), (
            f"section #{sid} hides via display:none — delete instead"
        )
        assert "display: none" not in tag


def test_no_query_string_backdoor_markup_in_html():
    # No <script> blocks at all in index.html: backdoors would live in app.js.
    assert "<script" not in HTML.replace(
        '<script src="/static/app.js"></script>', ""
    )


def test_all_section_ids_are_known_research_panels():
    ids = [i for i in _element_ids(HTML)]
    sections = {i for i in ids if i.startswith("panel-")}
    assert set(BANNED_PANEL_IDS).isdisjoint(sections)
    assert sections == set(REQUIRED_PANEL_IDS)


# ---------------------------------------------------------------------------
# 2. Research face remains present and branded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
def test_required_panel_present_as_real_section(panel_id):
    assert f'id="{panel_id}"' in HTML
    tag = _sections_with_ids(HTML).get(panel_id)
    assert tag is not None, f"#{panel_id} exists but is not a <section>"
    assert 'class="' in tag and "panel" in tag


def test_title_is_research_appliance():
    m = re.search(r"<title>(.*?)</title>", HTML, re.S)
    assert m, "no <title>"
    title = m.group(1).lower()
    assert "callisto" in title
    assert "research" in title


def test_brand_sub_labels_research():
    assert "research appliance" in LOW


def test_footer_declares_read_only_refresh():
    assert "read-only" in LOW
    assert "auto-refresh" in LOW


def test_offline_banner_present_for_api_fallback():
    assert 'id="offline-banner"' in HTML


def test_stylesheet_and_script_mounted_via_static():
    assert '/static/styles.css' in HTML
    assert '/static/app.js' in HTML


def test_viewport_meta_present():
    assert 'name="viewport"' in HTML


def test_doctype_is_html5():
    assert HTML.lstrip().lower().startswith("<!doctype html>")


# ---------------------------------------------------------------------------
# 3. app.js fetches only research endpoints
# ---------------------------------------------------------------------------


def test_api_map_contains_only_research_keys():
    declared = set(re.findall(r'^\s{2}(\w+):\s*"', JS, re.M))
    assert declared == {"status", "ingestion", "alerts"}


@pytest.mark.parametrize("key", MONEY_API_KEYS)
def test_api_map_has_no_money_key(key):
    assert not re.search(rf'\b{key}\s*:', JS)


@pytest.mark.parametrize("endpoint", ["orders", "portfolio", "api/hypoth"])
def test_no_money_endpoint_strings_in_js(endpoint):
    assert endpoint not in JS.lower()


def test_jsonfetch_never_called_with_money_endpoints():
    for call in re.findall(r"jsonFetch\(([^)]*)\)", JS):
        for key in MONEY_API_KEYS:
            assert f"API.{key}" not in call


def test_data_urls_in_js_are_research_only():
    urls = _data_urls(JS)
    assert urls, "expected at least one api/ url literal"
    for u in urls:
        low = u.lower()
        for bad in ("order", "portfolio", "hypoth", "executor", "bet"):
            assert bad not in low, f"suspicious endpoint literal: {u}"


def test_no_executor_enable_reference_anywhere():
    assert "/executor/enable" not in COMBINED


def test_no_trading_mode_backdoor_in_js():
    assert "TRADING_MODE" not in JS
    assert "applyTradingMode" not in JS
    assert "trading=1" not in COMBINED


def test_no_live_hypotheses_face():
    assert "LIVE hypotheses" not in COMBINED
    assert "api/hypotheses/live" not in COMBINED


def test_ops_dashboard_branding_absent():
    assert "ops dashboard" not in LOW


# ---------------------------------------------------------------------------
# 4. Structural sanity of the research face
# ---------------------------------------------------------------------------


def test_three_panels_inside_main_grid():
    main = re.search(r"<main\b[^>]*>(.*?)</main>", HTML, re.S)
    assert main, "no <main> grid"
    body = main.group(1)
    for pid in REQUIRED_PANEL_IDS:
        assert f'id="{pid}"' in body


def test_every_panel_has_heading_and_body():
    html = HTML
    for pid in REQUIRED_PANEL_IDS:
        idx = html.index(f'id="{pid}"')
        chunk = html[idx : idx + 400]
        assert "<h2>" in chunk, f"{pid} missing heading"
        assert 'class="panel-body"' in chunk, f"{pid} missing panel-body"


def test_poll_interval_is_15s_read_only_cadence():
    assert "REFRESH_MS = 15000" in JS


def test_setInterval_only_wires_the_single_refresh_loop():
    assert len(re.findall(r"setInterval\(", JS)) == 1


def test_renderers_exist_for_exactly_three_panels():
    for fn in ("renderState", "renderIngestion", "renderAlerts"):
        assert re.search(rf"function {fn}\(", JS)


def test_no_renderer_for_banned_panels():
    for fn in ("renderHyps", "renderOrders", "renderPortfolio"):
        assert fn not in JS


def test_pill_helper_sanitizes_color_class():
    m = re.search(r"function pill\(.*?\n\}", JS, re.S)
    assert m, "pill() helper missing"
    src = m.group(0)
    assert '["green", "yellow", "red", "muted"].includes(c)' in src


def test_escapeHtml_defined_and_used():
    assert re.search(r"function escapeHtml\(", JS)
    # used by every renderer
    for fn in ("renderState", "renderIngestion", "renderAlerts"):
        block = re.search(rf"function {fn}\b.*?\n\}}", JS, re.S)
        assert block, f"{fn} missing"
        assert "escapeHtml(" in block.group(0)


def test_html_file_is_small_static_document():
    # A research appliance face should stay lean; guard against the old
    # trading markup creeping back in bulk.
    assert len(HTML.splitlines()) < 120


# ---------------------------------------------------------------------------
# 5. Fail-closed safety pins (never arm live betting)
# ---------------------------------------------------------------------------


def test_paper_trade_signal_statuses_pin_exact():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"}), (
        "FAIL CLOSED: paper-trade statuses widened beyond paper_trading"
    )


def test_paper_trade_statuses_never_contain_live():
    from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

    assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES


def test_generate_paper_trade_signal_refuses_live_status():
    # The gate lives on BacktestEngine.generate_paper_trade_signal and is
    # enforced by tools.signals.paper.reject_non_paper. We pin the refusal
    # logic directly (no engine construction, no network, no betting).
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("live") is True
    assert reject_non_paper("anything_else") is True
    assert reject_non_paper("paper_trading") is False


def test_backtest_engine_source_gates_via_reject_non_paper():
    import inspect

    from tools.backtest import BacktestEngine

    src = inspect.getsource(BacktestEngine.generate_paper_trade_signal)
    assert "reject_non_paper" in src
    # Slice-4 moved the odds body into tools.btest.paper_pipeline. The
    # facade must still reject non-paper statuses BEFORE that delegation.
    assert "paper_pipeline.generate_paper_trade_signal" in src
    gate_pos = src.index("reject_non_paper")
    pipeline_pos = src.index("paper_pipeline.generate_paper_trade_signal")
    assert gate_pos < pipeline_pos


def test_generate_paper_trade_signal_docstring_forbids_live():
    import inspect

    from tools.backtest import BacktestEngine

    doc = inspect.getdoc(BacktestEngine.generate_paper_trade_signal) or ""
    assert "live" in doc.lower()
    assert "FORBIDDEN" in doc


def test_dashboard_tests_reference_nothing_that_arms_betting():
    # Meta-guard: this characterization module itself must not contain
    # code that could arm anything. Check the executable lines only
    # (docstrings/comments are excluded from coverage-style parsing here
    # by simply scanning for call-shaped patterns outside quotes).
    import ast

    own_src = Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(own_src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", getattr(func, "id", ""))
            assert name != "enable", f"arming call at line {node.lineno}"
    stripped = re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", own_src)
    assert "_enabled" not in re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', "", stripped)
