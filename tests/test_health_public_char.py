"""Pin the public health endpoints: /health, /health/livez, /health/readyz.

These three endpoints MUST stay unauthenticated — they are polled by the
sentinel (Layer 3), k8s-style probes, and external watchdogs that do not
carry an admin token. If anyone adds `Depends(require_admin)` or
`Depends(require_admin_or_loopback)` to any of them, these tests fail.

Two layers of pinning:
  1. Static route inspection: walk `api.app.routes` and assert none of the
     three routes carries require_admin / require_admin_or_loopback in its
     FastAPI dependency graph.
  2. Behavioral: exercise each endpoint through TestClient with no auth
     header at all and assert a non-401/403 response.

We deliberately avoid entering the real lifespan (it loads DBs, MCP, etc.)
by using TestClient without touching app startup where possible; the health
handlers themselves are safe to call because they degrade gracefully when
subsystem globals are unset.
"""

from __future__ import annotations

import os
import sys
import importlib

import pytest
from fastapi.routing import APIRoute


# ---------------------------------------------------------------------------
# Import api.py without triggering lifespan.
# ---------------------------------------------------------------------------

def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""


pytestmark = pytest.mark.skipif(
    api_mod is None, reason=f"Could not import api module: {_import_err_msg}"
)

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
_ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}


def _collect_dependencies(route: APIRoute) -> set[str]:
    """Return names of every callable reachable from the route's deps."""
    seen: set[str] = set()

    def _walk(dep):
        if dep is None:
            return
        call = getattr(dep, "call", None)
        if call is not None and getattr(call, "__name__", "") in _ADMIN_DEP_NAMES:
            seen.add(call.__name__)
        for sub in getattr(dep, "dependencies", []) or []:
            _walk(sub)

    for d in route.dependencies:
        _walk(d)
    _walk(route.dependant)
    return seen


# ---------------------------------------------------------------------------
# 1. Static route-graph inspection — no admin gate anywhere in the dep tree.
# ---------------------------------------------------------------------------

class TestHealthRoutesArePublic:
    def test_all_three_routes_exist(self):
        paths = {
            r.path for r in api_mod.app.routes
            if isinstance(r, APIRoute)
        }
        for p in PUBLIC_HEALTH_PATHS:
            assert p in paths, f"{p} is missing from the app's routes"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_require_admin_in_dependency_graph(self, path: str):
        routes = [
            r for r in api_mod.app.routes
            if isinstance(r, APIRoute) and r.path == path
        ]
        assert routes, f"no APIRoute found for {path}"
        for route in routes:
            dep_names = _collect_dependencies(route)
            banned = dep_names & _ADMIN_DEP_NAMES
            assert not banned, (
                f"{path} must be public but carries auth dependencies: "
                f"{sorted(banned)}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_is_get_only_and_unsecured(self, path: str):
        """The route should accept GET and have zero route-level deps."""
        routes = [
            r for r in api_mod.app.routes
            if isinstance(r, APIRoute) and r.path == path
        ]
        assert routes
        route = routes[0]
        assert "GET" in route.methods
        assert not route.dependencies, (
            f"{path} gained route-level dependencies: {route.dependencies}"
        )


# ---------------------------------------------------------------------------
# 2. Behavioral — call each endpoint with no Authorization header at all.
# ---------------------------------------------------------------------------

class TestHealthEndpointsRespondWithoutAuth:
    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        # Constructing TestClient does NOT run lifespan until used as a
        # context manager; plain requests still work against the ASGI app.
        self.client = TestClient(api_mod.app)

    def test_health_livez_is_openly_reachable(self):
        resp = self.client.get("/health/livez")
        assert resp.status_code == 200
        body = resp.json()
        assert body["alive"] is True
        assert isinstance(body["ts"], float)

    def test_health_responds_without_token(self):
        resp = self.client.get("/health")
        # Must never be an auth rejection; content may reflect degraded state.
        assert resp.status_code != 401, "/health started requiring auth"
        assert resp.status_code != 403, "/health started requiring auth"
        body = resp.json()
        assert "healthy" in body

    def test_health_detailed_still_requires_auth(self):
        """Guardrail: the *detailed* variant keeps its gate (contrast case)."""
        detailed_routes = [
            r for r in api_mod.app.routes
            if isinstance(r, APIRoute) and r.path == "/health/detailed"
        ]
        assert detailed_routes, "/health/detailed missing"
        dep_names = _collect_dependencies(detailed_routes[0])
        assert dep_names & _ADMIN_DEP_NAMES, (
            "/health/detailed lost its auth gate — public/private split broken"
        )
