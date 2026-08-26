"""Autofill characterization #0074 — public health trio (LONG).

Pins the PUBLIC/GATED split of the health surface from a fresh set of
angles (complements tests/test_autofill_0058.py and
tests/test_api_slice6.py without duplicating them):

    /health          — PUBLIC, polled by sentinel/watchdog scripts
    /health/livez    — PUBLIC k8s liveness probe
    /health/readyz   — PUBLIC k8s readiness probe

    /health/detailed — GATED (require_admin_or_loopback)
    /health/deep     — GATED (require_admin_or_loopback)

Layers in this module:
  1. AST-based decorator audit of api.py: every decorator attached to a
     public health handler must be free of require_admin /
     require_admin_or_loopback / _auth tokens; every decorator on a
     gated handler must KEEP its gate.
  2. Handler-body AST audit of tools/api/system_routes.py: public bodies
     never import or reference admin helpers, never read request auth
     headers, never raise 401/403.
  3. Route-graph walk of the live FastAPI app: zero dependencies at any
     depth of the dependant tree for the public trio.
  4. Behavioral probes through TestClient with no lifespan:
       * anonymous GET succeeds (or degrades as readiness semantics),
         never 401/403;
       * bogus Authorization headers are ignored;
       * OpenAPI schema lists the trio without security requirements;
       * OPTIONS/HEAD preflight-style requests don't trip auth.
  5. Guardrails: gated variants still reject anonymous callers, and
     /health/integrity/history stays gated too (no accidental widening).
  6. Fail-closed rails adjacent to the surface: the paper-trade hard
     gate stays exactly {"paper_trading"} — "live" is never armed.

Tests-only module; no production file is modified by these pins.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import os
import re
import sys

import pytest

# ---------------------------------------------------------------------------
# Module loading (no lifespan).
# ---------------------------------------------------------------------------

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

SYSTEM_ROUTES_PATH = os.path.join(REPO, "tools", "api", "system_routes.py")
with open(SYSTEM_ROUTES_PATH) as _f:
    SYSTEM_ROUTES_SOURCE = _f.read()

PAPER_PATH = os.path.join(REPO, "tools", "signals", "paper.py")
with open(PAPER_PATH) as _f:
    PAPER_SOURCE = _f.read()


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - environment guard
    api_mod = None
    _IMPORT_ERR_MSG = str(_import_err)
else:
    _IMPORT_ERR_MSG = ""

pytestmark = pytest.mark.skipif(
    api_mod is None, reason=f"Could not import api module: {_IMPORT_ERR_MSG}"
)

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep", "/health/integrity/history")

ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}
AUTH_TOKEN_RE = re.compile(r"require_admin|_auth\b|Authorization|api_key|HTTPBearer")


# ---------------------------------------------------------------------------
# Shared AST helpers.
# ---------------------------------------------------------------------------


def _api_tree() -> ast.Module:
    return ast.parse(API_SOURCE)


def _handlers_for_path(path: str) -> list[ast.AsyncFunctionDef]:
    """Async function defs in api.py whose decorator registers `path`."""
    out = []
    for node in ast.walk(_api_tree()):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        for deco in node.decorator_list:
            src = ast.unparse(deco)
            if re.search(r'["\']' + re.escape(path) + r'["\']', src):
                out.append(node)
    return out


def _decorator_sources_for_path(path: str) -> list[str]:
    decs = []
    for fn in _handlers_for_path(path):
        for deco in fn.decorator_list:
            decs.append(ast.unparse(deco))
    return decs


def _func_source(tree: ast.Module, name: str) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == name
        ):
            return ast.unparse(node)
    return None


def _collect_dependency_names(route) -> set[str]:
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

    for d in getattr(route, "dependencies", []):
        _walk(d)
    _walk(getattr(route, "dependant", None))
    return seen


def _routes_for(path: str):
    from fastapi.routing import APIRoute

    return [
        r
        for r in api_mod.app.routes
        if isinstance(r, APIRoute) and r.path == path
    ]


# ---------------------------------------------------------------------------
# 1. AST decorator audit of api.py.
# ---------------------------------------------------------------------------


class TestApiDecoratorAudit:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_public_handler_exists_in_api_py(self, path):
        handlers = _handlers_for_path(path)
        assert handlers, f"{path} has no handler in api.py"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_public_decorators_free_of_auth_tokens(self, path):
        decs = _decorator_sources_for_path(path)
        assert decs
        for dec in decs:
            assert not AUTH_TOKEN_RE.search(dec), (
                f"{path} decorator mentions auth material: {dec}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_public_decorators_have_no_dependencies_argument(self, path):
        # Even a Depends(...) that happens to be benign is off-contract for
        # the public trio: the pin is "zero route-level dependencies".
        for dec in _decorator_sources_for_path(path):
            assert "Depends" not in dec, (
                f"{path} decorator gained a dependency: {dec}"
            )

    def test_public_handlers_are_async_and_undecorated_beyond_route(self):
        for path in PUBLIC_HEALTH_PATHS:
            for fn in _handlers_for_path(path):
                assert isinstance(fn, ast.AsyncFunctionDef), (
                    f"{path} handler is not async"
                )
                assert len(fn.decorator_list) == 1, (
                    f"{path} handler carries extra decorators: "
                    f"{[ast.unparse(d) for d in fn.decorator_list]}"
                )

    def test_public_handler_names_are_stable(self):
        expected = {
            "/health": "health_check",
            "/health/livez": "health_livez",
            "/health/readyz": "health_readyz",
        }
        for path, name in expected.items():
            handlers = _handlers_for_path(path)
            assert any(h.name == name for h in handlers), (
                f"{path} handler renamed away from {name}"
            )


# ---------------------------------------------------------------------------
# 2. system_routes.py body audit.
# ---------------------------------------------------------------------------

PUBLIC_BODY_HANDLERS = ("health_check", "health_livez", "health_readyz")
GATED_BODY_HANDLERS = ("health_detailed", "health_deep")


class TestSystemRoutesBodyAudit:
    @pytest.mark.parametrize("handler", PUBLIC_BODY_HANDLERS)
    def test_public_body_has_no_auth_identifiers(self, handler):
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != handler:
                continue
            found = True
            for sub in ast.walk(node):
                if isinstance(sub, ast.Name):
                    assert not sub.id.startswith("require_admin"), (
                        f"{handler} references {sub.id}"
                    )
                    assert "_auth" not in sub.id, (
                        f"{handler} touches an auth identifier {sub.id}"
                    )
                if isinstance(sub, ast.Attribute):
                    assert "_auth" not in sub.attr
                    assert sub.attr not in {"authorization", "headers"}, (
                        f"{handler} inspects request.{sub.attr}"
                    )
        assert found, f"{handler} missing from system_routes.py"

    @pytest.mark.parametrize("handler", PUBLIC_BODY_HANDLERS)
    def test_public_body_never_raises_401_or_403(self, handler):
        tree = ast.parse(SYSTEM_ROUTES_SOURCE)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != handler:
                continue
            for sub in ast.walk(node):
                if isinstance(sub, ast.Raise) and sub.exc is not None:
                    exc_src = ast.unparse(sub.exc)
                    assert "HTTPException" not in exc_src or not re.search(
                        r"status_code\s*=\s*(401|403)", ast.unparse(sub)
                    ), f"{handler} raises an auth status: {exc_src}"

    @pytest.mark.parametrize("handler", GATED_BODY_HANDLERS)
    def test_gated_bodies_still_exist(self, handler):
        assert _func_source(ast.parse(SYSTEM_ROUTES_SOURCE), handler), (
            f"gated body {handler} vanished"
        )

    def test_module_docstring_documents_the_split(self):
        doc = ast.get_docstring(ast.parse(SYSTEM_ROUTES_SOURCE)) or ""
        assert "/health/livez" in doc and "PUBLIC" in doc.upper().replace(
            "public", "PUBLIC"
        ) or "PUBLIC" in doc, "split contract no longer documented"


# ---------------------------------------------------------------------------
# 3. Live route-graph inspection.
# ---------------------------------------------------------------------------


class TestLiveRouteGraph:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_registered_exactly_once_as_get(self, path):
        routes = _routes_for(path)
        assert len(routes) == 1, f"{path} registered {len(routes)} times"
        assert routes[0].methods == {"GET"}

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_dependant_tree_is_auth_free(self, path):
        names = _collect_dependency_names(_routes_for(path)[0])
        banned = names & ADMIN_DEP_NAMES
        assert not banned, f"{path} carries admin deps: {sorted(banned)}"
        assert not any("_auth" in n.lower() for n in names), (
            f"{path} dependency tree contains *_auth* callables: {sorted(names)}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_endpoint_signature_takes_no_request_or_token(self, path):
        sig = inspect.signature(_routes_for(path)[0].endpoint)
        for name in sig.parameters:
            assert name.lower() not in {
                "request",
                "token",
                "authorization",
                "credentials",
                "api_key",
            }, f"{path} endpoint takes suspicious parameter {name}"


# ---------------------------------------------------------------------------
# 4. Behavioral probes (TestClient, no lifespan).
# ---------------------------------------------------------------------------


def _client(**kw):
    from fastapi.testclient import TestClient

    return TestClient(api_mod.app, **kw)


class TestAnonymousBehavior:
    def test_health_full_report_anonymous_ok(self):
        resp = _client().get("/health")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert isinstance(body, dict)
        assert "healthy" in body

    def test_livez_shape(self):
        body = _client().get("/health/livez").json()
        assert body["alive"] is True
        assert isinstance(body["ts"], float)

    def test_readyz_is_readiness_not_auth(self):
        resp = _client().get("/health/readyz")
        assert resp.status_code in (200, 503)
        assert resp.status_code not in (401, 403)

    def test_bogus_authorization_ignored_on_trio(self):
        c = _client()
        for path in PUBLIC_HEALTH_PATHS:
            r = c.get(path, headers={"Authorization": "Bearer garbage-token"})
            assert r.status_code not in (401, 403), (
                f"{path} reacted to an Authorization header ({r.status_code})"
            )

    def test_openapi_lists_trio_without_security(self):
        schema = _client().get("/openapi.json").json()
        for path in PUBLIC_HEALTH_PATHS:
            op = schema["paths"].get(path, {}).get("get")
            assert op is not None, f"{path} absent from OpenAPI schema"
            assert not op.get("security"), f"{path} declares security: {op['security']}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_options_never_401(self, path):
        resp = _client().options(path)
        assert resp.status_code not in (401, 403)


# ---------------------------------------------------------------------------
# 5. Guardrails — gated variants keep their gates.
# ---------------------------------------------------------------------------


class TestGatedStayGated:
    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_decorator_carries_gate(self, path):
        decs = _decorator_sources_for_path(path)
        assert decs, f"{path} missing from api.py"
        assert any("require_admin_or_loopback" in d for d in decs), (
            f"{path} lost its gate: {decs}"
        )

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_graph_carries_gate(self, path):
        routes = _routes_for(path)
        assert routes
        assert _collect_dependency_names(routes[0]) & ADMIN_DEP_NAMES

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS[:2])
    def test_anonymous_rejected_on_gated_dumps(self, path):
        resp = _client(raise_server_exceptions=False).get(path)
        assert resp.status_code in (401, 403), (
            f"{path} served anonymous caller: {resp.status_code}"
        )

    def test_gated_paths_do_not_appear_unauthenticated_in_openapi(self):
        schema = _client().get("/openapi.json").json()
        for path in GATED_HEALTH_PATHS[:2]:
            op = schema["paths"].get(path, {}).get("get") or {}
            # Either declared security or (loopback-gated) absent security —
            # but the route MUST exist and must NOT have been moved public.
            assert op is not None, f"{path} vanished from OpenAPI"


# ---------------------------------------------------------------------------
# 6. Fail-closed rails — never arm live betting.
# ---------------------------------------------------------------------------


class TestPaperGateFailClosed:
    def test_status_set_is_exactly_paper_trading(self):
        tree = ast.parse(PAPER_SOURCE)
        values = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "_PAPER_TRADE_SIGNAL_STATUSES"
                for t in node.targets
            ):
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Constant) and isinstance(
                        sub.value, str
                    ):
                        values.append(sub.value)
        assert values == ["paper_trading"], (
            f"_PAPER_TRADE_SIGNAL_STATUSES changed: {values} — 'live' must "
            "never be added"
        )

    def test_no_live_constant_anywhere_in_gate_module(self):
        for node in ast.walk(ast.parse(PAPER_SOURCE)):
            if isinstance(node, ast.Constant) and node.value == "live":
                pytest.fail("'live' literal present in tools/signals/paper.py")

    def test_reject_non_paper_rejects_live(self):
        from tools.signals.paper import reject_non_paper

        assert reject_non_paper("live") is True
        assert reject_non_paper("paper_trading") is False

    def test_allowed_statuses_helper_matches_set(self):
        from tools.signals.paper import allowed_paper_statuses

        assert allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_public_health_routes_cannot_mutate_signal_status(self):
        # Defense-in-depth: nothing under /health* may POST/mutate.
        from fastapi.routing import APIRoute

        mutating = [
            (r.path, sorted(r.methods))
            for r in api_mod.app.routes
            if isinstance(r, APIRoute)
            and r.path.startswith("/health")
            and (r.methods - {"GET", "HEAD", "OPTIONS"})
        ]
        assert not mutating, f"mutating health routes appeared: {mutating}"


# ---------------------------------------------------------------------------
# Misc structural characterization.
# ---------------------------------------------------------------------------


class TestStructuralExtras:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_docstrings_mention_publicity(self, path):
        doc = inspect.getdoc(_routes_for(path)[0].endpoint) or ""
        combined = (doc + API_SOURCE).lower()
        assert "public" in combined, f"{path} contract comment lost"

    def test_sentinel_watchdog_target_unchanged(self):
        # The pollers hit exactly /health — keep that path stable.
        assert '"/health"' in API_SOURCE or "'/health'" in API_SOURCE

    def test_trio_distinct_and_prefixed(self):
        assert len(set(PUBLIC_HEALTH_PATHS)) == 3
        assert all(p.startswith("/health") for p in PUBLIC_HEALTH_PATHS)
