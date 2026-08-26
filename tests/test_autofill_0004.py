"""Autofill characterization #0004 — dashboard research face (LONG).

Source contract: web/dashboard/index.html must present a RESEARCH face.
The trading-era panels (panel-hyps, panel-orders, panel-portfolio) must be
physically absent from the markup — not merely hidden via CSS or JS.

This module reads the dashboard sources as text (no browser, no network).
It characterizes the current state of the research-face gate so any
regression (a trading panel sneaking back into the HTML) fails loudly.

FAIL-CLOSED policy: every assertion below is written against the *absence*
of live-betting surfaces. If any of these pins is currently false, the test
fails and must NOT be weakened to re-arm anything. Never add "live" to
_PAPER_TRADE_SIGNAL_STATUSES; never widen generate_paper_trade_signal to
status == 'live'.
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
CSS = (
    CSS_PATH.read_text(encoding="utf-8")
    if CSS_PATH.exists()
    else ""
)

COMBINED = HTML + JS + CSS

# The three trading-era panels that must never return to the markup.
FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research panels that define the current default face.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# API surface fragments associated with money / order placement.
MONEY_API_FRAGMENTS = (
    "api/orders",
    "api/portfolio",
    "api/hypotheses/live",
    "api/executor/enable",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _html_lower() -> str:
    return HTML.lower()


def _id_occurrences(panel_id: str) -> list[str]:
    """Every textual occurrence of the panel id anywhere in the HTML."""
    return [m.group(0) for m in re.finditer(re.escape(panel_id), HTML)]


def _script_blocks() -> list[str]:
    """Contents of inline <script> blocks in index.html."""
    return re.findall(r"<script[^>]*>(.*?)</script>", HTML, flags=re.DOTALL | re.IGNORECASE)


def _style_blocks() -> list[str]:
    """Contents of inline <style> blocks in index.html."""
    return re.findall(r"<style[^>]*>(.*?)</style>", HTML, flags=re.DOTALL | re.IGNORECASE)


def _element_ids() -> set[str]:
    """All id="..." values declared in the HTML."""
    return set(re.findall(r'id\s*=\s*["\']([^"\']+)["\']', HTML))


def _class_names() -> set[str]:
    """All class tokens declared across the HTML."""
    tokens: set[str] = set()
    for chunk in re.findall(r'class\s*=\s*["\']([^"\']+)["\']', HTML):
        tokens.update(chunk.split())
    return tokens


# ---------------------------------------------------------------------------
# Core gate: forbidden trading panels are physically absent from the HTML
# ---------------------------------------------------------------------------


class TestForbiddenTradingPanelsAbsent:
    def test_panel_hyps_absent(self):
        assert "panel-hyps" not in HTML

    def test_panel_orders_absent(self):
        assert "panel-orders" not in HTML

    def test_panel_portfolio_absent(self):
        assert "panel-portfolio" not in HTML

    def test_all_forbidden_panels_absent_from_html(self):
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert f'id="{panel_id}"' not in HTML, (
                f"{panel_id} must be deleted from index.html, not merely hidden"
            )

    def test_forbidden_ids_not_even_mentioned_in_html_text(self):
        # Not just as element ids: no comment, data attribute, or string
        # should reference the removed panels either.
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert _id_occurrences(panel_id) == [], (
                f"{panel_id} appears {len(_id_occurrences(panel_id))}x in index.html"
            )

    def test_forbidden_panels_absent_case_insensitively(self):
        low = _html_lower()
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id.lower() not in low

    def test_no_hidden_trading_panels_via_style(self):
        # A regression could try display:none instead of deletion.
        for block in _style_blocks():
            for panel_id in FORBIDDEN_PANEL_IDS:
                assert panel_id not in block

    def test_no_dynamic_creation_of_forbidden_panels_in_inline_scripts(self):
        for block in _script_blocks():
            for panel_id in FORBIDDEN_PANEL_IDS:
                assert panel_id not in block


# ---------------------------------------------------------------------------
# Forbidden ids nowhere else in the dashboard bundle
# ---------------------------------------------------------------------------


class TestForbiddenPanelsAbsentFromBundle:
    def test_app_js_has_no_hyps_reference(self):
        assert "hyps" not in JS.lower()

    def test_app_js_has_no_orders_api_reference(self):
        assert "orders" not in JS.lower()

    def test_app_js_has_no_portfolio_reference(self):
        assert "portfolio" not in JS.lower()

    def test_css_has_no_forbidden_panel_selectors(self):
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in CSS

    def test_combined_sources_have_no_forbidden_panel_string(self):
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in COMBINED


# ---------------------------------------------------------------------------
# Research face is intact
# ---------------------------------------------------------------------------


class TestResearchFacePresent:
    def test_required_panels_present_in_html(self):
        ids = _element_ids()
        for panel_id in REQUIRED_PANEL_IDS:
            assert panel_id in ids, f"{panel_id} panel missing from index.html"

    def test_html_file_is_nonempty_and_wellformed_enough(self):
        assert len(HTML) > 500
        assert "<html" in HTML.lower()
        assert "</html>" in HTML.lower()

    def test_title_present(self):
        assert re.search(r"<title>[^<]+</title>", HTML, flags=re.IGNORECASE)

    def test_title_does_not_say_ops_dashboard(self):
        m = re.search(r"<title>([^<]*)</title>", HTML, flags=re.IGNORECASE)
        assert m is not None
        assert "ops dashboard" not in m.group(1).lower()

    def test_dashboard_references_its_own_stylesheet(self):
        assert "styles.css" in HTML

    def test_dashboard_references_its_own_script(self):
        assert "app.js" in HTML


# ---------------------------------------------------------------------------
# No money / live betting endpoints reachable from the front end
# ---------------------------------------------------------------------------


class TestNoMoneyEndpointsInFrontEnd:
    def test_no_money_fragments_anywhere_in_bundle(self):
        for frag in MONEY_API_FRAGMENTS:
            assert frag not in COMBINED, f"money endpoint fragment leaked: {frag}"

    def test_no_executor_enable_call(self):
        assert "/executor/enable" not in JS
        assert "/executor/enable" not in HTML

    def test_no_live_hypotheses_face_or_poll(self):
        combined = HTML + JS
        assert "LIVE hypotheses" not in combined
        assert "api/hypotheses/live" not in JS

    def test_no_json_fetch_of_removed_apis(self):
        assert "jsonFetch(API.hyps)" not in JS
        assert "jsonFetch(API.orders)" not in JS
        assert "jsonFetch(API.portfolio)" not in JS

    def test_no_trading_mode_backdoor(self):
        assert "TRADING_MODE" not in JS
        assert "applyTradingMode" not in JS
        assert "trading=1" not in COMBINED

    def test_no_post_place_order_calls(self):
        # Any POST that mentions order placement is out of bounds.
        for m in re.finditer(r"(?:fetch|post|jsonFetch)\s*\(([^)]{0,200})\)", JS):
            assert "order" not in m.group(1).lower(), m.group(0)

    def test_no_websocket_money_streams(self):
        low = JS.lower()
        for frag in ("ws://", "wss://"):
            if frag in low:
                # WebSockets allowed only if they carry no order/portfolio topic.
                idx = 0
                while True:
                    idx = low.find(frag, idx)
                    if idx == -1:
                        break
                    window = low[idx : idx + 200]
                    for bad in ("order", "portfolio", "fill"):
                        assert bad not in window
                    idx += 1


# ---------------------------------------------------------------------------
# Fail-closed structural checks on the production gate itself
# ---------------------------------------------------------------------------


class TestProductionGateFailClosed:
    def _gate_files(self) -> list[Path]:
        roots = [REPO / "src", REPO / "tools"]
        return [
            p
            for root in roots
            if root.exists()
            for p in root.rglob("*.py")
            if "_PAPER_TRADE_SIGNAL_STATUSES" in p.read_text(errors="ignore")
        ]

    def test_paper_trade_statuses_source_exists(self):
        hits = self._gate_files()
        assert hits, "_PAPER_TRADE_SIGNAL_STATUSES definition not found in src/ or tools/"

    def test_paper_trade_statuses_never_contain_live(self):
        pattern = re.compile(
            r"_PAPER_TRADE_SIGNAL_STATUSES[^=]*=\s*[\[{(](.*?)[\]})]",
            re.DOTALL,
        )
        found = False
        for p in self._gate_files():
            text = p.read_text(errors="ignore")
            for m in pattern.finditer(text):
                found = True
                body = m.group(1)
                for token in re.findall(r"[\"']([a-z_]+)[\"']", body):
                    assert token != "live", (
                        f"{p.name}: 'live' must never be in "
                        "_PAPER_TRADE_SIGNAL_STATUSES"
                    )
        assert found, "could not parse _PAPER_TRADE_SIGNAL_STATUSES literal"

    def test_generate_paper_trade_signal_not_widened_to_live(self):
        roots = [r for r in (REPO / "src", REPO / "tools") if r.exists()]
        for root in roots:
          for p in root.rglob("*.py"):
            text = p.read_text(errors="ignore")
            if "def generate_paper_trade_signal" in text:
                # Within the function's neighborhood, an equality check
                # against status=='live' would widen the paper-trade gate.
                fn_start = text.index("def generate_paper_trade_signal")
                window = text[fn_start : fn_start + 4000]
                assert "status == 'live'" not in window
                assert 'status == "live"' not in window


# ---------------------------------------------------------------------------
# HTML structure sanity — the research face is real markup, not stubs
# ---------------------------------------------------------------------------


class TestHtmlStructureSanity:
    def test_every_declared_id_is_unique(self):
        ids = re.findall(r'id\s*=\s*["\']([^"\']+)["\']', HTML)
        assert len(ids) == len(set(ids)), "duplicate element ids in index.html"

    def test_no_inline_event_handlers_wired_to_removed_panels(self):
        for handler in re.findall(r'on[a-z]+\s*=\s*["\']([^"\']*)["\']', HTML):
            for panel_id in FORBIDDEN_PANEL_IDS:
                assert panel_id not in handler

    def test_class_names_do_not_reference_removed_panels(self):
        for cls in _class_names():
            for panel_id in FORBIDDEN_PANEL_IDS:
                assert panel_id not in cls

    def test_no_commented_out_trading_markup_left_behind(self):
        comments = re.findall(r"<!--(.*?)-->", HTML, flags=re.DOTALL)
        for c in comments:
            for panel_id in FORBIDDEN_PANEL_IDS:
                assert panel_id not in c, (
                    f"commented-out {panel_id} markup left in index.html"
                )

    def _resolve_asset(self, ref: str) -> Path | None:
        """Resolve a local asset reference; None means external/unresolvable."""
        if ref.startswith(("http://", "https://", "//", "#")):
            return None
        path = ref.split("?")[0].split("#")[0].lstrip("/")
        if path.startswith("static/"):
            path = path[len("static/") :]
        if not path:
            return None
        candidates = [DASHBOARD / path, DASHBOARD.parent / path]
        for target in candidates:
            if target.resolve().exists():
                return target
        return candidates[-1]

    def test_head_links_resolve_to_existing_files(self):
        for href in re.findall(r'<link[^>]+href=["\']([^"\']+)["\']', HTML):
            target = self._resolve_asset(href)
            if target is None:
                continue
            assert target.exists(), f"stylesheet/link target missing: {href}"

    def test_script_srcs_resolve_to_existing_files(self):
        for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', HTML):
            target = self._resolve_asset(src)
            if target is None:
                continue
            assert target.exists(), f"script target missing: {src}"


# ---------------------------------------------------------------------------
# app.js characterization — polling only research endpoints
# ---------------------------------------------------------------------------


class TestAppJsResearchPolling:
    def test_app_js_nonempty(self):
        assert len(JS) > 100

    def test_api_map_has_no_money_keys(self):
        # If app.js declares an API map/object, none of its keys may be
        # hyps/orders/portfolio.
        for key in re.findall(r"\b(hyps|orders|portfolio)\s*:", JS):
            assert False, f"API key '{key}' reintroduced in app.js"

    def test_no_setinterval_polling_of_money_paths(self):
        for m in re.finditer(r"setInterval\s*\(", JS):
            start = m.start()
            window = JS[start : start + 400]
            for frag in MONEY_API_FRAGMENTS:
                assert frag not in window

    def test_status_labels_do_not_advertise_live_betting(self):
        low = JS.lower()
        assert "place bet" not in low
        assert "submit order" not in low
        assert "live trading" not in low
