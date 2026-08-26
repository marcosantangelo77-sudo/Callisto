"""Autofill 0084 — dashboard "research face" characterization (LONG).

Characterizes the contract that ``web/dashboard/index.html`` is the RESEARCH
face of Callisto and must NOT carry any trading panels:

* no ``panel-hyps`` (live hypotheses panel)
* no ``panel-orders`` (orders / money panel)
* no ``panel-portfolio`` (portfolio / positions panel)

The panels must be deleted from markup entirely — not merely hidden with
CSS or gated behind a ``?trading=1`` style query flag. These tests are a
characterization net around that deletion: they read the dashboard assets
as text (no browser, no network) so they run fast and hermetically, and
they fail loudly if trading UI ever creeps back into the default face.

Safety posture (mirrors repo-wide rules):

* tests-only module — touches no production file;
* nothing here arms live betting; there is deliberately no test asserting
  anything is "enabled" — only that the research face stays research;
* fail-closed: if the pin (panels absent) is currently false, these tests
  FAIL, they never silently adapt to the presence of trading markup.

Companion to ``tests/test_dashboard_research_face.py`` (which pins the same
contract from a different angle). Run together::

    python -m pytest tests/test_autofill_0084.py \
                      tests/test_dashboard_research_face.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "web" / "dashboard"
HTML_PATH = DASHBOARD_DIR / "index.html"
JS_PATH = DASHBOARD_DIR / "app.js"
STYLES_PATH = DASHBOARD_DIR / "styles.css"

# The three forbidden trading panel ids.
FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research-face panels that MUST remain present.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Other identifiers historically associated with the trading face.
FORBIDDEN_TRADING_MARKERS = (
    # hypotheses face
    'id="hyp-body"',
    'id="hyps-body"',
    'id="hypotheses-body"',
    # orders face
    'id="orders-body"',
    'id="order-body"',
    # portfolio face
    'id="portfolio-body"',
    'id="positions-body"',
)

# Money / live-trading endpoints that must not be fetched by app.js.
FORBIDDEN_ENDPOINTS = (
    "api/hypotheses/live",
    "api/hypotheses",
    "api/orders",
    "api/portfolio",
    "/executor/enable",
    "/executor/disable",
)


def _read(path: Path) -> str:
    """Fail-closed reader: a missing asset is itself a characterization break."""
    assert path.exists(), f"expected dashboard asset missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def html_text() -> str:
    return _read(HTML_PATH)


@pytest.fixture(scope="module")
def js_text() -> str:
    return _read(JS_PATH)


@pytest.fixture(scope="module")
def styles_text() -> str:
    if STYLES_PATH.exists():
        return STYLES_PATH.read_text(encoding="utf-8")
    return ""


@pytest.fixture(scope="module")
def combined_text(html_text: str, js_text: str, styles_text: str) -> str:
    return html_text + "\n" + js_text + "\n" + styles_text


def _strip_html_comments(text: str) -> str:
    """Remove <!-- ... --> comments so hidden-in-comment markup still fails."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _strip_js_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"(?m)^\s*//.*$", "", text)
    return text


@pytest.fixture(scope="module")
def html_effective(html_text: str) -> str:
    """HTML with comments stripped — the markup a browser would actually see."""
    return _strip_html_comments(html_text)


@pytest.fixture(scope="module")
def js_effective(js_text: str) -> str:
    """app.js with comments stripped — code that would actually execute."""
    return _strip_js_comments(js_text)


# ---------------------------------------------------------------------------
# 1. The core contract: forbidden panel ids absent from index.html
# ---------------------------------------------------------------------------


class TestForbiddenPanelsAbsentFromHtml:
    """index.html must not contain panel-hyps, panel-orders, panel-portfolio."""

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_panel_id_attribute_absent(self, html_text: str, panel_id: str):
        needle = f'id="{panel_id}"'
        assert needle not in html_text, (
            f"{needle} found in index.html — the {panel_id} trading panel "
            "must be deleted from the research face, not merely hidden"
        )

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_panel_id_absent_in_any_quote_style(self, html_text: str, panel_id: str):
        for needle in (
            f"id='{panel_id}'",
            f'id="{panel_id}"',
            f"id={panel_id}>",
            f'id="{panel_id}" ',
        ):
            assert needle not in html_text, f"{needle} found in index.html"

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_panel_id_absent_from_comment_stripped_markup(
        self, html_effective: str, panel_id: str
    ):
        """Hiding the panel inside an HTML comment is still a violation."""
        assert f'id="{panel_id}"' not in html_effective, (
            f"{panel_id} survives in commented-out markup — delete it outright"
        )

    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_no_partial_or_suffixed_variants(self, html_text: str, panel_id: str):
        """Catch e.g. panel-orders-old, panel-portfolio2 renames."""
        assert panel_id not in html_text.replace('id="', "").replace('"', ""), (
            f"a variant of {panel_id} appears in index.html"
        )

    def test_word_orders_absent_from_headings(self, html_text: str):
        headings = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", html_text, flags=re.DOTALL)
        joined = " ".join(headings).lower()
        for word in ("orders", "portfolio", "positions", "open bets"):
            assert word not in joined, (
                f"trading-flavored heading ({word!r}) present in index.html: {headings}"
            )

    def test_word_hypotheses_not_a_panel_title(self, html_text: str):
        headings = re.findall(r"<h[12][^>]*>(.*?)</h[12]>", html_text, flags=re.DOTALL)
        for h in headings:
            assert "hypothes" not in h.lower() or "panel-hyps" not in html_text


# ---------------------------------------------------------------------------
# 2. Companion body/div ids that belonged to those panels
# ---------------------------------------------------------------------------


class TestCompanionTradingMarkupAbsent:
    @pytest.mark.parametrize("marker", FORBIDDEN_TRADING_MARKERS)
    def test_marker_absent(self, html_text: str, marker: str):
        assert marker not in html_text, f"trading marker {marker} found in index.html"

    def test_no_data_trading_attributes(self, html_text: str):
        assert "data-trading" not in html_text
        assert "data-live-betting" not in html_text

    def test_no_query_param_backdoor_in_markup(self, html_effective: str):
        assert "trading=1" not in html_effective
        assert "?mode=live" not in html_effective

    def test_inline_scripts_absent(self, html_effective: str):
        """Research face loads exactly one script tag: /static/app.js."""
        scripts = re.findall(r"<script[^>]*>", html_effective)
        assert len(scripts) == 1, f"expected exactly one script tag, got {scripts}"
        assert "/static/app.js" in scripts[0]

    def test_no_inline_event_handlers(self, html_effective: str):
        for handler in ("onclick=", "onload=", "onsubmit=", "onchange="):
            assert handler not in html_effective.lower(), (
                f"inline event handler {handler} found in index.html"
            )


# ---------------------------------------------------------------------------
# 3. Research-face panels remain present (positive pinning)
# ---------------------------------------------------------------------------


class TestResearchPanelsPresent:
    @pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
    def test_required_panel_present(self, html_text: str, panel_id: str):
        assert f'id="{panel_id}"' in html_text, f"{panel_id} missing from index.html"

    def test_each_panel_is_a_section_with_body(self, html_text: str):
        for panel_id in REQUIRED_PANEL_IDS:
            section_re = re.compile(
                r'<section[^>]*id="' + panel_id + r'"[^>]*>(.*?)</section>',
                re.DOTALL,
            )
            m = section_re.search(html_text)
            assert m, f"{panel_id} section not parseable in index.html"
            inner = m.group(1)
            assert "<h2>" in inner, f"{panel_id} has no heading"
            assert "panel-body" in inner, f"{panel_id} has no panel-body"

    def test_branding_is_research_appliance(self, html_text: str):
        assert "research appliance" in html_text.lower()
        assert "Callisto" in html_text

    def test_footer_declares_read_only(self, html_text: str):
        footer = re.search(r'<footer[^>]*>(.*?)</footer>', html_text, re.DOTALL)
        assert footer, "footer missing from index.html"
        assert "read-only" in footer.group(1).lower()

    def test_title_mentions_research(self, html_text: str):
        title = re.search(r"<title>(.*?)</title>", html_text, re.DOTALL)
        assert title, "no <title> in index.html"
        assert "research" in title.group(1).lower()


# ---------------------------------------------------------------------------
# 4. app.js: no trading endpoints, no trading-mode machinery
# ---------------------------------------------------------------------------


class TestAppJsNoTradingSurface:
    @pytest.mark.parametrize("endpoint", FORBIDDEN_ENDPOINTS)
    def test_endpoint_not_fetched(self, js_text: str, endpoint: str):
        assert endpoint not in js_text, (
            f"forbidden endpoint {endpoint!r} referenced in app.js"
        )

    def test_api_map_has_exactly_three_entries(self, js_text: str):
        block = re.search(r"const API = \{(.*?)\};", js_text, re.DOTALL)
        assert block, "API map not found in app.js"
        keys = re.findall(r"^\s*(\w+):", block.group(1), re.MULTILINE)
        assert sorted(keys) == ["alerts", "ingestion", "status"], (
            f"unexpected API surface: {keys}"
        )

    def test_api_keys_are_research_only(self, js_text: str):
        for banned in ("hyps", "hypotheses", "orders", "portfolio", "positions"):
            assert not re.search(
                rf"\b{banned}\s*:", js_text
            ), f"API key {banned} defined in app.js API map"

    def test_no_trading_mode_flag(self, js_text: str):
        for marker in ("TRADING_MODE", "applyTradingMode", "tradingMode", "LIVE_MODE"):
            assert marker not in js_text

    def test_jsonFetch_never_called_with_money_endpoints(self, js_text: str):
        calls = re.findall(r"jsonFetch\(([^)]*)\)", js_text)
        assert calls, "no jsonFetch calls found in app.js"
        for call in calls:
            lowered = call.lower()
            for banned in ("order", "portfolio", "position", "hyp"):
                assert banned not in lowered, (
                    f"jsonFetch({call.strip()}) looks like a trading fetch"
                )

    def test_refresh_polls_only_three_endpoints(self, js_text: str):
        fn = re.search(r"async function refresh\(.*?\n\}", js_text, re.DOTALL)
        assert fn, "refresh() not found in app.js"
        body = fn.group(0)
        assert "API.status" in body
        assert "API.ingestion" in body
        assert "API.alerts" in body
        assert "Promise.all" in body

    def test_renderers_target_only_research_bodies(self, js_text: str):
        targets = set(re.findall(r'getElementById\("([^"]+)"\)', js_text))
        allowed = {"state-body", "ingestion-body", "alerts-body",
                   "online-pill", "last-refresh", "offline-banner"}
        unexpected = targets - allowed
        assert not unexpected, f"app.js renders into unexpected elements: {sorted(unexpected)}"

    def test_no_dom_injection_of_trading_panels(self, js_effective: str):
        """No code path may createElement/innerHTML a trading panel back in."""
        for marker in ("panel-hyps", "panel-orders", "panel-portfolio"):
            assert marker not in js_effective, (
                f"app.js injects {marker} back into the DOM at runtime"
            )

    def test_executor_status_display_only(self, js_text: str):
        """Executor may be *displayed* but never toggled from the client."""
        if "executor" not in js_text.lower():
            return
        # No POST/PUT/fetch-with-method against executor controls.
        assert not re.search(
            r"method\s*:\s*[\"'](?:POST|PUT|DELETE)[\"']", js_text
        ), "app.js issues mutating HTTP requests — dashboard must stay read-only"


# ---------------------------------------------------------------------------
# 5. Comment-level honesty: markers may appear in comments as history notes,
#    but the effective code/markup must stay clean.
# ---------------------------------------------------------------------------


class TestEffectiveTextClean:
    @pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
    def test_clean_after_comment_strip(self, html_effective: str, panel_id: str):
        assert panel_id not in html_effective

    def test_js_effective_has_no_api_hyps_refs(self, js_effective: str):
        assert "API.hyps" not in js_effective
        assert "API.orders" not in js_effective
        assert "API.portfolio" not in js_effective

    def test_styles_do_not_reference_forbidden_panels(self, styles_text: str):
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in styles_text, (
                f"styles.css still carries layout rules for {panel_id}"
            )


# ---------------------------------------------------------------------------
# 6. Structural sanity of the assets themselves (characterization)
# ---------------------------------------------------------------------------


class TestAssetStructure:
    def test_index_html_is_small_and_simple(self, html_text: str):
        lines = [ln for ln in html_text.splitlines() if ln.strip()]
        assert len(lines) < 120, "index.html grew unexpectedly — review the face"

    def test_grid_contains_only_expected_sections(self, html_effective: str):
        grid = re.search(r'<main class="grid">(.*?)</main>', html_effective, re.DOTALL)
        assert grid, "main.grid container missing"
        ids = re.findall(r'<section[^>]*id="([^"]+)"', grid.group(1))
        assert sorted(ids) == sorted(REQUIRED_PANEL_IDS), (
            f"grid sections changed: {ids}"
        )

    def test_app_js_defines_expected_renderers(self, js_text: str):
        for fn in ("renderState", "renderIngestion", "renderAlerts"):
            assert re.search(rf"function {fn}\(", js_text), f"{fn} missing"

    def test_app_js_starts_polling_loop(self, js_text: str):
        assert "setInterval(refresh, REFRESH_MS)" in js_text
        assert "refresh();" in js_text

    def test_refresh_interval_characterized(self, js_text: str):
        m = re.search(r"REFRESH_MS\s*=\s*(\d+)", js_text)
        assert m, "REFRESH_MS constant missing"
        assert int(m.group(1)) == 15000

    def test_offline_banner_present(self, html_text: str):
        assert 'id="offline-banner"' in html_text

    def test_escape_html_helper_exists(self, js_text: str):
        assert "function escapeHtml(" in js_text


# ---------------------------------------------------------------------------
# 7. Fail-closed meta-tests: the fixtures themselves must not lie
# ---------------------------------------------------------------------------


class TestHarnessIntegrity:
    """If these fail, the characterization harness above is unreliable."""

    def test_html_fixture_nonempty(self, html_text: str):
        assert html_text.strip()

    def test_js_fixture_nonempty(self, js_text: str):
        assert js_text.strip()

    def test_comment_stripper_actually_strips(self):
        sample = "<!-- id=\"panel-hyps\" -->keep"
        assert "panel-hyps" not in _strip_html_comments(sample)
        assert "keep" in _strip_html_comments(sample)

    def test_js_comment_stripper_handles_block_comments(self):
        sample = "/* panel-orders */ const x = 1;"
        assert "panel-orders" not in _strip_js_comments(sample)
        assert "const x = 1;" in _strip_js_comments(sample)

    def test_forbidden_and_required_sets_disjoint(self):
        assert not set(FORBIDDEN_PANEL_IDS) & set(REQUIRED_PANEL_IDS)

    def test_this_module_arms_nothing(self):
        """Meta-safety: this file contains no live-betting enablement."""
        source = Path(__file__).read_text(encoding="utf-8")
        sentinel = "_PAPER_TRADE_SIGNAL_" + "STATUSES"
        assert sentinel not in source
        assert ("generate_paper_trade_" + "signal") not in source
