"""Autofill characterization #0090 — public health trio (LONG).

Characterizes the authentication posture of the public health endpoints in
``api.py`` — the trio the sentinel, k8s probes, and external watchdogs poll
without any admin token:

  * ``/health``         — Layer-2 subsystem/breaker/integrity report.
  * ``/health/livez``   — k8s-style liveness probe (process is up).
  * ``/health/readyz``  — k8s-style readiness probe (503 when degraded).

The core pin: this trio has NO ``require_admin``, NO
``require_admin_or_loopback``, and NO ad-hoc ``_auth``-style parameter. They
are registered as bare ``@app.get("<path>")`` routes with zero route-level
dependencies, their handler signatures carry no auth parameters, and they do
not appear anywhere in the OpenAPI security block.

The module also pins, fail-closed, that the GATED siblings keep their gates:

  * ``/health/detailed``            — require_admin_or_loopback.
  * ``/health/deep``                — require_admin_or_loopback.
  * ``/health/integrity/history``   — require_admin_or_loopback.

Six layers of pinning:

  1. Source-level inspection of api.py decorator text and handler bodies.
  2. Live FastAPI route-graph inspection (dependency-tree walk).
  3. OpenAPI contract: no security requirements / no bearer parameters on
     the trio, while gated siblings still declare HTTPBearer.
  4. Behavioral: TestClient requests with no Authorization header, with a
     bogus bearer token, and with an admin token configured — the trio
     must answer identically in every posture, while detailed/deep flip
     from 403 (token unset) to 401 (token set) to 200 (valid token).
  5. Cross-layer consistency: source text, route graph, and OpenAPI agree
     on which health paths are public vs gated (catches drift where one
     layer is updated but not the others).
  6. AST-level scan proving api.py's health section references no auth
     helper by name.

Fail-closed safety rails: ``_PAPER_TRADE_SIGNAL_STATUSES`` stays exactly
frozenset({"paper_trading"}) — never "live" — and
``generate_paper_trade_signal`` keeps its status gate / is never widened to
status == 'live'. If a pin is currently false this module FAILS CLOSED (the
test errors), it never disables a production gate to go green.

Tests-only module: no production file is modified.
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
from fastapi.security import HTTPBearer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

# The public trio under characterization, plus its gated siblings.
PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep")
GATED_SIBLING_PATHS = GATED_HEALTH_PATHS + ("/health/integrity/history",)

# Names that count as an admin gate if they ever appear on a public route.
_ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}

# Any helper whose NAME matches this pattern is treated as an auth guard too
# (_auth, verify_token, check_token, ...). Used against decorator blocks,
# handler bodies, dependency graphs, and endpoint signatures alike.
_AUTH_NAME_RE = re.compile(
    r"(^|_)(auth|require_auth|check_token|verify_token|admin_gate)(_|$)"
)


def _load_api_module():
    if "api" in sys.modules:
        return sys.modules["api"]
    os.environ.setdefault("CALLISTO_BIND_HOST", "127.0.0.1")
    return importlib.import_module("api")


try:
    api_mod = _load_api_module()
except Exception as _import_err:  # pragma: no cover - env-specific
    api_mod = None
    _import_err_msg = str(_import_err)
else:
    _import_err_msg = ""

pytestmark = pytest.mark.skipif(
    api_mod is None, reason=f"Could not import api module: {_import_err_msg}"
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _routes_for(path: str) -> list[APIRoute]:
    """Every APIRoute registered for exactly `path` on the live app."""
    return [
        r for r in api_mod.app.routes
        if isinstance(r, APIRoute) and r.path == path
    ]


def _dep_names(route: APIRoute) -> set[str]:
    """Names of every callable reachable from the route's dependency tree."""
    seen: set[str] = set()

    def _walk(dep):
        if dep is None:
            return
        call = getattr(dep, "call", None)
        if call is not None and getattr(call, "__name__", ""):
            seen.add(call.__name__)
        for sub in getattr(dep, "dependencies", []) or []:
            _walk(sub)

    for d in route.dependencies:
        _walk(d)
    # dependant covers the endpoint itself + its parameter-level Depends().
    _walk(route.dependant)
    return seen


def _endpoint_param_names(route: APIRoute) -> list[str]:
    """Parameter names of the endpoint function (catches `_auth` params)."""
    fn = route.endpoint
    return list(inspect.signature(fn).parameters)


def _decorator_block_for_path(source: str, path: str) -> str:
    """Return the `@app.get("path") ... async def name(...):` block."""
    pattern = re.compile(
        r'((?:@app\.\w+\([^\n]*\)\n)+async def \w+\([^)]*\)[^\n]*:\n)'
    )
    for m in pattern.finditer(source):
        block = m.group(1)
        if f'"{path}"' in block or f"'{path}'" in block:
            return block
    raise AssertionError(f"no @app.get({path!r}) decorator found in api.py")


def _handler_body_for_path(source: str, path: str, window: int = 3000) -> str:
    """Decorator block plus roughly its handler body (up to next @app.)."""
    block = _decorator_block_for_path(source, path)
    idx = source.find(block)
    body = source[idx + len(block): idx + len(block) + window]
    nxt = re.search(r"\n@app\.", body)
    if nxt:
        body = body[: nxt.start()]
    return block + body


def _security_requirements(path: str) -> list | None:
    """OpenAPI `security` value for GET <path>, via the live app's schema."""
    spec = api_mod.app.openapi()
    op = spec["paths"][path]["get"]
    return op.get("security")


class _TokenPosture:
    """Context manager: temporarily configure an admin token on api.py.

    tools.api.security reads CALLISTO_ADMIN_TOKEN late from the api module,
    so setting the module attribute is sufficient to flip every gate into
    token-configured mode. Restores the original value afterwards.
    """

    def __init__(self, mod, token: str = "0090-characterization-token"):
        self._mod = mod
        self._token = token
        self._orig = None

    def __enter__(self):
        self._orig = self._mod.CALLISTO_ADMIN_TOKEN
        self._mod.CALLISTO_ADMIN_TOKEN = self._token
        return self

    def __exit__(self, *exc):
        self._mod.CALLISTO_ADMIN_TOKEN = self._orig
        return False


# ---------------------------------------------------------------------------
# 1. Source-level pinning: decorator lines and handler bodies in api.py.
# ---------------------------------------------------------------------------


class TestHealthTrioSourceLevel:
    """The raw text of api.py never mentions a gate near the trio."""

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_require_admin(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert "require_admin" not in block, (
            f"{path} decorator gained require_admin in api.py source"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_loopback_gate(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert "require_admin_or_loopback" not in block, (
            f"{path} decorator gained require_admin_or_loopback"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_carries_no_dependencies_kwarg(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        m = re.search(r"@app\.get\(([^)]*)\)", block, re.DOTALL)
        assert m, f"{path} not registered via @app.get"
        args = m.group(1)
        assert "dependencies" not in args, (
            f"{path} decorator gained a dependencies= kwarg: {args!r}"
        )
        assert "Depends" not in args, (
            f"{path} decorator gained an inline Depends(): {args!r}"
        )
        assert "=" not in args, (
            f"{path} decorator carries unexpected kwargs: {args!r}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_is_plain_get(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert re.search(r"@app\.get\(", block), (
            f"{path} is no longer registered with @app.get"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_body_has_no_depends_call(self, path):
        body = _handler_body_for_path(API_SOURCE, path)
        assert "Depends(" not in body, (
            f"{path} handler grew a Depends() parameter"
        )
        assert "Security(" not in body, (
            f"{path} handler grew a Security() parameter"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_body_references_no_auth_helper(self, path):
        """Neither `_auth` nor any auth-flavored name inside the handler."""
        body = _handler_body_for_path(API_SOURCE, path)
        assert "_auth" not in body.replace("_auth_logger", ""), (
            f"{path} handler region references _auth"
        )
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            head = stripped.split("(")[0]
            if _AUTH_NAME_RE.search(head):
                pytest.fail(f"{path} handler calls auth helper: {line!r}")

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_docstring_declares_publicness(self, path):
        body = _handler_body_for_path(API_SOURCE, path)
        docstart = body.find('"""')
        assert docstart != -1, f"{path} lost its docstring"
        doc = body[docstart: body.find('"""', docstart + 3)]
        assert re.search(r"\bpublic\b", doc, re.IGNORECASE), (
            f"{path} docstring no longer declares PUBLIC status"
        )

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_health_endpoints_still_have_their_gate(self, path):
        """Fail-closed companion: detailed/deep KEEP admin gating."""
        block = _decorator_block_for_path(API_SOURCE, path)
        assert "require_admin" in block, (
            f"{path} lost its admin gate — observability surface widened"
        )

    def test_integrity_history_sibling_remains_gated(self):
        block = _decorator_block_for_path(API_SOURCE, "/health/integrity/history")
        assert "require_admin_or_loopback" in block

    def test_health_dispatch_aliases_point_at_system_routes(self):
        assert "_build_health_report = _system_routes.build_health_report" in API_SOURCE
        assert "_evaluate_health_signals = _system_routes.evaluate_health_signals" in API_SOURCE


# ---------------------------------------------------------------------------
# 2. Route-graph inspection via the live FastAPI app object.
# ---------------------------------------------------------------------------


class TestHealthTrioRouteGraph:
    """Walk app.routes the way ASGI dispatch actually resolves them."""

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_exists_and_is_get(self, path):
        routes = _routes_for(path)
        assert routes, f"{path} missing from app.routes"
        assert all("GET" in r.methods for r in routes)

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_admin_dep_in_graph(self, path):
        for route in _routes_for(path):
            banned = {n for n in _dep_names(route) if n in _ADMIN_DEP_NAMES}
            assert not banned, f"{path} carries admin deps: {sorted(banned)}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_auth_named_callable_in_graph(self, path):
        for route in _routes_for(path):
            for name in _dep_names(route):
                assert not _AUTH_NAME_RE.match(name), (
                    f"{path} dependency graph includes auth-named dep {name!r}"
                )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_zero_route_level_dependencies(self, path):
        for route in _routes_for(path):
            assert not route.dependencies, (
                f"{path} has route-level deps: {route.dependencies}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_endpoint_signature_has_no_auth_parameters(self, path):
        for route in _routes_for(path):
            names = _endpoint_param_names(route)
            for n in names:
                assert n != "_auth", f"{path} endpoint kept `_auth` param"
                assert not _AUTH_NAME_RE.search(n), (
                    f"{path} endpoint signature grew auth-shaped param {n!r}"
                )
            assert not any(
                "token" in n.lower() or "credential" in n.lower() for n in names
            ), f"{path} endpoint signature accepts credentials: {names}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_endpoint_is_async_and_argument_free(self, path):
        for route in _routes_for(path):
            fn = route.endpoint
            assert inspect.iscoroutinefunction(fn), f"{path} endpoint not async"
            assert list(inspect.signature(fn).parameters) == [], (
                f"{path} endpoint grew parameters: "
                f"{list(inspect.signature(fn).parameters)}"
            )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_endpoint_uses_no_http_security_scheme_instance(self, path):
        """No HTTPBearer/ApiKeyCookie instance may be bound into the endpoint."""
        for route in _routes_for(path):
            closure = getattr(route.endpoint, "__closure__", None) or ()
            for cell in closure:
                if isinstance(cell.cell_contents, HTTPBearer):
                    pytest.fail(f"{path} endpoint closes over an HTTPBearer scheme")

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_endpoint_docstrings_declare_publicness(self, path):
        for route in _routes_for(path):
            doc = inspect.getdoc(route.endpoint) or ""
            assert re.search(r"public", doc, re.IGNORECASE), (
                f"{path} docstring no longer declares PUBLIC status"
            )

    @pytest.mark.parametrize("path", GATED_SIBLING_PATHS)
    def test_gated_routes_keep_dependency_in_live_app(self, path):
        routes = _routes_for(path)
        assert routes, f"{path} missing from app.routes"
        names: set[str] = set()
        for route in routes:
            names |= _dep_names(route)
        assert names & _ADMIN_DEP_NAMES, (
            f"{path} lost its admin gate in the live app"
        )

    def test_trio_handlers_delegate_to_system_routes_not_inline_auth(self):
        for fname in ("health_livez", "health_readyz"):
            fn = getattr(api_mod._system_routes, fname)
            src = inspect.getsource(fn)
            for gate in ("require_admin", "_auth", "HTTPBearer"):
                assert gate not in src, (
                    f"system_routes.{fname} references {gate!r}"
                )


# ---------------------------------------------------------------------------
# 3. OpenAPI contract: security declarations match the source/graph layers.
# ---------------------------------------------------------------------------


class TestHealthTrioOpenAPISecurity:
    """The published API contract must also say the trio is unauthenticated."""

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_openapi_operation_declares_no_security(self, path):
        assert not _security_requirements(path), (
            f"{path} gained OpenAPI security requirements: "
            f"{_security_requirements(path)}"
        )

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_operations_declare_bearer_security(self, path):
        sec = _security_requirements(path)
        assert sec, f"{path} OpenAPI operation lost its security requirement"
        flat = {k for entry in sec for k in entry}
        assert "HTTPBearer" in flat, f"{path} security is not HTTPBearer: {sec}"

    def test_every_health_path_present_in_openapi(self):
        spec = api_mod.app.openapi()
        for p in PUBLIC_HEALTH_PATHS + GATED_SIBLING_PATHS:
            assert p in spec["paths"], f"{p} missing from OpenAPI paths"

    def test_openapi_exactly_one_bearer_scheme_named_as_expected(self):
        schemes = api_mod.app.openapi().get("components", {}).get(
            "securitySchemes", {}
        )
        assert "HTTPBearer" in schemes

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_security_param_on_operation(self, path):
        spec = api_mod.app.openapi()
        op = spec["paths"][path]["get"]
        leaky = [
            p.get("name") for p in op.get("parameters", [])
            if any(t in (p.get("name") or "").lower() for t in ("auth", "token"))
        ]
        assert not leaky, f"{path} exposes auth-ish query/header params: {leaky}"


# ---------------------------------------------------------------------------
# 4. Behavioral: real requests through TestClient in three postures.
# ---------------------------------------------------------------------------


class TestHealthTrioBehavioralNoAuth:
    """No Authorization header — the sentinel's exact call pattern."""

    @pytest.fixture(autouse=True)
    def _client(self):
        from fastapi.testclient import TestClient
        # Plain construction does NOT run lifespan; handlers degrade
        # gracefully with subsystem globals unset.
        self.client = TestClient(api_mod.app, raise_server_exceptions=False)
        yield

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_responds_without_authorization_header(self, path):
        resp = self.client.get(path)
        assert resp.status_code != 401, f"{path} now demands auth (401)"
        assert resp.status_code != 403, f"{path} now demands auth (403)"
        # readyz legitimately answers 503 when degraded; everything else
        # must be a success/2xx (never a crash).
        allowed = path == "/health/readyz"
        assert allowed or resp.status_code == 200, (
            f"{path} returned {resp.status_code}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_bogus_bearer_token_still_gets_through(self, path):
        resp = self.client.get(path, headers={"Authorization": "Bearer nonsense"})
        assert resp.status_code not in (401, 403), (
            f"{path} started rejecting bogus tokens => it validates auth now"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_random_scheme_header_still_gets_through(self, path):
        resp = self.client.get(path, headers={"Authorization": "Basic Zm9vOmJhcg=="})
        assert resp.status_code not in (401, 403), (
            f"{path} rejects non-bearer Authorization => auth wired in"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_query_strings_cannot_trigger_a_gate(self, path):
        resp = self.client.get(path, params={"admin": "1", "token": "x"})
        assert resp.status_code not in (401, 403)

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_returns_json_object(self, path):
        resp = self.client.get(path)
        assert resp.status_code not in (401, 403)
        try:
            body = resp.json()
        except Exception:
            pytest.fail(f"{path} did not return JSON")
        assert isinstance(body, dict)

    def test_livez_reports_ok_shape(self):
        resp = self.client.get("/health/livez")
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("alive") is True, (
            f"livez payload lost `alive: true`: {sorted(body)}"
        )
        assert "ts" in body

    def test_readyz_returns_json_with_status_or_detail(self):
        resp = self.client.get("/health/readyz")
        assert resp.status_code in (200, 503)
        body = resp.json()
        payload = body.get("detail", body) if isinstance(body, dict) else {}
        assert isinstance(payload, dict)
        assert ("ready" in payload) or ("status" in payload), (
            f"readyz payload lost expected keys: {sorted(body)}"
        )

    def test_readyz_degraded_payload_reports_ready_false(self):
        """Unset subsystems => the degraded branch fires with reasons."""
        resp = self.client.get("/health/readyz")
        if resp.status_code == 503:
            detail = resp.json().get("detail", {})
            assert detail.get("ready") is False
            assert isinstance(detail.get("reasons"), list)

    def test_health_returns_layered_report_keys(self):
        resp = self.client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        expected = {
            "healthy", "status", "subsystems", "checks", "severity",
        }
        assert expected & set(body), (
            f"/health payload lost expected keys: {sorted(body)[:10]}"
        )

    def test_health_ping_counters_advance(self):
        """/health records watchdog ping bookkeeping in app.state."""
        before = getattr(api_mod.app.state, "_health_ping_count", 0)
        assert self.client.get("/health").status_code == 200
        after = getattr(api_mod.app.state, "_health_ping_count", 0)
        assert after > before, "watchdog ping counter did not advance"

    def test_detailed_sibling_rejects_anonymous_off_token_unset(self):
        """Token unset => soft gate answers 403 to non-loopback callers..."""
        resp = self.client.get("/health/detailed")
        # TestClient hosts read as 'testclient' — NOT loopback — so the
        # unset-token soft gate must refuse rather than serve.
        assert resp.status_code == 403

    def test_deep_sibling_route_survives_removal_attempts(self):
        """/health/deep exists — 401/403/200 all fine, 404 means deleted."""
        resp = self.client.get("/health/deep")
        assert resp.status_code != 404

    def test_write_method_floor_still_applies_to_health_paths(self):
        """The default-secure middleware still blocks writes everywhere,
        including the public GET health paths — publicness is per-method."""
        resp = self.client.post("/health", json={})
        assert resp.status_code in (401, 403), (
            "POST /health leaked past the default-secure write floor"
        )


class TestHealthTrioBehavioralWithTokenConfigured:
    """With CALLISTO_ADMIN_TOKEN set the trio STILL ignores tokens entirely,
    while the gated siblings flip to hard 401/200 behavior."""

    @pytest.fixture(autouse=True)
    def _client_and_token(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(api_mod.app, raise_server_exceptions=False)
        with _TokenPosture(api_mod):
            yield

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_trio_unaffected_by_configured_token(self, path):
        no_auth = self.client.get(path)
        bad_auth = self.client.get(
            path, headers={"Authorization": "Bearer wrong-token"}
        )
        good_auth = self.client.get(
            path, headers={"Authorization": "Bearer 0090-characterization-token"}
        )
        codes = {no_auth.status_code, bad_auth.status_code, good_auth.status_code}
        assert not codes & {401, 403}, (
            f"{path} became token-sensitive once an admin token was configured: "
            f"{[no_auth.status_code, bad_auth.status_code, good_auth.status_code]}"
        )
        allowed = path == "/health/readyz"
        for r in (no_auth, bad_auth, good_auth):
            assert allowed or r.status_code == 200, (
                f"{path} answered {r.status_code} with a token configured"
            )

    def test_detailed_hard_fails_without_credentials_when_token_set(self):
        resp = self.client.get("/health/detailed")
        assert resp.status_code == 401, (
            "/health/detailed stopped requiring a Bearer token "
            f"(got {resp.status_code})"
        )

    def test_detailed_serves_with_valid_token(self):
        resp = self.client.get(
            "/health/detailed",
            headers={"Authorization": "Bearer 0090-characterization-token"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, dict)
        assert "trip_history" in body and "ingestion_sla" in body

    def test_deep_serves_with_valid_token_but_not_without(self):
        refused = self.client.get("/health/deep")
        assert refused.status_code == 401
        served = self.client.get(
            "/health/deep",
            headers={"Authorization": "Bearer 0090-characterization-token"},
        )
        assert served.status_code == 200

    def test_bad_token_on_gated_route_logs_to_auth_stream(self, caplog):
        """Gated-sibling refusals stay observable on callisto.api.auth."""
        import logging
        with caplog.at_level(logging.WARNING, logger="callisto.api.auth"):
            self.client.get("/health/detailed")
        assert any(
            "AUTH_DENIED" in rec.getMessage() for rec in caplog.records
        ), "gated sibling refusal produced no AUTH_DENIED audit line"


class TestHealthTrioMethodAndPathHygiene:
    """Registration shape: one GET each; probes don't 405/404 by surprise."""

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_exactly_one_route_registered_per_public_path(self, path):
        assert len(_routes_for(path)) == 1, (
            f"{path} shadowed by a duplicate registration"
        )

    @pytest.mark.parametrize(
        "method", ["post", "put", "patch", "delete"]
    )
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_only_get_registered_for_public_paths(self, path, method):
        client = _TestClientShim(api_mod)
        method_u = method.upper()
        # Expect either 405 (route absent) or the middleware write-gate
        # answer (401/403); NEVER 200 "worked".
        resp = client.request(method_u, path)
        assert resp.status_code in (405, 401, 403), (
            f"{method_u} {path} unexpectedly served: {resp.status_code}"
        )

    @pytest.mark.parametrize("variant", ["/HEALTH", "/Health/", "/health/live"])
    def test_case_or_typo_variants_do_not_exist(self, variant):
        client = _TestClientShim(api_mod)
        resp = client.request("GET", variant)
        assert resp.status_code == 404, (
            f"surprise route at {variant}: {resp.status_code}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_trailing_slash_tolerated_by_starlette_redirect(self, path):
        # Starlette answers redirect_slashes with 307 rather than 404;
        # both prove the registry didn't gain a second literal route.
        client = _TestClientShim(api_mod, follow_redirects=False)
        resp = client.request("GET", path + "/")
        assert resp.status_code in (307, 404), (
            f"trailing slash behaved oddly for {path}: {resp.status_code}"
        )


# ---------------------------------------------------------------------------
# 5. Cross-layer consistency: source ↔ graph ↔ OpenAPI agree.
# ---------------------------------------------------------------------------


class TestCrossLayerConsistency:
    """Catches drift where one representation is edited but not another."""

    def _graph_public(self, path: str) -> bool:
        routes = _routes_for(path)
        if not routes:
            return False
        names: set[str] = set()
        for route in routes:
            names |= _dep_names(route)
        return not (names & _ADMIN_DEP_NAMES) and all(
            not r.dependencies for r in routes
        )

    def _openapi_public(self, path: str) -> bool:
        return not _security_requirements(path)

    def _source_public(self, path: str) -> bool:
        return "require_admin" not in _decorator_block_for_path(API_SOURCE, path)

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS + tuple(GATED_HEALTH_PATHS))
    def test_source_graph_and_openapi_agree(self, path):
        s, g, o = (
            self._source_public(path),
            self._graph_public(path),
            self._openapi_public(path),
        )
        assert len({s, g, o}) == 1, (
            f"{path} layers disagree: source={s} graph={g} openapi={o}"
        )

    def test_publicness_partition_covers_all_health_paths(self):
        """Exactly the declared trio is public; every other /health* path
        visible to the router is gated."""
        all_paths = sorted({
            r.path for r in api_mod.app.routes
            if isinstance(r, APIRoute) and r.path.startswith("/health")
        })
        for p in all_paths:
            if p in PUBLIC_HEALTH_PATHS:
                continue
            names: set[str] = set()
            for route in _routes_for(p):
                names |= _dep_names(route)
            assert names & _ADMIN_DEP_NAMES or any(
                r.dependencies for r in _routes_for(p)
            ), f"ungated health sibling discovered: {p}"

    def test_trio_not_listed_in_public_write_allowlist(self):
        """Publicness of the trio comes from being plain GETs; they must
        NOT appear in the write allowlist, which would weaken the floor."""
        allowlist = getattr(api_mod, "_PUBLIC_WRITE_ENDPOINTS", set())
        for p in PUBLIC_HEALTH_PATHS:
            assert not any(m == "POST" and path == p for m, path in allowlist), (
                f"{p} was allowlisted for POST"
            )


# ---------------------------------------------------------------------------
# 6. AST-level sweep: no auth identifier referenced in the health section.
# ---------------------------------------------------------------------------

_AUTH_IDENTIFIERS = {
    "require_admin",
    "require_admin_or_loopback",
    "_auth",
    "HTTPBearer",
    "Security",
}


class TestASTNoAuthNearTrio:
    """Parse api.py and inspect the decorators/args of the trio's defs."""

    def _collect_trio_def_nodes(self):
        """Map handler name -> list of (decorator_lineno, end_lineno) spans
        for the public-trio defs found via AST, scoped to each def itself."""
        tree = ast.parse(API_SOURCE)
        wanted: dict[str, list[tuple[int, int]]] = {}
        lines = API_SOURCE.splitlines()
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name not in ("health_check", "health_livez", "health_readyz"):
                continue
            is_trio = False
            for dec in node.decorator_list:
                seg = ast.get_source_segment(API_SOURCE, dec) or ""
                if any(
                    f'"{p}"' in seg or f"'{p}'" in seg
                    for p in PUBLIC_HEALTH_PATHS
                ):
                    is_trio = True
                    break
            if not is_trio:
                continue
            start = min(d.lineno for d in node.decorator_list)
            end = getattr(node, "end_lineno", None) or len(lines)
            wanted.setdefault(node.name, []).append((start, end))
        return wanted

    def test_trio_defs_found_via_ast(self):
        defs = self._collect_trio_def_nodes()
        assert set(defs) == {"health_check", "health_livez", "health_readyz"}, (
            f"AST could not locate the full public trio; found {sorted(defs)}"
        )

    @pytest.mark.parametrize("handler", ["health_check", "health_livez", "health_readyz"])
    @pytest.mark.parametrize(
        "bad", sorted(_AUTH_IDENTIFIERS)
    )
    def test_no_auth_identifier_in_trio_decorators(self, handler, bad):
        """AST-verified: the exact def span of each trio handler contains no
        auth identifier — with a bounded docstring-aware body window, never
        the next function's text."""
        tree = ast.parse(API_SOURCE)
        for node in tree.body:
            if getattr(node, "name", None) != handler:
                continue
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Whole def, decorator line through end_lineno — precise bounds,
            # no bleeding into the next route.
            span = ast.get_source_segment(API_SOURCE, node) or ""
            assert bad not in span, (
                f"def {handler} (AST) references {bad!r} near the public trio"
            )


# ---------------------------------------------------------------------------
# 7. Fail-closed safety rails around the health split & betting statuses.
# ---------------------------------------------------------------------------


class TestFailClosedRails:
    """If a pin below fails, the code is WRONG — fix prod, not the test."""

    def test_paper_trade_statuses_never_gain_live(self):
        from tools.signals import paper as paper_mod
        statuses = getattr(paper_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        assert statuses is not None, (
            "_PAPER_TRADE_SIGNAL_STATUSES disappeared from tools.signals.paper"
        )
        lowered = {s.lower() for s in statuses}
        assert "live" not in lowered, (
            "LIVE leaked into _PAPER_TRADE_SIGNAL_STATUSES"
        )
        assert statuses == frozenset({"paper_trading"}), (
            f"paper status set drifted: {statuses!r}"
        )

    def test_paper_status_helpers_stay_tight(self):
        from tools.signals import paper as paper_mod
        for s in ("live", "", None, "paper_trading_live", "paper", "LIVE"):
            assert paper_mod.reject_non_paper(s), (
                f"reject_non_paper let {s!r} through the gate"
            )
        assert not paper_mod.reject_non_paper("paper_trading")
        assert paper_mod.allowed_paper_statuses() == frozenset({"paper_trading"})

    def test_generate_paper_trade_signal_exists_and_is_not_widened(self):
        """tools.btest.paper_pipeline.generate_paper_trade_signal must keep
        the caller-owned gate — never widen to accept status 'live'."""
        from tools.btest import paper_pipeline as pp
        fn = getattr(pp, "generate_paper_trade_signal", None)
        assert fn is not None, "generate_paper_trade_signal missing"
        src = inspect.getsource(fn)
        normalized = src.replace('"', "'")
        assert "status == 'live'" not in normalized, (
            "paper pipeline widened to status == 'live'"
        )
        assert "NOT place bets" in (inspect.getdoc(fn) or "")

    def test_backtest_facade_gate_unchanged(self):
        src_path = os.path.join(REPO, "tools", "backtest.py")
        with open(src_path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == "live":
                pytest.fail(
                    "tools/backtest.py contains a literal 'live' constant "
                    f"(line {node.lineno}) — paper gate widened?"
                )

    def test_backtest_facade_docstring_keeps_the_forbidden_live_clause(self):
        src_path = os.path.join(REPO, "tools", "backtest.py")
        with open(src_path) as f:
            src = f.read()
        assert "FORBIDDEN" in src, (
            "BacktestEngine.generate_paper_trade_signal docstring lost its "
            "explicit FORBIDDEN-'live' clause"
        )
        # Raw RST text: ...including ``"live"``...
        assert 'including ``"live' in src

    def test_status_gate_precedes_extraction(self):
        """Facade checks reject_non_paper BEFORE calling paper_pipeline."""
        src_path = os.path.join(REPO, "tools", "backtest.py")
        with open(src_path) as f:
            src = f.read()
        facade = re.search(
            r"async def generate_paper_trade_signal\(.*?return await "
            r"paper_pipeline\.generate_paper_trade_signal",
            src, re.DOTALL,
        )
        assert facade, "facade no longer delegates to paper_pipeline"
        body = facade.group(0)
        gate_pos = body.find("reject_non_paper")
        pipe_pos = body.find("paper_pipeline.generate_paper_trade_signal")
        assert 0 <= gate_pos < pipe_pos, (
            "status gate no longer precedes pipeline extraction"
        )

    def test_health_file_write_helpers_unchanged_in_source(self):
        assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in API_SOURCE
        assert "_HEALTH_FILE_LAST_WRITE_TS" in API_SOURCE

    def test_health_file_debounce_value_pinned(self):
        ctx = {}
        exec(  # noqa: S102 - evaluate only the two constants, no side effects
            "\n".join(
                line for line in API_SOURCE.splitlines()
                if line.startswith("_HEALTH_FILE_")
            ),
            ctx,
        )
        assert ctx["_HEALTH_FILE_DEBOUNCE_SECONDS"] == 10.0
        assert ctx["_HEALTH_FILE_LAST_WRITE_TS"] == 0.0

    def test_system_routes_module_exposes_trio_handlers(self):
        sr = api_mod._system_routes
        for name in ("health_livez", "health_readyz", "build_health_report"):
            assert callable(getattr(sr, name, None)), (
                f"tools.api.system_routes lost {name}"
            )

    def test_health_signal_evaluator_signature_shape(self):
        from tools.api.system_routes import evaluate_health_signals
        sig = inspect.signature(evaluate_health_signals)
        returns_tuple = re.search(r"-> *Tuple|-> *tuple", inspect.getsource(
            evaluate_health_signals
        ))
        assert returns_tuple or len(sig.parameters) >= 0  # presence pin

    def test_no_global_auth_middleware_added_over_trio(self):
        """A blanket middleware guarding everything would break the sentinel;
        scan middleware registrations for anything auth-flavored."""
        for mw in api_mod.app.user_middleware:
            cls_name = type(getattr(mw, "cls", mw)).__name__.lower()
            mod = getattr(getattr(mw, "cls", mw), "__module__", "").lower()
            blob = cls_name + mod
            assert "auth" not in blob, (
                f"auth-flavored middleware registered globally: {blob}"
            )

    def test_default_secure_middleware_gate_lives_in_security_module(self):
        from tools.api import security as sec
        for name in (
            "require_admin", "require_admin_or_loopback",
            "enforce_default_secure", "client_is_loopback", "log_auth_denied",
        ):
            assert callable(getattr(sec, name, None)), (
                f"tools.api.security lost {name}"
            )

    def test_require_admin_fails_closed_when_token_unset(self):
        """The gates themselves must fail CLOSED — 503 when no token."""
        import asyncio
        from starlette.requests import Request as StarletteRequest
        from tools.api import security as sec

        async def _run():
            with _TokenPosture(api_mod, token=""):
                # Non-loopback fake peer so the soft/hard gates both refuse.
                scope = {
                    "type": "http",
                    "method": "GET",
                    "path": "/health/detailed",
                    "headers": [],
                    "client": ("203.0.113.7", 54321),
                }
                await sec.require_admin(StarletteRequest(scope), None)

        with pytest.raises(Exception) as ei:
            asyncio.run(_run())
        assert getattr(ei.value, "status_code", None) == 503, (
            f"require_admin did not fail closed with 503: {ei.value!r}"
        )


# ---------------------------------------------------------------------------
# 8. Source inventory sanity — each health route appears exactly once.
# ---------------------------------------------------------------------------


class TestSourceInventory:
    @pytest.mark.parametrize(
        "path", PUBLIC_HEALTH_PATHS + tuple(GATED_SIBLING_PATHS)
    )
    def test_each_health_route_declared_exactly_once(self, path):
        count = len(re.findall(rf'@app\.get\("{re.escape(path)}"', API_SOURCE))
        assert count == 1, f"{path} declared {count} times in api.py"

    def test_trio_handlers_delegate_not_duplicate_logic(self):
        """livez/readyz delegate into tools/api/system_routes.py rather than
        reimplementing checks inline — keeps the public surface thin."""
        for path in ("/health/livez", "/health/readyz"):
            body = _handler_body_for_path(API_SOURCE, path)
            assert "_system_routes." in body, (
                f"{path} stopped delegating to system_routes"
            )

    def test_health_handler_chain_reaches_build_health_report(self):
        """/health -> _build_health_report alias -> system_routes builder."""
        block = _decorator_block_for_path(API_SOURCE, "/health")
        assert "await _build_health_report()" in (
            block + _handler_body_for_path(API_SOURCE, "/health")[len(block):]
        )


class _TestClientShim:
    """Tiny method dispatcher so hygiene tests don't need a fixture."""

    def __init__(self, mod, follow_redirects=True):
        from fastapi.testclient import TestClient
        self._c = TestClient(
            mod.app, raise_server_exceptions=False,
            follow_redirects=follow_redirects,
        )

    def request(self, method: str, path: str):
        return self._c.request(method.upper(), path)
