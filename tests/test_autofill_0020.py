"""Autofill characterization #0020 — dashboard research face (LONG).

Characterizes the structural contract of ``web/dashboard/index.html`` and its
companion assets: the dashboard's default face is a READ-ONLY research
appliance, not a live betting / trading console.

Core invariant (fail-closed): the HTML must not contain the trading panel
markup ``panel-hyps``, ``panel-orders`` or ``panel-portfolio`` — deleted, not
merely hidden. Every test here reads files as text; no browser, no network,
no arming of live betting. If any pin below is currently false, the tests
FAIL CLOSED (loudly red) rather than papering over a live-betting surface.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DASHBOARD = REPO / "web" / "dashboard"

HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"

# Fail closed if the fixture files are missing entirely — a missing file must
# never be interpreted as "contract satisfied".
if not HTML_PATH.exists():  # pragma: no cover - guard for broken worktrees
    raise AssertionError(f"missing required file: {HTML_PATH}")

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8") if JS_PATH.exists() else ""
COMBINED = HTML + "\n" + JS
COMBINED_LOWER = COMBINED.lower()

# The three banned trading panel ids (the headline pin of this module).
BANNED_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# Research-face panels that MUST remain present.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Money / live-trading endpoints that a research face must never poll.
MONEY_ENDPOINT_FRAGMENTS = (
    "/api/orders",
    "/api/portfolio",
    "/api/hypotheses/live",
    "api/hypotheses/live",
    "/executor/enable",
    "/executor/disable",
)

# Trading-mode backdoor identifiers.
TRADING_BACKDOOR_TOKENS = (
    "TRADING_MODE",
    "applyTradingMode",
    "trading=1",
    "tradingMode",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_comments(html: str) -> str:
    """Remove HTML comments so 'hidden in a comment' still counts as absent."""
    return re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)


def _visible_html() -> str:
    return _strip_comments(HTML)


def _all_id_attributes() -> list[str]:
    return re.findall(r'id="([^"]+)"', _visible_html())


def _section_blocks() -> dict[str, str]:
    """Map each <section ... id="X"> opening tag to its full markup."""
    blocks: dict[str, str] = {}
    for match in re.finditer(
        r"<section\b[^>]*\bid=\"([^\"]+)\"[^>]*>.*?</section>",
        _visible_html(),
        flags=re.DOTALL,
    ):
        blocks[match.group(1)] = match.group(0)
    return blocks


def _script_srcs() -> list[str]:
    return re.findall(r"<script[^>]*\bsrc=\"([^\"]+)\"", _visible_html())


def _stylesheet_srcs() -> list[str]:
    return re.findall(r"<link[^>]*\bhref=\"([^\"]+)\"", _visible_html())


def _count_occurrences(haystack: str, needle: str) -> int:
    return haystack.count(needle)


# ---------------------------------------------------------------------------
# 1. Headline pin: trading panels absent from index.html
# ---------------------------------------------------------------------------


class TestTradingPanelsAbsent:
    def test_panel_hyps_absent(self):
        assert 'id="panel-hyps"' not in HTML

    def test_panel_orders_absent(self):
        assert 'id="panel-orders"' not in HTML

    def test_panel_portfolio_absent(self):
        assert 'id="panel-portfolio"' not in HTML

    def test_all_banned_panel_ids_absent_at_once(self):
        present = [p for p in BANNED_PANEL_IDS if f'id="{p}"' in HTML]
        assert present == [], (
            f"trading panels re-introduced into index.html: {present}; "
            "they must be deleted, not merely hidden"
        )

    def test_banned_ids_not_even_in_comments(self):
        # Deleted means deleted: hiding inside an HTML comment is a violation.
        for panel_id in BANNED_PANEL_IDS:
            assert f'id="{panel_id}"' not in _visible_html(), (
                f"{panel_id} survives only as commented-out markup"
            )

    def test_banned_ids_nowhere_in_combined_assets(self):
        for panel_id in BANNED_PANEL_IDS:
            assert panel_id not in COMBINED_LOWER, (
                f"{panel_id} referenced somewhere in index.html/app.js"
            )

    def test_no_hyp_orders_portfolio_words_as_ids(self):
        ids = [i.lower() for i in _all_id_attributes()]
        for token in ("hyps", "orders", "portfolio"):
            offending = [i for i in ids if token in i]
            assert offending == [], (
                f"id attributes containing '{token}': {offending}"
            )

    def test_no_data_panel_attribute_aliasing_trading_panels(self):
        # Guard against renames like data-panel="orders".
        assert not re.search(
            r'data-panel="(hyps|orders|portfolio)"', _visible_html()
        )


# ---------------------------------------------------------------------------
# 2. Research panels remain present and well-formed
# ---------------------------------------------------------------------------


class TestResearchPanelsPresent:
    def test_required_panels_present(self):
        missing = [
            p for p in REQUIRED_PANEL_IDS if f'id="{p}"' not in _visible_html()
        ]
        assert missing == [], f"research panels missing from index.html: {missing}"

    def test_each_research_panel_is_a_section_with_class_panel(self):
        blocks = _section_blocks()
        for panel_id in REQUIRED_PANEL_IDS:
            block = blocks.get(panel_id)
            assert block is not None, f"{panel_id} section not found"
            assert 'class="panel' in block or "class='panel" in block, (
                f"{panel_id} lost its .panel class"
            )
            assert "<h2>" in block, f"{panel_id} has no heading"

    def test_each_research_panel_has_a_body_placeholder(self):
        expected_bodies = {
            "panel-state": "state-body",
            "panel-ingestion": "ingestion-body",
            "panel-alerts": "alerts-body",
        }
        for panel_id, body_id in expected_bodies.items():
            assert f'id="{body_id}"' in HTML, (
                f"{panel_id} lost its body placeholder {body_id}"
            )

    def test_exactly_three_panels_on_the_grid(self):
        sections = _section_blocks()
        unexpected = set(sections) - set(REQUIRED_PANEL_IDS)
        assert unexpected == set(), (
            f"unexpected extra panels on the dashboard grid: {sorted(unexpected)}"
        )

    def test_wide_layout_class_only_where_expected(self):
        wide = {
            m.group(1)
            for m in re.finditer(
                r"<section\b(?=[^>]*\bid=\"([^\"]+)\")"
                r"(?=[^>]*class=\"[^\"]*\bwide\b)",
                _visible_html(),
            )
        }
        assert wide == {"panel-ingestion"}, (
            f"wide layout applied to unexpected panels: {sorted(wide)}"
        )


# ---------------------------------------------------------------------------
# 3. No money / executor endpoints anywhere on the research face
# ---------------------------------------------------------------------------


class TestNoMoneyEndpoints:
    def test_no_money_endpoint_fragments_in_html(self):
        for frag in MONEY_ENDPOINT_FRAGMENTS:
            assert frag not in HTML, (
                f"money endpoint fragment '{frag}' found in index.html"
            )

    def test_no_money_endpoint_fragments_in_js(self):
        for frag in MONEY_ENDPOINT_FRAGMENTS:
            assert frag not in JS, (
                f"money endpoint fragment '{frag}' found in app.js"
            )

    def test_app_js_does_not_fetch_orders_or_portfolio(self):
        assert "jsonFetch(API.orders)" not in JS
        assert "jsonFetch(API.portfolio)" not in JS

    def test_app_js_has_no_api_keys_for_trading_surfaces(self):
        for key in ("API.hyps", "API.orders", "API.portfolio"):
            assert key not in JS

    def test_no_executor_enable_reference(self):
        assert "/executor/enable" not in JS
        assert "/executor/enable" not in HTML

    def test_no_live_word_used_as_trading_descriptor_in_visible_markup(self):
        # Comments may mention "not live betting" as an explanatory guard;
        # the rendered markup itself must not advertise a live face.
        assert "LIVE hypotheses" not in _visible_html()
        assert "live betting" not in _visible_html().lower()


# ---------------------------------------------------------------------------
# 4. No trading-mode backdoor
# ---------------------------------------------------------------------------


class TestNoTradingBackdoor:
    def test_no_trading_mode_toggle_token(self):
        for token in TRADING_BACKDOOR_TOKENS:
            assert token not in COMBINED, (
                f"trading backdoor token '{token}' present"
            )

    def test_no_query_string_flag_checking_in_js(self):
        assert "URLSearchParams" not in JS
        assert "location.search" not in JS

    def test_no_conditional_panel_injection_helpers(self):
        for helper in (
            "injectPanel",
            "renderOrders",
            "renderPortfolio",
            "renderHypotheses",
            "showTrading",
        ):
            assert helper not in JS, (
                f"conditional trading renderer '{helper}' present in app.js"
            )

    def test_no_dynamic_script_or_style_injection_of_trading_ui(self):
        assert not re.search(r"createElement\(['\"]section", JS), (
            "app.js dynamically creates <section> elements (panel injection)"
        )
        assert "innerHTML += '<section" not in JS


# ---------------------------------------------------------------------------
# 5. Branding / framing stays "research appliance"
# ---------------------------------------------------------------------------


class TestResearchFraming:
    def test_title_is_research_appliance(self):
        match = re.search(r"<title>(.*?)</title>", HTML, flags=re.DOTALL)
        assert match is not None, "index.html has no <title>"
        title = match.group(1).lower()
        assert "callisto" in title
        assert "research" in title
        assert "trading" not in title
        assert "betting" not in title

    def test_brand_sub_labels_it_research(self):
        assert "research appliance" in HTML.lower()

    def test_no_ops_dashboard_language(self):
        assert "ops dashboard" not in COMBINED_LOWER
        assert "operations dashboard" not in COMBINED_LOWER

    def test_footer_declares_read_only(self):
        footer = re.search(
            r'<footer class="footer">.*?</footer>', HTML, flags=re.DOTALL
        )
        assert footer is not None, "footer disappeared from index.html"
        assert "read-only" in footer.group(0).lower()

    def test_loop_health_framing_not_live_framing(self):
        heading_block = re.search(
            r'id="panel-state".*?<h2>(.*?)</h2>', HTML, flags=re.DOTALL
        )
        assert heading_block is not None, "panel-state has no heading"
        heading = heading_block.group(1).lower()
        assert "loop" in heading or "system" in heading
        assert "live" not in heading


# ---------------------------------------------------------------------------
# 6. Document structure sanity
# ---------------------------------------------------------------------------


class TestDocumentStructure:
    def test_valid_doctype_and_root(self):
        assert HTML.lstrip().lower().startswith("<!doctype html>")
        assert '<html lang="en">' in HTML

    def test_single_main_grid(self):
        assert HTML.count("<main") == 1
        assert 'class="grid"' in HTML

    def test_balanced_section_tags(self):
        assert _visible_html().count("<section") == _visible_html().count(
            "</section>"
        ), "unbalanced <section> tags in index.html"

    def test_script_tag_references_static_app_js(self):
        srcs = _script_srcs()
        assert srcs == ["/static/app.js"], (
            f"unexpected script sources: {srcs}"
        )

    def test_stylesheet_reference_present(self):
        assert any("styles.css" in s for s in _stylesheet_srcs()), (
            "styles.css link missing from index.html"
        )

    def test_offline_banner_exists_for_db_fallback_messaging(self):
        assert 'id="offline-banner"' in HTML
        assert "DB-fallback" in HTML

    def test_online_pill_and_refresh_indicator_present(self):
        assert 'id="online-pill"' in HTML
        assert 'id="last-refresh"' in HTML

    def test_build_info_slot_present(self):
        assert 'id="build-info"' in HTML

    def test_no_inline_event_handlers(self):
        assert not re.search(r"\son(click|load|error)=", _visible_html()), (
            "inline event handlers reintroduced into index.html"
        )

    def test_no_inline_script_blocks(self):
        visible = _visible_html()
        inline_scripts = re.findall(r"<script(?![^>]*src=)[^>]*>", visible)
        assert inline_scripts == [], (
            f"inline <script> blocks found: {inline_scripts}"
        )


# ---------------------------------------------------------------------------
# 7. Companion asset characterization (best-effort when app.js exists)
# ---------------------------------------------------------------------------

POLL_INTERVAL_SECONDS = 15


class TestCompanionAssets:
    def test_app_js_exists_alongside_index(self):
        assert JS_PATH.exists(), "web/dashboard/app.js missing"

    def test_app_js_poll_interval_matches_footer_claim(self):
        if "setInterval" in JS:
            # Delay may be a named constant (e.g. REFRESH_MS); accept either.
            matches = re.findall(r"setInterval\([^,]+,\s*([^)]+?)\s*\)", JS)
            assert matches, "setInterval call missing its delay argument"
            resolved: list[int] = []
            for expr in matches:
                if expr.isdigit():
                    resolved.append(int(expr))
                else:
                    const = re.search(
                        rf"\b{re.escape(expr)}\s*=\s*(\d+)", JS
                    )
                    assert const is not None, (
                        f"setInterval delay '{expr}' is not numeric and not "
                        "resolvable to a numeric constant in app.js"
                    )
                    resolved.append(int(const.group(1)))
            ms = max(resolved)
            assert ms >= POLL_INTERVAL_SECONDS * 1000 * 0.5, (
                f"polling faster than half the advertised {POLL_INTERVAL_SECONDS}s "
                f"cadence: {ms}ms"
            )

    def test_app_js_targets_the_documented_body_ids(self):
        if JS:
            for body_id in ("state-body", "ingestion-body", "alerts-body"):
                assert body_id in JS, f"app.js no longer drives #{body_id}"

    def test_app_js_has_no_websocket_to_executor(self):
        assert "ws://" not in JS and "wss://" not in JS

    def test_no_credentials_hardcoded_in_dashboard_assets(self):
        secretish = re.findall(
            r"(?i)(password|api[_-]?key|secret|token)\s*[:=]\s*['\"][^'\"]+['\"]",
            COMBINED,
        )
        assert secretish == [], f"possible hardcoded secrets: {secretish}"


# ---------------------------------------------------------------------------
# 8. Meta characterization of this test module itself
# ---------------------------------------------------------------------------


class TestModuleSelfCheck:
    def test_banned_list_covers_exactly_the_specified_ids(self):
        assert BANNED_PANEL_IDS == (
            "panel-hyps",
            "panel-orders",
            "panel-portfolio",
        )

    def test_fixture_load_is_nonempty(self):
        assert len(HTML) > 500, "index.html suspiciously small; check worktree"

    def test_comment_stripper_works(self):
        assert _strip_comments("a<!-- id=\"panel-hyps\" -->b") == "ab"

    def test_every_required_panel_is_characterized(self):
        for panel_id in REQUIRED_PANEL_IDS:
            assert panel_id in _section_blocks()

    def test_dashboard_dir_contains_only_expected_asset_kinds(self):
        kinds = {p.suffix for p in DASHBOARD.iterdir() if p.is_file()}
        unexpected = kinds - {".html", ".js", ".css"}
        assert unexpected == set(), (
            f"unexpected asset kinds in web/dashboard/: {unexpected}"
        )
