"""Autofill characterization #0052 — dashboard research face (LONG).

Source contract under characterization:

  * ``web/dashboard/index.html`` is the *default* face of the Callisto
    dashboard and it must present a RESEARCH appliance, not a live betting
    operations console.
  * The live-betting panels — ``panel-hyps``, ``panel-orders``,
    ``panel-portfolio`` — must be fully DELETED from index.html (not merely
    hidden with CSS or toggled by a query-string backdoor).
  * The research panels (``panel-state``, ``panel-ingestion``, ``panel-alerts``)
    must remain present so the page still renders useful telemetry.

These tests are pure text characterizations: they read the shipped sources as
text and assert on their contents. No browser, no network, no production code
is imported or executed. If any assertion here fails, that means somebody
re-introduced live-betting UI into the default dashboard face and the change
must be reverted — these tests FAIL CLOSED by design (they never patch or
disable anything; they only observe).
"""

from pathlib import Path
import re

# ---------------------------------------------------------------------------
# Fixtures-as-constants: read the real shipped files exactly once per module.
# ---------------------------------------------------------------------------

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "web" / "dashboard"
HTML_PATH = DASHBOARD_DIR / "index.html"
JS_PATH = DASHBOARD_DIR / "app.js"
CSS_PATH = DASHBOARD_DIR / "styles.css"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""

COMBINED_LOWER = (HTML + JS + CSS).lower()

# The three banned live-betting panel ids.
BANNED_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research panels that must survive.
RESEARCH_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Other DOM ids referenced from app.js that must keep existing in the HTML.
REQUIRED_DOM_IDS = (
    "online-pill",
    "last-refresh",
    "offline-banner",
    "offline-target",
    "state-body",
    "ingestion-body",
    "alerts-body",
    "build-info",
)

# Trading-flavored tokens that have no business on the research face.
BANNED_TRADING_TOKENS = (
    # endpoint fragments
    "/executor/enable",
    "api/hypotheses/live",
    "api/orders",
    "api/portfolio",
    # JS identifiers used by the old trading face
    "TRADING_MODE",
    "applyTradingMode",
    "renderHyps",
    "renderOrders",
    "renderPortfolio",
    "API.hyps",
    "API.orders",
    "API.portfolio",
)

BANNED_TRADING_TOKENS_LOWER = tuple(t.lower() for t in BANNED_TRADING_TOKENS)


def _assert_absent(needle: str, haystack: str, where: str) -> None:
    assert needle not in haystack, (
        f"banned token {needle!r} found in {where}; "
        "the dashboard default face must stay research-only"
    )


def _panel_section(panel_id: str) -> str:
    """Return the full <section ...>...</section> block for a panel id."""
    pattern = re.compile(
        r"<section\b[^>]*id=\"" + re.escape(panel_id) + r"\"[^>]*>.*?</section>",
        re.DOTALL,
    )
    m = pattern.search(HTML)
    assert m is not None, f"expected <section id=\"{panel_id}\"> in index.html"
    return m.group(0)


# ---------------------------------------------------------------------------
# 1. The core contract: banned panels absent from index.html
# ---------------------------------------------------------------------------


class TestBannedPanelsAbsent:
    def test_panel_hyps_absent_as_dom_id(self):
        _assert_absent('id="panel-hyps"', HTML, "index.html")

    def test_panel_orders_absent_as_dom_id(self):
        _assert_absent('id="panel-orders"', HTML, "index.html")

    def test_panel_portfolio_absent_as_dom_id(self):
        _assert_absent('id="panel-portfolio"', HTML, "index.html")

    def test_banned_ids_absent_in_any_attribute_form(self):
        # Not just id="...": no class/data/href/name attribute may reference them either.
        for panel_id in BANNED_PANEL_IDS:
            for attr in ("class=", "data-panel", "href=", "name=", "data-target"):
                assert f'{attr}"{panel_id}"' not in HTML

    def test_banned_ids_not_even_mentioned_in_comments(self):
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id not in HTML, (
                f"{panel_id} should be deleted outright, even from comments"
            )

    def test_no_hidden_css_keeps_banned_panels_alive(self):
        # A deleted panel must not linger as display:none / hidden rules.
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id not in CSS.lower(), (
                f"{panel_id} still referenced by styles.css — delete, don't hide"
            )

    def test_banned_ids_absent_from_app_js(self):
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id not in JS, f"{panel_id} referenced from app.js"

    def test_no_get_element_by_id_for_banned_panels(self):
        for panel_id in BANNED_PANEL_IDS:
            assert f'getElementById("{panel_id}")' not in JS

    def test_banned_ids_case_insensitive(self):
        lower_html = HTML.lower()
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id.lower() not in lower_html


# ---------------------------------------------------------------------------
# 2. Research panels still present and well-formed
# ---------------------------------------------------------------------------


class TestResearchPanelsPresent:
    def test_panel_state_present(self):
        assert 'id="panel-state"' in HTML

    def test_panel_ingestion_present(self):
        assert 'id="panel-ingestion"' in HTML

    def test_panel_alerts_present(self):
        assert 'id="panel-alerts"' in HTML

    def test_research_panels_are_sections_with_panel_class(self):
        for panel_id in RESEARCH_PANEL_IDS:
            block = _panel_section(panel_id)
            assert 'class="panel' in block or "class='panel" in block, (
                f"{panel_id} lost its .panel class"
            )

    def test_each_research_panel_has_a_heading_and_body(self):
        for panel_id, body_id in (
            ("panel-state", "state-body"),
            ("panel-ingestion", "ingestion-body"),
            ("panel-alerts", "alerts-body"),
        ):
            block = _panel_section(panel_id)
            assert "<h2>" in block
            assert f'id="{body_id}"' in block

    def test_exactly_three_panels_on_the_page(self):
        found = re.findall(r'<section\b[^>]*id="([^"]+)"', HTML)
        assert sorted(found) == sorted(RESEARCH_PANEL_IDS), (
            f"unexpected panel set on index.html: {found}"
        )

    def test_grid_main_container_exists(self):
        assert '<main class="grid">' in HTML


# ---------------------------------------------------------------------------
# 3. Required DOM hooks referenced by app.js
# ---------------------------------------------------------------------------


class TestRequiredDomHooks:
    def test_all_app_js_dom_targets_exist_in_html(self):
        for dom_id in REQUIRED_DOM_IDS:
            assert f'id="{dom_id}"' in HTML, f"#{dom_id} missing from index.html"

    def test_online_pill_and_offline_banner_pairing(self):
        assert 'id="online-pill"' in HTML
        assert 'id="offline-banner"' in HTML
        # banner starts hidden until connectivity is proven
        assert "hidden" in HTML.split('id="offline-banner"')[1].split(">")[0] + ">"

    def test_script_tag_points_at_static_app_js(self):
        assert '<script src="/static/app.js"></script>' in HTML

    def test_stylesheet_link_points_at_static_styles(self):
        assert '/static/styles.css' in HTML


# ---------------------------------------------------------------------------
# 4. No trading endpoints / trading-mode backdoors anywhere on the face
# ---------------------------------------------------------------------------


class TestNoTradingFace:
    def test_combined_sources_free_of_trading_tokens(self):
        combined = HTML + JS + CSS
        for token in BANNED_TRADING_TOKENS:
            _assert_absent(token, combined, "dashboard sources")

    def test_lowercased_sweep_for_trading_tokens(self):
        for token in BANNED_TRADING_TOKENS_LOWER:
            _assert_absent(token, COMBINED_LOWER, "lowercased dashboard sources")

    def test_no_query_string_trading_backdoor(self):
        for needle in ("trading=1", "trading=true", "?mode=live", "live=1"):
            _assert_absent(needle, HTML + JS, "query backdoor")

    def test_no_localstorage_trading_flag(self):
        for needle in ("localStorage", "sessionStorage"):
            _assert_absent(needle, JS, "trading-flag persistence")

    def test_title_is_research_appliance(self):
        m = re.search(r"<title>(.*?)</title>", HTML)
        assert m is not None
        title = m.group(1).lower()
        assert "research" in title
        for bad in ("live", "betting", "order", "portfolio", "ops"):
            assert bad not in title

    def test_brand_sub_labels_it_research(self):
        assert "research appliance" in HTML.lower()

    def test_footer_claims_read_only(self):
        footer = HTML.split("<footer")[1]
        assert "read-only" in footer.lower()

    def test_no_post_put_delete_fetch_methods(self):
        # A read-only research face only ever GETs.
        for method in ("method: \"POST\"", "method: \"PUT\"", "method: \"DELETE\"",
                       "method: 'POST'", "method: 'PUT'", "method: 'DELETE'",
                       "method: \"PATCH\""):
            _assert_absent(method, JS, "mutating fetch")

    def test_only_get_fetches_exist(self):
        fetches = re.findall(r"fetch\([^)]*\)", JS)
        assert fetches, "app.js should still fetch research telemetry"
        for call in fetches:
            assert "POST" not in call.upper() and "PUT" not in call.upper()

    def test_api_map_contains_exactly_three_endpoints(self):
        keys = re.findall(r"^  (\w+):\s+\"", JS, re.MULTILINE)
        assert sorted(keys) == ["alerts", "ingestion", "status"], (
            f"API map drifted: {keys}"
        )

    def test_api_endpoints_are_read_only_telemetry(self):
        assert '"api/status"' in JS
        assert '"api/ingestion"' in JS
        assert '"api/alerts?limit=20"' in JS

    def test_refresh_loop_polls_three_endpoints(self):
        loop = JS[JS.index("async function refresh"):]
        assert loop.count("jsonFetch(") == 3

    def test_no_websocket_or_eventsource(self):
        for needle in ("WebSocket", "EventSource", "socket.io"):
            _assert_absent(needle, JS, "push channel")


# ---------------------------------------------------------------------------
# 5. Executor / money surface stays out of the markup
# ---------------------------------------------------------------------------


class TestExecutorAndMoneySurface:
    def test_no_executor_enable_in_html(self):
        _assert_absent("/executor/enable", HTML, "index.html")

    def test_no_executor_enable_in_js(self):
        _assert_absent("/executor/enable", JS, "app.js")

    def test_html_has_no_money_words(self):
        lower = HTML.lower()
        for word in ("stake", "wager", "payout", "balance", "bankroll"):
            assert word not in lower, f"money word {word!r} on research face"

    def test_place_bet_ui_absent(self):
        combined = (HTML + JS).lower()
        for phrase in ("place bet", "placeBet".lower(), "submit order", "confirm bet"):
            assert phrase not in combined


# ---------------------------------------------------------------------------
# 6. Characterization of remaining page structure (regression net)
# ---------------------------------------------------------------------------


class TestPageStructureCharacterization:
    def test_doctype_present(self):
        assert HTML.lstrip().startswith("<!DOCTYPE html>")

    def test_lang_is_english(self):
        assert '<html lang="en">' in HTML

    def test_viewport_meta_present(self):
        assert 'name="viewport"' in HTML

    def test_charset_meta_present(self):
        assert 'charset="utf-8"' in HTML

    def test_header_topbar_exists(self):
        assert '<header class="topbar">' in HTML

    def test_brand_name_is_callisto(self):
        assert "<span class=\"brand-name\">Callisto</span>" in HTML

    def test_offline_banner_copy_mentions_fallback(self):
        banner = HTML[HTML.index('id="offline-banner"'):]
        banner = banner[:banner.index("</div>")]
        assert "unreachable" in banner
        assert "last-known" in banner

    def test_state_panel_headline_mentions_loop_health(self):
        block = _panel_section("panel-state")
        assert "loop health" in block.lower()

    def test_ingestion_panel_headline(self):
        assert "<h2>Data ingestion</h2>" in HTML

    def test_alerts_panel_headline(self):
        assert "<h2>Alerts</h2>" in HTML

    def test_autorefresh_interval_matches_js(self):
        # Footer says 15s; app.js defines REFRESH_MS = 15000.
        assert "Auto-refresh every 15s" in HTML
        assert "REFRESH_MS = 15000" in JS

    def test_comment_marks_state_panel_as_research_face(self):
        # The comment sits immediately above the section, not inside it.
        idx = HTML.index('id="panel-state"')
        preceding = HTML[max(0, idx - 300):idx]
        assert "research face" in preceding.lower()

    def test_no_inline_scripts_in_html(self):
        # All behavior lives in app.js — no inline <script> bodies.
        inline = re.findall(r"<script(?![^>]*src)[^>]*>(.*?)</script>", HTML, re.DOTALL)
        assert all(not body.strip() for body in inline), (
            f"inline script content found: {inline}"
        )

    def test_no_inline_event_handlers(self):
        for handler in ("onclick", "onload", "onchange", "onsubmit", "onerror="):
            assert handler not in HTML.lower()

    def test_html_ends_with_closing_tags(self):
        stripped = HTML.rstrip()
        assert stripped.endswith("</html>")
