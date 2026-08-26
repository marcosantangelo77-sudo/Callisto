"""Pin: the dashboard's default face has NO live trading panels.

The research-face migration (see tests/test_dashboard_research_face.py)
deleted the hypotheses / orders / portfolio panels from
web/dashboard/index.html.  This module pins the deletion so that:

  * nobody re-adds ``panel-hyps``, ``panel-orders`` or ``panel-portfolio``
    to the HTML (even hidden behind CSS or a query-string flag),
  * no JS resurrects the money endpoints they used to poll,
  * the remaining panels stay exactly the three research panels.

Everything here reads the dashboard files as text — no browser, no server,
no network.  These are source-contract tests: cheap, deterministic and
runnable in any sandbox.

Run with:
    /tmp/callisto-pytest/bin/python -m pytest tests/test_dashboard_char2.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

DASHBOARD = Path(__file__).resolve().parent.parent / "web" / "dashboard"
HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8")
COMBINED = HTML + "\n" + JS
LOW = COMBINED.lower()

# The three live-trading panel ids that were deleted from index.html.
BANNED_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

# The research panels that ARE allowed (and required) to exist.
REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Money/live endpoint fragments that must not appear anywhere.
BANNED_ENDPOINTS = (
    "api/hypotheses",
    "api/orders",
    "api/portfolio",
    "api/hyps",
    "/live",
)

# JS identifiers that belonged to the deleted trading face.
BANNED_JS_IDENTIFIERS = (
    "API.hyps",
    "API.orders",
    "API.portfolio",
    "renderHypotheses",
    "renderOrders",
    "renderPortfolio",
    "applyTradingMode",
    "TRADING_MODE",
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _id_attributes(text: str) -> list[str]:
    """Every id="..." value appearing anywhere in the text."""
    return re.findall(r'id\s*=\s*["\']([^"\']+)["\']', text)


def _assert_absent(needle: str, haystack: str, where: str) -> None:
    assert needle not in haystack, (
        f"{needle!r} must not appear in {where}; "
        "the live-trading face was deleted on purpose — do not reintroduce it"
    )


# ---------------------------------------------------------------------------
# HTML: banned panels are gone for good
# ---------------------------------------------------------------------------

class TestBannedPanelsAbsentFromHtml:
    def test_no_panel_hyps_id(self):
        _assert_absent('id="panel-hyps"', HTML, "index.html")

    def test_no_panel_orders_id(self):
        _assert_absent('id="panel-orders"', HTML, "index.html")

    def test_no_panel_portfolio_id(self):
        _assert_absent('id="panel-portfolio"', HTML, "index.html")

    def test_banned_ids_not_even_in_comments_or_css(self):
        # Hidden-via-comment or hidden-via-CSS is still a regression vector;
        # pin against the bare token anywhere in the file.
        for pid in BANNED_PANEL_IDS:
            assert pid not in HTML, (
                f"{pid} referenced somewhere in index.html (comment/CSS/attr?)"
            )

    def test_no_dynamic_id_construction_of_banned_panels(self):
        # e.g. id="panel-" + name  or  getElementById('panel-' + key)
        dynamic = re.findall(r'["\']panel-["\']\s*\+', HTML + JS)
        assert not dynamic, (
            "dynamic 'panel-' + x id construction found; "
            "panels must be statically declared"
        )


# ---------------------------------------------------------------------------
# HTML: required research panels still present
# ---------------------------------------------------------------------------

class TestResearchFaceIntact:
    def test_required_panels_present(self):
        ids = set(_id_attributes(HTML))
        for pid in REQUIRED_PANEL_IDS:
            assert pid in ids, f"{pid} missing from index.html"

    def test_every_declared_panel_id_is_a_known_research_panel(self):
        known = set(REQUIRED_PANEL_IDS)
        unknown = [i for i in _id_attributes(HTML) if i.startswith("panel-") and i not in known]
        assert not unknown, f"unexpected panel ids in index.html: {unknown}"

    def test_offline_banner_and_online_pill_survive(self):
        ids = set(_id_attributes(HTML))
        assert "offline-banner" in ids
        assert "online-pill" in ids


# ---------------------------------------------------------------------------
# JS: money endpoints and identifiers stay dead
# ---------------------------------------------------------------------------

class TestJsMoneyEndpointsDead:
    def test_no_hypotheses_endpoint(self):
        for ep in ("api/hypotheses", "api/hyps"):
            _assert_absent(ep, JS, "app.js")

    def test_no_orders_endpoint(self):
        _assert_absent("api/orders", JS, "app.js")

    def test_no_portfolio_endpoint(self):
        _assert_absent("api/portfolio", JS, "app.js")

    def test_api_object_has_only_safe_keys(self):
        m = re.search(r"const\s+API\s*=\s*\{(.*?)\}", JS, re.S)
        assert m, "could not locate API object literal in app.js"
        keys = set(re.findall(r"(\w+)\s*:", m.group(1)))
        banned = {"hyps", "orders", "portfolio", "hypothesesLive"}
        assert not (keys & banned), f"banned API keys present: {sorted(keys & banned)}"

    def test_no_fetch_of_money_routes(self):
        for frag in ('jsonFetch(API.orders)', 'jsonFetch(API.portfolio)', "jsonFetch(API.hyps)"):
            _assert_absent(frag, JS, "app.js")


class TestJsTradingIdentifiersDead:
    def test_banned_identifiers_absent(self):
        for ident in BANNED_JS_IDENTIFIERS:
            _assert_absent(ident, JS, "app.js")

    def test_no_trading_querystring_backdoor(self):
        for frag in ("trading=1", "?trading", "searchParams.get('trading'", 'searchParams.get("trading"'):
            _assert_absent(frag, LOW, "dashboard sources (case-insensitive)")

    def test_no_render_functions_for_deleted_panels(self):
        for fn in ("renderHyps", "renderOrders", "renderPortfolio"):
            _assert_absent(fn, JS, "app.js")


# ---------------------------------------------------------------------------
# Whole-file sweep
# ---------------------------------------------------------------------------

class TestCombinedSourceContract:
    def test_word_live_not_used_as_face_label(self):
        # 'LIVE hypotheses' style labels are gone; allow incidental words like
        # 'delivered' but forbid standalone LIVE branding.
        assert not re.search(r"\bLIVE\b", COMBINED), (
            "standalone 'LIVE' label found in dashboard sources"
        )

    def test_no_hidden_display_none_on_banned_names(self):
        # even CSS rules targeting the dead ids are forbidden
        for pid in BANNED_PANEL_IDS:
            assert f"#{pid}" not in HTML + JS

    def test_title_is_research_facing(self):
        m = re.search(r"<title>(.*?)</title>", HTML, re.S | re.I)
        assert m, "no <title> in index.html"
        title = m.group(1).strip()
        assert "ops" not in title.lower(), f"title regressed to ops framing: {title!r}"
        assert "trading" not in title.lower()

    def test_dashboard_dir_contains_expected_files(self):
        files = {p.name for p in DASHBOARD.iterdir()}
        assert {"index.html", "app.js"} <= files

    def test_files_nonempty(self):
        assert len(HTML) > 200, "index.html suspiciously small"
        assert len(JS) > 200, "app.js suspiciously small"
