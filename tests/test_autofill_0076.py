"""Autofill characterization #0076 — dashboard research face (LONG).

Locks in the "research appliance" character of web/dashboard/index.html:

  * the trading panels (panel-hyps / panel-orders / panel-portfolio) must be
    GONE from the markup entirely — not merely hidden via CSS or JS;
  * the default face is research: loop health, data ingestion, alerts;
  * no live-betting fetch paths, no trading backdoors (query params,
    feature flags), no executor-enable affordances.

Reads web/dashboard/index.html and web/dashboard/app.js as plain text.
No browser, no network, no live betting — this module never arms anything.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "web" / "dashboard"
HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"
CSS_PATH = DASHBOARD / "styles.css"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""
COMBINED = HTML + JS + CSS
LOW = COMBINED.lower()

# The three trading panels that were removed from the research face.
FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research panels that must remain present.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Endpoint fragments that belong to a trading face, not a research one.
FORBIDDEN_ENDPOINT_FRAGMENTS = (
    "api/hypotheses/live",
    "api/orders",
    "api/portfolio",
    "/executor/enable",
    "/executor/disable",
    "/betslip",
    "placebet",
)

# Identifiers that would indicate a reintroduced trading mode backdoor.
FORBIDDEN_JS_IDENTIFIERS = (
    "TRADING_MODE",
    "applyTradingMode",
    "API.hyps",
    "API.orders",
    "API.portfolio",
    "jsonFetch(API.orders)",
    "jsonFetch(API.portfolio)",
)

FORBIDDEN_PHRASES = (
    "LIVE hypotheses",
    "ops dashboard",
    "trading=1",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _strip_js_block_comments(text: str) -> str:
    return re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)


HTML_ACTIVE = _strip_html_comments(HTML)
JS_ACTIVE = _strip_js_block_comments(JS)

ALL_ELEMENT_IDS = set(re.findall(r'id="([^"]+)"', HTML))


def panel_present(panel_id: str) -> bool:
    """True only when an actual <section ... id="..."> element exists."""
    pattern = rf'<section[^>]*id="{re.escape(panel_id)}"'
    return re.search(pattern, HTML_ACTIVE) is not None


def panel_hidden_only(panel_id: str) -> bool:
    """Detect the cheat: panel deleted from markup but still referenced."""
    referenced = (
        f'"{panel_id}"' in JS_ACTIVE
        or f"'{panel_id}'" in JS_ACTIVE
        or f"#{panel_id}" in CSS
        or f"{panel_id}" in CSS
    )
    return referenced and not panel_present(panel_id)


# ---------------------------------------------------------------------------
# Core contract: forbidden panels are truly gone
# ---------------------------------------------------------------------------


def test_forbidden_panel_ids_absent_as_element_ids():
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert f'id="{panel_id}"' not in HTML, (
            f'{panel_id} must be deleted from index.html, not merely hidden'
        )


def test_forbidden_panels_have_no_section_elements():
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert not panel_present(panel_id), (
            f"{panel_id} reappeared as a <section> in index.html"
        )


def test_forbidden_panel_names_absent_from_active_markup():
    active = HTML_ACTIVE.lower()
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert panel_id not in active, (
            f"{panel_id} string appears outside comments in index.html"
        )


def test_forbidden_panel_names_not_referenced_by_app_js():
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert panel_id not in JS_ACTIVE, (
            f"app.js still references {panel_id}; dead wiring for a removed panel"
        )


def test_forbidden_panel_names_not_styled_in_css():
    for panel_id in FORBIDDEN_PANEL_IDS:
        assert panel_id not in CSS, (
            f"styles.css still carries #{panel_id} rules; hidden-not-deleted smell"
        )


def test_no_hidden_display_none_panels_in_inline_styles():
    # A panel could be "removed" by display:none inline style — forbid that.
    assert 'style="display:none"' not in HTML_ACTIVE
    assert "display: none" not in HTML_ACTIVE.lower()


def test_no_trading_section_elements_beyond_known_three():
    sections = re.findall(r'<section[^>]*id="([^"]+)"', HTML_ACTIVE)
    for sid in sections:
        assert sid not in FORBIDDEN_PANEL_IDS, sid


def test_all_html_ids_are_known_research_or_chrome_ids():
    allowed = {
        "online-pill",
        "last-refresh",
        "offline-banner",
        "offline-target",
        "state-body",
        "ingestion-body",
        "alerts-body",
        "build-info",
        "panel-state",
        "panel-ingestion",
        "panel-alerts",
    }
    unknown = ALL_ELEMENT_IDS - allowed - set(REQUIRED_PANEL_IDS)
    assert not unknown, f"unexpected new ids appeared on the research face: {sorted(unknown)}"


# ---------------------------------------------------------------------------
# Research face is intact
# ---------------------------------------------------------------------------


def test_required_research_panels_present():
    for panel_id in REQUIRED_PANEL_IDS:
        assert panel_present(panel_id), f"{panel_id} panel missing from index.html"


def test_each_research_panel_has_a_body_container():
    pairs = {"panel-state": "state-body", "panel-ingestion": "ingestion-body", "panel-alerts": "alerts-body"}
    for pid, body_id in pairs.items():
        assert panel_present(pid)
        assert body_id in ALL_ELEMENT_IDS, f"{pid} lacks its {body_id} render target"


def test_title_is_research_appliance():
    m = re.search(r"<title>(.*?)</title>", HTML, flags=re.DOTALL)
    assert m, "index.html has no <title>"
    title = m.group(1).lower()
    assert "research appliance" in title
    assert "trading" not in title
    assert "live" not in title


def test_brand_subtitle_says_research():
    assert "research appliance" in HTML_ACTIVE.lower()


def test_footer_declares_read_only():
    assert "read-only" in HTML_ACTIVE.lower()


def test_refresh_interval_is_polling_not_streaming():
    assert "Auto-refresh every 15s" in HTML_ACTIVE
    assert "REFRESH_MS = 15000" in JS_ACTIVE


# ---------------------------------------------------------------------------
# No live-betting fetch surface
# ---------------------------------------------------------------------------


def test_api_object_has_exactly_three_research_endpoints():
    block = re.search(r"const API = \{(.*?)\};", JS_ACTIVE, flags=re.DOTALL)
    assert block, "API endpoint map missing from app.js"
    entries = dict(re.findall(r"(\w+):\s*\"([^\"]+)\"", block.group(1)))
    assert sorted(entries) == ["alerts", "ingestion", "status"], entries
    assert all("order" not in k for k in entries)
    assert all("portfolio" not in k for k in entries)
    assert all("hyp" not in k for k in entries)


def test_no_forbidden_endpoint_fragments_anywhere():
    for frag in FORBIDDEN_ENDPOINT_FRAGMENTS:
        assert frag not in JS_ACTIVE, f"forbidden endpoint fragment in app.js: {frag}"
        assert frag not in HTML_ACTIVE, f"forbidden endpoint fragment in index.html: {frag}"


def test_no_live_hypotheses_face_or_poll():
    combined = HTML_ACTIVE + JS_ACTIVE
    assert "LIVE hypotheses" not in combined
    assert "api/hypotheses/live" not in JS_ACTIVE


def test_app_js_does_not_fetch_money_endpoints():
    assert "jsonFetch(API.orders)" not in JS_ACTIVE
    assert "jsonFetch(API.portfolio)" not in JS_ACTIVE


def test_no_trading_mode_backdoor_in_js():
    for ident in ("TRADING_MODE", "applyTradingMode"):
        assert ident not in JS_ACTIVE, f"trading-mode backdoor identifier present: {ident}"


def test_no_query_param_backdoors_in_js():
    for needle in ("URLSearchParams", "location.search", "trading=1"):
        assert needle not in JS_ACTIVE, f"query-param backdoor present: {needle}"


def test_no_executor_enable_reference():
    assert "/executor/enable" not in JS_ACTIVE
    assert "/executor/enable" not in HTML_ACTIVE


def test_no_post_put_delete_fetches_in_app_js():
    # A read-only face never mutates server state.
    mutations = re.findall(r"fetch\([^)]*\{[^}]*(?:method\s*:\s*[\"'](POST|PUT|DELETE|PATCH))", JS_ACTIVE, flags=re.IGNORECASE)
    assert not mutations, f"mutating fetch calls found: {mutations}"


def test_all_fetches_go_through_jsonfetch_wrapper():
    raw = [m for m in re.findall(r"(?<![\w.])fetch\(", JS_ACTIVE)]
    assert len(raw) == 1, "app.js should call fetch exactly once, inside jsonFetch"


def test_no_websocket_or_eventsource_on_dashboard():
    for needle in ("new WebSocket", "EventSource", "wss://", "ws://localhost"):
        assert needle not in JS_ACTIVE, f"streaming channel found: {needle}"


# ---------------------------------------------------------------------------
# Forbidden phrases across the whole face
# ---------------------------------------------------------------------------


def test_forbidden_phrases_absent():
    for phrase in FORBIDDEN_PHRASES:
        assert phrase not in HTML_ACTIVE + JS_ACTIVE, f"forbidden phrase present: {phrase}"


def test_word_live_appears_only_in_safe_contexts():
    # "live" may appear describing loop state counts, never as a betting face.
    hits = re.findall(r"[^\n]*\blive\b[^\n]*", LOW, flags=re.IGNORECASE)
    for line in hits:
        line_l = line.strip()
        ok_contexts = ("loopback status, not live betting", "live ·", "hypotheses")
        assert any(ctx in line_l for ctx in ok_contexts) or "not live betting" in line_l, (
            f"suspicious use of 'live': {line_l!r}"
        )


def test_no_betslip_or_wager_vocabulary():
    vocab = ("betslip", "wager", "stake amount", "max bet", "bet now", "cashout")
    for word in vocab:
        assert word not in LOW, f"betting vocabulary leaked into dashboard: {word}"


# ---------------------------------------------------------------------------
# Structural sanity of the research face
# ---------------------------------------------------------------------------


def test_html_has_single_main_grid():
    assert HTML_ACTIVE.count("<main") == 1
    assert '<main class="grid">' in HTML_ACTIVE


def test_grid_contains_exactly_three_panels():
    panels = re.findall(r'<section[^>]*class="panel[^"]*"[^>]*>', HTML_ACTIVE)
    assert len(panels) == 3, f"expected exactly 3 research panels, got {len(panels)}"


def test_one_panel_is_marked_wide():
    assert 'class="panel wide"' in HTML_ACTIVE


def test_script_tag_references_static_app_js_once():
    assert HTML_ACTIVE.count("<script") == 1
    assert '/static/app.js' in HTML_ACTIVE


def test_stylesheet_link_present():
    assert '/static/styles.css' in HTML_ACTIVE


def test_offline_banner_defaults_to_hidden():
    banner = re.search(r'<div id="offline-banner"[^>]*>', HTML_ACTIVE)
    assert banner, "offline-banner div missing"
    assert "hidden" in banner.group(0)


def test_online_pill_starts_muted():
    pill_el = re.search(r'<span id="online-pill"[^>]*>', HTML_ACTIVE)
    assert pill_el and "pill-muted" in pill_el.group(0)


def test_viewport_meta_present():
    assert 'name="viewport"' in HTML_ACTIVE


def test_html_lang_en():
    assert '<html lang="en">' in HTML_ACTIVE


# ---------------------------------------------------------------------------
# app.js behavioral characterization (read-only face)
# ---------------------------------------------------------------------------


def test_renderers_exist_for_exactly_the_three_research_panels():
    for fn in ("renderState", "renderIngestion", "renderAlerts"):
        assert f"function {fn}(" in JS_ACTIVE, f"missing renderer {fn}"
    # And nothing else renders into removed panel bodies.
    extra = re.findall(r"function (render[A-Z]\w+)\(", JS_ACTIVE)
    assert sorted(extra) == ["renderAlerts", "renderCircuits", "renderIngestion", "renderState"], extra


def test_renderers_target_existing_body_ids():
    for target in ("state-body", "ingestion-body", "alerts-body"):
        assert f'getElementById("{target}")' in JS_ACTIVE


def test_refresh_polls_exactly_three_endpoints():
    calls = re.findall(r"jsonFetch\(API\.(\w+)\)", JS_ACTIVE)
    assert sorted(calls) == ["alerts", "ingestion", "status"]


def test_json_fetch_never_throws():
    fn = re.search(r"async function jsonFetch\(path\) \{.*?\n\}", JS_ACTIVE, flags=re.DOTALL)
    assert fn, "jsonFetch missing"
    body = fn.group(0)
    assert "try" in body and "catch" in body
    assert "__error" in body


def test_error_path_uses_stale_marker_not_crash():
    assert '__error' in JS_ACTIVE
    assert "class=\"error\"" in JS_ACTIVE or "'error'" in JS_ACTIVE or '"error"' in JS_ACTIVE


def test_set_online_toggles_banner_classes():
    fn = re.search(r"function setOnline\(online\) \{.*?\n\}", JS_ACTIVE, flags=re.DOTALL)
    assert fn, "setOnline missing"
    body = fn.group(0)
    assert "pill-green" in body and "pill-red" in body
    assert 'classList.add("hidden")' in body and 'classList.remove("hidden")' in body


def test_escape_html_used_before_interpolating_external_strings():
    assert "function escapeHtml(" in JS_ACTIVE
    assert "escapeHtml(data.__error" in JS_ACTIVE


def test_pill_helper_restricts_colors_to_palette():
    fn = re.search(r"function pill\(text, color\) \{.*?\n\}", JS_ACTIVE, flags=re.DOTALL)
    assert fn, "pill helper missing"
    assert '["green", "yellow", "red", "muted"]' in fn.group(0)


def test_executor_status_is_reported_not_controlled():
    # The executor row may *display* enabled/disabled but offers no toggle.
    assert "exec.enabled" in JS_ACTIVE
    for control in ("toggleExecutor", "enableExecutor", "disableExecutor", "onclick=\"executor"):
        assert control not in JS_ACTIVE, f"executor control found: {control}"


def test_main_loop_uses_interval_not_recursion():
    assert "setInterval(refresh, REFRESH_MS)" in JS_ACTIVE
    assert "setTimeout(refresh" not in JS_ACTIVE


def test_no_localstorage_session_side_effects():
    for needle in ("localStorage", "sessionStorage", "document.cookie"):
        assert needle not in JS_ACTIVE, f"client-side persistence found: {needle}"


# ---------------------------------------------------------------------------
# Fail-closed guard: this module itself must never arm live betting
# ---------------------------------------------------------------------------


def test_test_module_does_not_import_production_runtime():
    source = Path(__file__).read_text(encoding="utf-8")
    for bad in ("import " + "inference", "import " + "orchestrator",
                "generate_" + "paper_trade_signal", "_PAPER_TRADE_" + "SIGNAL_STATUSES"):
        assert bad not in source, f"characterization test must not touch runtime symbol {bad}"


def test_test_module_declares_read_only_intent():
    doc = __doc__ or ""
    assert "never arms anything" in doc or "no live betting" in doc


def test_dashboard_files_exist_and_nonempty():
    assert HTML_PATH.stat().st_size > 0
    assert JS_PATH.stat().st_size > 0
