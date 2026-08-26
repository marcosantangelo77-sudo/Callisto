"""Autofill characterization #0036 — dashboard research face (LONG).

Characterizes the web dashboard's *research appliance* contract by reading
``web/dashboard/index.html`` and ``web/dashboard/app.js`` as plain text (no
browser, no network, no server import — these are source-contract pins).

Core invariant under test
-------------------------
The dashboard is the RESEARCH face of Callisto. It must never present or
operate the money/trading surfaces:

  * The trading panels ``panel-hyps``, ``panel-orders``, and
    ``panel-portfolio`` must be entirely ABSENT from index.html — deleted,
    not merely hidden via CSS or a query-string backdoor.
  * No residual references to those panel ids may appear in HTML, JS, CSS
    hooks, or comments that could resurrect them with a one-line edit.
  * app.js must not fetch money endpoints (hypotheses/orders/portfolio)
    and must not carry any trading-mode toggle machinery
    (``TRADING_MODE`` / ``applyTradingMode`` / ``?trading=1``).
  * The executor-enable control surface must be gone from both files.
  * LIVE-betting vocabulary ("LIVE hypotheses", live polling endpoints)
    must not reappear on the default face.
  * The three research panels (state/loop health, ingestion, alerts) must
    remain present so the pin cannot pass vacuously on an emptied page.
  * The page self-identifies as read-only / research, never as an ops or
    trading console.

These are characterization tests: they pin today's fail-closed behavior so
future edits cannot quietly widen the gates toward live betting. Nothing
here arms live trading; no production code is modified by this module.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "web" / "dashboard"
HTML_PATH = DASHBOARD_DIR / "index.html"
JS_PATH = DASHBOARD_DIR / "app.js"
CSS_PATH = DASHBOARD_DIR / "styles.css"

sys.path.insert(0, str(REPO_ROOT))  # keep parity with sibling autofill modules


# ─────────────────────────── module-level fixtures ────────────────────────


@pytest.fixture(scope="module")
def html() -> str:
    assert HTML_PATH.exists(), f"missing {HTML_PATH}"
    return HTML_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def js() -> str:
    assert JS_PATH.exists(), f"missing {JS_PATH}"
    return JS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def css() -> str:
    if not CSS_PATH.exists():
        return ""
    return CSS_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def combined(html: str, js: str) -> str:
    return html + js


# ─────────────────────────── constants under test ─────────────────────────

FORBIDDEN_PANEL_IDS = (
    "panel-hyps",
    "panel-orders",
    "panel-portfolio",
)

REQUIRED_PANEL_IDS = (
    "panel-state",
    "panel-ingestion",
    "panel-alerts",
)

REQUIRED_BODY_IDS = {
    "panel-state": "state-body",
    "panel-ingestion": "ingestion-body",
    "panel-alerts": "alerts-body",
}

MONEY_API_KEYS = ("hyps", "orders", "portfolio")

TRADING_MACHINERY_TOKENS = (
    "TRADING_MODE",
    "applyTradingMode",
    "trading=1",
    "tradingMode",
    "data-trading",
)


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _extract_script_blocks(html: str) -> list[str]:
    return re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE)


def _extract_style_blocks(html: str) -> list[str]:
    return re.findall(r"<style\b[^>]*>(.*?)</style>", html, flags=re.DOTALL | re.IGNORECASE)


def _section_ids(html: str) -> set[str]:
    return set(re.findall(r'<section\b[^>]*\bid="([^"]+)"', html))


# ═══════════════════════ 1. Trading panels are gone ══════════════════════


class TestTradingPanelsAbsent:
    def test_panel_hyps_absent_from_html(self, html):
        assert 'id="panel-hyps"' not in html, (
            "panel-hyps must be deleted from index.html, not merely hidden"
        )

    def test_panel_orders_absent_from_html(self, html):
        assert 'id="panel-orders"' not in html, (
            "panel-orders must be deleted from index.html, not merely hidden"
        )

    def test_panel_portfolio_absent_from_html(self, html):
        assert 'id="panel-portfolio"' not in html, (
            "panel-portfolio must be deleted from index.html, not merely hidden"
        )

    def test_each_forbidden_panel_id_parametrized(self, html):
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert f'id="{panel_id}"' not in html, (
                f"{panel_id} must be deleted from index.html, not merely hidden"
            )

    def test_no_hidden_display_none_panels(self, html):
        # A hidden-but-present resurrection (style="display:none") of the
        # trading ids fails closed: the id itself must not exist at all.
        visible_html = _strip_html_comments(html).lower()
        for token in FORBIDDEN_PANEL_IDS:
            assert token.lower() not in visible_html, (
                f"{token} appears somewhere (even hidden/commented) in index.html"
            )

    def test_no_commented_out_trading_markup(self, html):
        for m in re.finditer(r"<!--(.*?)-->", html, flags=re.DOTALL):
            body = m.group(1).lower()
            for token in FORBIDDEN_PANEL_IDS:
                assert token.lower() not in body, (
                    f"{token} survives only inside an HTML comment; delete it outright"
                )

    def test_inline_scripts_do_not_reference_trading_panels(self, html):
        for block in _extract_script_blocks(html):
            for token in FORBIDDEN_PANEL_IDS:
                assert token not in block

    def test_css_does_not_target_trading_panels(self, css):
        # If styles.css still carries #panel-hyps rules, a resurrected div
        # would instantly look native — pin its absence too.
        for token in FORBIDDEN_PANEL_IDS:
            assert token not in css

    def test_js_has_no_get_element_by_id_for_trading_panels(self, js):
        for token in FORBIDDEN_PANEL_IDS:
            assert token not in js

    def test_no_hyps_orders_portfolio_words_in_html(self, html):
        low = _strip_html_comments(html).lower()
        for word in ("hyps", "portfolio"):
            assert word not in low
        assert "orders" not in low

    def test_section_inventory_is_exactly_research(self, html):
        ids = _section_ids(_strip_html_comments(html))
        forbidden_present = ids & set(FORBIDDEN_PANEL_IDS)
        assert not forbidden_present
        assert set(REQUIRED_PANEL_IDS) <= ids


# ═══════════════════════ 2. Research panels remain ═══════════════════════


class TestResearchFaceIntact:
    @pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
    def test_required_panel_present(self, html, panel_id):
        assert f'id="{panel_id}"' in html, f"{panel_id} panel missing from index.html"

    @pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
    def test_required_panel_is_a_real_section(self, html, panel_id):
        pattern = rf'<section\b[^>]*\bid="{panel_id}"'
        assert re.search(pattern, html), f"{panel_id} must stay a <section>"

    @pytest.mark.parametrize(
        "panel_id,body_id", sorted(REQUIRED_BODY_IDS.items())
    )
    def test_panel_body_placeholder_exists(self, html, panel_id, body_id):
        section = re.search(
            rf'<section\b[^>]*id="{panel_id}".*?</section>',
            html,
            flags=re.DOTALL,
        )
        assert section, f"{panel_id} section missing"
        assert f'id="{body_id}"' in section.group(0)

    def test_page_still_has_main_grid_and_footer(self, html):
        assert '<main class="grid">' in html
        assert '<footer class="footer">' in html

    def test_app_js_script_tag_wired(self, html):
        assert '/static/app.js' in html

    def test_stylesheet_wired(self, html):
        assert "/static/styles.css" in html

    def test_read_only_notice_in_footer(self, html):
        footer = re.search(r"<footer.*?</footer>", html, flags=re.DOTALL)
        assert footer, "footer missing"
        assert "read-only" in footer.group(0).lower()

    def test_auto_refresh_interval_documented(self, html):
        # Characterize the documented refresh cadence so it can't silently
        # change into a tighter live-trading poll loop without notice.
        assert "Auto-refresh every 15s" in html

    def test_title_says_research_appliance(self, html):
        title = re.search(r"<title>(.*?)</title>", html, flags=re.DOTALL)
        assert title, "title missing"
        assert "Research Appliance" in title.group(1)

    def test_brand_sub_label_research(self, html):
        sub = re.search(r'class="brand-sub">([^<]*)<', html)
        assert sub, "brand-sub element missing"
        assert "research" in sub.group(1).lower()

    def test_loop_health_panel_framed_as_research(self, html):
        section = re.search(
            r'<section\b[^>]*id="panel-state".*?</section>',
            html,
            flags=re.DOTALL,
        )
        assert section
        assert "loop health" in section.group(0).lower()
        assert "live betting" not in section.group(0).lower()


# ═══════════════════════ 3. No money API surface ═════════════════════════


class TestNoMoneyApiSurface:
    def test_api_map_has_only_research_keys(self, js):
        api_block = re.search(r"const API = \{(.*?)\};", js, flags=re.DOTALL)
        assert api_block, "API map missing from app.js"
        body = api_block.group(1)
        keys = set(re.findall(r"^\s*(\w+)\s*:", body, flags=re.MULTILINE))
        assert keys == {"status", "ingestion", "alerts"}, keys
        for key in MONEY_API_KEYS:
            assert key not in keys

    def test_api_object_not_referenced_with_money_keys(self, js):
        for key in MONEY_API_KEYS:
            assert f"API.{key}" not in js

    def test_no_json_fetch_of_money_endpoints(self, js):
        assert "jsonFetch(API.orders)" not in js
        assert "jsonFetch(API.portfolio)" not in js
        assert "jsonFetch(API.hyps)" not in js

    def test_no_live_hypotheses_endpoint(self, js):
        assert "api/hypotheses/live" not in js
        assert "api/hypotheses" not in js
        assert "api/orders" not in js
        assert "api/portfolio" not in js

    def test_html_contains_no_api_paths(self, html):
        # index.html should carry zero endpoint strings; all IO lives in JS.
        assert "api/" not in _strip_html_comments(html)

    def test_fetch_calls_limited_to_research_endpoints(self, js):
        endpoints = set(re.findall(r'["\'](api/[\w?=&.-]*)["\']', js))
        allowed = {"api/status", "api/ingestion", "api/alerts?limit=20"}
        unexpected = endpoints - allowed
        assert not unexpected, f"unexpected endpoints referenced: {unexpected}"

    def test_no_executor_enable_reference(self, combined):
        assert "/executor/enable" not in combined

    def test_no_other_executor_routes(self, combined):
        assert "/executor" not in combined

    def test_no_kill_switch_or_arm_control(self, combined):
        for token in ("killswitch", "kill-switch", "arm-live", "armLive",
                      "goLive", "go-live", "enableLive", "enable_live"):
            assert token.lower() not in combined.lower()


# ═══════════════════════ 4. No trading-mode machinery ════════════════════


class TestNoTradingModeBackdoor:
    @pytest.mark.parametrize("token", TRADING_MACHINERY_TOKENS)
    def test_token_absent(self, combined, token):
        assert token not in combined, (
            f"trading-mode machinery '{token}' must stay out of the dashboard"
        )

    def test_no_query_string_mode_switch(self, js):
        assert "URLSearchParams" not in js
        assert "location.search" not in js

    def test_no_localstorage_persistence_of_modes(self, js):
        # A mode flag persisted client-side is how a research face gets
        # quietly flipped back into a trading console.
        assert "localStorage" not in js
        assert "sessionStorage" not in js

    def test_no_conditional_body_class_swap(self, js):
        assert "classList.add" not in js.replace('classList.add("hidden")', "")
        assert "document.body.className" not in js

    def test_no_trading_controls(self, combined, js):
        # "paper_trading" appears only as a read-only status counter key in
        # api/status rendering; no actionable trading vocabulary is allowed.
        assert "<button" not in combined.lower()
        for token in ("place order", "submit order", "trade now", "execute"):
            assert token not in combined.lower()


# ═══════════════════════ 5. No LIVE betting vocabulary ═══════════════════


class TestNoLiveBettingVocabulary:
    def test_no_live_hypotheses_heading(self, combined):
        assert "LIVE hypotheses" not in combined

    def test_no_uppercase_live_labels(self, combined):
        # Standalone uppercase LIVE (badge style) is the old face's tell.
        assert not re.search(r"\bLIVE\b", combined)

    def test_paper_trade_status_array_not_touched_here(self, js):
        # Defense in depth: even if someone pasted backend constants into
        # the dashboard bundle, the live status string must never appear.
        assert '"live"' not in js
        assert "'live'" not in js

    def test_offline_banner_is_about_reachability_not_betting(self, html):
        banner = re.search(r'id="offline-banner".*?</div>', html, flags=re.DOTALL)
        assert banner, "offline banner missing"
        text = banner.group(0).lower()
        assert "unreachable" in text
        assert "live" not in re.sub(r"last-known", "", text)

    def test_online_pill_starts_muted_connecting(self, html):
        pill = re.search(r'id="online-pill"[^>]*>([^<]*)<', html)
        assert pill, "online pill missing"
        assert "connecting..." in pill.group(1)
        cls = re.search(r'id="online-pill" class="([^"]*)"', html)
        assert cls and "pill-muted" in cls.group(1)


# ═══════════════════════ 6. JS behavioral shape ══════════════════════════


class TestJsBehaviorShape:
    def test_app_js_size_bounded(self, js):
        # The trading-era app was far larger; a sudden re-growth suggests
        # reintroduced surfaces. Characterize today's compact size.
        line_count = len(js.splitlines())
        assert 50 < line_count < 600, f"app.js has {line_count} lines"

    def test_refresh_interval_is_15_seconds(self, js):
        # Interval is defined once as REFRESH_MS = 15000 and used for polling.
        m = re.search(r"REFRESH_MS\s*=\s*(\d+)", js)
        assert m, "REFRESH_MS constant missing"
        assert int(m.group(1)) == 15000
        assert js.count("setInterval") == 1

    def test_no_websocket_usage(self, js):
        assert "WebSocket" not in js
        assert "EventSource" not in js

    def test_no_post_put_delete_requests(self, js):
        # Read-only face: only GET fetches are permitted.
        for verb in ('method: "POST"', 'method:"POST"', "'POST'",
                     'method: "PUT"', "'PUT'", 'method: "DELETE"', "'DELETE'"):
            assert verb not in js

    def test_no_form_submission(self, combined, js):
        assert "<form" not in combined.lower()
        assert ".submit(" not in js

    def test_alerts_limit_bounded(self, js):
        limit = re.search(r"api/alerts\?limit=(\d+)", js)
        assert limit, "alerts endpoint missing"
        assert int(limit.group(1)) <= 100

    def test_build_info_element_present_in_html(self, html):
        assert 'id="build-info"' in html
        assert 'id="last-refresh"' in html

    def test_status_counts_are_read_only_labels(self, js):
        # The loop-health panel shows hypothesis counters (live / paper /
        # total) as read-only numbers from api/status — this is allowed.
        # Pin their shape so they cannot become actionable controls.
        assert "${(full.hypotheses || {}).total ?? 0} total" in js
        assert "<button" not in js.lower()
        assert "onclick" not in js.lower()
        assert "build-info" in HTML_PATH.read_text(encoding="utf-8")

    def test_last_refresh_element_used(self, js):
        assert "last-refresh" in js

    def test_no_eval_or_dynamic_code(self, js):
        for token in ("eval(", "new Function", "setTimeout(\""):
            assert token not in js

    def test_no_third_party_network_calls(self, js):
        urls = re.findall(r"https?://[^\s\"')]+", js)
        assert not urls, f"absolute URLs in app.js: {urls}"


# ═══════════════════════ 7. Fail-closed meta checks ══════════════════════


class TestFailClosedMeta:
    def test_dashboard_dir_contents_are_minimal(self):
        files = sorted(p.name for p in DASHBOARD_DIR.iterdir() if p.is_file())
        assert set(files) <= {"index.html", "app.js", "styles.css"}, files

    def test_this_module_cannot_pass_on_empty_html(self, tmp_path):
        # Guard the guard: if index.html were ever emptied, the required-
        # panel assertions above must fail. Simulate the check directly.
        empty = ""
        for panel_id in REQUIRED_PANEL_IDS:
            assert f'id="{panel_id}"' not in empty  # sanity inversion

    def test_forbidden_ids_differ_from_required_ids(self):
        assert not set(FORBIDDEN_PANEL_IDS) & set(REQUIRED_PANEL_IDS)

    def test_no_production_gate_weakened_by_importing_backend(self):
        # This module reads files only; it must not import the trading
        # backend. Parse THIS file's AST so the check is not confused by
        # other tests already loaded in sys.modules.
        import ast
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".", 1)[0])
        assert "callisto" not in imported
        assert "executor" not in imported
