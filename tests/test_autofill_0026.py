"""autofill #0026 — public health trio characterization (LONG).

Characterizes the public/gated split of the health endpoint family:

  PUBLIC (must never gain require_admin / require_admin_or_loopback):
    * /health          — polled by the sentinel and watchdog
    * /health/livez    — k8s-style liveness probe
    * /health/readyz   — k8s-style readiness probe

  GATED (must KEEP their admin gate — the contrast case):
    * /health/detailed
    * /health/deep

Pinning strategy, four layers:

  1. Source-level: read api.py text and assert the three public routes are
     declared with a bare ``@app.get(path)`` decorator (no ``dependencies=``
     kwarg, no ``require_admin`` anywhere on that line), while the gated two
     carry ``Depends(require_admin_or_loopback)``.
  2. Route-graph: walk ``api.app.routes`` and assert the FastAPI dependency
     trees for the public trio contain no admin dependency, while the gated
     pair still does.
  3. Behavioral: hit each endpoint through TestClient with no Authorization
     header; the public trio must never answer 401/403, the gated pair must
     refuse non-loopback unauthenticated access.
  4. Handler-body: call the underlying handlers from tools/api/system_routes
     directly to pin response shapes (liveness always 200; readiness demotes
     to 503 when unhealthy).

Fail-closed stance: if any pin is currently false, this module FAILS — it
never relaxes production gates. No test here touches betting status logic;
the executor-enable seal ("live" never in _PAPER_TRADE_SIGNAL_STATUSES) is
only asserted as an untouched invariant.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import sys

import pytest
from fastapi.routing import APIRoute

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Load api.py without triggering lifespan.
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

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep")
_ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

SYSTEM_ROUTES_PATH = os.path.join(REPO, "tools", "api", "system_routes.py")
if os.path.exists(SYSTEM_ROUTES_PATH):
    with open(SYSTEM_ROUTES_PATH) as _f:
        SYSTEM_ROUTES_SOURCE = _f.read()
else:
    SYSTEM_ROUTES_SOURCE = ""


def _routes_for(path: str) -> list[APIRoute]:
    return [
        r
        for r in api_mod.app.routes
        if isinstance(r, APIRoute) and r.path == path
    ]


def _collect_dependency_names(route: APIRoute) -> set[str]:
    """Names of every admin callable reachable in the route's dep graph."""
    seen: set[str] = set()

    def _walk(dep) -> None:
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


def _decorator_block(source: str, path: str) -> str:
    """Return the @app.get(...) decorator line(s) for `path`."""
    pattern = re.compile(
        r'@app\.get\(\s*["\']' + re.escape(path) + r'["\'][^\n]*',
    )
    m = pattern.search(source)
    return m.group(0) if m else ""


# ---------------------------------------------------------------------------
# Layer 1 — source-level pins on api.py decorators.
# ---------------------------------------------------------------------------


class TestSourceLevelPublicTrio:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_exists(self, path: str):
        block = _decorator_block(API_SOURCE, path)
        assert block, f"@app.get({path!r}) decorator missing from api.py"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_dependencies_kwarg(self, path: str):
        block = _decorator_block(API_SOURCE, path)
        assert "dependencies=" not in block, (
            f"{path} decorator gained a dependencies= kwarg: {block!r}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_require_admin_text(self, path: str):
        block = _decorator_block(API_SOURCE, path)
        for name in sorted(_ADMIN_DEP_NAMES):
            assert name not in block, f"{path} decorator mentions {name}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_is_plain_get(self, path: str):
        block = _decorator_block(API_SOURCE, path)
        assert block.startswith(f'@app.get("{path}")') or block.startswith(
            f"@app.get('{path}')"
        ), f"{path} is no longer declared as a plain bare GET: {block!r}"

    def test_public_trio_docstrings_stay_public(self):
        """The docstrings self-document the public contract; keep them."""
        for path, token in [
            ("/health", "PUBLIC"),
            ("/health/livez", "PUBLIC"),
            ("/health/readyz", "PUBLIC"),
        ]:
            block = _decorator_block(API_SOURCE, path)
            assert block, f"{path} missing"
            tail = API_SOURCE[
                API_SOURCE.index(block) : API_SOURCE.index(block) + 600
            ]
            assert token in tail, (
                f"{path} docstring dropped its PUBLIC marker"
            )


class TestSourceLevelGatedContrast:
    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_decorator_keeps_loopback_admin_gate(self, path: str):
        block = _decorator_block(API_SOURCE, path)
        assert block, f"@app.get({path!r}) missing from api.py"
        assert "require_admin_or_loopback" in block, (
            f"{path} lost its require_admin_or_loopback gate in source"
        )
        assert "Depends(require_admin_or_loopback)" in block, (
            f"{path} gate is not wired through Depends(): {block!r}"
        )

    def test_detailed_and_deep_are_not_accidentally_public(self):
        for path in GATED_HEALTH_PATHS:
            block = _decorator_block(API_SOURCE, path)
            assert "dependencies=[Depends(require_admin_or_loopback)]" in block


class TestSourceLevelSystemRoutesSplit:
    """The moved handler module documents/enforces the same split."""

    def test_system_routes_header_documents_the_split(self):
        if not SYSTEM_ROUTES_SOURCE:
            pytest.skip("tools/api/system_routes.py absent")
        assert "stay PUBLIC" in SYSTEM_ROUTES_SOURCE
        assert "require_admin_or_loopback gated" in SYSTEM_ROUTES_SOURCE

    def test_livez_body_has_no_auth_logic(self):
        if not SYSTEM_ROUTES_SOURCE:
            pytest.skip("tools/api/system_routes.py absent")
        m = re.search(
            r"async def health_livez\(.*?(?=\nasync def |\n@app|\Z)",
            SYSTEM_ROUTES_SOURCE,
            re.DOTALL,
        )
        assert m, "health_livez handler missing from system_routes"
        body = m.group(0)
        for banned in ("require_admin", "HTTPException", "Authorization"):
            assert banned not in body, (
                f"health_livez body references {banned}"
            )

    def test_readyz_body_demotes_to_503(self):
        if not SYSTEM_ROUTES_SOURCE:
            pytest.skip("tools/api/system_routes.py absent")
        m = re.search(
            r"async def health_readyz\(.*?(?=\nasync def |\n@app|\Z)",
            SYSTEM_ROUTES_SOURCE,
            re.DOTALL,
        )
        assert m, "health_readyz handler missing from system_routes"
        body = m.group(0)
        assert "503" in body
        assert "HTTPException" in body
        assert "ready" in body

    def test_api_py_health_handlers_delegate_not_reimplement_auth(self):
        """api.py's thin wrappers must not add gating around delegation."""
        for fn_name in ("health_check", "health_livez", "health_readyz"):
            src = inspect.getsource(getattr(api_mod, fn_name))
            for banned in sorted(_ADMIN_DEP_NAMES):
                assert banned not in src, (
                    f"api.{fn_name} references {banned}"
                )


# ---------------------------------------------------------------------------
# Layer 2 — live route-graph inspection on app.routes.
# ---------------------------------------------------------------------------


class TestRouteGraphPublicTrio:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_exists_as_get(self, path: str):
        routes = _routes_for(path)
        assert routes, f"{path} route missing from app.routes"
        assert "GET" in routes[0].methods

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_has_zero_route_level_dependencies(self, path: str):
        route = _routes_for(path)[0]
        assert not route.dependencies, (
            f"{path} gained route-level dependencies: {route.dependencies}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_dep_graph_has_no_admin_callable(self, path: str):
        names = _collect_dependency_names(_routes_for(path)[0])
        overlap = names & _ADMIN_DEP_NAMES
        assert not overlap, (
            f"{path} is public but carries auth deps: {sorted(overlap)}"
        )

    def test_exactly_one_route_per_public_path(self):
        for path in PUBLIC_HEALTH_PATHS:
            assert len(_routes_for(path)) == 1, (
                f"duplicate route registrations for {path}"
            )

    def test_public_paths_have_no_path_params(self):
        for path in PUBLIC_HEALTH_PATHS:
            route = _routes_for(path)[0]
            assert "{" not in route.path, f"{path} grew a path parameter"

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_public_trio_accepts_only_safe_methods(self, path, method):
        """No mutating variant of the public trio should exist."""
        for r in api_mod.app.routes:
            if isinstance(r, APIRoute) and r.path == path:
                assert method not in r.methods, (
                    f"{method} {path} exists — health endpoints are read-only"
                )


class TestRouteGraphGatedContrast:
    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_route_dep_graph_keeps_admin(self, path: str):
        routes = _routes_for(path)
        assert routes, f"{path} route missing"
        names = _collect_dependency_names(routes[0])
        assert names & _ADMIN_DEP_NAMES, (
            f"{path} lost its admin gate — public/private split broken"
        )

    def test_integrity_history_also_gated(self):
        routes = _routes_for("/health/integrity/history")
        assert routes, "/health/integrity/history missing"
        names = _collect_dependency_names(routes[0])
        assert names & _ADMIN_DEP_NAMES

    def test_full_status_endpoint_remains_gated(self):
        routes = _routes_for("/system/full-status")
        assert routes, "/system/full-status missing"
        names = _collect_dependency_names(routes[0])
        assert names & _ADMIN_DEP_NAMES


# ---------------------------------------------------------------------------
# Layer 3 — behavioral: TestClient with NO Authorization header.
# ---------------------------------------------------------------------------


def _make_client():
    from fastapi.testclient import TestClient

    # Constructing TestClient does NOT run lifespan until used as a context
    # manager; plain requests work against the ASGI app directly.
    return TestClient(api_mod.app)


class TestBehavioralPublicTrioNoAuthHeader:
    def setup_method(self):
        self.client = _make_client()

    def test_livez_openly_reachable_200(self):
        resp = self.client.get("/health/livez")
        assert resp.status_code == 200
        body = resp.json()
        assert body["alive"] is True
        assert isinstance(body["ts"], float)

    def test_livez_with_bogus_bearer_still_200(self):
        """Even a bad token cannot make liveness fail — it ignores auth."""
        resp = self.client.get(
            "/health/livez", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 200
        assert resp.json()["alive"] is True

    def test_health_never_answers_401_or_403(self):
        resp = self.client.get("/health")
        assert resp.status_code not in (401, 403), (
            "/health started requiring auth"
        )
        assert "healthy" in resp.json()

    def test_health_with_bogus_bearer_never_401_or_403(self):
        resp = self.client.get(
            "/health", headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code not in (401, 403)

    def test_readyz_never_answers_401_or_403(self):
        """Readiness may be 503 (degraded), but never an auth rejection."""
        resp = self.client.get("/health/readyz")
        assert resp.status_code in (200, 503)
        assert resp.status_code not in (401, 403)

    def test_readyz_body_shape_when_unhealthy(self):
        resp = self.client.get("/health/readyz")
        if resp.status_code == 503:
            detail = resp.json().get("detail", {})
            assert detail.get("ready") is False
            assert "severity" in detail
            assert isinstance(detail.get("reasons"), list)
        else:
            body = resp.json()
            assert body.get("ready") is True
            assert body.get("severity") == "ok"


class TestBehavioralGatedContrast:
    """The gated variants must actually refuse unauthenticated callers.

    TestClient presents as loopback (testclient host), so when
    CALLISTO_ADMIN_TOKEN is unset require_admin_or_loopback would allow a
    loopback caller through. To keep this deterministic we set a token for
    the duration of the request so the soft-gate demands a Bearer header.
    """

    def setup_method(self):
        self.client = _make_client()

    @pytest.fixture(autouse=True)
    def _admin_token_env(self, monkeypatch):
        monkeypatch.setattr(api_mod, "CALLISTO_ADMIN_TOKEN", "unit-test-token")

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_refuses_without_token(self, path: str):
        resp = self.client.get(path)
        assert resp.status_code in (401, 403), (
            f"{path} served unauthenticated content with a token configured "
            f"(status={resp.status_code})"
        )

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_refuses_bad_token(self, path: str):
        resp = self.client.get(
            path, headers={"Authorization": "Bearer wrong-token"}
        )
        assert resp.status_code == 403, (
            f"{path} accepted a bad bearer token (status={resp.status_code})"
        )


# ---------------------------------------------------------------------------
# Layer 4 — direct handler-body characterization.
# ---------------------------------------------------------------------------


class TestHandlerBodiesDirect:
    def test_livez_handler_shape(self):
        import asyncio

        body = asyncio.run(api_mod._system_routes.health_livez())
        assert set(body) >= {"alive", "ts"}
        assert body["alive"] is True

    def test_readyz_handler_503_when_report_unhealthy(self):
        import asyncio

        from fastapi import HTTPException

        original = api_mod._system_routes.build_health_report

        async def fake_report():
            return {
                "healthy": False,
                "severity": "critical",
                "reasons": ["unit-test forced unhealthy"],
                "uptime_seconds": 1.5,
            }

        api_mod._system_routes.build_health_report = fake_report
        try:
            with pytest.raises(HTTPException) as excinfo:
                asyncio.run(api_mod._system_routes.health_readyz())
        finally:
            api_mod._system_routes.build_health_report = original
        assert excinfo.value.status_code == 503
        detail = excinfo.value.detail
        assert detail["ready"] is False
        assert detail["severity"] == "critical"
        assert "unit-test forced unhealthy" in detail["reasons"]

    def test_readyz_handler_ok_when_report_healthy(self):
        import asyncio

        original = api_mod._system_routes.build_health_report

        async def fake_report():
            return {"healthy": True, "uptime_seconds": 42.0}

        api_mod._system_routes.build_health_report = fake_report
        try:
            body = asyncio.run(api_mod._system_routes.health_readyz())
        finally:
            api_mod._system_routes.build_health_report = original
        assert body == {
            "ready": True,
            "severity": "ok",
            "uptime_seconds": 42.0,
        }


# ---------------------------------------------------------------------------
# Adjacent invariants that share the fail-closed spirit of this pin.
# ---------------------------------------------------------------------------


class TestAdjacentFailClosedInvariants:
    def test_paper_trade_signal_statuses_never_contain_live(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, (frozenset, set))
        assert "live" not in _PAPER_TRADE_SIGNAL_STATUSES, (
            "'live' entered _PAPER_TRADE_SIGNAL_STATUSES — NEVER do this"
        )
        # The frozenset literal in paper.py is the single source of truth.
        with open(os.path.join(REPO, "tools", "signals", "paper.py")) as f:
            paper_src = f.read()
        m = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES\s*=\s*frozenset\(\{([^}]*)\}\)", paper_src)
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES definition changed shape"
        statuses = {s.strip().strip('"\'') for s in m.group(1).split(",") if s.strip()}
        assert "live" not in statuses

    def test_generate_paper_trade_signal_source_not_widened_to_live(self):
        fn = getattr(api_mod, "generate_paper_trade_signal", None)
        if fn is None:
            pytest.skip("generate_paper_trade_signal not present")
        src = inspect.getsource(fn)
        assert "status == 'live'" not in src
        assert 'status=="live"' not in src

    def test_default_secure_middleware_still_present_in_source(self):
        assert "_default_secure_middleware" in API_SOURCE, (
            "default-secure write gate middleware vanished from api.py"
        )

    def test_public_write_allowlist_stays_a_set_and_small(self):
        allowlist = getattr(api_mod, "_PUBLIC_WRITE_ENDPOINTS", None)
        assert isinstance(allowlist, set), (
            "_PUBLIC_WRITE_ENDPOINTS is not a set"
        )
        assert len(allowlist) <= 32, (
            f"_PUBLIC_WRITE_ENDPOINTS ballooned: {len(allowlist)} entries"
        )
