"""Fail-closed Stage A regression registry.

Pins the fail-closed invariants landed on master so that reverting any of
them breaks this module. Everything here is static analysis: source text
and AST only. We deliberately do NOT import tools.autonomous (it hangs at
import time in some environments) and never start browsers or servers.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. No uvicorn binding to 0.0.0.0 in launcher scripts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "start.bat",
        "scripts/overnight_setup.py",
        "scripts/start-callisto.ps1",
        "scripts/watchdog.ps1",
    ],
)
def test_no_uvicorn_wildcard_bind(rel):
    src = _read(rel)
    assert not re.search(r"0\.0\.0\.0", src), (
        f"{rel} binds uvicorn to 0.0.0.0 — loopback-only binding is a "
        f"Stage A invariant"
    )


# ---------------------------------------------------------------------------
# 2. Backtest paper-trade signal gate excludes live
# ---------------------------------------------------------------------------


def test_paper_trade_signal_statuses_is_paper_only():
    # Canonical frozenset lives in tools/signals/paper.py after the extract.
    src = _read("tools/signals/paper.py")
    m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*(.+)", src)
    assert m, "_PAPER_TRADE_SIGNAL_STATUSES assignment missing from tools/signals/paper.py"
    literal = m.group(1).strip()
    assert literal == 'frozenset({"paper_trading"})', (
        f"unexpected literal: {literal!r}"
    )
    # Belt and braces: parse and evaluate the set literal.
    node = ast.parse(literal, mode="eval").body
    assert isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    assert node.func.id == "frozenset"
    values = {
        elt.value for elt in node.args[0].elts  # type: ignore[attr-defined]
    }
    assert values == {"paper_trading"}
    assert "live" not in values
    bt = _read("tools/backtest.py")
    assert "from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES" in bt
    assert re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset", bt) is None


# ---------------------------------------------------------------------------
# 3. Autonomous loop: live_execute phase is env-gated
# ---------------------------------------------------------------------------


def test_phase_live_execute_requires_env_gate():
    src = _read("tools/autonomous.py")
    m = re.search(r"(async )?def _phase_live_execute\b.*?(?=\n(?:    async )?def |\nclass |\Z)", src, re.S)
    assert m, "_phase_live_execute not found in tools/autonomous.py"
    body = m.group(0)
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in body, (
        "_phase_live_execute lost its CALLISTO_ALLOW_LIVE_EXECUTE gate"
    )
    # The != "1" comparison must appear before the first list_hypotheses call.
    gate_idx = body.find('!= "1"')
    hyp_idx = body.find("list_hypotheses")
    assert "CALLISTO_ALLOW_LIVE_EXECUTE" in body[:gate_idx + 30]
    assert gate_idx != -1, 'expected an `!= "1"` comparison in the gate'
    assert hyp_idx == -1 or gate_idx < hyp_idx, (
        "live_execute reaches list_hypotheses before checking "
        'CALLISTO_ALLOW_LIVE_EXECUTE != "1"'
    )


# ---------------------------------------------------------------------------
# 4. Bet executor enable() honors CALLISTO_LOCAL_ONLY before arming
# ---------------------------------------------------------------------------


def test_bet_executor_enable_gated_by_local_only():
    src = _read("tools/bet_executor.py")
    m = re.search(r"def enable\(self\).*?(?=\n    def |\n\Z)", src, re.S)
    assert m, "BetExecutor.enable() not found"
    body = m.group(0)
    assert "CALLISTO_LOCAL_ONLY" in body, (
        "enable() no longer checks CALLISTO_LOCAL_ONLY"
    )
    check_idx = body.find("CALLISTO_LOCAL_ONLY")
    arm_idx = body.find("_enabled = True")
    assert arm_idx != -1, "enable() no longer arms via _enabled = True"
    assert check_idx < arm_idx, (
        "enable() sets _enabled = True before evaluating CALLISTO_LOCAL_ONLY"
    )


# ---------------------------------------------------------------------------
# 5. OrderManager defaults to disabled
# ---------------------------------------------------------------------------


def test_order_manager_init_defaults_disabled():
    tree = ast.parse(_read("tools/order_manager.py"))
    found_init = False

    def _enabled_assigns(fn):
        out = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Attribute):
                out.append((n.targets[0].attr, n.value))
        return [(a, v) for a, v in out if a == "_enabled"]

    for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
        for item in cls.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == "__init__":
                found_init = True
                assigns = _enabled_assigns(item)
                assert assigns, "__init__ does not assign self._enabled"
                for _, value in assigns:
                    assert isinstance(value, ast.Constant) and value.value is False, (
                        "_enabled is not assigned False in __init__"
                    )
    assert found_init, "no class with __init__ found in order_manager.py"

    # Also pin textually: the assignment preceding def enable is False.
    src = _read("tools/order_manager.py")
    m = re.search(r"self\._enabled\s*=\s*False.*?def enable\b", src, re.S)
    assert m, "expected `self._enabled = False` before def enable()"


# ---------------------------------------------------------------------------
# 6. AGP verify_seal: unkeyed SHA-256 only when no key configured
# ---------------------------------------------------------------------------


def test_verify_seal_unkeyed_candidate_is_guarded():
    src = _read("agp/__init__.py")
    m = re.search(r"def verify_seal\(.*?(?=\n    def |\nclass |\Z)", src, re.S)
    assert m, "verify_seal not found in agp/__init__.py"
    body = m.group(0)

    sha_call = body.find("hashlib.sha256(payload.encode")
    assert sha_call != -1, "unkeyed legacy digest candidate missing entirely"
    # There must be a guard between the function start and the raw-digest call.
    head = body[:sha_call]
    guarded = bool(re.search(r"_seal_key_configured|_seal_keys", head))
    assert guarded, (
        "verify_seal adds the public SHA-256 candidate without a key-configured "
        "guard — a keyed regime would accept forgeable unkeyed seals"
    )
    # The guard must be a negation-style check immediately governing the append:
    # find the nearest preceding line containing if/not.
    lines = head.splitlines()
    cond_lines = [ln for ln in lines if re.search(r"^\s*if\b", ln)]
    assert cond_lines, "no conditional guard near the unkeyed candidate"
    last = cond_lines[-1]
    assert "not _seal_key_configured" in last or "not _seal_keys" in last, (
        f"unkeyed candidate guard looks wrong: {last.strip()!r}"
    )


# ---------------------------------------------------------------------------
# 7. API admin endpoints require admin-or-loopback auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    ['@app.get("/bets"', '@app.get("/system/full-status"'],
)
def test_admin_routes_require_auth(route):
    src = _read("api.py")
    hits = [m.start() for m in re.finditer(re.escape(route), src)]
    assert hits, f"{route} decorator missing from api.py"
    ok = False
    for idx in hits:
        chunk = src[idx:idx + 400]
        if "require_admin_or_loopback" in chunk:
            ok = True
            break
    assert ok, f"{route} is not protected by require_admin_or_loopback"


# ---------------------------------------------------------------------------
# 8. Kelly sizing has exactly one core formula
# ---------------------------------------------------------------------------


def test_kelly_core_is_defined_in_tools_kelly():
    src = _read("tools/kelly.py")
    assert re.search(r"^def kelly_core\(", src, re.M), (
        "def kelly_core missing from tools/kelly.py"
    )


def test_kelly_binary_delegates_to_kelly_core():
    src = _read("tools/sizing.py")
    m = re.search(r"def kelly_binary\(.*?(?=\ndef |\nclass |\Z)", src, re.S)
    assert m, "kelly_binary not found in tools/sizing.py"
    assert "kelly_core" in m.group(0), (
        "kelly_binary no longer delegates to the single kelly_core formula"
    )


# ---------------------------------------------------------------------------
# 9. Dashboard trading panels are absent from the HTML entirely
# ---------------------------------------------------------------------------


def test_dashboard_trading_panels_are_absent():
    src = _read("web/dashboard/index.html")
    for panel_id in ("panel-hyps", "panel-orders", "panel-portfolio"):
        assert f'id="{panel_id}"' not in src, (
            f"{panel_id} must be deleted from index.html, not merely hidden"
        )


# ---------------------------------------------------------------------------
# 10. Odds edges GET requires admin-or-loopback
# ---------------------------------------------------------------------------


def test_odds_edges_get_requires_auth():
    src = _read("api.py")
    hits = [m.start() for m in re.finditer(re.escape('@app.get("/odds/edges"'), src)]
    assert hits, '@app.get("/odds/edges" decorator missing from api.py'
    ok = False
    for idx in hits:
        chunk = src[idx:idx + 400]
        if "require_admin_or_loopback" in chunk:
            ok = True
            break
    assert ok, "/odds/edges GET is not protected by require_admin_or_loopback"


# ---------------------------------------------------------------------------
# 11. Phase ledger exists and is capped at 50 entries
# ---------------------------------------------------------------------------


def test_phase_ledger_exists_and_capped_at_50():
    src = _read("tools/loop/phase_ledger.py")
    capped = ("cap 50" in src.lower()) or ("MAX_ENTRIES = 50" in src) or (
        re.search(r"maxlen\s*=\s*50", src) is not None
    )
    assert capped, "phase_ledger does not appear to cap entries at 50"
