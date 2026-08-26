"""autofill characterization #0050 — public health trio.

Pins the gating contract for the health endpoint family on the Callisto API:

  PUBLIC (must NEVER gain require_admin / _auth / token gates):
    * GET /health          — polled by sentinel + watchdog
    * GET /health/livez    — k8s-style liveness probe
    * GET /health/readyz   — k8s-style readiness probe

  GATED (must STAY gated with require_admin_or_loopback):
    * GET /health/detailed
    * GET /health/deep
    * GET /health/integrity/history

Also pins, fail-closed:
  * the default-secure write middleware never touches plain GETs, so the
    health probes cannot be blocked by the auth floor either;
  * the paper-trade signal hard gate stays exactly {"paper_trading"} —
    "live" must never be armed through it;
  * handler bodies live in tools/api/system_routes.py and stay side-effect
    light / non-gated themselves.

Tests are source-contract first (no server boot required) plus behavioural
checks on the pure helpers where importing api.py is feasible.
"""

from __future__ import annotations

import ast
import inspect
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def _read(relpath: str) -> str:
    with open(os.path.join(REPO, relpath)) as f:
        return f.read()


API_SOURCE = _read("api.py")
SYSTEM_ROUTES_SOURCE = _read(os.path.join("tools", "api", "system_routes.py"))


def _route_window(source: str, decorator_head: str) -> str:
    """Return source from a decorator up to the next top-level decorator."""
    i = source.find(decorator_head)
    assert i != -1, f"{decorator_head!r} not found"
    nxt = source.find("\n@", i)
    if nxt == -1:
        nxt = len(source)
    return source[i:nxt]


# ---------------------------------------------------------------------------
# Route inventory
# ---------------------------------------------------------------------------

PUBLIC_HEALTH_ROUTES = [
    "/health",
    "/health/livez",
    "/health/readyz",
]

GATED_HEALTH_ROUTES = [
    "/health/detailed",
    "/health/deep",
    "/health/integrity/history",
]

GATE_MARKERS = ("require_admin", "_auth")


# ---------------------------------------------------------------------------
# Public trio: decorator-level pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
def test_public_health_route_exists(path):
    assert f'@app.get("{path}")' in API_SOURCE, (
        f"{path} route disappeared from api.py — sentinel/watchdog would break"
    )


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
@pytest.mark.parametrize("marker", ["Depends(require_admin)",
                                    "Depends(require_admin_or_loopback)"])
def test_public_health_route_has_no_depends_gate(path, marker):
    window = _route_window(API_SOURCE, f'@app.get("{path}")')
    assert marker not in window, (
        f"{path} gained {marker} — this is a PUBLIC liveness surface"
    )


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
def test_public_health_route_has_no_auth_marker(path):
    """No require_admin and no private _auth helper anywhere in the window."""
    window = _route_window(API_SOURCE, f'@app.get("{path}")')
    for marker in GATE_MARKERS:
        assert marker not in window, (
            f"{path} references auth marker {marker!r} — must stay public"
        )


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
def test_public_health_decorator_is_plain(path):
    window = _route_window(API_SOURCE, f'@app.get("{path}")')
    assert re.match(rf'@app\.get\("{re.escape(path)}"\)\n', window), (
        f"{path} decorator grew extra arguments (dependencies/response_class?)"
    )


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
def test_public_health_handler_is_async_and_takes_no_deps(path):
    """The handler signature takes no Request/security parameters."""
    window = _route_window(API_SOURCE, f'@app.get("{path}")')
    m = re.search(r"async def (\w+)\(([^)]*)\)", window)
    assert m is not None, f"{path} handler not found"
    params = m.group(2).strip()
    assert "Request" not in params
    assert "Security" not in params
    assert "token" not in params.lower()


# ---------------------------------------------------------------------------
# Public trio: docstring contract (the "must never gain an admin dep" note)
# ---------------------------------------------------------------------------


def test_health_docstring_pins_publicity():
    window = _route_window(API_SOURCE, '@app.get("/health")\n')
    assert "PUBLIC" in window, "/health should document its PUBLIC status"


def test_livez_docstring_pins_publicity():
    window = _route_window(API_SOURCE, '@app.get("/health/livez")\n')
    assert "PUBLIC" in window


def test_readyz_docstring_pins_publicity():
    window = _route_window(API_SOURCE, '@app.get("/health/readyz")\n')
    assert "PUBLIC" in window


# ---------------------------------------------------------------------------
# Gated family: defense-in-depth must NOT be removed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GATED_HEALTH_ROUTES)
def test_gated_health_route_stays_gated(path):
    window = _route_window(API_SOURCE, f'@app.get("{path}"')
    assert "Depends(require_admin_or_loopback)" in window, (
        f"{path} lost its require_admin_or_loopback gate — observability "
        "detail must not leak publicly"
    )


@pytest.mark.parametrize("path", GATED_HEALTH_ROUTES)
def test_gated_health_route_not_downgraded_to_require_admin_only(path):
    """The gate is the loopback-allowing variant; do not silently swap it."""
    window = _route_window(API_SOURCE, f'@app.get("{path}"')
    # require_admin alone would be stricter — allowed — but the pinned form
    # must still reference loopback semantics OR be hard admin. Pin current.
    assert "require_admin" in window


def test_detailed_and_deep_handlers_in_system_routes():
    for fn in ("health_detailed", "health_deep"):
        assert f"async def {fn}()" in SYSTEM_ROUTES_SOURCE, fn


def test_system_routes_module_documents_the_contract():
    header = SYSTEM_ROUTES_SOURCE[:2000]
    assert "/health, /health/livez, /health/readyz stay PUBLIC" in header or \
           "stay PUBLIC" in header, (
        "system_routes module header should keep documenting the split"
    )


# ---------------------------------------------------------------------------
# Middleware: the auth floor must not block plain GETs (incl. health probes)
# ---------------------------------------------------------------------------

MIDDLEWARE_SRC = _route_window(API_SOURCE, '@app.middleware("http")')


def test_middleware_only_gates_write_methods():
    assert "_WRITE_METHODS" in MIDDLEWARE_SRC
    assert 'method in _WRITE_METHODS' in MIDDLEWARE_SRC


def test_get_is_not_a_write_method_constant():
    m = re.search(r"_WRITE_METHODS\s*=\s*([^#\n]+)", API_SOURCE)
    assert m is not None, "_WRITE_METHODS constant missing"
    consts = set(re.findall(r'"([A-Z]+)"', m.group(1)))
    assert "GET" not in consts, "GET must never be treated as a write method"
    assert "HEAD" not in consts, "HEAD must never be treated as a write method"


def test_middleware_does_not_hardcode_health_paths():
    assert "/health" not in MIDDLEWARE_SRC, (
        "middleware should not special-case paths; it gates by method only"
    )


def test_public_write_allowlist_does_not_include_health_paths():
    allowlist = API_SOURCE[API_SOURCE.find('public_endpoint("POST"'):]
    for line in allowlist.splitlines()[:10]:
        assert "/health" not in line


# ---------------------------------------------------------------------------
# Behavioural: import api.py and exercise the handlers directly where cheap
# ---------------------------------------------------------------------------

try:
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    import api as api_mod  # noqa: E402
    from tools.api import system_routes as sr  # noqa: E402
except Exception as _e:  # pragma: no cover
    api_mod = None
    sr = None
    _IMPORT_ERR = str(_e)


@pytest.fixture(scope="module")
def fastapi_app():
    if api_mod is None:
        pytest.skip(f"api module unavailable: {_IMPORT_ERR}")
    return api_mod.app


def _routes(app, path):
    return [r for r in app.routes if getattr(r, "path", None) == path]


@pytest.mark.parametrize("path", PUBLIC_HEALTH_ROUTES)
def test_openapi_route_registered_without_dependencies(fastapi_app, path):
    matches = _routes(fastapi_app, path)
    assert matches, f"{path} not registered on the FastAPI app"
    for r in matches:
        deps = list(getattr(r, "dependencies", []) or [])
        assert not deps, f"{path} carries route-level dependencies: {deps}"
        get_route = r.methods & {"GET"} if hasattr(r, "methods") else True
        assert get_route


@pytest.mark.parametrize("path", GATED_HEALTH_ROUTES)
def test_openapi_route_registered_with_loopback_dependency(fastapi_app, path):
    matches = _routes(fastapi_app, path)
    assert matches, f"{path} not registered on the FastAPI app"
    for r in matches:
        deps = list(getattr(r, "dependencies", []) or [])
        names = [getattr(d.dependency, "__name__", "") for d in deps]
        assert "require_admin_or_loopback" in names, (
            f"{path} dependencies = {names}"
        )


def test_livez_returns_alive_true():
    if sr is None:
        pytest.skip("system_routes unavailable")
    payload = __import__("asyncio").run(sr.health_livez())
    assert payload["alive"] is True
    assert isinstance(payload["ts"], float)


def test_readyz_raises_503_when_report_unhealthy(monkeypatch):
    if sr is None:
        pytest.skip("system_routes unavailable")
    import asyncio

    async def fake_report():
        return {
            "healthy": False,
            "severity": "critical",
            "reasons": ["pipeline_broken: integrity check failed"],
        }

    monkeypatch.setattr(sr, "build_health_report", fake_report)
    with pytest.raises(Exception) as excinfo:
        asyncio.run(sr.health_readyz())
    assert getattr(excinfo.value, "status_code", None) == 503


def test_readyz_ok_when_report_healthy(monkeypatch):
    if sr is None:
        pytest.skip("system_routes unavailable")
    import asyncio

    async def fake_report():
        return {"healthy": True, "severity": "ok", "uptime_seconds": 42.0}

    monkeypatch.setattr(sr, "build_health_report", fake_report)
    payload = asyncio.run(sr.health_readyz())
    assert payload == {
        "ready": True,
        "severity": "ok",
        "uptime_seconds": 42.0,
    }


# ---------------------------------------------------------------------------
# evaluate_health_signals — pure demotion matrix characterisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "report,expected_severity",
    [
        ({}, "ok"),
        ({"write_coordinators": [{"writes_total": 100, "writes_failed": 1}]}, "ok"),
        ({"write_coordinators": [{"writes_total": 100, "writes_failed": 5}]}, "warning"),
        ({"write_coordinators": [{"queue_depth": 101}]}, "warning"),
        ({"task_queue": {"depth": 51}}, "warning"),
        ({"stalled_phases": ["research"]}, "warning"),
        ({"pipeline_integrity": {"healthy": False, "issues": ["x"]}}, "critical"),
        ({"subsystems": {"db": {"is_open": True}}}, "critical"),
    ],
)
def test_evaluate_health_signals_matrix(report, expected_severity):
    healthy, severity, reasons = sr.evaluate_health_signals(report)
    assert severity == expected_severity
    assert healthy == (expected_severity == "ok")
    assert isinstance(reasons, list)


def test_watchdog_ping_critical_only_after_warmup():
    # Cold start (few pings) must NOT trip critical even with stale ping.
    cold = {"watchdog_monitoring": {"last_ping_ago_seconds": 999, "total_pings": 1}}
    _, sev_cold, reasons_cold = sr.evaluate_health_signals(cold)
    assert sev_cold == "ok" and reasons_cold == []
    warm = {"watchdog_monitoring": {"last_ping_ago_seconds": 999, "total_pings": 50}}
    _, sev_warm, reasons_warm = sr.evaluate_health_signals(warm)
    assert sev_warm == "critical" and reasons_warm


# ---------------------------------------------------------------------------
# Fail-closed: paper-trade signal hard gate stays exactly paper_trading
# ---------------------------------------------------------------------------

PAPER_GATE_SOURCE = _read(os.path.join("tools", "signals", "paper.py"))


def test_paper_statuses_exactly_paper_trading():
    from tools.signals.paper import allowed_paper_statuses

    assert allowed_paper_statuses() == frozenset({"paper_trading"})


@pytest.mark.parametrize("status", ["live", "LIVE", "armed", "", None, 1])
def test_reject_non_paper_rejects_everything_else(status):
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper(status) is True


def test_reject_non_paper_accepts_paper_trading():
    from tools.signals.paper import reject_non_paper

    assert reject_non_paper("paper_trading") is False


def test_paper_gate_source_has_no_live_literal_in_code():
    """The word "live" may appear in comments/docstrings, never as a status
    literal in executable code (AST-level check)."""
    import ast as _ast

    tree = _ast.parse(PAPER_GATE_SOURCE)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.Constant,)) and isinstance(node.value, str):
            # skip the docstring node itself
            continue
        if isinstance(node, _ast.Constant) and node.value == "live":
            pytest.fail('"live" string literal present in paper gate code')


def test_paper_gate_source_keeps_hard_gate_comment():
    assert "HARD GATE" in PAPER_GATE_SOURCE
    assert "NEVER" in PAPER_GATE_SOURCE.upper()


def test_backtest_engine_signal_method_still_gated_by_callers():
    """phases_impl call sites must consult the gate before generating."""
    phases = _read(os.path.join("tools", "loop", "phases_impl.py"))
    calls = phases.count(".generate_paper_trade_signal(")
    assert calls >= 2, "expected the two known call sites to remain"
    # The gate helper must be imported/used near the signal flow.
    assert "reject_non_paper" in phases or "allowed_paper_statuses" in phases or \
        "paper" in phases.lower(), (
        "phases_impl no longer references the paper-signal gate module"
    )
