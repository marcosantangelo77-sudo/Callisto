"""Autofill characterization #0012 — dashboard research face (LONG).

Characterizes the web dashboard's static contract as a RESEARCH face:

* ``web/dashboard/index.html`` must NOT contain the trading-era panels
  ``panel-hyps``, ``panel-orders`` or ``panel-portfolio`` — neither as
  element ids nor anywhere in markup, comments or inline scripts.
* The companion assets (app.js / styles.css) must not resurrect those
  panels through fetches, API constants, CSS rules or a trading-mode
  backdoor.
* The paper-trading gate in the Python source must stay closed:
  "live" must never appear in ``_PAPER_TRADE_SIGNAL_STATUSES`` and
  ``generate_paper_trade_signal`` must never widen to status=='live'.

All tests read files as text — no browser, no network, no live betting.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD = REPO_ROOT / "web" / "dashboard"
HTML_PATH = DASHBOARD / "index.html"
JS_PATH = DASHBOARD / "app.js"
CSS_PATH = DASHBOARD / "styles.css"

HTML = HTML_PATH.read_text(encoding="utf-8")
JS = JS_PATH.read_text(encoding="utf-8") if JS_PATH.exists() else ""
CSS = CSS_PATH.read_text(encoding="utf-8") if CSS_PATH.exists() else ""

FORBIDDEN_PANEL_IDS = ("panel-hyps", "panel-orders", "panel-portfolio")

REQUIRED_PANEL_IDS = ("panel-state", "panel-ingestion", "panel-alerts")

# Money/trading endpoints that belonged to the old LIVE face.
FORBIDDEN_ENDPOINT_FRAGMENTS = (
    "/api/hypotheses",
    "/api/orders",
    "/api/portfolio",
    "api/hypotheses/live",
    "/executor/enable",
)

# Identifiers that would indicate a trading-mode backdoor in app.js.
FORBIDDEN_JS_IDENTIFIERS = (
    "API.hyps",
    "API.hypotheses",
    "API.orders",
    "API.portfolio",
    "TRADING_MODE",
    "applyTradingMode",
    "renderHypotheses",
    "renderOrders",
    "renderPortfolio",
    "jsonFetch(API.orders)",
    "jsonFetch(API.portfolio)",
)


# ---------------------------------------------------------------------------
# 1. index.html: forbidden panel ids absent
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_id_absent_as_attribute(panel_id):
    assert f'id="{panel_id}"' not in HTML, (
        f'{panel_id} must be deleted from index.html, not merely hidden'
    )


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_id_absent_anywhere_in_html(panel_id):
    # Not just id= attributes: no comments, no data attrs, no inline JS.
    assert panel_id not in HTML, f"{panel_id} appears somewhere in index.html"


@pytest.mark.parametrize("panel_id", FORBIDDEN_PANEL_IDS)
def test_panel_fragment_not_in_css_or_js(panel_id):
    assert panel_id not in CSS, f"{panel_id} referenced from styles.css"
    assert panel_id not in JS, f"{panel_id} referenced from app.js"


def test_no_hidden_trading_panels_via_style():
    """No display:none section that smuggles back an old panel."""
    for match in re.finditer(r'<section[^>]*style="[^"]*display\s*:\s*none', HTML):
        snippet = match.group(0)
        for panel_id in FORBIDDEN_PANEL_IDS:
            assert panel_id not in snippet, (
                f"hidden section reintroduces {panel_id}"
            )


def test_html_section_ids_inventory_is_research_only():
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    overlap = ids & set(FORBIDDEN_PANEL_IDS)
    assert not overlap, f"trading-era ids present: {sorted(overlap)}"


# ---------------------------------------------------------------------------
# 2. index.html: required research face still intact
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
def test_required_panel_present(panel_id):
    assert f'id="{panel_id}"' in HTML, f"{panel_id} panel missing from index.html"


@pytest.mark.parametrize("panel_id", REQUIRED_PANEL_IDS)
def test_required_panel_has_body(panel_id):
    body_id = panel_id.replace("panel-", "") + "-body"
    expected = {
        "panel-state": "state-body",
        "panel-ingestion": "ingestion-body",
        "panel-alerts": "alerts-body",
    }[panel_id]
    assert expected == body_id or True
    assert f'id="{expected}"' in HTML, f"{panel_id} lacks its {expected} div"


def test_dashboard_files_exist_and_nonempty():
    for path in (HTML_PATH, JS_PATH, CSS_PATH):
        assert path.exists(), f"{path.name} missing"
        text = path.read_text(encoding="utf-8")
        assert len(text.strip()) > 0, f"{path.name} is empty"


# ---------------------------------------------------------------------------
# 3. Research-not-ops wording
# ---------------------------------------------------------------------------


def test_title_and_brand_are_research_not_ops_dashboard():
    combined = (HTML + JS).lower()
    assert "ops dashboard" not in combined


@pytest.mark.parametrize(
    "phrase",
    ["LIVE hypotheses", "live betting", "place orders", "order ticket"],
)
def test_no_live_face_phrases(phrase):
    # Strip HTML comments first: index.html legitimately says
    # "...research face..., not live betting" in a comment.
    html_no_comments = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
    combined = html_no_comments + JS + CSS
    assert phrase.lower() not in combined.lower(), (
        f"LIVE-face phrase {phrase!r} found in dashboard assets"
    )


def test_no_dollar_pnl_widgets_in_html():
    assert "pnl" not in HTML.lower()
    assert "bankroll" not in HTML.lower()


# ---------------------------------------------------------------------------
# 4. app.js: no money endpoints, no trading-mode backdoor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fragment", FORBIDDEN_ENDPOINT_FRAGMENTS)
def test_no_forbidden_endpoint_in_js(fragment):
    assert fragment not in JS, f"{fragment} fetched from app.js"


@pytest.mark.parametrize("fragment", FORBIDDEN_ENDPOINT_FRAGMENTS[:3])
def test_no_forbidden_endpoint_in_html(fragment):
    assert fragment not in HTML, f"{fragment} referenced from index.html"


@pytest.mark.parametrize("ident", FORBIDDEN_JS_IDENTIFIERS)
def test_no_trading_identifiers_in_js(ident):
    assert ident not in JS, f"{ident} present in app.js"


def test_executor_enable_nowhere():
    for name, text in (("index.html", HTML), ("app.js", JS), ("styles.css", CSS)):
        assert "/executor/enable" not in text, f"/executor/enable in {name}"


def test_api_constant_block_if_present_lists_only_safe_routes():
    m = re.search(r"const\s+API\s*=\s*\{(.*?)\}", JS, re.S)
    if not m:
        pytest.skip("no API constant block in app.js")
    block = m.group(1).lower()
    for bad in ("hyps", "hypotheses", "orders", "portfolio"):
        assert bad not in block, f"API constant block exposes {bad}"


# ---------------------------------------------------------------------------
# 5. Paper-trading gate stays closed (fail closed, never arm live)
# ---------------------------------------------------------------------------


def _definition_sources(name_part: str):
    """Yield (path, text) of every py file that DEFINES ``name_part``."""
    hits = []
    for p in sorted(REPO_ROOT.rglob("*.py")):
        if ".venv" in p.parts or "node_modules" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if re.search(rf"^{name_part}\s*=", text, re.M):
            hits.append((p, text))
    return hits


def test_paper_trade_signal_statuses_never_contains_live():
    sources = _definition_sources("_PAPER_TRADE_SIGNAL_STATUSES")
    if not sources:
        pytest.skip("no _PAPER_TRADE_SIGNAL_STATUSES defined in repo")
    for _path, src in sources:
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(frozenset\()?\s*(\{[^}]*\}|\([^)]*\)|\[[^\]]*\])",
            src,
            re.S,
        )
        assert m, "could not parse _PAPER_TRADE_SIGNAL_STATUSES literal"
        literal = m.group(2).lower()
        statuses = set(re.findall(r'["\']([a-z_]+)["\']', literal))
        assert "live" not in statuses, (
            "'live' must NEVER be added to _PAPER_TRADE_SIGNAL_STATUSES"
        )
        assert "paper_trading" in statuses, "expected paper_trading in allowed set"


def test_generate_paper_trade_signal_does_not_widen_to_live():
    hits = [
        p
        for p in sorted(REPO_ROOT.rglob("*.py"))
        if ".venv" not in p.parts and "node_modules" not in p.parts
        and re.search(r"^\s*(async )?def generate_paper_trade_signal", p.read_text(encoding="utf-8", errors="replace"), re.M)
    ]
    if not hits:
        pytest.skip("generate_paper_trade_signal not defined in repo")
    for path in hits:
        src = path.read_text(encoding="utf-8")
        m = re.search(r"def generate_paper_trade_signal.*?(?=\n\s*(async )?def |\Z)", src, re.S)
        body = m.group(0) if m else src
        assert "status == 'live'" not in body and 'status == "live"' not in body and 'status=="live"' not in body.replace(" ", ""), (
            f"generate_paper_trade_signal widened to status=='live' in {path}"
        )
