"""autofill characterization #0042 — public health trio.

Pins the security posture of the health endpoint family in api.py:

  PUBLIC (must NEVER gain an admin dependency or auth gate):
    * GET /health            — sentinel + watchdog poll this; disk-write debounce.
    * GET /health/livez      — k8s-style liveness.
    * GET /health/readyz     — k8s-style readiness (503 when degraded).

  GATED (must KEEP their require_admin_or_loopback dependency):
    * GET /health/detailed
    * GET /health/deep
    * GET /health/integrity/history

Three layers of pinning per convention of the earlier autofill modules:
  1. Source contract — the decorators in api.py literally carry (or omit)
     the Depends(require_admin...) argument.
  2. App introspection — the live FastAPI route objects have no auth
     callables in their dependant graph for the public trio.
  3. Behaviour — a real HTTP request through the middleware stack with a
     non-loopback client and an unset/invalid admin token still reaches the
     handler (no 401/403) for the public trio, while the gated endpoints
     refuse.
Also pins the paper-trade seal is untouched by anything here: "live" never
appears in _PAPER_TRADE_SIGNAL_STATUSES and generate_paper_trade_signal is
never widened to status == 'live'.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

PUBLIC_TRIO = ["/health", "/health/livez", "/health/readyz"]
GATED_HEALTH = [
    "/health/detailed",
    "/health/deep",
    "/health/integrity/history",
]


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment dependent
    api_mod = None
    _IMPORT_ERR = str(_import_err)
else:
    _IMPORT_ERR = ""


needs_api = pytest.mark.skipif(api_mod is None, reason=f"could not import api: {_IMPORT_ERR}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _route_block(path: str) -> str:
    """Return the decorator+def text block for @app.get(path) in api.py."""
    needle = f'@app.get("{path}"'
    idx = API_SOURCE.find(needle)
    assert idx != -1, f"no @app.get decorator found for {path!r} in api.py"
    # Block ends at the next top-level decorator after this one.
    nxt = API_SOURCE.find("\n@app.", idx + len(needle))
    if nxt == -1:
        nxt = len(API_SOURCE)
    return API_SOURCE[idx:nxt]


def _decorator_line(path: str) -> str:
    return API_SOURCE.splitlines()[
        API_SOURCE[: API_SOURCE.index(f'@app.get("{path}"')].count("\n")
    ]


def _find_route(path: str):
    for r in api_mod.app.routes:
        if getattr(r, "path", None) == path and "GET" in getattr(r, "methods", set()):
            return r
    return None


def _dependant_callables(route) -> list:
    """All callables in the route's dependency graph (params-level deps)."""
    names = []
    seen = set()
    stack = [getattr(route, "dependant", None)]
    while stack:
        d = stack.pop()
        if d is None or id(d) in seen:
            continue
        seen.add(id(d))
        if d.call is not None:
            names.append(getattr(d.call, "__name__", repr(d.call)))
        stack.extend(d.dependencies)
    return names


AUTH_CALLABLE_NAMES = {"require_admin", "require_admin_or_loopback"}


# ---------------------------------------------------------------------------
# Layer 1 — source contract: public trio decorators carry no Depends(auth)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_decorator_has_no_depends(path):
    block = _route_block(path)
    assert "Depends(" not in block, (
        f"{path} gained a Depends(...) dependency; it must stay fully public"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_decorator_is_bare(path):
    line = _decorator_line(path)
    assert re.fullmatch(rf'@app\.get\("{re.escape(path)}"\)\s*', line), (
        f"{path} decorator changed shape: {line!r}"
    )


@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_source_never_mentions_auth(path):
    block = _route_block(path).lower()
    for bad in ("require_admin", "_auth", "bearer", "token"):
        assert bad not in block, f"{path} block mentions {bad!r}"


def test_public_trio_docstrings_still_promise_publicity():
    assert "must never gain an admin dep" in _route_block("/health")
    assert "PUBLIC" in _route_block("/health/livez")
    assert "PUBLIC" in _route_block("/health/readyz")


def test_no_other_health_path_accidentally_public():
    """Every other /health* route must carry an explicit admin dep."""
    blocks = re.findall(r'@app\.get\("(\/health[^"]*)"[^\n]*\n', API_SOURCE)
    for path in blocks:
        if path in PUBLIC_TRIO:
            continue
        block = _route_block(path)
        assert "require_admin_or_loopback" in block or "require_admin" in block, (
            f"{path} is neither in the public trio nor explicitly gated"
        )


# ---------------------------------------------------------------------------
# Layer 1b — gated health endpoints keep their gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_endpoints_keep_loopback_gate(path):
    block = _route_block(path)
    assert "Depends(require_admin_or_loopback)" in block, (
        f"{path} lost its require_admin_or_loopback gate"
    )


@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_not_hard_token_only(path):
    """They are loopback-gated, not require_admin-hard-token (characterization)."""
    block = _route_block(path)
    assert "Depends(require_admin)" not in block.replace(
        "require_admin_or_loopback", ""
    ), f"{path} unexpectedly uses hard require_admin"


# ---------------------------------------------------------------------------
# Layer 2 — live app introspection
# ---------------------------------------------------------------------------


@needs_api
@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_live_route_has_no_auth_dependency(path):
    route = _find_route(path)
    assert route is not None, f"route {path} missing from app.routes"
    deps = set(_dependant_callables(route))
    assert not (deps & AUTH_CALLABLE_NAMES), (
        f"{path} dependant graph contains auth callable(s): {deps & AUTH_CALLABLE_NAMES}"
    )
    assert not getattr(route, "dependencies", []), (
        f"{path} has router-level dependencies: {route.dependencies}"
    )


@needs_api
@pytest.mark.parametrize("path", GATED_HEALTH)
def test_live_route_keeps_auth_dependency(path):
    route = _find_route(path)
    assert route is not None, f"route {path} missing from app.routes"
    deps = set(_dependant_callables(route))
    assert "require_admin_or_loopback" in deps, (
        f"{path} no longer wired to require_admin_or_loopback in the live app"
    )


@needs_api
def test_health_handlers_are_coroutines():
    for path in PUBLIC_TRIO:
        route = _find_route(path)
        assert route is not None
        assert inspect.iscoroutinefunction(route.endpoint), f"{path} not async"


@needs_api
def test_health_trio_delegates_to_system_routes():
    """Handlers delegate to tools.api.system_routes (split preserved)."""
    sr = sys.modules.get("tools.api.system_routes")
    assert sr is not None, "tools.api.system_routes not importable"
    for fn_name in ("build_health_report", "health_livez", "health_readyz"):
        assert hasattr(sr, fn_name), fn_name


# ---------------------------------------------------------------------------
# Layer 3 — behaviour through middleware with hostile client
# ---------------------------------------------------------------------------


def _client():
    from fastapi.testclient import TestClient

    return TestClient(api_mod.app)


@needs_api
@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_serves_without_token_from_remote_client(path, monkeypatch):
    """Non-loopback client, admin token unset → still 200-ish, never 401/403."""
    monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "", raising=False)
    with _client() as c:
        # Simulate a remote client via forwarded headers the middleware reads.
        resp = c.get(path, headers={"x-forwarded-for": "203.0.113.7"})
        assert resp.status_code not in (401, 403), (
            f"{path} refused a tokenless request ({resp.status_code}); it must stay public"
        )


@needs_api
@pytest.mark.parametrize("path", PUBLIC_TRIO)
def test_public_trio_ignores_bad_bearer(path, monkeypatch):
    monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "sekrit", raising=False)
    with _client() as c:
        resp = c.get(
            path,
            headers={
                "authorization": "Bearer wrong-token",
                "x-forwarded-for": "203.0.113.7",
            },
        )
        assert resp.status_code not in (401, 403), (
            f"{path} rejected a bad bearer — public endpoints ignore auth entirely"
        )


@needs_api
@pytest.mark.parametrize("path", GATED_HEALTH)
def test_gated_health_refuses_bad_bearer(path, monkeypatch):
    monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "sekrit", raising=False)
    with _client() as c:
        resp = c.get(
            path,
            headers={
                "authorization": "Bearer wrong-token",
                "x-forwarded-for": "203.0.113.7",
            },
        )
        assert resp.status_code in (401, 403), (
            f"{path} served an unauthenticated remote request ({resp.status_code})"
        )


@needs_api
def test_write_middleware_does_not_touch_get_health():
    """The default-secure middleware only gates write methods."""
    src = API_SOURCE[API_SOURCE.index("_default_secure_middleware"):]
    src = src[:src.index("\n@app.post")]
    m = re.search(r"if method in (_WRITE_METHODS)", src)
    assert m, "middleware no longer scopes to write methods"


# ---------------------------------------------------------------------------
# Payload-shape characterization (public handlers, mocked subsystems)
# ---------------------------------------------------------------------------


@needs_api
def test_build_health_report_returns_dict_with_expected_keys():
    from tools.api import system_routes as sr

    report = asyncio.run(sr.build_health_report())
    assert isinstance(report, dict)
    assert "status" in report or "ok" in report or report.keys(), (
        "build_health_report returned an empty payload"
    )


@needs_api
def test_livez_readyz_shapes():
    from tools.api import system_routes as sr

    livez = asyncio.run(sr.health_livez())
    readyz = asyncio.run(sr.health_readyz())
    assert isinstance(livez, dict)
    assert isinstance(readyz, dict)
    assert any(k in livez for k in ("status", "alive", "ok")), livez.keys()


# ---------------------------------------------------------------------------
# Seal: nothing here arms live betting
# ---------------------------------------------------------------------------


@needs_api
def test_paper_trade_signal_statuses_exclude_live():
    """Seal lives in tools/signals/paper.py; api re-exports it via tools.backtest."""
    from tools.signals import paper as paper_mod

    statuses = getattr(paper_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
    assert isinstance(statuses, (frozenset, set)), "seal constant missing"
    normalized = {str(s).lower() for s in statuses}
    assert "live" not in normalized, "LIVE BETTING ARMED — refusing"
    assert "paper_trading" in normalized  # characterization: paper-only set
    # api module must not shadow it with a widened copy
    assert getattr(api_mod, "_PAPER_TRADE_SIGNAL_STATUSES", statuses) is statuses


@needs_api
def test_generate_paper_trade_signal_not_widened_to_live():
    fn = getattr(api_mod, "generate_paper_trade_signal", None)
    if fn is None:
        pytest.skip("generate_paper_trade_signal not exposed on api module")
    body = inspect.getsource(fn).lower()
    assert not re.search(r"status\s*(==|in\b)[^\n]*['\"]live['\"]", body), (
        "generate_paper_trade_signal widened to status=='live'"
    )


def test_source_wide_seal_intact():
    src = API_SOURCE
    m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES[^=]*=\s*[\{\(\[]([^\}\)]*)", src)
    if m:
        entries = {e.strip().strip("'\"").lower() for e in m.group(1).split(",") if e.strip()}
        assert "live" not in entries
