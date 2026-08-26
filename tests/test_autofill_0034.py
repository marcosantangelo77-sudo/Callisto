"""autofill #0034 — characterization: the PUBLIC health trio.

Long-form pin of the public-health gating contract:

  * /health, /health/livez, /health/readyz must have NO require_admin and
    NO _auth anywhere in their decorator, route-level dependencies, or
    reachable dependency graph. They are polled by the sentinel/watchdog
    and by k8s probes; an auth gate there is an outage.
  * /health/detailed and /health/deep MAY stay gated
    (require_admin_or_loopback) — this module pins that they DO stay gated,
    and never asserts the opposite.

Fail-closed posture: every assertion here either (a) confirms a currently
true public property of the trio, or (b) confirms the gated pair keeps its
gate. Nothing in this file arms live betting or widens paper-trade signal
semantics.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import os
import re
import sys

import pytest
from fastapi.routing import APIRoute

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Load api.py without running lifespan.
# ---------------------------------------------------------------------------


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment dependent
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""

pytestmark = pytest.mark.skipif(
    api_mod is None, reason=f"Could not import api module: {_import_err_msg}"
)

PUBLIC_TRIO = ("/health", "/health/livez", "/health/readyz")
GATED_PAIR = ("/health/detailed", "/health/deep")
ALL_HEALTH_PATHS = PUBLIC_TRIO + GATED_PAIR + (
    "/health/integrity/history",
)
_ADMIN_CALLABLES = {"require_admin", "require_admin_or_loopback"}

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

SYSTEM_ROUTES_SOURCE = ""
_SR_PATH = os.path.join(REPO, "tools", "api", "system_routes.py")
if os.path.exists(_SR_PATH):
    with open(_SR_PATH) as _f:
        SYSTEM_ROUTES_SOURCE = _f.read()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _routes_for(path: str) -> list[APIRoute]:
    return [
        r for r in api_mod.app.routes if isinstance(r, APIRoute) and r.path == path
    ]


def _single_route(path: str) -> APIRoute:
    routes = _routes_for(path)
    assert len(routes) == 1, f"expected exactly one route for {path}, got {len(routes)}"
    return routes[0]


def _collect_dependency_names(route: APIRoute) -> set[str]:
    """Every callable __name__ reachable in the route's dependency graph."""
    seen: set[str] = set()

    def _walk(dep) -> None:
        if dep is None:
            return
        call = getattr(dep, "call", None)
        if call is not None:
            seen.add(getattr(call, "__name__", repr(call)))
        for sub in getattr(dep, "dependencies", []) or []:
            _walk(sub)

    for d in route.dependencies:
        _walk(d)
    _walk(route.dependant)
    return seen


def _decorator_block(source: str, path: str) -> str | None:
    m = re.search(r'@app\.get\(\s*["\']' + re.escape(path) + r'["\'][^\n]*', source)
    return m.group(0) if m else None


def _handler_body(source: str, handler_name: str) -> str:
    """Source text from `async def <name>` up to the next top-level def/app.get."""
    m = re.search(
        rf"async def {handler_name}\(.*?(?=\nasync def |\n@app|\ndef |\Z)",
        source,
        re.DOTALL,
    )
    assert m is not None, f"handler {handler_name} not found in source"
    return m.group(0)


# ===========================================================================
# Layer 1 — static decorator analysis in api.py
# ===========================================================================


class TestPublicTrioDecoratorsAreBare:
    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_decorator_exists(self, path):
        assert _decorator_block(API_SOURCE, path) is not None, (
            f"@app.get(\"{path}\") decorator vanished from api.py"
        )

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_decorator_has_no_dependencies_kwarg(self, path):
        deco = _decorator_block(API_SOURCE, path)
        assert "dependencies=" not in deco, f"{path} gained a route-level dependency"

    @pytest.mark.parametrize(
        "forbidden",
        ["require_admin", "_auth", "Depends(", "Security(", "APIKeyHeader"],
    )
    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_decorator_has_no_auth_text(self, path, forbidden):
        deco = _decorator_block(API_SOURCE, path)
        assert forbidden not in deco, (
            f"{path} decorator mentions '{forbidden}'"
        )

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_decorator_is_get_only_form(self, path):
        deco = _decorator_block(API_SOURCE, path)
        assert deco.strip().startswith("@app.get("), f"{path} changed HTTP verb"

    def test_public_trio_docstrings_stay_public(self):
        for marker in ("PUBLIC", "public"):
            pass
        body = _handler_body(API_SOURCE, "health_check")
        assert "PUBLIC" in body or "sentinel" in body, (
            "/health docstring lost its public-contract note"
        )
        lz = _handler_body(API_SOURCE, "health_livez")
        assert "PUBLIC" in lz
        rz = _handler_body(API_SOURCE, "health_readyz")
        assert "PUBLIC" in rz


class TestGatedPairDecoratorsKeepGates:
    """The gated variants MUST keep require_admin_or_loopback."""

    @pytest.mark.parametrize("path", GATED_PAIR)
    def test_gated_decorator_keeps_loopback_gate(self, path):
        deco = _decorator_block(API_SOURCE, path)
        assert deco is not None, f"{path} route missing entirely"
        assert "dependencies=[Depends(require_admin_or_loopback)]" in deco, (
            f"{path} lost require_admin_or_loopback"
        )

    @pytest.mark.parametrize("path", GATED_PAIR)
    def test_integrity_history_also_gated(self, path):
        deco = _decorator_block(API_SOURCE, "/health/integrity/history")
        assert deco is not None
        assert "require_admin_or_loopback" in deco


# ===========================================================================
# Layer 2 — live FastAPI route objects
# ===========================================================================


class TestLiveRoutes:
    @pytest.mark.parametrize("path", ALL_HEALTH_PATHS)
    def test_route_exists_as_get(self, path):
        route = _single_route(path)
        assert "GET" in route.methods

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_route_has_zero_route_level_dependencies(self, path):
        route = _single_route(path)
        assert list(route.dependencies) == [], (
            f"{path} has route-level dependencies: {route.dependencies}"
        )

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_route_dep_graph_has_no_admin_callable(self, path):
        names = _collect_dependency_names(_single_route(path))
        overlap = names & _ADMIN_CALLABLES
        assert not overlap, f"{path} dependency graph reaches {overlap}"

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_route_dep_graph_has_no_auth_sounding_callable(self, path):
        names = _collect_dependency_names(_single_route(path))
        bad = {
            n
            for n in names
            if re.search(r"auth|admin|token|api_?key|security", n, re.I)
        }
        assert not bad, f"{path} dep graph contains auth-sounding callables: {bad}"

    @pytest.mark.parametrize("path", GATED_PAIR)
    def test_gated_route_dep_graph_keeps_admin(self, path):
        names = _collect_dependency_names(_single_route(path))
        assert "require_admin_or_loopback" in names, f"{path} gate unreachable"

    def test_exactly_one_route_per_public_path(self):
        for path in PUBLIC_TRIO:
            assert len(_routes_for(path)) == 1

    def test_public_paths_have_no_path_params(self):
        for path in PUBLIC_TRIO:
            assert "{" not in path and "}" not in path

    @pytest.mark.parametrize(
        ("path", "method"), [(p, m) for p in PUBLIC_TRIO for m in ("POST", "PUT", "DELETE", "PATCH")]
    )
    def test_public_trio_accepts_only_safe_methods(self, path, method):
        route = _single_route(path)
        assert method not in route.methods

    def test_openapi_documents_public_trio_without_security(self):
        schema = api_mod.app.openapi()
        for path in PUBLIC_TRIO:
            op = schema["paths"].get(path, {}).get("get")
            assert op is not None, f"{path} missing from OpenAPI"
            assert not op.get("security"), f"{path} declares OpenAPI security"


# ===========================================================================
# Layer 3 — behavioral via TestClient
# ===========================================================================


def _make_client():
    from fastapi.testclient import TestClient

    return TestClient(api_mod.app)


class TestBehavioralTrioOpenAccess:
    def setup_method(self):
        self.client = _make_client()

    def test_health_reachable_without_credentials(self):
        resp = self.client.get("/health")
        assert resp.status_code not in (401, 403, 404)
        body = resp.json()
        assert isinstance(body, dict)
        assert "healthy" in body

    def test_livez_returns_200_alive(self):
        resp = self.client.get("/health/livez")
        assert resp.status_code == 200
        body = resp.json()
        assert body["alive"] is True
        assert isinstance(body["ts"], float)

    def test_readyz_never_auth_rejected(self):
        resp = self.client.get("/health/readyz")
        assert resp.status_code in (200, 503), resp.status_code
        assert resp.status_code not in (401, 403)

    @pytest.mark.parametrize(
        "headers",
        [
            {"Authorization": "Bearer garbage-token"},
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"X-API-Key": "not-a-real-key"},
        ],
    )
    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_bogus_credentials_change_nothing(self, path, headers):
        plain = self.client.get(path)
        withcred = self.client.get(path, headers=headers)
        assert withcred.status_code == plain.status_code, (
            f"{path} behaves differently with credentials {list(headers)}"
        )
        assert withcred.status_code not in (401, 403)

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_head_requests_ok(self, path):
        resp = self.client.head(path)
        assert resp.status_code in (200, 405)
        assert resp.status_code not in (401, 403)

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_options_preflight_not_auth_blocked(self, path):
        resp = self.client.options(path)
        assert resp.status_code not in (401, 403)


class TestBehavioralGatedContrast:
    """With CALLISTO_ADMIN_TOKEN set on the module, gated paths refuse."""

    def setup_method(self):
        self.client = _make_client()

    @pytest.fixture(autouse=True)
    def _token_env(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "unit-test-token")

    @pytest.mark.parametrize("path", GATED_PAIR)
    def test_gated_refuses_without_token(self, path):
        resp = self.client.get(path)
        assert resp.status_code in (401, 403), resp.status_code

    @pytest.mark.parametrize("path", GATED_PAIR)
    def test_gated_refuses_bad_token(self, path):
        resp = self.client.get(path, headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 403, resp.status_code

    @pytest.mark.parametrize("path", PUBLIC_TRIO)
    def test_public_trio_unaffected_by_token_config(self, path):
        """Setting an admin token must NOT make the trio demand it."""
        resp = self.client.get(path)
        assert resp.status_code not in (401, 403)


# ===========================================================================
# Layer 4 — handler bodies / delegation
# ===========================================================================


class TestHandlerDelegationAndBodies:
    def test_api_py_handlers_delegate_to_system_routes(self):
        for name in ("health_check", "health_livez", "health_readyz"):
            body = _handler_body(API_SOURCE, name)
            assert "_system_routes" in body or "_build_health_report" in body, (
                f"{name} reimplements logic instead of delegating"
            )
            assert "Authorization" not in body, f"{name} inspects auth headers"
            assert "_auth" not in body

    def test_system_routes_docstring_pins_contract(self):
        if not SYSTEM_ROUTES_SOURCE:
            pytest.skip("tools/api/system_routes.py not present")
        head = SYSTEM_ROUTES_SOURCE[:2000]
        assert "PUBLIC" in head
        assert "require_admin" in head

    def test_livez_handler_direct_call_shape(self):
        body = asyncio.run(api_mod._system_routes.health_livez())
        assert body["alive"] is True
        assert {"alive", "ts"} <= set(body)

    def test_readyz_handler_demotes_to_503_when_unhealthy(self):
        src = SYSTEM_ROUTES_SOURCE
        if not src:
            pytest.skip("system_routes source unavailable")
        rz = _handler_body(src, "health_readyz")
        assert "503" in rz, "readiness handler lost its 503 demotion"

    def test_evaluate_health_signals_pure_function_exists(self):
        fn = getattr(api_mod._system_routes, "evaluate_health_signals", None)
        assert callable(fn)
        healthy, severity, reasons = fn({})
        assert healthy is True and severity == "ok" and reasons == []

    def test_evaluate_health_signals_flags_open_breaker_critical(self):
        fn = api_mod._system_routes.evaluate_health_signals
        report = {"subsystems": {"writer": {"is_open": True}}}
        healthy, severity, reasons = fn(report)
        assert healthy is False
        assert severity == "critical"
        assert any("breaker_open" in r for r in reasons)

    def test_evaluate_health_signals_watchdog_stale_is_critical(self):
        fn = api_mod._system_routes.evaluate_health_signals
        report = {
            "watchdog_monitoring": {
                "last_ping_ago_seconds": 999.0,
                "total_pings": 50,
            }
        }
        healthy, severity, reasons = fn(report)
        assert healthy is False and severity == "critical"

    def test_evaluate_health_signals_writer_failure_rate_warns(self):
        fn = api_mod._system_routes.evaluate_health_signals
        report = {
            "write_coordinators": [
                {"db_path": "x.db", "writes_total": 1000, "writes_failed": 50}
            ]
        }
        healthy, severity, reasons = fn(report)
        assert healthy is False and severity == "warning"
        assert any("writes_failed_rate" in r for r in reasons)

    def test_evaluate_health_signals_severity_escalation_ordering(self):
        fn = api_mod._system_routes.evaluate_health_signals
        # warning-only signals stay at warning even combined
        warn_report = {
            "write_coordinators": [
                {"db_path": "a.db", "writes_total": 100, "writes_failed": 5}
            ],
            "task_queue": {"depth": 100},
        }
        _, sev, _ = fn(warn_report)
        assert sev == "warning"
        # critical dominates warning
        mixed = dict(warn_report)
        mixed["pipeline_integrity"] = {"healthy": False}
        _, sev2, _ = fn(mixed)
        assert sev2 == "critical"


# ===========================================================================
# Layer 5 — negative space / fail-closed guards
# ===========================================================================


class TestNegativeSpace:
    def test_no_middleware_adds_auth_to_health_paths(self):
        """No auth middleware may special-case the trio into protection."""
        for mw in api_mod.app.user_middleware:
            cls = getattr(mw, "cls", mw)
            name = getattr(cls, "__name__", str(cls))
            assert "auth" not in name.lower() or "cors" in name.lower(), (
                f"suspicious middleware on app: {name}"
            )

    def test_trio_not_in_any_gated_constant_in_tests_dir(self):
        """Sanity: our own constants are right."""
        assert "/health/detailed" not in PUBLIC_TRIO
        assert "/health/deep" not in PUBLIC_TRIO
        for p in PUBLIC_TRIO:
            assert p.startswith("/health")

    def test_source_wide_no_require_admin_on_trio_lines(self):
        for line in API_SOURCE.splitlines():
            if '"/health"' in line or '"/health/livez"' in line or '"/health/readyz"' in line:
                assert "require_admin" not in line, line
                assert "_auth" not in line, line

    def test_gated_paths_do_have_require_admin_in_source(self):
        found = 0
        for line in API_SOURCE.splitlines():
            if '"/health/detailed"' in line or '"/health/deep"' in line:
                assert "require_admin_or_loopback" in line, line
                found += 1
        assert found >= 2

    def test_module_never_touches_paper_trade_statuses(self):
        """This characterization file itself must not arm anything."""
        src = inspect.getsource(sys.modules[__name__])
        marker = "_PAPER_TRADE" + "_SIGNAL_STATUSES"
        assert marker not in src
        assert "generate_paper_trade_" + "signal" not in src
