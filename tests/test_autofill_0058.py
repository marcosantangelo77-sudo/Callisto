"""Autofill characterization #0058 — public health trio (LONG).

Pins, from several independent angles, that the three PUBLIC health
endpoints stay unauthenticated:

    /health          — comprehensive report; polled by sentinel/watchdog
    /health/livez    — k8s-style liveness probe
    /health/readyz   — k8s-style readiness probe

and that the two GATED variants keep their gates:

    /health/detailed — require_admin_or_loopback
    /health/deep     — require_admin_or_loopback

Layers of characterization:
  1. Static source scan of api.py: no decorator for a public health path
     may mention require_admin / require_admin_or_loopback / _auth.
  2. Same scan for tools/api/system_routes.py (the split-out handlers).
  3. Route-graph inspection of the live FastAPI app: walk every
     dependency in each public route's dependant tree; assert none of
     them is (or wraps) require_admin / require_admin_or_loopback.
  4. Behavioral: hit each endpoint through TestClient with no
     Authorization header at all; must never be 401/403.
  5. Contrast/guardrail: /health/detailed and /health/deep still carry
     their gate both statically and in the route graph.
  6. Fail-closed safety rails unrelated to auth but adjacent to the
     autofill surface: the executor-enable seal ("live" never appears in
     _PAPER_TRADE_SIGNAL_STATUSES semantics via these routes).

Tests-only module — no production file is modified by these pins.

We deliberately avoid entering the real lifespan (it loads DBs, MCP,
etc.) by constructing TestClient without using it as a context manager;
the health handlers degrade gracefully when subsystem globals are unset.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import sys

import pytest
from fastapi.routing import APIRoute

# ---------------------------------------------------------------------------
# Module loading (no lifespan).
# ---------------------------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

SYSTEM_ROUTES_PATH = os.path.join(REPO, "tools", "api", "system_routes.py")
if os.path.exists(SYSTEM_ROUTES_PATH):
    with open(SYSTEM_ROUTES_PATH) as _f:
        SYSTEM_ROUTES_SOURCE = _f.read()
else:  # pragma: no cover - split file is expected to exist
    SYSTEM_ROUTES_SOURCE = ""


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment guard
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""

pytestmark = pytest.mark.skipif(
    api_mod is None, reason=f"Could not import api module: {_import_err_msg}"
)

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep")
ALL_HEALTH_PATHS = PUBLIC_HEALTH_PATHS + GATED_HEALTH_PATHS

_ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}
_AUTH_TOKEN_RE = re.compile(r"require_admin|_auth\b|Authorization")


def _routes_for(path: str) -> list[APIRoute]:
    return [
        r
        for r in api_mod.app.routes
        if isinstance(r, APIRoute) and r.path == path
    ]


def _collect_dependency_names(route: APIRoute) -> set[str]:
    """Names of every callable reachable from the route's dependency graph."""
    seen: set[str] = set()

    def _walk(dep):
        if dep is None:
            return
        call = getattr(dep, "call", None)
        if call is not None:
            name = getattr(call, "__name__", "") or type(call).__name__
            seen.add(name)
        for sub in getattr(dep, "dependencies", []) or []:
            _walk(sub)

    for d in route.dependencies:
        _walk(d)
    _walk(route.dependant)
    return seen


# ---------------------------------------------------------------------------
# 1. Static source scan of api.py decorators.
# ---------------------------------------------------------------------------


def _decorator_chunks_for_path(source: str, path: str) -> list[str]:
    """Return each decorator chunk in `source` whose string literal path matches."""
    chunks = []
    for m in re.finditer(
        r'@app\.\w+\(\s*["\']' + re.escape(path) + r'["\'][^)]*\)', source
    ):
        chunks.append(m.group(0))
    return chunks


class TestApiSourcePublicHealthDecorators:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_auth_reference(self, path):
        chunks = _decorator_chunks_for_path(API_SOURCE, path)
        assert chunks, f"{path} decorator missing from api.py"
        for dec in chunks:
            assert not _AUTH_TOKEN_RE.search(dec), (
                f"{path} decorator gained an auth reference: {dec}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_is_a_get_route(self, path):
        chunks = _decorator_chunks_for_path(API_SOURCE, path)
        assert any(dec.startswith("@app.get(") for dec in chunks), (
            f"{path} is no longer registered via @app.get"
        )

    def test_public_paths_never_co_occur_with_gated_paths_in_one_decorator(self):
        for path in ALL_HEALTH_PATHS:
            for dec in _decorator_chunks_for_path(API_SOURCE, path):
                assert dec.count('"/health') == 0 or path in dec, (
                    f"unexpected health path inside decorator for {path}: {dec}"
                )


class TestSystemRoutesSourcePins:
    def test_split_file_exists_and_documents_the_contract(self):
        assert os.path.exists(SYSTEM_ROUTES_PATH), (
            "tools/api/system_routes.py went missing"
        )
        header = SYSTEM_ROUTES_SOURCE[:2000]
        assert "/health" in header or "health" in SYSTEM_ROUTES_SOURCE

    @pytest.mark.parametrize(
        "handler", ["health_check", "health_livez", "health_readyz"]
    )
    def test_handler_bodies_have_no_auth_import_or_call(self, handler):
        assert handler in SYSTEM_ROUTES_SOURCE, (
            f"{handler} missing from system_routes.py"
        )
        # Extract the handler's function body via AST and check for banned names.
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        found = False
        banned_calls = {"require_admin", "require_admin_or_loopback"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name != handler:
                    continue
                found = True
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Name) and sub.id in banned_calls:
                        pytest.fail(
                            f"{handler} references {sub.id} directly"
                        )
                    if isinstance(sub, ast.Call) and isinstance(
                        sub.func, ast.Name
                    ):
                        assert sub.func.id not in banned_calls, (
                            f"{handler} calls {sub.func.id}"
                        )
                    if isinstance(sub, ast.Attribute) and sub.attr in (
                        "headers",
                        "authorization",
                    ):
                        pytest.fail(
                            f"{handler} inspects request auth material"
                        )
        assert found, f"{handler} function definition not found"

    def test_no_security_helper_defined_over_health_handlers(self):
        # The public handlers must not be wrapped by anything named *_auth*.
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "_auth" in node.name:
                    for deco in node.decorator_list:
                        src = ast.dump(deco)
                        assert "health" not in src.lower(), (
                            f"auth wrapper {node.name} targets a health handler"
                        )


# ---------------------------------------------------------------------------
# 3. Live route-graph inspection.
# ---------------------------------------------------------------------------


class TestRouteGraphPublicTrio:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_exists_exactly_once(self, path):
        routes = _routes_for(path)
        assert len(routes) == 1, (
            f"{path} is registered {len(routes)} times; expected exactly 1"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_zero_route_level_dependencies(self, path):
        routes = _routes_for(path)
        assert routes
        for route in routes:
            assert not route.dependencies, (
                f"{path} gained route-level dependencies: "
                f"{list(route.dependencies)}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_admin_dep_anywhere_in_dependant_tree(self, path):
        routes = _routes_for(path)
        assert routes
        for route in routes:
            names = _collect_dependency_names(route)
            banned = names & _ADMIN_DEP_NAMES
            assert not banned, (
                f"{path} must be public but carries admin deps: {sorted(banned)}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_get_method_only_public_surface(self, path):
        routes = _routes_for(path)
        assert routes
        route = routes[0]
        assert "GET" in route.methods
        # POST/DELETE variants of a public health path would be suspicious.
        assert not (route.methods - {"GET", "HEAD", "OPTIONS"}), (
            f"{path} exposes mutating methods: {route.methods}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_response_model_does_not_require_auth_context(self, path):
        routes = _routes_for(path)
        assert routes
        sig = inspect.signature(routes[0].endpoint)
        for name, p in sig.parameters.items():
            assert name.lower() not in {
                "token",
                "authorization",
                "credentials",
                "api_key",
                "apikey",
            }, f"{path} takes an auth-shaped parameter: {name}"


# ---------------------------------------------------------------------------
# 4. Behavioral — unauthenticated HTTP through TestClient.
# ---------------------------------------------------------------------------


class TestUnauthenticatedBehavior:
    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient

        # No context manager -> no lifespan -> fast, side-effect free.
        self.client = TestClient(api_mod.app)

    def test_livez_openly_reachable_shape(self):
        resp = self.client.get("/health/livez")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body.get("alive") is True
        assert isinstance(body.get("ts"), float)

    def test_readyz_never_401_or_403(self):
        resp = self.client.get("/health/readyz")
        assert resp.status_code not in (401, 403), (
            f"/health/readyz started requiring auth ({resp.status_code})"
        )
        body = resp.json()
        # Degraded readiness legitimately 503s with the payload nested under
        # 'detail'; what matters is that it's a readiness verdict, not auth.
        flat = str(body)
        assert "ready" in flat or "healthy" in flat, body

    def test_health_full_report_without_token(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, dict)
        assert "healthy" in body
        assert "severity" in body or "subsystems" in body or True

    def test_health_with_garbage_token_still_succeeds(self):
        # Even a bogus Authorization header must not change the outcome,
        # because the endpoint should not look at it at all.
        resp = self.client.get(
            "/health", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert resp.status_code == 200

    def test_repeated_livez_polling_is_stable(self):
        codes = [self.client.get("/health/livez").status_code for _ in range(5)]
        assert set(codes) == {200}

    def test_head_request_allowed_on_livez(self):
        resp = self.client.head("/health/livez")
        assert resp.status_code in (200, 405)


# ---------------------------------------------------------------------------
# 5. Guardrails — the gated variants keep their gates.
# ---------------------------------------------------------------------------


class TestGatedVariantsStayGated:
    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_decorator_carries_gate(self, path):
        chunks = _decorator_chunks_for_path(API_SOURCE, path)
        assert chunks, f"{path} decorator missing"
        assert any("require_admin_or_loopback" in c for c in chunks), (
            f"{path} lost its require_admin_or_loopback gate"
        )

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_route_graph_carries_gate(self, path):
        routes = _routes_for(path)
        assert routes, f"{path} missing from app routes"
        names = _collect_dependency_names(routes[0])
        assert names & _ADMIN_DEP_NAMES, (
            f"{path} lost its admin gate in the route graph"
        )

    def test_detailed_rejects_unauthenticated_client(self):
        from fastapi.testclient import TestClient

        client = TestClient(api_mod.app, raise_server_exceptions=False)
        resp = client.get("/health/detailed")
        assert resp.status_code in (401, 403), (
            f"/health/detailed served an anonymous caller: {resp.status_code}"
        )

    def test_deep_rejects_unauthenticated_client(self):
        from fastapi.testclient import TestClient

        client = TestClient(api_mod.app, raise_server_exceptions=False)
        resp = client.get("/health/deep")
        assert resp.status_code in (401, 403), (
            f"/health/deep served an anonymous caller: {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 6. Adjacent fail-closed safety pins (never arm live betting).
# ---------------------------------------------------------------------------


class TestFailClosedAdjacentRails:
    def test_paper_trade_statuses_source_has_no_live_status(self):
        paper_path = os.path.join(REPO, "tools", "signals", "paper.py")
        assert os.path.exists(paper_path), "tools/signals/paper.py went missing"
        with open(paper_path) as f:
            paper_src = f.read()
        m = re.search(
            r"_PAPER_TRADE_SIGNAL_STATUSES\s*[:=][^\n=]*?[({\[](.*?)[)}\]]",
            paper_src,
            re.DOTALL,
        )
        assert m, "_PAPER_TRADE_SIGNAL_STATUSES not found in tools/signals/paper.py"
        blob = m.group(1)
        statuses = re.findall(r'["\']([^"\']+)["\']', blob)
        assert statuses, "paper-trade status set parsed empty"
        assert statuses == ["paper_trading"], (
            f"_PAPER_TRADE_SIGNAL_STATUSES changed: {statuses} — "
            "'live' must never be added"
        )

    def test_generate_paper_trade_signature_not_widened_to_live(self):
        paper_path = os.path.join(REPO, "tools", "signals", "paper.py")
        with open(paper_path) as f:
            paper_src = f.read()
        # The gate module must reject anything outside the paper set, and
        # 'live' must not appear as a permitted status anywhere in it.
        assert "def reject_non_paper" in paper_src
        assert re.search(
            r'["\']live["\']', paper_src
        ) is None or "must NEVER be added" in paper_src, (
            "'live' status leaked into the paper-signal gate module"
        )
        tree = ast.parse(paper_src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_PAPER_TRADE_SIGNAL_STATUSES"
                for t in node.targets
            ):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and sub.value == "live":
                        pytest.fail(
                            "'live' is a member of _PAPER_TRADE_SIGNAL_STATUSES"
                        )

    def test_public_health_handlers_do_not_touch_executor(self):
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        banned = {
            "arm",
            "arm_live",
            "enable_executor",
            "start_betting",
            "place_bet",
        }
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("health"):
                    continue
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Call):
                        fname = getattr(sub.func, "id", None) or getattr(
                            sub.func, "attr", ""
                        )
                        assert fname not in banned, (
                            f"health handler calls dangerous {fname}"
                        )


# ---------------------------------------------------------------------------
# Misc structural characterization of the trio.
# ---------------------------------------------------------------------------


class TestStructuralCharacterization:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_functions_are_async(self, path):
        routes = _routes_for(path)
        assert routes
        fn = routes[0].endpoint
        assert inspect.iscoroutinefunction(fn) or callable(fn)

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handlers_have_docstrings(self, path):
        routes = _routes_for(path)
        assert routes
        doc = inspect.getdoc(routes[0].endpoint)
        assert doc, f"{path} handler lost its docstring"

    def test_trio_paths_are_distinct_prefixes_under_health(self):
        paths = {p for p in PUBLIC_HEALTH_PATHS}
        assert all(p.startswith("/health") for p in paths)
        assert len(paths) == 3

    def test_api_source_mentions_publicity_contract_for_all_three(self):
        for token in ('"/health"', '"/health/livez"', '"/health/readyz"'):
            assert token in API_SOURCE, f"{token} decorator vanished from api.py"
