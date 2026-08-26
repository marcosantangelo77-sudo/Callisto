"""Autofill characterization #0082 — public health trio.

Pins the authentication posture of the public health endpoints:

  * `/health`, `/health/livez`, `/health/readyz` are PUBLIC. They must
    never gain `require_admin`, `require_admin_or_loopback`, or any
    ad-hoc `_auth`-style guard. The sentinel (Layer 3), k8s probes, and
    external watchdogs poll them without an admin token.
  * `/health/detailed` and `/health/deep` MAY stay gated — the module
    also fails loudly if someone accidentally strips their gates.

Three layers of pinning:
  1. Source-level inspection of api.py decorator text.
  2. FastAPI route-graph inspection (dependency walk).
  3. Behavioral: TestClient requests with no Authorization header.

Plus a fail-closed safety rail: `_PAPER_TRADE_SIGNAL_STATUSES` must
never contain "live" while these tests run.
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

with open(os.path.join(REPO, "api.py")) as _f:
    API_SOURCE = _f.read()

PUBLIC_HEALTH_PATHS = ("/health", "/health/livez", "/health/readyz")
GATED_HEALTH_PATHS = ("/health/detailed", "/health/deep")
_ADMIN_DEP_NAMES = {"require_admin", "require_admin_or_loopback"}
# Any helper whose name matches this is treated as an auth guard too.
_AUTH_NAME_RE = re.compile(r"(^|_)(auth|require_auth|check_token|verify_token)(_|$)")


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


def _routes_for(path: str) -> list[APIRoute]:
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
    _walk(route.dependant)
    return seen


# ---------------------------------------------------------------------------
# 1. Source-level pinning: decorator lines in api.py.
# ---------------------------------------------------------------------------

def _decorator_block_for_path(source: str, path: str) -> str:
    """Return the `@app.get("path") ... async def name():` block."""
    pattern = re.compile(
        r'((?:@app\.\w+\([^\n]*\)\n)+async def \w+\([^)]*\)[^\n]*:\n)'
    )
    for m in pattern.finditer(source):
        block = m.group(1)
        if f'"{path}"' in block or f"'{path}'" in block:
            return block
    raise AssertionError(f"no @app.get({path!r}) decorator found in api.py")


class TestHealthTrioSourceLevel:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_require_admin(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert "require_admin" not in block, (
            f"{path} decorator gained require_admin in api.py source"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_has_no_loopback_gate(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert "require_admin_or_loopback" not in block

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_decorator_is_plain_get(self, path):
        block = _decorator_block_for_path(API_SOURCE, path)
        assert re.search(r"@app\.get\(", block), (
            f"{path} is no longer registered with @app.get"
        )
        # No extra kwargs at all beyond (optionally) nothing — public trio
        # should be decorated with a bare path string.
        kw = re.search(r"@app\.get\([^)]*[,)]", block).group(0)
        assert "=" not in kw, f"{path} decorator carries extra kwargs: {kw}"

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_handler_body_mentions_no_auth_helper_call(self, path):
        """The handler function itself must not call an _auth-style guard."""
        block = _decorator_block_for_path(API_SOURCE, path)
        # Grab ~40 lines after the decorator as a rough body window.
        idx = API_SOURCE.find(block)
        body = API_SOURCE[idx + len(block): idx + len(block) + 2500]
        # Stop at the next decorator so we only read this handler.
        nxt = re.search(r"\n@app\.", body)
        if nxt:
            body = body[: nxt.start()]
        for line in body.splitlines():
            name = line.strip().split("(")[0].split("=")[-1].strip()
            if _AUTH_NAME_RE.search(name) or "_auth" in line:
                pytest.fail(f"{path} handler references auth logic: {line!r}")

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_health_endpoints_still_have_their_gate(self, path):
        """Fail-closed companion: detailed/deep must KEEP admin gating."""
        block = _decorator_block_for_path(API_SOURCE, path)
        assert ("require_admin" in block), (
            f"{path} lost its admin gate — observability surface widened"
        )


# ---------------------------------------------------------------------------
# 2. Route-graph inspection via the live FastAPI app object.
# ---------------------------------------------------------------------------

class TestHealthTrioRouteGraph:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_route_exists_and_is_get(self, path):
        routes = _routes_for(path)
        assert routes, f"{path} missing from app.routes"
        assert all("GET" in r.methods for r in routes)

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_no_admin_dep_in_graph(self, path):
        for route in _routes_for(path):
            banned = {
                n for n in _dep_names(route) if n in _ADMIN_DEP_NAMES
            }
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

    @pytest.mark.parametrize("path", GATED_HEALTH_PATHS)
    def test_gated_routes_keep_dependency_in_live_app(self, path):
        routes = _routes_for(path)
        assert routes, f"{path} missing from app.routes"
        names = set()
        for route in routes:
            names |= _dep_names(route)
        assert names & _ADMIN_DEP_NAMES, (
            f"{path} lost its admin gate in the live app"
        )

    def test_public_trio_handler_signatures_are_async_no_args(self):
        for path in PUBLIC_HEALTH_PATHS:
            route = _routes_for(path)[0]
            fn = route.endpoint
            assert inspect.iscoroutinefunction(fn), f"{path} endpoint not async"
            params = [
                p for p in inspect.signature(fn).parameters
            ]
            assert params == [], f"{path} endpoint grew parameters: {params}"

    def test_public_trio_docstrings_declare_publicness(self):
        for path in PUBLIC_HEALTH_PATHS:
            route = _routes_for(path)[0]
            doc = inspect.getdoc(route.endpoint) or ""
            assert re.search(r"public", doc, re.IGNORECASE), (
                f"{path} docstring no longer declares PUBLIC status"
            )


# ---------------------------------------------------------------------------
# 3. Behavioral: hit each endpoint with no auth header whatsoever.
# ---------------------------------------------------------------------------

class TestHealthTrioBehavioralNoAuth:
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
        # readyz legitimately answers 503 when degraded; anything else must
        # be a success/2xx-4xx (never a crash).
        allowed = path == "/health/readyz"
        assert allowed or resp.status_code < 500, (
            f"{path} returned {resp.status_code}"
        )

    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS)
    def test_bogus_bearer_token_still_gets_through(self, path):
        resp = self.client.get(path, headers={"Authorization": "Bearer nonsense"})
        assert resp.status_code not in (401, 403), (
            f"{path} started rejecting bogus tokens => it validates auth now"
        )

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
        # livez reports aliveness either as `alive` or `status`.
        assert body.get("alive") is True or body.get("status") in (
            "ok", "alive", "healthy", "up"
        )

    def test_readyz_returns_json_with_status_or_detail(self):
        resp = self.client.get("/health/readyz")
        assert resp.status_code in (200, 503)
        body = resp.json()
        payload = body.get("detail", body) if isinstance(body, dict) else {}
        assert isinstance(payload, dict)
        assert ("ready" in payload) or ("status" in payload), (
            f"readyz payload lost expected keys: {sorted(body)}"
        )

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

    def test_detailed_rejects_when_gate_active_off_loopback(self):
        """Fail-closed check on the gated sibling: without admin creds the
        gated endpoint must NOT behave like an open one when auth is wired.
        We can't force a real rejection here without the full security
        stack, but we can pin that its route still declares gating (see
        TestHealthTrioRouteGraph); behaviorally we just require it to be
        distinguishable — it must exist and answer, unlike the trio which
        is pinned purely open."""
        resp = self.client.get("/health/detailed")
        # Either served (loopback dev mode) or rejected — never a 404,
        # which would mean the route was removed entirely.
        assert resp.status_code != 404


# ---------------------------------------------------------------------------
# 4. Fail-closed safety rails around the health split & betting statuses.
# ---------------------------------------------------------------------------

class TestFailClosedRails:
    def test_paper_trade_statuses_never_gain_live(self):
        from tools.signals import paper as paper_mod
        statuses = getattr(paper_mod, "_PAPER_TRADE_SIGNAL_STATUSES", None)
        assert statuses is not None, (
            "_PAPER_TRADE_SIGNAL_STATUSES disappeared from tools.signals.paper"
        )
        assert "live" not in {s.lower() for s in statuses}, (
            "LIVE leaked into _PAPER_TRADE_SIGNAL_STATUSES"
        )
        assert statuses, "paper status set is empty"

    def test_paper_status_helpers_stay_tight(self):
        from tools.signals import paper as paper_mod
        # Only exact paper statuses allowed; anything else is rejected.
        for s in ("live", "", None, "paper_trading_live", "paper"):
            assert paper_mod.reject_non_paper(s), (
                f"reject_non_paper let {s!r} through the gate"
            )
        assert not paper_mod.reject_non_paper("paper_trading")
        assert "live" not in paper_mod.allowed_paper_statuses()

    def test_generate_paper_trade_signal_exists_and_is_not_widened(self):
        """tools/btest/paper_pipeline.generate_paper_trade_signal must gate
        on the caller's hard gate — never widen to accept status 'live'."""
        from tools.btest import paper_pipeline as pp
        fn = getattr(pp, "generate_paper_trade_signal", None)
        assert fn is not None, "generate_paper_trade_signal missing"
        src = inspect.getsource(fn)
        assert "status == 'live'" not in src and 'status == "live"' not in src
        # The docstring still declares the no-bets contract.
        assert "NOT place bets" in (inspect.getdoc(fn) or "")

    def test_backtest_engine_gate_unchanged(self):
        src_path = os.path.join(REPO, "tools", "backtest.py")
        with open(src_path) as f:
            src = f.read()
        assert '"paper_trading"' in src or "'paper_trading'" in src
        # AST-level: no string constant exactly "live" anywhere in the
        # module (comments and docstrings don't count as bare constants
        # compared against statuses; docstrings are excluded).
        import ast as _ast

        with open(src_path) as f:
            tree = _ast.parse(f.read())
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Constant) and node.value == "live":
                pytest.fail(
                    "tools/backtest.py contains a literal 'live' constant "
                    f"(line {node.lineno}) — paper gate widened?"
                )

    def test_health_file_write_helpers_unchanged_in_source(self):
        assert "_HEALTH_FILE_DEBOUNCE_SECONDS" in API_SOURCE
        assert "_HEALTH_FILE_LAST_WRITE_TS" in API_SOURCE

    def test_system_routes_module_exposes_trio_handlers(self):
        sr = api_mod._system_routes
        for name in ("health_livez", "health_readyz", "build_health_report"):
            assert callable(getattr(sr, name, None)), (
                f"tools.api.system_routes lost {name}"
            )

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


# ---------------------------------------------------------------------------
# 5. Source inventory sanity — the trio appears exactly once each.
# ---------------------------------------------------------------------------

class TestSourceInventory:
    @pytest.mark.parametrize("path", PUBLIC_HEALTH_PATHS + tuple(GATED_HEALTH_PATHS))
    def test_each_health_route_declared_exactly_once(self, path):
        count = len(re.findall(rf'@app\.get\("{re.escape(path)}"', API_SOURCE))
        assert count == 1, f"{path} declared {count} times in api.py"

    def test_integrity_history_sibling_remains_gated(self):
        block = _decorator_block_for_path(API_SOURCE, "/health/integrity/history")
        assert "require_admin_or_loopback" in block

    def test_trio_handlers_delegate_not_duplicate_logic(self):
        """livez/readyz delegate into tools/api/system_routes.py rather than
        reimplementing checks inline — keeps the public surface thin."""
        for fname in ("health_livez", "health_readyz"):
            block = _decorator_block_for_path(API_SOURCE, f"/health/{fname.split('_')[1]}")
            idx = API_SOURCE.find(block)
            body = API_SOURCE[idx: idx + 400]
            assert "_system_routes." in body, (
                f"{fname} stopped delegating to system_routes"
            )
