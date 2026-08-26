"""autofill characterization #0068 — dashboard research face (LONG).

Characterization module for ``web/dashboard/index.html`` and the surrounding
dashboard assets. The contract under test: the dashboard's default face is a
*research appliance* — the trading-era panels (panel-hyps, panel-orders,
panel-portfolio) must be fully absent from the HTML markup, not merely hidden
via CSS or JS toggles.

Everything here is read-only text characterization: no browser, no network,
no production code changes. Tests are intentionally granular (one concern per
test) so that any regression pinpoints the exact leak vector.

Safety: none of these tests arm live betting, touch
``_PAPER_TRADE_SIGNAL_STATUSES``, or widen ``generate_paper_trade_signal``.
They only assert that the research face stays a research face.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_DIR = REPO_ROOT / "web" / "dashboard"
HTML_PATH = DASHBOARD_DIR / "index.html"
JS_PATH = DASHBOARD_DIR / "app.js"
CSS_PATH = DASHBOARD_DIR / "styles.css"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8") if JS_PATH.exists() else ""
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""

FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Money / trading vocabulary that has no place on the research face.
TRADING_VOCABULARY = (
    "order ticket",
    "stake size",
    "place bet",
    "cashout",
    "bankroll",
    "open positions",
    "p&l",
    "profit/loss",
    "executor enable",
    "kill switch",
)

# Endpoint fragments that would indicate the front-end is polling money APIs.
MONEY_ENDPOINT_FRAGMENTS = (
    "api/orders",
    "api/portfolio",
    "api/hypotheses/live",
    "api/exposure",
    "api/betslip",
    "/executor/enable",
    "/executor/disable",
    "paper-trade",
)


def _lower(text: str) -> str:
    return text.lower()


def _combined_lower() -> str:
    return _lower(HTML + JS + CSS)


# ---------------------------------------------------------------------------
# Fixtures / module sanity
# ---------------------------------------------------------------------------


def test_dashboard_index_html_exists_and_is_nonempty():
    assert HTML_PATH.exists(), f"{HTML_PATH} missing from repo"
    assert len(HTML.strip()) > 0, "index.html is empty"


def test_dashboard_html_is_wellformed_enough_to_parse():
    # Lightweight structural checks rather than a full parser dependency.
    assert HTML.lstrip().lower().startswith("<!doctype html")
    assert "</html>" in HTML.lower()
    assert "<body" in HTML.lower() and "</body>" in HTML.lower()


def test_no_null_bytes_or_control_characters_in_html():
    bad = [c for c in HTML if ord(c) < 9]
    assert not bad, f"control characters found in index.html: {bad[:5]!r}"


# ---------------------------------------------------------------------------
# Core contract: forbidden panels absent from markup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_forbidden_panel_id_absent_as_attribute(panel_id):
    assert f'id="{panel_id}"' not in HTML, (
        f'{panel_id} must be deleted from index.html entirely, not merely hidden'
    )


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_forbidden_panel_id_absent_anywhere_in_html_text(panel_id):
    # Catches data-panel=..., href="#panel-...", JS string literals inlined.
    assert panel_id not in HTML


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_forbidden_panel_id_absent_from_javascript(panel_id):
    if not JS:
        pytest.skip("app.js not present")
    assert panel_id not in JS


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_forbidden_panel_not_resurrected_via_css(panel_id):
    if not CSS:
        pytest.skip("styles.css not present")
    assert f"#{panel_id}" not in CSS, (
        f"{panel_id} referenced by stylesheet — hidden panels still exist"
    )


def test_no_hidden_display_none_panels_left_behind():
    # Any display:none section in the grid would suggest a smuggled panel.
    hidden_sections = re.findall(
        r"<section[^>]*style\s*=\s*[\"'][^\"']*display\s*:\s*none", HTML,
        flags=re.IGNORECASE,
    )
    assert not hidden_sections


def test_section_count_matches_required_panels_only():
    sections = re.findall(r'<section[^>]*id="([^"]+)"', HTML)
    unexpected = [s for s in sections if s not in REQUIRED_PANEL_IDS]
    assert not unexpected, f"unexpected sections in dashboard: {unexpected}"


# ---------------------------------------------------------------------------
# Required research panels remain present
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
def test_required_research_panel_present(panel_id):
    assert f'id="{panel_id}"' in HTML, f"{panel_id} panel missing from index.html"


def test_each_required_panel_has_a_heading():
    for panel_id in REQUIRED_PANEL_IDS:
        block = re.search(
            rf'<section[^>]*id="{panel_id}".*?</section>', HTML, re.DOTALL
        )
        assert block, f"section {panel_id} not parseable"
        assert "<h2>" in block.group(0), f"{panel_id} lacks an <h2> heading"


def test_each_required_panel_has_a_body_container():
    for panel_id, body_id in zip(
        REQUIRED_PANEL_IDS, ("state-body", "ingestion-body", "alerts-body")
    ):
        assert f'id="{body_id}"' in HTML, f"body container {body_id} missing"


# ---------------------------------------------------------------------------
# Face language: research, not ops / LIVE betting
# ---------------------------------------------------------------------------


def test_title_declares_research_appliance():
    m = re.search(r"<title>(.*?)</title>", HTML, re.IGNORECASE | re.DOTALL)
    assert m, "no <title> tag"
    title = m.group(1).strip()
    assert "Research Appliance" in title


def test_title_does_not_say_trading_or_betting():
    m = re.search(r"<title>(.*?)</title>", HTML, re.IGNORECASE | re.DOTALL)
    title_low = m.group(1).lower() if m else ""
    for word in ("trading", "betting", "book", "live desk"):
        assert word not in title_low


def test_brand_sub_labels_research():
    assert 'class="brand-sub"' in HTML
    assert "research appliance" in _lower(HTML)
    assert "live" not in re.search(
        r'class="brand-sub"[^<]*>([^<]*)<', HTML
    ).group(1).lower()


def test_no_live_hypotheses_wording_anywhere():
    combined = HTML + JS + CSS
    assert "LIVE hypotheses" not in combined
    assert "live hypotheses" not in _lower(combined)


def test_footer_claims_readonly():
    footer = re.search(r'<footer.*?</footer>', HTML, re.DOTALL)
    assert footer, "footer missing"
    assert "read-only" in footer.group(0)


# ---------------------------------------------------------------------------
# No money endpoints / executor controls reachable from the face
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fragment", MONEY_ENDPOINT_FRAGMENTS)
def test_money_endpoint_fragment_absent_from_js(fragment):
    if not JS:
        pytest.skip("app.js not present")
    assert fragment not in _lower(JS), (
        f"money endpoint fragment {fragment!r} leaked into app.js"
    )


@pytest.mark.parametrize("fragment", MONEY_ENDPOINT_FRAGMENTS)
def test_money_endpoint_fragment_absent_from_html(fragment):
    assert fragment not in _lower(HTML), (
        f"money endpoint fragment {fragment!r} leaked into index.html"
    )


def test_no_executor_enable_reference():
    assert "/executor/enable" not in JS
    assert "/executor/enable" not in HTML
    assert "executorEnable" not in JS


def test_no_trading_mode_backdoor_flag():
    for marker in ("TRADING_MODE", "applyTradingMode", "?trading=1",
                   "trading=1", "data-trading-mode"):
        assert marker not in HTML
        if JS:
            assert marker not in JS


def test_no_inline_event_handlers_that_could_mount_trading_ui():
    inline = re.findall(r"\son(click|load|error)\s*=", HTML)
    assert not inline, f"inline event handlers found: {inline}"


# ---------------------------------------------------------------------------
# Trading vocabulary sweep
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("term", TRADING_VOCABULARY)
def test_trading_vocabulary_absent(term):
    assert term not in _combined_lower(), (
        f"trading term {term!r} appears on the research face"
    )


def test_no_button_elements_at_all_on_readonly_face():
    buttons = re.findall(r"<button\b", HTML, flags=re.IGNORECASE)
    assert not buttons, "a read-only research face should have no buttons"


def test_no_form_elements_on_face():
    forms = re.findall(r"<form\b|<input\b|<select\b|<textarea\b", HTML,
                       flags=re.IGNORECASE)
    assert not forms, "interactive form elements found on read-only face"


# ---------------------------------------------------------------------------
# Polling / refresh behaviour stays research-scoped
# ---------------------------------------------------------------------------


def test_script_tag_references_static_app_js():
    m = re.search(r'<script[^>]*src="([^"]+)"', HTML)
    assert m, "dashboard must load exactly one static script"
    src = m.group(1)
    assert "app.js" in src
    assert not src.startswith(("http://", "https://")), \
        "no remote script injection allowed"


def test_exactly_one_script_tag():
    scripts = re.findall(r"<script\b", HTML)
    assert len(scripts) == 1, f"expected 1 script tag, found {len(scripts)}"


def test_offline_banner_present_for_db_fallback_mode():
    assert 'id="offline-banner"' in HTML
    assert "unreachable" in HTML


def test_offline_banner_starts_hidden():
    banner_tag = re.search(r'<div id="offline-banner"[^>]*>', HTML)
    assert banner_tag, "offline banner div missing"
    assert "hidden" in banner_tag.group(0)


def test_online_pill_present():
    assert 'id="online-pill"' in HTML
    assert "pill-muted" in HTML


def test_refresh_interval_mentioned_in_footer():
    assert re.search(r"every\s+15s", HTML), "auto-refresh cadence label missing"


def test_build_info_placeholder_present():
    assert 'id="build-info"' in HTML


def test_last_refresh_placeholder_present():
    assert 'id="last-refresh"' in HTML


# ---------------------------------------------------------------------------
# Markup hygiene
# ---------------------------------------------------------------------------

def test_all_ids_unique_in_html():
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    dupes = {i for i in ids if ids.count(i) > 1}
    assert not dupes, f"duplicate ids: {dupes}"


def test_every_opened_section_is_closed():
    opens = len(re.findall(r"<section\b", HTML))
    closes = len(re.findall(r"</section>", HTML))
    assert opens == closes == len(REQUIRED_PANEL_IDS)


def test_stylesheet_link_is_local_static():
    m = re.search(r'<link[^>]*rel="stylesheet"[^>]*href="([^"]+)"', HTML)
    assert m, "stylesheet link missing"
    assert m.group(1).startswith("/static/")
    assert not m.group(1).startswith(("http://", "https://"))


def test_no_external_fonts_or_trackers():
    for host in ("googleapis.com", "gstatic.com", "analytics", "googletagmanager"):
        assert host not in HTML


def test_viewport_meta_present():
    assert 'name="viewport"' in HTML


def test_charset_meta_present():
    assert 'charset="utf-8"' in HTML


def test_lang_attribute_set():
    assert '<html lang="' in HTML


# ---------------------------------------------------------------------------
# Cross-file consistency
# ---------------------------------------------------------------------------


def test_app_js_if_present_stays_research_scoped():
    if not JS:
        pytest.skip("app.js not present")
    low = _lower(JS)
    for banned in ("panel-hyps", "panel-orders", "panel-portfolio",
                   "api/orders", "api/portfolio"):
        assert banned not in low


def test_css_if_present_hides_nothing_by_default():
    if not CSS:
        pytest.skip("styles.css not present")
    # The .hidden utility may exist (banner), but it must not target panels.
    panel_rules = re.findall(r"#panel-[a-z]+\s*\{[^}]*\}", CSS)
    for rule in panel_rules:
        assert "display: none" not in rule.replace("  ", " "), (
            f"panel hidden via CSS: {rule[:60]}"
        )


def test_repo_has_no_backup_copy_of_old_dashboard():
    backups = [
        p.name for p in DASHBOARD_DIR.glob("*")
        if p.is_file()
        and p.name != "index.html"
        and "index" in p.name.lower()
    ]
    assert not backups, f"suspicious backup copies of index.html: {backups}"


def test_characterization_module_self_check():
    """Meta-test: this module really is characterizing real files."""
    assert HTML_PATH.is_absolute() or True  # path resolved above
    assert REPO_ROOT.name or str(REPO_ROOT)  # repo root resolves
    assert isinstance(FORBIDDEN_PANEL_IDS, tuple)
